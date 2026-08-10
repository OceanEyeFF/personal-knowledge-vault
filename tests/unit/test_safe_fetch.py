"""Deterministic contracts for the URL processors' pinned network boundary."""

from __future__ import annotations

import ast
from collections import deque
from pathlib import Path
import threading
from time import monotonic
from unittest.mock import MagicMock, patch

import pytest
import yaml

from src.processors.safe_fetch import (
    MAX_RESOLVED_PEER_ADDRESSES,
    _BoundedResolverRunner,
    PinnedTarget,
    SafeFetcher,
    SafeFetchTransportError,
    SafeResponse,
    Urllib3PinnedTransport,
    describe_url_target,
    parse_http_target,
    resolve_public_target,
)
from src.runtime.errors import ErrorCode, PKVRuntimeError


FIXTURE_PATH = (
    Path(__file__).resolve().parent.parent
    / "fixtures"
    / "w2"
    / "mcp"
    / "v1"
    / "matrix.yaml"
)
W2_MCP_FIXTURE = yaml.safe_load(FIXTURE_PATH.read_text(encoding="utf-8"))
PUBLIC_V4 = W2_MCP_FIXTURE["ssrf"]["public_addresses"]["ipv4"]
PUBLIC_V6 = W2_MCP_FIXTURE["ssrf"]["public_addresses"]["ipv6"]
REDIRECT_FIXTURE = W2_MCP_FIXTURE["ssrf"]["redirect"]
FIRST_URL = REDIRECT_FIXTURE["first_url"]
SECOND_URL = REDIRECT_FIXTURE["second_url"]
PRIVATE_REDIRECT_URL = REDIRECT_FIXTURE["private_location"]
REBINDING_URL = REDIRECT_FIXTURE["rebinding_url"]


class RecordingTransport:
    def __init__(self, responses: list[SafeResponse]) -> None:
        self.responses = deque(responses)
        self.calls: list[tuple[PinnedTarget, dict[str, str], int]] = []

    def request(self, target, *, headers, max_response_bytes, deadline=None):
        self.calls.append((target, dict(headers), max_response_bytes))
        return self.responses.popleft()


def response(
    url: str,
    *,
    status: int = 200,
    headers: dict[str, str] | None = None,
    content: bytes = b"ok",
) -> SafeResponse:
    return SafeResponse(
        url=url,
        status_code=status,
        headers={key.lower(): value for key, value in (headers or {}).items()},
        content=content,
    )


@pytest.mark.parametrize(
    "case",
    W2_MCP_FIXTURE["ssrf"]["invalid_urls"],
    ids=lambda case: case["id"],
)
def test_parse_http_target_rejects_ambiguous_or_non_http_urls(case):
    with pytest.raises(PKVRuntimeError) as exc_info:
        parse_http_target(case["value"])

    assert exc_info.value.code.value == case["error_code"]


def test_resolver_validates_every_ipv4_and_ipv6_answer():
    target = resolve_public_target(
        "https://example.com/path?q=1#ignored",
        resolver=lambda _host, _port: [PUBLIC_V4, PUBLIC_V6, PUBLIC_V4],
    )

    assert target.peer_addresses == (PUBLIC_V4, PUBLIC_V6)
    assert target.request_target == "/path?q=1"
    assert target.host_header == "example.com"
    assert target.url == "https://example.com/path?q=1"


def test_log_safe_target_never_includes_path_query_or_fragment():
    secret = "pkv-url-secret"

    label = describe_url_target(
        f"https://example.com:8443/private/{secret}?token={secret}#{secret}"
    )

    assert label == "https://example.com:8443"
    assert secret not in label


@pytest.mark.parametrize(
    "case",
    W2_MCP_FIXTURE["ssrf"]["forbidden_addresses"],
    ids=lambda case: case["id"],
)
def test_resolution_rejects_each_non_public_address_family(case):
    with pytest.raises(PKVRuntimeError) as exc_info:
        resolve_public_target(
            "https://example.com/",
            resolver=lambda _host, _port: [case["value"]],
        )

    assert exc_info.value.code.value == case["error_code"]


def test_reserved_internal_hostname_is_rejected_before_dns_resolution():
    resolver = MagicMock(return_value=[PUBLIC_V4])

    with pytest.raises(PKVRuntimeError) as exc_info:
        resolve_public_target("https://service.internal/private", resolver=resolver)

    assert exc_info.value.code is ErrorCode.SSRF_TARGET_FORBIDDEN
    resolver.assert_not_called()


def test_mixed_public_private_dns_answer_fails_closed():
    with pytest.raises(PKVRuntimeError) as exc_info:
        resolve_public_target(
            "https://example.com/",
            resolver=lambda _host, _port: [PUBLIC_V4, "127.0.0.1"],
        )

    assert exc_info.value.code is ErrorCode.SSRF_TARGET_FORBIDDEN


def test_resolution_rejects_excessive_peer_address_set():
    addresses = [f"8.8.8.{index}" for index in range(1, MAX_RESOLVED_PEER_ADDRESSES + 2)]

    with pytest.raises(PKVRuntimeError) as exc_info:
        resolve_public_target(
            "https://many-peers.example/",
            resolver=lambda _host, _port: addresses,
        )

    assert exc_info.value.code is ErrorCode.SSRF_RESOLUTION_FAILED


def test_dns_failure_has_stable_resolution_error():
    def fail_resolution(_hostname: str, _port: int):
        raise OSError("offline resolver failure")

    with pytest.raises(PKVRuntimeError) as exc_info:
        resolve_public_target("https://example.com/", resolver=fail_resolution)

    assert exc_info.value.code is ErrorCode.SSRF_RESOLUTION_FAILED
    assert exc_info.value.recoverable is True
    assert "offline resolver failure" not in str(exc_info.value)


def test_numeric_target_is_validated_without_calling_dns():
    resolver = MagicMock(side_effect=AssertionError("numeric host must not resolve"))

    target = resolve_public_target(f"https://{PUBLIC_V4}/", resolver=resolver)

    assert target.peer_addresses == (PUBLIC_V4,)
    resolver.assert_not_called()


def test_redirect_revalidates_and_pins_every_hop():
    resolver_calls: list[str] = []

    def resolver(hostname: str, _port: int):
        resolver_calls.append(hostname)
        return {
            parse_http_target(FIRST_URL).hostname: [PUBLIC_V4],
            parse_http_target(SECOND_URL).hostname: [PUBLIC_V6],
        }[hostname]

    transport = RecordingTransport(
        [
            response(
                FIRST_URL,
                status=302,
                headers={"Location": SECOND_URL},
            ),
            response(SECOND_URL, content=b"done"),
        ]
    )
    fetcher = SafeFetcher(resolver=resolver, transport=transport)

    result = fetcher.fetch_sync(
        FIRST_URL,
        headers={
            "Authorization": "Bearer secret",
            "Cookie": "session=secret",
            "Proxy-Authorization": "Basic secret",
            "User-Agent": "pkv-test",
        },
    )

    assert result.content == b"done"
    assert resolver_calls == [
        parse_http_target(FIRST_URL).hostname,
        parse_http_target(SECOND_URL).hostname,
    ]
    assert transport.calls[0][0].peer_addresses == (PUBLIC_V4,)
    assert transport.calls[1][0].peer_addresses == (PUBLIC_V6,)
    assert transport.calls[0][1]["Authorization"] == "Bearer secret"
    assert transport.calls[0][1]["Cookie"] == "session=secret"
    assert transport.calls[0][1]["Proxy-Authorization"] == "Basic secret"
    for sensitive_header in ("Authorization", "Cookie", "Proxy-Authorization"):
        assert sensitive_header not in transport.calls[1][1]
    assert transport.calls[1][1]["User-Agent"] == "pkv-test"


def test_https_redirect_to_public_http_is_rejected_independently():
    public_http_url = f"http://{PUBLIC_V4}/final"
    transport = RecordingTransport(
        [
            response(
                FIRST_URL,
                status=302,
                headers={"Location": public_http_url},
            ),
            response(public_http_url, content=b"must-not-be-reached"),
        ]
    )
    fetcher = SafeFetcher(
        resolver=lambda _host, _port: [PUBLIC_V4],
        transport=transport,
    )

    with pytest.raises(PKVRuntimeError) as exc_info:
        fetcher.fetch_sync(FIRST_URL)

    assert exc_info.value.code is ErrorCode.SSRF_TARGET_FORBIDDEN
    assert len(transport.calls) == 1


def test_redirect_to_private_target_is_rejected_before_second_transport_call():
    transport = RecordingTransport(
        [
            response(
                FIRST_URL,
                status=302,
                headers={"Location": PRIVATE_REDIRECT_URL},
            )
        ]
    )
    fetcher = SafeFetcher(
        resolver=lambda _host, _port: [PUBLIC_V4],
        transport=transport,
    )

    with pytest.raises(PKVRuntimeError) as exc_info:
        fetcher.fetch_sync(FIRST_URL)

    # HTTPS downgrade is rejected even before the literal private-IP check.
    assert exc_info.value.code is ErrorCode.SSRF_TARGET_FORBIDDEN
    assert len(transport.calls) == 1


def test_dns_rebinding_on_same_host_redirect_is_caught():
    answers = iter(([PUBLIC_V4], ["127.0.0.1"]))
    transport = RecordingTransport(
        [
            response(
                REBINDING_URL,
                status=302,
                headers={"Location": "/again"},
            )
        ]
    )
    fetcher = SafeFetcher(
        resolver=lambda _host, _port: next(answers),
        transport=transport,
    )

    with pytest.raises(PKVRuntimeError) as exc_info:
        fetcher.fetch_sync(REBINDING_URL)

    assert exc_info.value.code is ErrorCode.SSRF_TARGET_FORBIDDEN
    assert len(transport.calls) == 1


def test_redirect_chain_shares_one_total_response_byte_budget():
    transport = RecordingTransport(
        [
            response(
                FIRST_URL,
                status=302,
                headers={"Location": SECOND_URL},
                content=b"123",
            ),
            response(SECOND_URL, content=b"456"),
        ]
    )
    fetcher = SafeFetcher(
        resolver=lambda _host, _port: [PUBLIC_V4],
        transport=transport,
        max_response_bytes=5,
    )

    with pytest.raises(SafeFetchTransportError, match="response exceeds"):
        fetcher.fetch_sync(FIRST_URL)

    assert len(transport.calls) == 2
    assert transport.calls[0][2] == 5
    assert transport.calls[1][2] == 2


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf")])
def test_safe_fetcher_rejects_non_finite_or_non_positive_timeout(timeout):
    with pytest.raises(ValueError, match="timeout_seconds"):
        SafeFetcher(timeout_seconds=timeout, transport=MagicMock())


def test_first_hop_dns_resolution_obeys_absolute_deadline():
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    transport = MagicMock()

    def blocking_resolver(_hostname: str, _port: int):
        try:
            entered.set()
            release.wait(timeout=2)
            return [PUBLIC_V4]
        finally:
            finished.set()

    fetcher = SafeFetcher(
        timeout_seconds=0.05,
        resolver=blocking_resolver,
        resolver_runner=_BoundedResolverRunner(max_concurrency=1),
        transport=transport,
    )

    started = monotonic()
    try:
        with pytest.raises(PKVRuntimeError) as exc_info:
            fetcher.fetch_sync(FIRST_URL)
        elapsed = monotonic() - started
    finally:
        release.set()

    assert entered.wait(timeout=1)
    assert finished.wait(timeout=1)
    assert elapsed < 0.5
    assert exc_info.value.code is ErrorCode.SSRF_RESOLUTION_FAILED
    assert exc_info.value.stage == "network_policy"
    assert exc_info.value.recoverable is True
    transport.request.assert_not_called()


def test_redirect_dns_resolution_obeys_same_absolute_deadline():
    second_entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    first_hostname = parse_http_target(FIRST_URL).hostname
    second_hostname = parse_http_target(SECOND_URL).hostname
    transport = RecordingTransport(
        [
            response(
                FIRST_URL,
                status=302,
                headers={"Location": SECOND_URL},
            )
        ]
    )

    def resolver(hostname: str, _port: int):
        if hostname == first_hostname:
            return [PUBLIC_V4]
        assert hostname == second_hostname
        try:
            second_entered.set()
            release.wait(timeout=2)
            return [PUBLIC_V6]
        finally:
            finished.set()

    fetcher = SafeFetcher(
        timeout_seconds=0.05,
        resolver=resolver,
        resolver_runner=_BoundedResolverRunner(max_concurrency=1),
        transport=transport,
    )

    started = monotonic()
    try:
        with pytest.raises(PKVRuntimeError) as exc_info:
            fetcher.fetch_sync(FIRST_URL)
        elapsed = monotonic() - started
    finally:
        release.set()

    assert second_entered.wait(timeout=1)
    assert finished.wait(timeout=1)
    assert elapsed < 0.5
    assert exc_info.value.code is ErrorCode.SSRF_RESOLUTION_FAILED
    assert exc_info.value.stage == "network_policy"
    assert exc_info.value.recoverable is True
    assert len(transport.calls) == 1


def test_dns_result_at_deadline_is_rejected_before_transport():
    resolver_runner = MagicMock()
    resolver_runner.resolve.return_value = [PUBLIC_V4]
    transport = MagicMock()
    fetcher = SafeFetcher(
        timeout_seconds=1,
        resolver=lambda _host, _port: [PUBLIC_V4],
        resolver_runner=resolver_runner,
        transport=transport,
    )

    with patch(
        "src.processors.safe_fetch.monotonic",
        side_effect=[0.0, 0.0, 1.0],
    ):
        with pytest.raises(PKVRuntimeError) as exc_info:
            fetcher.fetch_sync(FIRST_URL)

    assert exc_info.value.code is ErrorCode.SSRF_RESOLUTION_FAILED
    assert exc_info.value.stage == "network_policy"
    assert exc_info.value.recoverable is True
    transport.request.assert_not_called()


def test_global_dns_slot_exhaustion_fails_closed_before_transport():
    runner = _BoundedResolverRunner(max_concurrency=1)
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    first_transport = RecordingTransport([response(FIRST_URL)])
    second_transport = MagicMock()
    second_resolver = MagicMock(return_value=[PUBLIC_V4])
    first_result: dict[str, object] = {}

    def blocking_resolver(_hostname: str, _port: int):
        try:
            entered.set()
            release.wait(timeout=2)
            return [PUBLIC_V4]
        finally:
            finished.set()

    first_fetcher = SafeFetcher(
        timeout_seconds=1,
        resolver=blocking_resolver,
        resolver_runner=runner,
        transport=first_transport,
    )
    second_fetcher = SafeFetcher(
        timeout_seconds=1,
        resolver=second_resolver,
        resolver_runner=runner,
        transport=second_transport,
    )

    def run_first_fetch() -> None:
        try:
            first_result["response"] = first_fetcher.fetch_sync(FIRST_URL)
        except Exception as exc:  # pragma: no cover - asserted below
            first_result["error"] = exc

    first_thread = threading.Thread(target=run_first_fetch, daemon=True)
    first_thread.start()
    assert entered.wait(timeout=1)

    started = monotonic()
    try:
        with pytest.raises(PKVRuntimeError) as exc_info:
            second_fetcher.fetch_sync(SECOND_URL)
        elapsed = monotonic() - started
    finally:
        release.set()
        first_thread.join(timeout=1)

    assert finished.is_set()
    assert not first_thread.is_alive()
    assert "error" not in first_result
    assert isinstance(first_result.get("response"), SafeResponse)
    assert elapsed < 0.5
    assert exc_info.value.code is ErrorCode.SSRF_RESOLUTION_FAILED
    assert exc_info.value.stage == "network_policy"
    assert exc_info.value.recoverable is True
    second_resolver.assert_not_called()
    second_transport.request.assert_not_called()


def test_redirect_limit_is_stable_and_fail_closed():
    transport = RecordingTransport(
        [
            response(
                "https://loop.example/a",
                status=302,
                headers={"Location": "/b"},
            )
        ]
    )
    fetcher = SafeFetcher(
        max_redirects=0,
        resolver=lambda _host, _port: [PUBLIC_V4],
        transport=transport,
    )

    with pytest.raises(PKVRuntimeError) as exc_info:
        fetcher.fetch_sync("https://loop.example/a")

    assert exc_info.value.code is ErrorCode.SSRF_REDIRECT_LIMIT
    assert len(transport.calls) == 1


def test_default_http_transport_connects_to_pinned_ip_not_hostname():
    raw_response = MagicMock()
    raw_response.status = 200
    raw_response.headers = {"Content-Type": "text/plain; charset=utf-8"}
    raw_response.read1.side_effect = [b"pinned", b""]
    pool = MagicMock()
    pool.urlopen.return_value = raw_response
    target = resolve_public_target(
        "http://example.com:8080/path?q=1",
        resolver=lambda _host, _port: [PUBLIC_V4],
    )

    with patch("src.processors.safe_fetch.HTTPConnectionPool", return_value=pool) as pool_type:
        result = Urllib3PinnedTransport(timeout_seconds=2).request(
            target,
            headers={"Host": "attacker.invalid", "Accept-Encoding": "gzip"},
            max_response_bytes=1024,
        )

    assert result.text == "pinned"
    pool_type.assert_called_once()
    assert pool_type.call_args.kwargs["host"] == PUBLIC_V4
    request = pool.urlopen.call_args
    assert request.args[:2] == ("GET", "/path?q=1")
    assert request.kwargs["headers"]["Host"] == "example.com:8080"
    assert request.kwargs["headers"]["Accept-Encoding"] == "identity"
    assert request.kwargs["redirect"] is False
    assert request.kwargs["retries"] is False


def test_default_https_transport_pins_ip_and_original_tls_name():
    raw_response = MagicMock()
    raw_response.status = 200
    raw_response.headers = {}
    raw_response.read1.side_effect = [b"tls", b""]
    pool = MagicMock()
    pool.urlopen.return_value = raw_response
    target = resolve_public_target(
        "https://secure.example/data",
        resolver=lambda _host, _port: [PUBLIC_V6],
    )

    with (
        patch("src.processors.safe_fetch.ssl.create_default_context", return_value="tls-context"),
        patch("src.processors.safe_fetch.HTTPSConnectionPool", return_value=pool) as pool_type,
    ):
        Urllib3PinnedTransport(timeout_seconds=2).request(
            target,
            headers={},
            max_response_bytes=1024,
        )

    kwargs = pool_type.call_args.kwargs
    assert kwargs["host"] == PUBLIC_V6
    assert kwargs["server_hostname"] == "secure.example"
    assert kwargs["assert_hostname"] == "secure.example"
    assert kwargs["ssl_context"] == "tls-context"


def test_response_over_limit_fails_without_trying_another_peer():
    raw_response = MagicMock()
    raw_response.status = 200
    raw_response.headers = {}
    raw_response.read1.return_value = b"12345"
    first_pool = MagicMock()
    first_pool.urlopen.return_value = raw_response
    second_pool = MagicMock()
    target = resolve_public_target(
        "http://bounded.example/data",
        resolver=lambda _host, _port: [PUBLIC_V4, PUBLIC_V6],
    )

    with patch(
        "src.processors.safe_fetch.HTTPConnectionPool",
        side_effect=[first_pool, second_pool],
    ) as pool_type:
        with pytest.raises(SafeFetchTransportError, match="response exceeds"):
            Urllib3PinnedTransport(timeout_seconds=2).request(
                target,
                headers={},
                max_response_bytes=4,
            )

    assert pool_type.call_count == 1


def test_body_read_failure_does_not_retry_a_second_peer():
    raw_response = MagicMock()
    raw_response.status = 200
    raw_response.headers = {}
    raw_response.read1.side_effect = [b"partial", OSError("peer reset")]
    first_pool = MagicMock()
    first_pool.urlopen.return_value = raw_response
    second_pool = MagicMock()
    target = resolve_public_target(
        "http://bounded.example/data",
        resolver=lambda _host, _port: [PUBLIC_V4, PUBLIC_V6],
    )

    with patch(
        "src.processors.safe_fetch.HTTPConnectionPool",
        side_effect=[first_pool, second_pool],
    ) as pool_type:
        with pytest.raises(SafeFetchTransportError, match="响应读取失败"):
            Urllib3PinnedTransport(timeout_seconds=2).request(
                target,
                headers={},
                max_response_bytes=1024,
            )

    assert pool_type.call_count == 1


def test_absolute_deadline_stops_slow_trickle_response():
    raw_response = MagicMock()
    raw_response.status = 200
    raw_response.headers = {}
    raw_response.read1.side_effect = [b"a", b"b", b""]
    pool = MagicMock()
    pool.urlopen.return_value = raw_response
    target = resolve_public_target(
        "http://slow.example/data",
        resolver=lambda _host, _port: [PUBLIC_V4],
    )

    with (
        patch("src.processors.safe_fetch.HTTPConnectionPool", return_value=pool),
        patch("src.processors.safe_fetch.monotonic", side_effect=[0.0, 0.0, 0.5, 1.1]),
    ):
        with pytest.raises(SafeFetchTransportError, match="请求超时"):
            Urllib3PinnedTransport(timeout_seconds=1).request(
                target,
                headers={},
                max_response_bytes=1024,
            )


def test_installed_urllib3_pool_accepts_pinned_peer_and_tls_name():
    """Constructor sentinel: fail if the installed urllib3 drops this SNI seam."""

    target = resolve_public_target(
        "https://secure.example/data",
        resolver=lambda _host, _port: [PUBLIC_V6],
    )
    pool = Urllib3PinnedTransport(timeout_seconds=2)._pool_for(target, PUBLIC_V6)
    try:
        assert pool.host == PUBLIC_V6
        assert pool.assert_hostname == "secure.example"
        assert pool.conn_kw["server_hostname"] == "secure.example"
    finally:
        pool.close()


def test_url_processors_have_no_unpinned_network_client_exit():
    project_root = Path(__file__).resolve().parent.parent.parent
    banned_modules = {
        "aiohttp",
        "http.client",
        "httpx",
        "playwright",
        "requests",
        "socket",
        "urllib.request",
        "urllib3",
    }
    for relative_path in (
        "src/processors/generic_processor.py",
        "src/processors/wechat_processor.py",
        "src/processors/zhihu_processor.py",
    ):
        source = (project_root / relative_path).read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported_modules = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        } | {
            node.module or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        }
        assert not {
            module
            for module in imported_modules
            if any(
                module == banned or module.startswith(f"{banned}.")
                for banned in banned_modules
            )
        }
        assert "requests.get(" not in source
        assert "client.get(" not in source
        assert "page.goto(" not in source
        assert "async_playwright" not in source
