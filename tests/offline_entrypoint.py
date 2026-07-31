"""Dedicated base-config entrypoint for CLI and MCP child-process tests."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Callable

from tests.offline_runtime import (
    LOAD_LOCAL_SENTINEL,
    OFFLINE_SENTINEL,
    PROJECT_ROOT_SENTINEL,
    RUNTIME_PATH_ENV_KEYS,
    assert_config_runtime_paths,
    install_offline_network_guard,
    scrub_child_process_env,
    validate_test_runtime_paths,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BASE_CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"
LOCAL_CONFIG_PATH = PROJECT_ROOT / "config" / "local.yaml"


def _live_mode_enabled() -> bool:
    load_local = os.environ.get(LOAD_LOCAL_SENTINEL) == "1"
    run_live = os.environ.get("PKV_RUN_LIVE") == "1"
    offline = os.environ.get(OFFLINE_SENTINEL) == "1"
    if load_local and run_live and not offline:
        return True
    if not load_local and not run_live and offline:
        return False
    raise RuntimeError("child environment live/offline sentinels are inconsistent")


def _validate_child_environment() -> None:
    expected_lexical = Path(
        os.path.abspath(os.path.normpath(os.fspath(PROJECT_ROOT)))
    )
    configured_root = os.environ.get(PROJECT_ROOT_SENTINEL)
    if configured_root is None or not Path(configured_root).is_absolute():
        raise RuntimeError("child project-root sentinel is missing or invalid")
    configured_lexical = Path(
        os.path.abspath(os.path.normpath(configured_root))
    )
    if os.path.normcase(os.fspath(configured_lexical)) != os.path.normcase(
        os.fspath(expected_lexical)
    ):
        raise RuntimeError("child project-root sentinel is missing or invalid")

    expected_root = expected_lexical.resolve()
    if configured_lexical.resolve() != expected_root:
        raise RuntimeError("child project-root sentinel is missing or invalid")

    runtime_overrides = {
        key: os.environ[key]
        for key in RUNTIME_PATH_ENV_KEYS
        if key in os.environ
    }
    canonical = validate_test_runtime_paths(
        project_root=expected_root,
        runtime_overrides=runtime_overrides,
    )
    os.environ.update(canonical)


def _install_test_config(*, load_local: bool) -> Callable[..., object]:
    """Install Config before importing either product entrypoint."""

    from src.utils import config as config_module

    def new_config(*_args, **_kwargs):
        local_path = str(LOCAL_CONFIG_PATH) if load_local else None
        return config_module.Config(str(BASE_CONFIG_PATH), local_path)

    config = new_config()
    assert_config_runtime_paths(
        config,
        {key: os.environ[key] for key in RUNTIME_PATH_ENV_KEYS},
    )
    config.ensure_dirs()
    config_module._config_instance = config

    def validated_config_factory(*_args, **_kwargs):
        return config

    return validated_config_factory


def _run_cli(config_factory: Callable[..., object]) -> None:
    import src.cli.commands as commands_module

    # commands.py imports Config directly, so patch that binding as well as the
    # shared config singleton before importing the lazy CLI entrypoint.
    commands_module.Config = config_factory

    from src.main import main

    main()


def _run_mcp() -> None:
    from src.mcp.server import main

    main()


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("usage: offline_entrypoint.py {cli|mcp} [arguments...]")

    target = sys.argv[1]
    if target not in {"cli", "mcp"}:
        raise SystemExit(f"unsupported test child target: {target}")

    _validate_child_environment()
    scrub_child_process_env(os.environ)
    load_local = _live_mode_enabled()
    if not load_local:
        install_offline_network_guard(block_raw_sockets=False)

    sys.argv = [sys.argv[0], *sys.argv[2:]]
    config_factory = _install_test_config(load_local=load_local)
    if target == "cli":
        _run_cli(config_factory)
        return
    _run_mcp()


if __name__ == "__main__":
    main()
