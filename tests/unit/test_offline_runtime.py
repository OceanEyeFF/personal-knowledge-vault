"""Contracts for the fail-closed automated-test runtime."""

from __future__ import annotations

import os
import socket
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from tests import offline_entrypoint
from tests.offline_runtime import (
    LOAD_LOCAL_SENTINEL,
    OFFLINE_SENTINEL,
    PROJECT_ROOT_SENTINEL,
    OfflineNetworkError,
    OfflineProcessError,
    clear_offline_runtime_ready,
    install_offline_network_guard,
    install_offline_process_guard,
    mark_offline_runtime_ready,
    prepare_live_child_env,
    prepare_offline_child_env,
    require_offline_runtime_ready,
    validate_test_runtime_paths,
)
from src.runtime.layout import RuntimeLayout


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
        "PKV_DATA_ROOT": "C:/user-product-root-sentinel",
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
    assert child["PKV_DATA_ROOT"] == str(runtime["DATA_DIR"])
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


def test_offline_data_dir_wins_over_inherited_product_root(tmp_path: Path) -> None:
    """The wrapper may pin PKV_DATA_ROOT, but nested fixtures own DATA_DIR."""

    resources = tmp_path / "resources"
    resources.mkdir()
    selected_root = tmp_path / "project" / ".data-test" / "nested-case"
    inherited_root = tmp_path / "user-product-root-sentinel"

    layout = RuntimeLayout.resolve(
        resources_root=resources,
        environment={
            "PKV_TEST_OFFLINE": "1",
            "DATA_DIR": str(selected_root),
            "PKV_DATA_ROOT": str(inherited_root),
        },
    )

    assert layout.user_data_root == selected_root.resolve()


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
    monkeypatch.setenv("PKV_DATA_ROOT", "C:/user-product-root-sentinel")
    for key in ("VAULT_DIR", "VECTOR_DIR", "LOG_DIR", "TMP_DIR"):
        monkeypatch.delenv(key, raising=False)

    offline_entrypoint._validate_child_environment()

    assert os.environ["DATA_DIR"] == str(
        (project_root / ".data-test" / "entrypoint-canonical").resolve()
    )
    assert os.environ["DB_PATH"] == str(
        (project_root / ".data-test" / "entrypoint-canonical" / "db" / "test.db").resolve()
    )
    assert os.environ["PKV_DATA_ROOT"] == os.environ["DATA_DIR"]


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


@pytest.mark.parametrize(
    ("target", "arguments"),
    [
        ("python", ["scripts/setup-test-db.py"]),
        ("pytest", ["tests/unit"]),
    ],
)
def test_generic_entrypoint_rejects_live_mode_before_product_config(
    monkeypatch: pytest.MonkeyPatch,
    target: str,
    arguments: list[str],
) -> None:
    install_config = MagicMock(side_effect=AssertionError("must not install config"))
    monkeypatch.setattr(offline_entrypoint, "_validate_child_environment", lambda: None)
    monkeypatch.setattr(offline_entrypoint, "_install_test_config", install_config)
    monkeypatch.setenv("PKV_RUN_LIVE", "1")
    monkeypatch.setenv(OFFLINE_SENTINEL, "0")
    monkeypatch.setenv(LOAD_LOCAL_SENTINEL, "1")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(offline_entrypoint.__file__),
            target,
            *arguments,
        ],
    )

    with pytest.raises(RuntimeError, match="only in offline mode"):
        offline_entrypoint.main()
    install_config.assert_not_called()


def test_pytest_entrypoint_preserves_real_config_class(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_factory = MagicMock(name="validated_config_factory")
    bind_factory = MagicMock(
        side_effect=AssertionError("pytest must retain the real Config class")
    )
    run_pytest = MagicMock(side_effect=SystemExit(0))

    monkeypatch.setattr(offline_entrypoint, "_validate_child_environment", lambda: None)
    monkeypatch.setattr(offline_entrypoint, "_live_mode_enabled", lambda: False)
    monkeypatch.setattr(offline_entrypoint, "scrub_child_process_env", lambda _env: None)
    monkeypatch.setattr(offline_entrypoint, "install_offline_network_guard", lambda **_kwargs: None)
    monkeypatch.setattr(offline_entrypoint, "_install_test_config", lambda **_kwargs: config_factory)
    monkeypatch.setattr(offline_entrypoint, "_bind_test_config_factory", bind_factory)
    monkeypatch.setattr(offline_entrypoint, "mark_offline_runtime_ready", lambda **_kwargs: None)
    monkeypatch.setattr(offline_entrypoint, "_run_pytest", run_pytest)
    for key in offline_entrypoint.LIVE_ENV_KEYS:
        monkeypatch.setenv(key, "parent-sentinel")
    monkeypatch.setattr(
        sys,
        "argv",
        [str(offline_entrypoint.__file__), "pytest", "tests/unit"],
    )

    with pytest.raises(SystemExit) as exc_info:
        offline_entrypoint.main()

    assert exc_info.value.code == 0
    bind_factory.assert_not_called()
    run_pytest.assert_called_once_with()


def test_entrypoint_base_config_does_not_load_user_config(
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
        user_config_path=None,
    )
    config.ensure_dirs.assert_called_once_with()
    assert config_module._config_instance is config
    assert installed_factory() is config
    factory.assert_called_once()


def test_entrypoint_live_config_uses_user_profile_and_keeps_isolated_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Live config remains opt-in and cannot retarget the validated data root."""

    from src.utils import config as config_module

    config = MagicMock()
    config.data_dir = Path(os.environ["DATA_DIR"])
    config.db_path = Path(os.environ["DB_PATH"])
    config.vault_dir = Path(os.environ["VAULT_DIR"])
    config.vector_index_dir = Path(os.environ["VECTOR_DIR"])
    config.log_dir = Path(os.environ["LOG_DIR"])
    config.tmp_dir = Path(os.environ["TMP_DIR"])
    user_profile = tmp_path / "mock-user-profile"
    isolated_root = Path(os.environ["DATA_DIR"])
    factory = MagicMock(return_value=config)
    monkeypatch.setenv("USERPROFILE", str(user_profile))
    monkeypatch.setenv("PKV_DATA_ROOT", str(isolated_root))
    monkeypatch.setattr(config_module, "Config", factory)
    monkeypatch.setattr(config_module, "_config_instance", None)

    offline_entrypoint._install_test_config(load_local=True)

    factory.assert_called_once_with(
        str(offline_entrypoint.BASE_CONFIG_PATH),
        user_config_path=str(user_profile / ".pkv" / "config.yaml"),
    )
    assert os.environ["PKV_DATA_ROOT"] == str(isolated_root)
    config.ensure_dirs.assert_called_once_with()


def test_synthetic_ready_child_does_not_take_a_setup_lease_before_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The synthetic READY seam may seed once, but must not pre-write every child."""

    from src.utils import config as config_module

    config = MagicMock()
    config.data_dir = Path(os.environ["DATA_DIR"])
    config.db_path = Path(os.environ["DB_PATH"])
    config.vault_dir = Path(os.environ["VAULT_DIR"])
    config.vector_index_dir = Path(os.environ["VECTOR_DIR"])
    config.log_dir = Path(os.environ["LOG_DIR"])
    config.tmp_dir = Path(os.environ["TMP_DIR"])
    seed = MagicMock()
    monkeypatch.setenv("PKV_TEST_SYNTHETIC_RUNTIME_READY", "1")
    monkeypatch.setattr(config_module, "Config", MagicMock(return_value=config))
    monkeypatch.setattr(config_module, "_config_instance", None)
    monkeypatch.setattr(offline_entrypoint, "_seed_synthetic_ready_runtime_snapshot", seed)

    offline_entrypoint._install_test_config(load_local=False)

    config.ensure_dirs.assert_not_called()
    seed.assert_called_once_with(config)


def test_synthetic_ready_seed_preserves_existing_runtime_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fresh offline child must observe existing drift/degradation unchanged."""

    from src.storage import migration_manager
    from src.storage.migration_manager import DatabaseState

    manager = MagicMock(
        return_value=SimpleNamespace(
            inspect_database=MagicMock(
                return_value=SimpleNamespace(state=DatabaseState.READY)
            )
        )
    )
    existing_snapshot = {
        "schema_version": 1,
        "database": {"schema_version": "1.2.5"},
        "embedding": {"provider": "offline", "fingerprint": {}},
    }
    config = SimpleNamespace(
        layout=SimpleNamespace(
            db_path=Path("isolated") / "knowledge_vault.db",
            migrations_dir=Path("resources") / "migrations",
            backup_dir=Path("isolated") / "backups",
        ),
        read_runtime_config_snapshot=MagicMock(return_value=existing_snapshot),
        write_runtime_config_snapshot=MagicMock(),
    )
    monkeypatch.setattr(migration_manager, "MigrationManager", manager)

    offline_entrypoint._seed_synthetic_ready_runtime_snapshot(config)

    config.read_runtime_config_snapshot.assert_called_once_with()
    config.write_runtime_config_snapshot.assert_not_called()


def test_synthetic_ready_seed_prewarms_cache_only_with_new_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The child-only fixture gives its first writer a complete read contract."""

    from src.runtime.write_lease import has_active_write_lease
    from src.storage.migration_manager import MigrationManager
    from src.utils import text_utils
    from src.utils.config import Config

    layout = RuntimeLayout.resolve(
        resources_root=offline_entrypoint.PROJECT_ROOT,
        user_data_root=tmp_path / "ready-data",
        profile_root=tmp_path / "profile",
        environment={},
    )
    config = Config(layout=layout)
    layout.ensure_user_directories()
    MigrationManager(layout.db_path, layout.migrations_dir).initialize_fresh()
    observed_lease_states: list[bool] = []

    class _PrewarmTextProcessor:
        def __init__(self, *, runtime_config: Config, initialize_cache: bool) -> None:
            assert runtime_config is config
            assert initialize_cache is True
            observed_lease_states.append(has_active_write_lease(config.layout))
            (config.layout.tmp_dir / "jieba.cache").write_bytes(b"fixture-cache")

    monkeypatch.setattr(text_utils, "TextProcessor", _PrewarmTextProcessor)

    offline_entrypoint._seed_synthetic_ready_runtime_snapshot(config)
    offline_entrypoint._seed_synthetic_ready_runtime_snapshot(config)

    assert observed_lease_states == [True]
    assert layout.runtime_config_path.is_file()
    assert (layout.tmp_dir / "jieba.cache").read_bytes() == b"fixture-cache"


def test_entrypoint_binds_direct_config_imports_to_validated_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.utils import config as config_module

    factory = MagicMock(name="validated_config_factory")
    original_config_class = config_module.Config

    # ``_bind_test_config_factory`` intentionally mutates the module for a
    # short-lived child process.  This unit test shares a pytest process with
    # later in-process CLI tests, so register the original before exercising
    # that mutation.
    monkeypatch.setattr(config_module, "Config", original_config_class)
    offline_entrypoint._bind_test_config_factory(factory)

    assert config_module.Config is not original_config_class
    assert config_module.Config() is factory.return_value
    # The child constructor facade must retain Config's static/class surface;
    # runtime snapshot validation recursively resolves ``Config`` by name.
    assert config_module.Config._runtime_snapshot_has_sensitive_field(
        {"nested": {"api_key": "fixture"}}
    ) is True


def test_direct_python_module_forwards_arguments_without_new_interpreter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_module = MagicMock()
    monkeypatch.setattr(offline_entrypoint.runpy, "run_module", run_module)
    monkeypatch.setattr(sys, "argv", ["pytest-sentinel"])

    offline_entrypoint._run_python(
        ["-m", "src.utils.verify_setup", "--flag", "value"]
    )

    assert sys.argv == ["src.utils.verify_setup", "--flag", "value"]
    run_module.assert_called_once_with(
        "src.utils.verify_setup",
        run_name="__main__",
        alter_sys=True,
    )


def test_direct_module_validation_matches_package_precedence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module_script = tmp_path / "collision.py"
    package = tmp_path / "collision"
    package.mkdir()
    module_script.write_text("raise AssertionError('wrong target')\n", encoding="utf-8")
    package_init = package / "__init__.py"
    package_main = package / "__main__.py"
    package_init.write_text("", encoding="utf-8")
    package_main.write_text("", encoding="utf-8")
    validate_script = MagicMock(side_effect=lambda value: Path(value))
    monkeypatch.setattr(offline_entrypoint, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(
        offline_entrypoint,
        "_try_validated_direct_script",
        validate_script,
    )

    assert offline_entrypoint._validated_direct_module("collision") == "collision"
    assert [Path(item.args[0]) for item in validate_script.call_args_list] == [
        package_init,
        package_main,
    ]


def test_direct_module_does_not_fall_back_when_package_has_no_main(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module_script = tmp_path / "collision.py"
    package = tmp_path / "collision"
    package.mkdir()
    module_script.write_text("", encoding="utf-8")
    (package / "__init__.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(offline_entrypoint, "PROJECT_ROOT", tmp_path)

    with pytest.raises(SystemExit, match="repository target"):
        offline_entrypoint._validated_direct_module("collision")


@pytest.mark.parametrize("as_module", [False, True])
def test_direct_target_rejects_reparse_component_before_resolve(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    as_module: bool,
) -> None:
    project_root = tmp_path / "project"
    scripts = project_root / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "__init__.py").write_text("", encoding="utf-8")
    link = scripts / "linked"
    candidate = link / ("__init__.py" if as_module else "probe.py")
    real_lstat = os.lstat
    real_resolve = Path.resolve
    reparse_stat = SimpleNamespace(
        st_mode=stat.S_IFDIR | 0o755,
        st_file_attributes=0x400,
        st_nlink=1,
    )

    def guarded_lstat(value):
        if Path(value) == link:
            return reparse_stat
        if Path(value) == candidate:
            raise AssertionError("child behind reparse point must not be probed")
        return real_lstat(value)

    def guarded_resolve(path: Path, *args, **kwargs):
        if path == candidate or link in path.parents:
            raise AssertionError("rejected Direct target must not be resolved")
        return real_resolve(path, *args, **kwargs)

    monkeypatch.setattr(offline_entrypoint, "PROJECT_ROOT", project_root)
    monkeypatch.setattr(offline_entrypoint.os, "lstat", guarded_lstat)
    monkeypatch.setattr(Path, "resolve", guarded_resolve)

    with pytest.raises(SystemExit, match="symlink or reparse point"):
        if as_module:
            offline_entrypoint._validated_direct_module("scripts.linked.probe")
        else:
            offline_entrypoint._validated_direct_script(str(candidate))


def test_direct_script_rechecks_prohibited_root_after_resolve(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "project"
    candidate = project_root / "scripts" / "alias.py"
    protected = project_root / ".data" / "private.py"
    candidate.parent.mkdir(parents=True)
    protected.parent.mkdir(parents=True)
    candidate.write_text("", encoding="utf-8")
    protected.write_text("", encoding="utf-8")
    real_resolve = Path.resolve
    real_is_file = Path.is_file

    def redirected_resolve(path: Path, *args, **kwargs):
        if path == candidate:
            return protected
        return real_resolve(path, *args, **kwargs)

    def guarded_is_file(path: Path) -> bool:
        if path == protected:
            raise AssertionError("prohibited resolved target must not be probed")
        return real_is_file(path)

    monkeypatch.setattr(offline_entrypoint, "PROJECT_ROOT", project_root)
    monkeypatch.setattr(Path, "resolve", redirected_resolve)
    monkeypatch.setattr(Path, "is_file", guarded_is_file)

    with pytest.raises(SystemExit, match="prohibited project root"):
        offline_entrypoint._validated_direct_script(str(candidate))


def test_direct_script_rejects_hardlink_to_prohibited_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "project"
    protected = project_root / "config" / "private.py"
    candidate = project_root / "scripts" / "alias.py"
    protected.parent.mkdir(parents=True)
    candidate.parent.mkdir(parents=True)
    protected.write_text("raise AssertionError('must not execute')\n", encoding="utf-8")
    try:
        os.link(protected, candidate)
    except OSError as exc:
        pytest.skip(f"hard links unavailable: {exc}")

    try:
        monkeypatch.setattr(offline_entrypoint, "PROJECT_ROOT", project_root)

        with pytest.raises(SystemExit, match="hard-linked"):
            offline_entrypoint._validated_direct_script(str(candidate))
    finally:
        candidate.unlink(missing_ok=True)


@pytest.mark.parametrize(
    "arguments",
    [
        ["src", "--collect-only"],
        ["@outside-args.txt"],
        ["tests/unit", "--cov-report=xml:outside.xml"],
        ["tests/unit", "-p", "unsafe_plugin"],
    ],
)
def test_pytest_validator_rejects_collection_or_output_escape(
    arguments: list[str],
) -> None:
    with pytest.raises(SystemExit):
        offline_entrypoint._validate_pytest_arguments(arguments)


def test_pytest_validator_accepts_repository_suite_and_terminal_coverage() -> None:
    offline_entrypoint._validate_pytest_arguments(
        [
            "tests/unit/test_offline_runtime.py",
            "-k",
            "offline",
            "-m",
            "not network and not manual",
            "--cov=src.mcp",
            "--cov-report=term-missing",
            "--cov-fail-under=95",
            f"--basetemp={Path(os.environ['TMP_DIR']) / 'pytest'}",
            "-o",
            f"cache_dir={Path(os.environ['TMP_DIR']) / 'pytest-cache'}",
        ]
    )


def test_direct_module_execution_prefers_validated_project_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "project"
    shadow_root = tmp_path / "shadow"
    for root, marker in ((project_root, "validated"), (shadow_root, "shadow")):
        package = root / "collision"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "__main__.py").write_text(f"MARKER = {marker!r}\n", encoding="utf-8")
    run_module = MagicMock()
    monkeypatch.setattr(offline_entrypoint, "PROJECT_ROOT", project_root)
    monkeypatch.setattr(offline_entrypoint.runpy, "run_module", run_module)
    monkeypatch.setattr(sys, "path", [str(shadow_root), str(project_root)])
    monkeypatch.setattr(sys, "argv", ["pytest-sentinel"])

    offline_entrypoint._run_python(["-m", "collision"])

    assert Path(sys.path[0]).resolve() == project_root.resolve()
    run_module.assert_called_once_with(
        "collision",
        run_name="__main__",
        alter_sys=True,
    )


def test_direct_script_execution_prefers_validated_project_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "project"
    shadow_root = tmp_path / "shadow"
    script = project_root / "scripts" / "probe.py"
    script.parent.mkdir(parents=True)
    shadow_root.mkdir()
    script.write_text("", encoding="utf-8")
    run_path = MagicMock()
    monkeypatch.setattr(offline_entrypoint, "PROJECT_ROOT", project_root)
    monkeypatch.setattr(offline_entrypoint.runpy, "run_path", run_path)
    monkeypatch.setattr(sys, "path", [str(shadow_root), str(project_root)])
    monkeypatch.setattr(sys, "argv", ["pytest-sentinel"])

    offline_entrypoint._run_python([str(script), "--flag"])

    assert Path(sys.path[0]).resolve() == project_root.resolve()
    run_path.assert_called_once_with(str(script.resolve()), run_name="__main__")


def test_direct_python_rejects_outside_script_before_resolve(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outside = tmp_path / "outside-direct.py"
    real_resolve = Path.resolve

    def guarded_resolve(path: Path, *args, **kwargs):
        if path == outside:
            raise AssertionError("outside direct target must not be resolved")
        return real_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", guarded_resolve)

    with pytest.raises(SystemExit, match="repository target"):
        offline_entrypoint._validated_direct_script(str(outside))


@pytest.mark.parametrize(
    "arguments",
    [[], ["-m"], ["-c"], ["-I", "-c", "pass"]],
)
def test_direct_python_rejects_incomplete_or_unsupported_invocations(
    arguments: list[str],
) -> None:
    with pytest.raises(SystemExit):
        offline_entrypoint._run_python(arguments)


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


def test_direct_process_guard_and_runtime_attestation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clear_offline_runtime_ready()
    with pytest.raises(RuntimeError, match="offline target"):
        require_offline_runtime_ready()

    install_offline_network_guard(monkeypatch.setattr, block_raw_sockets=False)
    install_offline_process_guard(monkeypatch.setattr)
    mark_offline_runtime_ready(process_guarded=True)
    require_offline_runtime_ready(process_guarded=True)

    with pytest.raises(OfflineProcessError, match="child-process creation"):
        subprocess.Popen.__init__(object())
    with pytest.raises(OfflineProcessError, match="child-process creation"):
        os.system("must-not-run")
    for name in ("fork", "forkpty", "posix_spawn", "posix_spawnp"):
        if hasattr(os, name):
            with pytest.raises(OfflineProcessError, match="child-process creation"):
                getattr(os, name)()

    clear_offline_runtime_ready()
    mark_offline_runtime_ready(process_guarded=False)


def test_process_guard_covers_posix_direct_spawn_apis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    names = ("fork", "forkpty", "posix_spawn", "posix_spawnp")
    for name in names:
        monkeypatch.setattr(os, name, MagicMock(name=name), raising=False)

    install_offline_process_guard(monkeypatch.setattr)

    for name in names:
        with pytest.raises(OfflineProcessError, match="child-process creation"):
            getattr(os, name)()


@pytest.mark.parametrize(
    "command",
    [
        [sys.executable, "-m", "evals.mcp_quality", "--help"],
        [sys.executable, "scripts/setup-test-db.py", "--help"],
        [sys.executable, "scripts/rebuild-dev-vault.py", "--help"],
    ],
)
def test_guarded_direct_targets_reject_naked_processes(
    command: list[str],
) -> None:
    project_root = Path(__file__).resolve().parents[2]
    runtime = {key: os.environ[key] for key in (
        "DATA_DIR",
        "DB_PATH",
        "LOG_DIR",
        "TMP_DIR",
        "VAULT_DIR",
        "VECTOR_DIR",
    )}
    env = prepare_offline_child_env(
        project_root=project_root,
        runtime_overrides=runtime,
    )

    result = subprocess.run(
        command,
        cwd=project_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert result.returncode != 0
    assert "offline target must run through scripts/run-test.ps1" in (
        result.stdout + result.stderr
    )


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
