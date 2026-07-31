"""Contracts for the fail-closed automated-test runtime."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tests import offline_entrypoint
from tests.offline_runtime import (
    LOAD_LOCAL_SENTINEL,
    OFFLINE_SENTINEL,
    PROJECT_ROOT_SENTINEL,
    OfflineNetworkError,
    install_offline_network_guard,
    prepare_live_child_env,
    prepare_offline_child_env,
    validate_test_runtime_paths,
)


def test_offline_child_env_scrubs_live_secret_provider_and_proxy_sentinels(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    test_data = project_root / ".data-test" / "case"
    parent = {
        "PATH": "safe-path-sentinel",
        "SAFE_PARENT_VALUE": "preserved-sentinel",
        "PKV_RUN_LIVE": "1",
        LOAD_LOCAL_SENTINEL: "1",
        "PKV_E2E_ARCHIVE_URL": "https://live.example/sentinel",
        "PKV_MCP_AUTH_TOKEN": "auth-token-sentinel",
        "PKV_LLM_API_KEY": "llm-secret-sentinel",
        "OPENAI_API_KEY": "provider-secret-sentinel",
        "HTTPS_PROXY": "http://proxy.example/sentinel",
        "http_proxy": "http://lowercase-proxy.example/sentinel",
        "PYTHONPATH": "inherited-pythonpath-sentinel",
    }
    runtime = {
        "DATA_DIR": test_data,
        "DB_PATH": test_data / "db" / "test.db",
    }

    child = prepare_offline_child_env(
        project_root=project_root,
        runtime_overrides=runtime,
        parent_env=parent,
    )

    assert parent["PKV_RUN_LIVE"] == "1"
    assert child["SAFE_PARENT_VALUE"] == "preserved-sentinel"
    assert child["PKV_RUN_LIVE"] == "0"
    assert child[OFFLINE_SENTINEL] == "1"
    assert child[LOAD_LOCAL_SENTINEL] == "0"
    assert child["PYTHONPATH"] == str(project_root.resolve())
    assert child["DATA_DIR"] == str(runtime["DATA_DIR"])
    assert child["DB_PATH"] == str(runtime["DB_PATH"])
    for key in (
        "PKV_E2E_ARCHIVE_URL",
        "PKV_MCP_AUTH_TOKEN",
        "PKV_LLM_API_KEY",
        "OPENAI_API_KEY",
        "HTTPS_PROXY",
        "http_proxy",
    ):
        assert key not in child


@pytest.mark.parametrize(
    "unsafe_key",
    ["PKV_RUN_LIVE", "PKV_LLM_API_KEY", "OPENAI_API_KEY", "HTTPS_PROXY"],
)
def test_offline_child_env_rejects_unsafe_runtime_overrides(
    tmp_path: Path,
    unsafe_key: str,
) -> None:
    with pytest.raises(ValueError, match="unsafe child runtime override"):
        prepare_offline_child_env(
            project_root=tmp_path,
            runtime_overrides={unsafe_key: "unsafe-sentinel"},
            parent_env={},
        )


def test_runtime_paths_reject_production_data_root(tmp_path: Path) -> None:
    project_root = tmp_path / "project"

    with pytest.raises(ValueError, match="unsafe automated-test DATA_DIR"):
        validate_test_runtime_paths(
            project_root=project_root,
            runtime_overrides={"DATA_DIR": project_root / ".data"},
        )


@pytest.mark.parametrize(
    "unsafe_data_dir",
    [
        ".data/case",
        ".git/case",
        "config/case",
        "../sibling-worktree/.data/case",
        "../outside-temp/case",
    ],
)
def test_runtime_paths_only_accept_project_data_test(
    tmp_path: Path,
    unsafe_data_dir: str,
) -> None:
    project_root = tmp_path / "project"

    with pytest.raises(ValueError, match="unsafe automated-test DATA_DIR"):
        validate_test_runtime_paths(
            project_root=project_root,
            runtime_overrides={"DATA_DIR": unsafe_data_dir},
        )


def test_runtime_paths_reject_unsafe_candidate_before_resolve(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "project"
    unsafe = project_root / "config" / "forbidden-runtime"
    real_resolve = Path.resolve

    def guarded_resolve(path: Path, *args, **kwargs):
        if path == unsafe:
            raise AssertionError("unsafe candidate must not be resolved")
        return real_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", guarded_resolve)

    with pytest.raises(ValueError, match="unsafe automated-test DATA_DIR"):
        validate_test_runtime_paths(
            project_root=project_root,
            runtime_overrides={"DATA_DIR": unsafe},
        )


def test_runtime_paths_resolve_relative_values_from_project_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "project"
    outside_cwd = tmp_path / "outside-cwd"
    outside_cwd.mkdir()
    monkeypatch.chdir(outside_cwd)

    canonical = validate_test_runtime_paths(
        project_root=project_root,
        runtime_overrides={
            "DATA_DIR": ".data-test/case",
            "DB_PATH": ".data-test/case/db/test.db",
        },
    )

    assert canonical == {
        "DATA_DIR": str((project_root / ".data-test" / "case").resolve()),
        "DB_PATH": str((project_root / ".data-test" / "case" / "db" / "test.db").resolve()),
    }


def test_runtime_paths_reject_storage_outside_test_data_root(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    with pytest.raises(ValueError, match="DB_PATH must remain inside"):
        validate_test_runtime_paths(
            project_root=project_root,
            runtime_overrides={
                "DATA_DIR": project_root / ".data-test" / "isolated-data",
                "DB_PATH": project_root / ".data-test" / "sibling" / "test.db",
            },
        )


def test_runtime_paths_reject_unknown_or_duplicate_overrides(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="unsupported child runtime override"):
        validate_test_runtime_paths(
            project_root=tmp_path / "project",
            runtime_overrides={
                "DATA_DIR": tmp_path / "isolated-data",
                "ARBITRARY_PATH": tmp_path / "elsewhere",
            },
        )

    with pytest.raises(ValueError, match="duplicate child runtime override"):
        validate_test_runtime_paths(
            project_root=tmp_path / "project",
            runtime_overrides={
                "DATA_DIR": tmp_path / "isolated-data",
                "data_dir": tmp_path / "other-data",
            },
        )


@pytest.mark.parametrize("data_dir", [".data", "config/case"])
def test_entrypoint_rejects_unsafe_relative_runtime_before_config(
    data_dir: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    outside_cwd = tmp_path / "outside-entrypoint-cwd"
    outside_cwd.mkdir()
    monkeypatch.chdir(outside_cwd)
    monkeypatch.setenv(
        PROJECT_ROOT_SENTINEL,
        str(offline_entrypoint.PROJECT_ROOT.resolve()),
    )
    monkeypatch.setenv("DATA_DIR", data_dir)
    for key in ("DB_PATH", "VAULT_DIR", "VECTOR_DIR", "LOG_DIR", "TMP_DIR"):
        monkeypatch.delenv(key, raising=False)

    with pytest.raises(ValueError, match="unsafe automated-test DATA_DIR"):
        offline_entrypoint._validate_child_environment()


def test_entrypoint_writes_canonical_runtime_paths_back_to_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = offline_entrypoint.PROJECT_ROOT.resolve()
    monkeypatch.setenv(PROJECT_ROOT_SENTINEL, str(project_root))
    monkeypatch.setenv("DATA_DIR", ".data-test/entrypoint-canonical")
    monkeypatch.setenv("DB_PATH", ".data-test/entrypoint-canonical/db/test.db")
    for key in ("VAULT_DIR", "VECTOR_DIR", "LOG_DIR", "TMP_DIR"):
        monkeypatch.delenv(key, raising=False)

    offline_entrypoint._validate_child_environment()

    assert os.environ["DATA_DIR"] == str(
        (project_root / ".data-test" / "entrypoint-canonical").resolve()
    )
    assert os.environ["DB_PATH"] == str(
        (project_root / ".data-test" / "entrypoint-canonical" / "db" / "test.db").resolve()
    )


def test_entrypoint_rejects_unknown_target_before_config_install(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_config = MagicMock(side_effect=AssertionError("must not install config"))
    monkeypatch.setattr(offline_entrypoint, "_install_test_config", install_config)
    monkeypatch.setattr(
        sys,
        "argv",
        [str(offline_entrypoint.__file__), "unsupported-target"],
    )

    with pytest.raises(SystemExit, match="unsupported test child target"):
        offline_entrypoint.main()
    install_config.assert_not_called()


def test_entrypoint_base_config_does_not_load_local(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.utils import config as config_module

    config = MagicMock()
    config.data_dir = Path(os.environ["DATA_DIR"])
    config.db_path = Path(os.environ["DB_PATH"])
    config.vault_dir = Path(os.environ["VAULT_DIR"])
    config.vector_index_dir = Path(os.environ["VECTOR_DIR"])
    config.log_dir = Path(os.environ["LOG_DIR"])
    config.tmp_dir = Path(os.environ["TMP_DIR"])
    factory = MagicMock(return_value=config)
    monkeypatch.setattr(config_module, "Config", factory)
    monkeypatch.setattr(config_module, "_config_instance", None)

    installed_factory = offline_entrypoint._install_test_config(load_local=False)

    factory.assert_called_once_with(
        str(offline_entrypoint.BASE_CONFIG_PATH),
        None,
    )
    config.ensure_dirs.assert_called_once_with()
    assert config_module._config_instance is config
    assert installed_factory() is config
    factory.assert_called_once()


def test_pytest_session_uses_base_only_config_for_processor_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.processors.generic_processor import GenericProcessor
    from src.utils import config as config_module

    session_config = config_module._config_instance
    assert session_config is not None
    assert session_config._local_config_path is None

    default_factory = MagicMock(
        side_effect=AssertionError("default Config/local.yaml must not be loaded")
    )
    monkeypatch.setattr(config_module, "Config", default_factory)

    processor = GenericProcessor()

    assert processor.user_agent
    default_factory.assert_not_called()


def test_entrypoint_rejects_config_path_drift_before_ensure_dirs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.utils import config as config_module

    config = MagicMock()
    config.data_dir = Path(os.environ["DATA_DIR"])
    config.db_path = Path(os.environ["DB_PATH"])
    config.vault_dir = Path(os.environ["VAULT_DIR"])
    config.vector_index_dir = Path(os.environ["VECTOR_DIR"])
    config.log_dir = offline_entrypoint.PROJECT_ROOT / ".forbidden-config-log"
    config.tmp_dir = Path(os.environ["TMP_DIR"])
    monkeypatch.setattr(config_module, "Config", MagicMock(return_value=config))

    with pytest.raises(RuntimeError, match="escaped validated runtime"):
        offline_entrypoint._install_test_config(load_local=False)

    config.ensure_dirs.assert_not_called()


def test_live_child_env_requires_explicit_parent_opt_in(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="PKV_RUN_LIVE=1"):
        prepare_live_child_env(
            project_root=tmp_path,
            runtime_overrides={"DATA_DIR": tmp_path / ".data-test" / "live"},
            parent_env={"PKV_RUN_LIVE": "0"},
        )


def test_network_guard_blocks_dns_connect_and_datagrams(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_offline_network_guard(monkeypatch.setattr)

    with pytest.raises(OfflineNetworkError, match="outbound network"):
        socket.getaddrinfo("network-sentinel.invalid", 443)
    with pytest.raises(OfflineNetworkError, match="outbound network"):
        socket.create_connection(("network-sentinel.invalid", 443))

    client = socket.socket()
    try:
        with pytest.raises(OfflineNetworkError, match="outbound network"):
            client.connect(("127.0.0.1", 9))
        with pytest.raises(OfflineNetworkError, match="outbound network"):
            client.sendto(b"sentinel", ("127.0.0.1", 9))
    finally:
        client.close()


def test_network_guard_allows_internal_socketpair_but_blocks_raw_external_ip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_offline_network_guard(monkeypatch.setattr, block_raw_sockets=False)

    client = socket.socket()
    try:
        for method, args in (
            (client.connect, (("169.254.169.254", 80),)),
            (client.connect_ex, (("8.8.8.8", 53),)),
            (client.sendto, (b"sentinel", ("192.0.2.1", 9))),
        ):
            with pytest.raises(OfflineNetworkError, match="outbound network"):
                method(*args)
        if hasattr(client, "sendmsg"):
            with pytest.raises(OfflineNetworkError, match="outbound network"):
                client.sendmsg([b"sentinel"], [], 0, ("192.0.2.1", 9))
    finally:
        client.close()

    left, right = socket.socketpair()
    left.close()
    right.close()


def test_project_tests_package_wins_over_shadow_package(tmp_path: Path) -> None:
    project_root = Path(__file__).resolve().parents[2]
    shadow = tmp_path / "shadow"
    shadow_tests = shadow / "tests"
    shadow_tests.mkdir(parents=True)
    (shadow_tests / "__init__.py").write_text(
        "raise RuntimeError('shadow tests package imported')\n",
        encoding="utf-8",
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join((str(project_root), str(shadow)))
    probe = (
        "from pathlib import Path; "
        "import tests.offline_runtime as module; "
        f"assert Path(module.__file__).resolve().is_relative_to(Path({str(project_root / 'tests')!r}).resolve())"
    )

    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
