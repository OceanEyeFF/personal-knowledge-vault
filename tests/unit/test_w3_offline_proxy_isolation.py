"""Keep synthetic Provider routes independent from Windows system proxies."""

from __future__ import annotations

import os
import socket
import sys
import urllib.request

import httpcore
import httpx
import pytest

from tests import offline_entrypoint, offline_runtime
from tests.offline_runtime import OfflineNetworkError


@pytest.mark.parametrize("synthetic_provider", [False, True], ids=["ordinary_child", "validated_provider_child"])
def test_only_validated_provider_child_bypasses_system_proxy_and_keeps_endpoint_guard(monkeypatch, synthetic_provider):
    # Match urllib's Windows precedence without reading/changing the registry:
    # use environment settings when nonempty, otherwise a synthetic system proxy.
    registry_proxy = {"http": "http://127.0.0.1:39002"}
    monkeypatch.setattr(os, "environ", dict(os.environ))
    for key in tuple(os.environ):
        if key.upper() in offline_runtime.PROXY_ENV_KEYS:
            os.environ.pop(key)

    def proxy_settings():
        return urllib.request.getproxies_environment() or registry_proxy

    monkeypatch.setattr(httpx._utils, "getproxies", proxy_settings)
    target = httpx.URL("http://127.0.0.1:39001/v1")
    with httpx.Client() as client:
        assert isinstance(client._transport_for_url(target)._pool, httpcore.HTTPProxy)

    # The entrypoint must discard an inherited wildcard before installing only
    # its own exact loopback bypass; ordinary children retain the blocking guard.
    os.environ["NO_PROXY"] = "*"
    monkeypatch.setattr(offline_entrypoint, "_validate_child_environment", lambda: None)
    monkeypatch.setattr(offline_entrypoint, "_live_mode_enabled", lambda: False)
    monkeypatch.setattr(offline_entrypoint, "_consume_absent_data_root_request", lambda: False)
    request = ("synthetic-config", frozenset({("127.0.0.1", 39001)})) if synthetic_provider else None
    monkeypatch.setattr(offline_entrypoint, "_consume_test_user_config_request", lambda **kwargs: request)
    monkeypatch.setattr(offline_entrypoint, "_install_test_config", lambda **kwargs: object())
    monkeypatch.setattr(offline_entrypoint, "_bind_test_config_factory", lambda factory: None)
    monkeypatch.setattr(offline_entrypoint, "mark_offline_runtime_ready", lambda **kwargs: None)
    monkeypatch.setattr(offline_runtime, "_ORIGINAL_GETADDRINFO", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        offline_entrypoint, "install_offline_network_guard",
        lambda **kwargs: offline_runtime.install_offline_network_guard(monkeypatch.setattr, **kwargs),
    )
    observed = []

    def inspect_child_boundary():
        assert os.environ.get("NO_PROXY") == ("127.0.0.1,localhost" if synthetic_provider else None)
        with httpx.Client() as client:
            uses_proxy = isinstance(client._transport_for_url(target)._pool, httpcore.HTTPProxy)
        assert uses_proxy is (not synthetic_provider)
        if synthetic_provider:
            assert socket.getaddrinfo("127.0.0.1", 39001) == []
        else:
            with pytest.raises(OfflineNetworkError):
                socket.getaddrinfo("127.0.0.1", 39001)
        with pytest.raises(OfflineNetworkError):
            socket.getaddrinfo("127.0.0.1", 39002)
        with pytest.raises(OfflineNetworkError):
            socket.getaddrinfo("example.invalid", 443)
        observed.append(True)

    monkeypatch.setattr(offline_entrypoint, "_run_mcp", inspect_child_boundary)
    monkeypatch.setattr(sys, "argv", [str(offline_entrypoint.__file__), "mcp"])
    offline_entrypoint.main()
    assert observed == [True]
