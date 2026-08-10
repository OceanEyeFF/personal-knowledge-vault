"""SSRF-resistant HTTP fetching for URL processors.

The security decision and the network connection are deliberately joined by a
``PinnedTarget``.  DNS is resolved once per hop, every answer must be globally
routable, and the default transport connects to one of those exact addresses
while retaining the original host for HTTP ``Host`` and HTTPS SNI/certificate
verification.  This closes the validation/connect DNS-rebinding gap.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import ipaddress
from math import isfinite
import queue
import re
import socket
import ssl
import threading
from time import monotonic
from typing import Mapping, Protocol, Sequence
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

from urllib3 import HTTPConnectionPool, HTTPSConnectionPool
from urllib3.exceptions import HTTPError
from urllib3.util import Timeout

from src.runtime.errors import ErrorCode, PKVRuntimeError


DEFAULT_MAX_REDIRECTS = 5
DEFAULT_MAX_RESPONSE_BYTES = 10 * 1024 * 1024
MAX_RESOLVED_PEER_ADDRESSES = 8
MAX_CONCURRENT_DNS_RESOLUTIONS = 4
_READ_CHUNK_BYTES = 64 * 1024
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_SENSITIVE_REDIRECT_HEADERS = frozenset(
    {"authorization", "cookie", "proxy-authorization"}
)
_CHARSET_RE = re.compile(r"charset\s*=\s*['\"]?([^;'\"\s]+)", re.IGNORECASE)


@dataclass(frozen=True)
class ParsedTarget:
    """A syntactically valid HTTP target before DNS resolution."""

    url: str
    scheme: str
    hostname: str
    port: int
    request_target: str
    host_header: str


@dataclass(frozen=True)
class PinnedTarget(ParsedTarget):
    """A target whose complete DNS answer set passed the public-IP policy."""

    peer_addresses: tuple[str, ...]


@dataclass(frozen=True)
class SafeResponse:
    """Small immutable response returned by the safe transport seam."""

    url: str
    status_code: int
    headers: Mapping[str, str]
    content: bytes

    @property
    def text(self) -> str:
        content_type = self.headers.get("content-type", "")
        match = _CHARSET_RE.search(content_type)
        encoding = match.group(1) if match else "utf-8"
        try:
            return self.content.decode(encoding, errors="replace")
        except LookupError:
            return self.content.decode("utf-8", errors="replace")

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise SafeFetchTransportError(
                f"HTTP request failed with status {self.status_code}"
            )


class SafeFetchTransportError(RuntimeError):
    """Raised for bounded HTTP transport failures after policy validation."""


class SafeFetchResponseLimitError(SafeFetchTransportError):
    """Raised when one fetch exhausts its cumulative response-byte budget."""


class Resolver(Protocol):
    def __call__(self, hostname: str, port: int) -> Sequence[str]: ...


class _BoundedResolverRunner:
    """Run potentially blocking DNS outside shared executors under one deadline.

    Python's platform resolver has no portable timeout.  A small process-wide
    daemon pool bound prevents stuck ``getaddrinfo`` calls from exhausting the
    asyncio default executor used by MCP storage and retrieval work.  Timed-out
    resolver threads may finish later, at which point they release their slot.
    """

    def __init__(self, *, max_concurrency: int = MAX_CONCURRENT_DNS_RESOLUTIONS) -> None:
        if (
            isinstance(max_concurrency, bool)
            or not isinstance(max_concurrency, int)
            or max_concurrency <= 0
        ):
            raise ValueError("max_concurrency must be a positive integer")
        self._slots = threading.BoundedSemaphore(max_concurrency)

    def resolve(
        self,
        resolver: Resolver,
        hostname: str,
        port: int,
        *,
        deadline: float,
    ) -> Sequence[str]:
        remaining = deadline - monotonic()
        if remaining <= 0:
            _reject_resolution_timeout()
        if not self._slots.acquire(blocking=False):
            _reject(
                ErrorCode.SSRF_RESOLUTION_FAILED,
                "域名解析资源暂不可用",
                recoverable=True,
            )

        result_queue: queue.Queue[tuple[bool, object]] = queue.Queue(maxsize=1)

        def run_resolution() -> None:
            try:
                try:
                    result: tuple[bool, object] = (
                        True,
                        resolver(hostname, port),
                    )
                except Exception as exc:
                    result = (False, exc)
                result_queue.put_nowait(result)
            finally:
                self._slots.release()

        worker = threading.Thread(
            target=run_resolution,
            name="pkv-dns-resolver",
            daemon=True,
        )
        try:
            worker.start()
        except Exception:
            self._slots.release()
            _reject(
                ErrorCode.SSRF_RESOLUTION_FAILED,
                "目标域名解析失败",
                recoverable=True,
            )

        remaining = deadline - monotonic()
        if remaining <= 0:
            _reject_resolution_timeout()
        try:
            succeeded, value = result_queue.get(timeout=remaining)
        except queue.Empty:
            _reject_resolution_timeout()
            raise AssertionError("unreachable")
        if succeeded:
            return value  # type: ignore[return-value]
        raise value  # type: ignore[misc]


_GLOBAL_RESOLVER_RUNNER = _BoundedResolverRunner()


class PinnedTransport(Protocol):
    def request(
        self,
        target: PinnedTarget,
        *,
        headers: Mapping[str, str],
        max_response_bytes: int,
        deadline: float | None = None,
    ) -> SafeResponse: ...


def parse_http_target(url: str) -> ParsedTarget:
    """Parse an HTTP(S) URL and reject ambiguous authority syntax."""

    if not isinstance(url, str) or not url.strip():
        _reject(ErrorCode.URL_INVALID, "URL 不能为空")
    normalized = url.strip()
    if len(normalized) > 8192:
        _reject(ErrorCode.URL_INVALID, "URL 长度超过限制")
    if "\\" in normalized or any(ord(char) <= 32 or ord(char) == 127 for char in normalized):
        _reject(ErrorCode.URL_INVALID, "URL 包含非法控制字符或反斜杠")

    try:
        parsed = urlsplit(normalized)
        port = parsed.port
    except ValueError:
        _reject(ErrorCode.URL_INVALID, "URL 格式无效")
        raise AssertionError("unreachable")

    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        _reject(
            ErrorCode.URL_INVALID,
            f"URL scheme 必须是 http 或 https，当前: {scheme or '(空)'}",
        )
    if not parsed.hostname:
        _reject(ErrorCode.URL_INVALID, "URL 缺少有效的域名或 IP")
    if parsed.username is not None or parsed.password is not None:
        _reject(ErrorCode.URL_INVALID, "URL 不得包含用户名或密码")

    hostname = parsed.hostname.rstrip(".").lower()
    if not hostname or "%" in hostname:
        _reject(ErrorCode.URL_INVALID, "URL hostname 无效")
    try:
        hostname = hostname.encode("idna").decode("ascii")
    except UnicodeError:
        _reject(ErrorCode.URL_INVALID, "URL hostname 编码无效")

    if port == 0:
        _reject(ErrorCode.URL_INVALID, "URL port 必须位于 1..65535")
    effective_port = port if port is not None else (443 if scheme == "https" else 80)
    path = quote(parsed.path or "/", safe="/%:@!$&'()*+,;=-._~")
    query = quote(parsed.query, safe="=&?/:@!$'()*+,;%-._~")
    request_target = path + (f"?{query}" if query else "")

    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        host_token = hostname
    else:
        host_token = f"[{hostname}]" if literal.version == 6 else hostname
    default_port = 443 if scheme == "https" else 80
    host_header = host_token if effective_port == default_port else f"{host_token}:{effective_port}"
    canonical_url = urlunsplit(
        (scheme, host_header, path, query, "")
    )
    return ParsedTarget(
        url=canonical_url,
        scheme=scheme,
        hostname=hostname,
        port=effective_port,
        request_target=request_target,
        host_header=host_header,
    )


def resolve_public_target(
    url: str,
    *,
    resolver: Resolver | None = None,
) -> PinnedTarget:
    """Resolve a URL and require every returned address to be globally routable."""

    parsed = parse_http_target(url)
    if is_forbidden_hostname(parsed.hostname):
        _reject(
            ErrorCode.SSRF_TARGET_FORBIDDEN,
            "禁止访问内网地址或其他非公网目标",
        )
    resolve = resolver or resolve_host_addresses
    try:
        literal = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        try:
            raw_addresses = resolve(parsed.hostname, parsed.port)
        except (OSError, ValueError) as exc:
            _reject(
                ErrorCode.SSRF_RESOLUTION_FAILED,
                "目标域名解析失败",
                recoverable=True,
            )
            raise AssertionError("unreachable") from exc
    else:
        raw_addresses = (str(literal),)

    addresses: list[str] = []
    for raw_address in raw_addresses:
        try:
            address = ipaddress.ip_address(str(raw_address).split("%", 1)[0])
        except ValueError:
            _reject(
                ErrorCode.SSRF_RESOLUTION_FAILED,
                "解析器返回了无效地址",
            )
        canonical = str(address)
        if canonical not in addresses:
            addresses.append(canonical)
            if len(addresses) > MAX_RESOLVED_PEER_ADDRESSES:
                _reject(
                    ErrorCode.SSRF_RESOLUTION_FAILED,
                    "目标域名返回了过多地址",
                )

    if not addresses:
        _reject(
            ErrorCode.SSRF_RESOLUTION_FAILED,
            "目标域名没有可用地址",
            recoverable=True,
        )
    forbidden = [address for address in addresses if not _is_public_ip(address)]
    if forbidden:
        _reject(
            ErrorCode.SSRF_TARGET_FORBIDDEN,
            "禁止访问内网地址或其他非公网目标",
        )

    return PinnedTarget(
        url=parsed.url,
        scheme=parsed.scheme,
        hostname=parsed.hostname,
        port=parsed.port,
        request_target=parsed.request_target,
        host_header=parsed.host_header,
        peer_addresses=tuple(addresses),
    )


def resolve_host_addresses(hostname: str, port: int) -> tuple[str, ...]:
    """Resolve both IPv4 and IPv6 stream endpoints without opening a socket."""

    results = socket.getaddrinfo(
        hostname,
        port,
        family=socket.AF_UNSPEC,
        type=socket.SOCK_STREAM,
    )
    addresses: list[str] = []
    for _family, _socktype, _proto, _canonname, sockaddr in results:
        address = str(sockaddr[0]).split("%", 1)[0]
        if address not in addresses:
            addresses.append(address)
    return tuple(addresses)


def is_forbidden_hostname(hostname: str) -> bool:
    """Perform the DNS-free portion of the policy for early adapter checks."""

    if not hostname:
        return True
    normalized = hostname.rstrip(".").lower()
    if normalized == "localhost" or normalized.endswith(
        (".localhost", ".local", ".internal", ".lan")
    ):
        return True
    try:
        return not _is_public_ip(normalized)
    except ValueError:
        return False


def _is_public_ip(address: str) -> bool:
    parsed = ipaddress.ip_address(address)
    if parsed.version == 6 and getattr(parsed, "ipv4_mapped", None) is not None:
        parsed = parsed.ipv4_mapped
    return bool(
        parsed.is_global
        and not parsed.is_loopback
        and not parsed.is_link_local
        and not parsed.is_private
        and not parsed.is_multicast
        and not parsed.is_reserved
        and not parsed.is_unspecified
    )


class Urllib3PinnedTransport:
    """Direct transport that never performs DNS after validation."""

    def __init__(self, *, timeout_seconds: float) -> None:
        timeout_value = float(timeout_seconds)
        if not isfinite(timeout_value) or timeout_value <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._timeout_seconds = timeout_value

    def request(
        self,
        target: PinnedTarget,
        *,
        headers: Mapping[str, str],
        max_response_bytes: int,
        deadline: float | None = None,
    ) -> SafeResponse:
        if max_response_bytes <= 0:
            raise ValueError("max_response_bytes must be positive")
        request_headers = {
            key: value
            for key, value in headers.items()
            if key.lower() not in {"host", "accept-encoding"}
        }
        request_headers["Host"] = target.host_header
        request_headers["Accept-Encoding"] = "identity"
        request_deadline = (
            monotonic() + self._timeout_seconds
            if deadline is None
            else deadline
        )
        for peer_address in target.peer_addresses:
            remaining = request_deadline - monotonic()
            if remaining <= 0:
                break
            pool = self._pool_for(
                target,
                peer_address,
                timeout_seconds=remaining,
            )
            response = None
            try:
                response = pool.urlopen(
                    "GET",
                    target.request_target,
                    headers=request_headers,
                    redirect=False,
                    retries=False,
                    preload_content=False,
                )
                content = self._read_bounded(
                    response,
                    max_response_bytes=max_response_bytes,
                    deadline=request_deadline,
                )
                response_headers = {
                    str(key).lower(): str(value)
                    for key, value in response.headers.items()
                }
                return SafeResponse(
                    url=target.url,
                    status_code=int(response.status),
                    headers=response_headers,
                    content=content,
                )
            except SafeFetchTransportError:
                raise
            except (HTTPError, OSError):
                if response is not None:
                    raise SafeFetchTransportError(
                        "安全 HTTP 响应读取失败"
                    ) from None
            finally:
                if response is not None:
                    response.release_conn()
                pool.close()

        error = SafeFetchTransportError("安全 HTTP 请求失败")
        raise error from None

    def _read_bounded(
        self,
        response,
        *,
        max_response_bytes: int,
        deadline: float,
    ) -> bytes:
        """Read one response under both a byte cap and an absolute deadline."""

        chunks: list[bytes] = []
        total = 0
        read_once = getattr(response, "read1", None)
        if not callable(read_once):
            raise SafeFetchTransportError("HTTP 响应读取接口不受支持")
        while True:
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise SafeFetchTransportError("安全 HTTP 请求超时")
            connection = getattr(response, "_connection", None)
            sock = getattr(connection, "sock", None)
            if sock is not None and hasattr(sock, "settimeout"):
                sock.settimeout(remaining)

            allowance = max_response_bytes + 1 - total
            amount = min(_READ_CHUNK_BYTES, allowance)
            chunk = read_once(amount, decode_content=False)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
            total += len(chunk)
            if total > max_response_bytes:
                raise SafeFetchResponseLimitError(
                    f"response exceeds {max_response_bytes} bytes"
                )

    def _pool_for(
        self,
        target: PinnedTarget,
        peer_address: str,
        *,
        timeout_seconds: float | None = None,
    ):
        timeout_budget = (
            self._timeout_seconds
            if timeout_seconds is None
            else timeout_seconds
        )
        common = {
            "host": peer_address,
            "port": target.port,
            "timeout": Timeout(
                total=timeout_budget,
                connect=timeout_budget,
                read=timeout_budget,
            ),
            "maxsize": 1,
            "block": True,
            "retries": False,
        }
        if target.scheme == "https":
            return HTTPSConnectionPool(
                **common,
                ssl_context=ssl.create_default_context(),
                assert_hostname=target.hostname,
                server_hostname=target.hostname,
            )
        return HTTPConnectionPool(**common)


class SafeFetcher:
    """Resolve, pin and fetch one URL while validating every redirect hop."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 20.0,
        max_redirects: int = DEFAULT_MAX_REDIRECTS,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        resolver: Resolver | None = None,
        resolver_runner: _BoundedResolverRunner | None = None,
        transport: PinnedTransport | None = None,
    ) -> None:
        timeout_value = float(timeout_seconds)
        if not isfinite(timeout_value) or timeout_value <= 0:
            raise ValueError("timeout_seconds must be positive")
        if max_redirects < 0:
            raise ValueError("max_redirects cannot be negative")
        if max_response_bytes <= 0:
            raise ValueError("max_response_bytes must be positive")
        self._resolver = resolver or resolve_host_addresses
        self._resolver_runner = resolver_runner or _GLOBAL_RESOLVER_RUNNER
        self._transport = transport or Urllib3PinnedTransport(
            timeout_seconds=timeout_value
        )
        self._timeout_seconds = timeout_value
        self._max_redirects = max_redirects
        self._max_response_bytes = max_response_bytes

    async def fetch(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        max_response_bytes: int | None = None,
    ) -> SafeResponse:
        return await asyncio.to_thread(
            self.fetch_sync,
            url,
            headers=headers,
            max_response_bytes=max_response_bytes,
        )

    def fetch_sync(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        max_response_bytes: int | None = None,
    ) -> SafeResponse:
        current_url = url
        current_headers = dict(headers or {})
        deadline = monotonic() + self._timeout_seconds
        response_bytes_used = 0
        response_limit = (
            self._max_response_bytes
            if max_response_bytes is None
            else max_response_bytes
        )
        if response_limit <= 0:
            raise ValueError("max_response_bytes must be positive")

        for redirect_count in range(self._max_redirects + 1):
            if monotonic() >= deadline:
                raise SafeFetchTransportError("安全 HTTP 请求超时")
            target = resolve_public_target(
                current_url,
                resolver=lambda hostname, port: self._resolver_runner.resolve(
                    self._resolver,
                    hostname,
                    port,
                    deadline=deadline,
                ),
            )
            if monotonic() >= deadline:
                _reject_resolution_timeout()
            remaining_response_bytes = response_limit - response_bytes_used
            if remaining_response_bytes <= 0:
                raise SafeFetchResponseLimitError(
                    f"response exceeds {response_limit} bytes"
                )
            response = self._transport.request(
                target,
                headers=current_headers,
                max_response_bytes=remaining_response_bytes,
                deadline=deadline,
            )
            response_bytes_used += len(response.content)
            if response_bytes_used > response_limit:
                raise SafeFetchResponseLimitError(
                    f"response exceeds {response_limit} bytes"
                )
            if monotonic() >= deadline:
                raise SafeFetchTransportError("安全 HTTP 请求超时")
            if response.status_code not in _REDIRECT_STATUSES:
                return response

            location = response.headers.get("location", "").strip()
            if not location:
                raise SafeFetchTransportError(
                    f"redirect status {response.status_code} missing Location"
                )
            if redirect_count >= self._max_redirects:
                _reject(ErrorCode.SSRF_REDIRECT_LIMIT, "URL 重定向次数超过限制")

            next_url = urljoin(target.url, location)
            next_target = parse_http_target(next_url)
            if target.scheme == "https" and next_target.scheme != "https":
                _reject(
                    ErrorCode.SSRF_TARGET_FORBIDDEN,
                    "禁止从 HTTPS 重定向到 HTTP",
                )
            if _origin(target) != _origin(next_target):
                current_headers = {
                    key: value
                    for key, value in current_headers.items()
                    if key.lower() not in _SENSITIVE_REDIRECT_HEADERS
                }
            current_url = next_target.url

        raise AssertionError("redirect loop exhausted without terminal state")


def _origin(target: ParsedTarget) -> tuple[str, str, int]:
    return target.scheme, target.hostname, target.port


def describe_url_target(url: str) -> str:
    """Return a log-safe origin label without userinfo, path, query or fragment."""

    try:
        target = parse_http_target(url)
    except Exception:
        return "invalid-url"
    return f"{target.scheme}://{target.host_header}"


def _reject(
    code: ErrorCode,
    message: str,
    *,
    recoverable: bool = False,
) -> None:
    raise PKVRuntimeError(
        code,
        message,
        stage="network_policy",
        recoverable=recoverable,
    )


def _reject_resolution_timeout() -> None:
    _reject(
        ErrorCode.SSRF_RESOLUTION_FAILED,
        "目标域名解析超时",
        recoverable=True,
    )
