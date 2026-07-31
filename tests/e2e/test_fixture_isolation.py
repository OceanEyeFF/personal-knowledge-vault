"""Regression tests for E2E fixture process isolation."""

from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.utils import config as config_module
from tests.e2e import fixture_utils
from tests.e2e.fixture_utils import (
    TEST_RUNTIME_ENV_KEYS,
    build_test_env,
    temporary_test_config,
)
from tests.offline_runtime import LOAD_LOCAL_SENTINEL, OFFLINE_SENTINEL


def test_build_test_env_does_not_mutate_parent_process(
    tmp_path_factory,
    monkeypatch,
):
    """The builder must configure only the environment passed to MCP children."""
    monkeypatch.setenv("PKV_RUN_LIVE", "1")
    monkeypatch.setenv("PKV_LLM_API_KEY", "parent-secret-sentinel")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid")
    previous_env = {
        key: os.environ.get(key)
        for key in TEST_RUNTIME_ENV_KEYS
    }
    previous_config = config_module._config_instance

    test_env = build_test_env(
        tmp_path_factory,
        prefix="pkv-e2e-isolation",
    )

    assert {
        key: os.environ.get(key)
        for key in TEST_RUNTIME_ENV_KEYS
    } == previous_env
    assert config_module._config_instance is previous_config

    assert test_env.env["DB_PATH"] == str(test_env.db_path)
    assert test_env.env["DATA_DIR"] == str(test_env.data_dir)
    assert test_env.env["VECTOR_DIR"] == str(test_env.vector_dir)
    assert test_env.env["VAULT_DIR"] == str(test_env.vault_dir)
    assert test_env.env["LOG_DIR"] == str(test_env.log_dir)
    assert test_env.env["TMP_DIR"] == str(test_env.tmp_dir)
    assert test_env.env["PKV_RUN_LIVE"] == "0"
    assert test_env.env[OFFLINE_SENTINEL] == "1"
    assert test_env.env[LOAD_LOCAL_SENTINEL] == "0"
    assert "PKV_LLM_API_KEY" not in test_env.env
    assert "HTTPS_PROXY" not in test_env.env
    assert os.environ["PKV_LLM_API_KEY"] == "parent-secret-sentinel"
    assert os.environ["HTTPS_PROXY"] == "http://proxy.invalid"


def test_build_test_env_validates_before_writing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_project_root = tmp_path / "fake-project"
    unsafe_data_dir = fake_project_root / ".data"
    store_factory = MagicMock(
        side_effect=AssertionError("SQLiteStore must not be constructed")
    )

    class UnsafeTempPathFactory:
        def getbasetemp(self) -> Path:
            return unsafe_data_dir

        def mktemp(self, _basename: str, numbered: bool = True) -> Path:
            del numbered
            raise AssertionError("mktemp must not run before base validation")

    monkeypatch.setattr(fixture_utils, "PROJECT_ROOT", fake_project_root)
    monkeypatch.setattr(fixture_utils, "SQLiteStore", store_factory)

    with pytest.raises(ValueError, match="unsafe automated-test DATA_DIR"):
        build_test_env(UnsafeTempPathFactory())

    store_factory.assert_not_called()
    assert not unsafe_data_dir.exists()


def test_temporary_test_config_revalidates_mutable_environment(
    test_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_factory = MagicMock(side_effect=AssertionError("must not load config"))
    tampered_env = dict(test_env.env)
    tampered_env["DB_PATH"] = str(
        fixture_utils.PROJECT_ROOT / ".forbidden-test-path" / "test.db"
    )
    tampered = replace(test_env, env=tampered_env)
    monkeypatch.setattr(fixture_utils, "Config", config_factory)

    with pytest.raises(ValueError, match="DB_PATH must remain inside"):
        with temporary_test_config(tampered):
            pytest.fail("context must reject before loading config")

    config_factory.assert_not_called()


def test_temporary_test_config_rejects_tampered_dataclass_path_without_probe(
    test_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    forbidden = fixture_utils.PROJECT_ROOT / ".forbidden-test-field"
    tampered = replace(test_env, log_dir=forbidden)
    config_factory = MagicMock(side_effect=AssertionError("must not load config"))
    original_resolve = Path.resolve

    def guarded_resolve(path: Path, strict: bool = False) -> Path:
        if os.path.normcase(os.fspath(path)) == os.path.normcase(
            os.fspath(forbidden)
        ):
            raise AssertionError("rejected fixture path must not be resolved")
        return original_resolve(path, strict=strict)

    monkeypatch.setattr(Path, "resolve", guarded_resolve)
    monkeypatch.setattr(fixture_utils, "Config", config_factory)

    with pytest.raises(ValueError, match="LOG_DIR must remain inside"):
        with temporary_test_config(tampered):
            pytest.fail("context must reject before loading config")

    config_factory.assert_not_called()


def test_temporary_test_config_rejects_effective_config_drift_before_writes(
    test_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    previous_env = {
        key: os.environ.get(key)
        for key in TEST_RUNTIME_ENV_KEYS
    }
    previous_config = config_module._config_instance
    config = MagicMock()
    config.data_dir = test_env.data_dir
    config.db_path = test_env.db_path
    config.vault_dir = test_env.vault_dir
    config.vector_index_dir = test_env.vector_dir
    config.log_dir = fixture_utils.PROJECT_ROOT / ".forbidden-config-log"
    config.tmp_dir = test_env.tmp_dir
    config_factory = MagicMock(return_value=config)
    monkeypatch.setattr(fixture_utils, "Config", config_factory)

    with pytest.raises(RuntimeError, match="escaped validated runtime"):
        with temporary_test_config(test_env):
            pytest.fail("context must reject before creating directories")

    config_factory.assert_called_once()
    config.ensure_dirs.assert_not_called()
    assert config_module._config_instance is previous_config
    assert {
        key: os.environ.get(key)
        for key in TEST_RUNTIME_ENV_KEYS
    } == previous_env


def test_temporary_test_config_restores_parent_process_after_failure(
    test_env,
    monkeypatch,
):
    """Base-only in-process config must restore state through ``finally``."""
    previous_env = {
        key: os.environ.get(key)
        for key in TEST_RUNTIME_ENV_KEYS
    }
    previous_config = config_module._config_instance
    config_calls: list[tuple[str, str | None]] = []
    real_config = fixture_utils.Config

    def tracked_config(base_path: str, local_path: str | None = None):
        config_calls.append((base_path, local_path))
        return real_config(base_path, local_path)

    monkeypatch.setattr(fixture_utils, "Config", tracked_config)

    with pytest.raises(RuntimeError, match="fixture restoration sentinel"):
        with temporary_test_config(test_env) as config:
            assert config_module._config_instance is config
            assert {
                key: os.environ.get(key)
                for key in TEST_RUNTIME_ENV_KEYS
            } == {
                key: test_env.env[key]
                for key in TEST_RUNTIME_ENV_KEYS
            }
            assert config.db_path.resolve() == test_env.db_path.resolve()
            assert config.vector_index_dir.resolve() == test_env.vector_dir.resolve()
            raise RuntimeError("fixture restoration sentinel")

    assert {
        key: os.environ.get(key)
        for key in TEST_RUNTIME_ENV_KEYS
    } == previous_env
    assert config_calls
    assert all(local_path is None for _, local_path in config_calls)
    assert config_module._config_instance is previous_config


def test_temporary_test_config_rejects_live_load_for_offline_fixture(
    test_env,
    monkeypatch,
) -> None:
    config_factory = MagicMock(side_effect=AssertionError("must not load config"))
    monkeypatch.setattr(fixture_utils, "Config", config_factory)

    with pytest.raises(RuntimeError, match="inconsistent live test environment"):
        with temporary_test_config(test_env, load_local=True):
            pytest.fail("context must reject before loading config")
    config_factory.assert_not_called()
