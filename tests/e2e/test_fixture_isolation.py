"""Regression tests for E2E fixture process isolation."""

from __future__ import annotations

import os

import pytest

from src.utils import config as config_module
from tests.e2e.fixture_utils import (
    TEST_RUNTIME_ENV_KEYS,
    build_test_env,
    temporary_test_config,
)


def test_build_test_env_does_not_mutate_parent_process(tmp_path_factory):
    """The builder must configure only the environment passed to MCP children."""
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


def test_temporary_test_config_restores_parent_process_after_failure(test_env):
    """In-process live configuration must restore state through ``finally``."""
    previous_env = {
        key: os.environ.get(key)
        for key in TEST_RUNTIME_ENV_KEYS
    }
    previous_config = config_module._config_instance

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
    assert config_module._config_instance is previous_config
