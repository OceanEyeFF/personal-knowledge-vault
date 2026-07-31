"""Repository-wide safety gates for automated pytest tests."""

from __future__ import annotations

from collections.abc import Iterator
import os
from pathlib import Path

import pytest

from src.utils import config as config_module
from src.utils.config import Config
from tests.offline_runtime import (
    LIVE_ENV_KEYS,
    LOAD_LOCAL_SENTINEL,
    OFFLINE_SENTINEL,
    PROJECT_ROOT_SENTINEL,
    RUNTIME_PATH_ENV_KEYS,
    assert_config_runtime_paths,
    install_offline_network_guard,
    scrub_child_process_env,
    validate_test_runtime_paths,
)


_LIVE_SKIP_REASON = "network tests require explicit PKV_RUN_LIVE=1"
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_BASE_CONFIG_PATH = _PROJECT_ROOT / "config" / "config.yaml"
_SCRUBBED_PARENT_ENV: dict[str, tuple[bool, str | None]] = {}
_PREVIOUS_CONFIG: Config | None = None
_OFFLINE_CONFIG: Config | None = None
_OFFLINE_CONFIG_INSTALLED = False


def _remember_env(key: str) -> None:
    if key not in _SCRUBBED_PARENT_ENV:
        _SCRUBBED_PARENT_ENV[key] = (key in os.environ, os.environ.get(key))


def _require_offline_entrypoint_contract() -> None:
    """Reject direct pytest starts that bypass the isolated test launcher."""

    expected = {
        "PKV_RUN_LIVE": "0",
        OFFLINE_SENTINEL: "1",
        LOAD_LOCAL_SENTINEL: "0",
    }
    for key, value in expected.items():
        if os.environ.get(key) != value:
            raise pytest.UsageError(
                "offline pytest must run through scripts/run-test.ps1 or the "
                f"equivalent CI launcher ({key}={value} required)"
            )

    root_sentinel = os.environ.get(PROJECT_ROOT_SENTINEL)
    if root_sentinel is None:
        raise pytest.UsageError(
            f"offline pytest requires {PROJECT_ROOT_SENTINEL} from its launcher"
        )
    actual_root = os.path.normcase(
        os.path.abspath(os.path.normpath(root_sentinel))
    )
    expected_root = os.path.normcase(os.path.abspath(_PROJECT_ROOT))
    if actual_root != expected_root:
        raise pytest.UsageError(
            f"offline pytest project root mismatch: {actual_root} != {expected_root}"
        )


def _install_base_only_config() -> None:
    """Install one validated base-only Config before test module collection."""

    global _OFFLINE_CONFIG, _OFFLINE_CONFIG_INSTALLED, _PREVIOUS_CONFIG

    missing = sorted(key for key in RUNTIME_PATH_ENV_KEYS if key not in os.environ)
    if missing:
        raise pytest.UsageError(
            "offline pytest launcher omitted runtime paths: " + ", ".join(missing)
        )
    canonical = validate_test_runtime_paths(
        project_root=_PROJECT_ROOT,
        runtime_overrides={key: os.environ[key] for key in RUNTIME_PATH_ENV_KEYS},
    )
    os.environ.update(canonical)

    isolated_config = Config(str(_BASE_CONFIG_PATH), None)
    assert_config_runtime_paths(isolated_config, canonical)
    isolated_config.ensure_dirs()

    _PREVIOUS_CONFIG = config_module._config_instance
    config_module._config_instance = isolated_config
    _OFFLINE_CONFIG = isolated_config
    _OFFLINE_CONFIG_INSTALLED = True


def pytest_configure(config: pytest.Config) -> None:
    """Scrub live state, install base config, and block I/O before collection."""

    del config
    if os.environ.get("PKV_RUN_LIVE") == "1":
        return

    _require_offline_entrypoint_contract()
    before = dict(os.environ)
    scrub_child_process_env(os.environ)
    for key, value in before.items():
        if key not in os.environ:
            _SCRUBBED_PARENT_ENV[key] = (True, value)
    for key in tuple(os.environ):
        if key.upper() in LIVE_ENV_KEYS:
            _remember_env(key)
            os.environ.pop(key, None)
    for key, value in (
        ("PKV_RUN_LIVE", "0"),
        (OFFLINE_SENTINEL, "1"),
        (LOAD_LOCAL_SENTINEL, "0"),
    ):
        _remember_env(key)
        os.environ[key] = value

    install_offline_network_guard(block_raw_sockets=False)
    _install_base_only_config()


def pytest_unconfigure(config: pytest.Config) -> None:
    """Restore parent config and environment values after the pytest process."""

    global _OFFLINE_CONFIG, _OFFLINE_CONFIG_INSTALLED, _PREVIOUS_CONFIG

    del config
    if _OFFLINE_CONFIG_INSTALLED:
        config_module._config_instance = _PREVIOUS_CONFIG
        _OFFLINE_CONFIG = None
        _PREVIOUS_CONFIG = None
        _OFFLINE_CONFIG_INSTALLED = False
    for key, (existed, value) in _SCRUBBED_PARENT_ENV.items():
        if existed and value is not None:
            os.environ[key] = value
        else:
            os.environ.pop(key, None)
    _SCRUBBED_PARENT_ENV.clear()


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Skip selected network tests before any fixture can load live config."""

    if os.environ.get("PKV_RUN_LIVE") == "1":
        return
    skip_live = pytest.mark.skip(reason=_LIVE_SKIP_REASON)
    for item in items:
        if item.get_closest_marker("network") is not None:
            item.add_marker(skip_live)


@pytest.fixture(autouse=True)
def _restore_base_only_config_singleton() -> Iterator[None]:
    """Keep every offline test isolated from process-global Config leakage."""

    if os.environ.get("PKV_RUN_LIVE") == "1":
        yield
        return
    if _OFFLINE_CONFIG is None:
        raise RuntimeError("offline base-only Config was not installed")

    config_module._config_instance = _OFFLINE_CONFIG
    try:
        yield
    finally:
        config_module._config_instance = _OFFLINE_CONFIG


@pytest.fixture(autouse=True)
def _fail_closed_network_gate(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Block parent-process outbound traffic for every non-network test."""

    if request.node.get_closest_marker("network") is not None:
        if os.environ.get("PKV_RUN_LIVE") != "1":
            pytest.skip(_LIVE_SKIP_REASON)
        return
    install_offline_network_guard(
        monkeypatch.setattr,
        block_raw_sockets=False,
    )
