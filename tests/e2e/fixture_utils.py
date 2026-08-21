"""Process-isolated helpers shared by E2E fixtures and their tests."""

from __future__ import annotations

import os
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterator, Protocol

from src.storage.sqlite_store import SQLiteStore
from tests.offline_runtime import (
    LOAD_LOCAL_SENTINEL,
    OFFLINE_SENTINEL,
    PROJECT_ROOT_SENTINEL,
    RUNTIME_PATH_ENV_KEYS,
    assert_config_runtime_paths,
    prepare_live_child_env,
    prepare_offline_child_env,
    validate_test_runtime_paths,
)
from src.utils import config as config_module
from src.utils.config import Config

PROJECT_ROOT = Path(__file__).resolve().parents[2]



TEST_RUNTIME_ENV_KEYS = (
    "DB_PATH",
    "DATA_DIR",
    "VECTOR_DIR",
    "VAULT_DIR",
    "LOG_DIR",
    "TMP_DIR",
    "LOG_LEVEL",
    "PYTHONDONTWRITEBYTECODE",
    "PKV_DATA_ROOT",
    "PKV_RUN_LIVE",
    OFFLINE_SENTINEL,
    LOAD_LOCAL_SENTINEL,
    PROJECT_ROOT_SENTINEL,
)


class TempPathFactory(Protocol):
    """Minimal protocol needed from pytest's temporary-path factory."""

    def getbasetemp(self) -> Path:
        """Return pytest's already-established base temp directory."""

    def mktemp(self, basename: str, numbered: bool = True) -> Path:
        """Create and return a temporary directory below ``getbasetemp``."""


@dataclass(frozen=True)
class TestEnv:
    data_dir: Path
    db_path: Path
    vault_dir: Path
    vector_dir: Path
    log_dir: Path
    tmp_dir: Path
    env: Dict[str, str]


def _live_user_config_path() -> Path:
    """Return the user-owned config source allowed for an opted-in live fixture."""

    user_home = os.environ.get("USERPROFILE") or os.environ.get("HOME")
    profile_root = Path(user_home) if user_home else Path.home()
    return profile_root / ".pkv" / "config.yaml"


def build_test_env(
    tmp_path_factory: TempPathFactory,
    *,
    prefix: str = "pkv-e2e",
    live: bool = False,
) -> TestEnv:
    """Build isolated paths and a child-process environment without globals."""
    validate_test_runtime_paths(
        project_root=PROJECT_ROOT,
        runtime_overrides={"DATA_DIR": tmp_path_factory.getbasetemp()},
    )
    data_dir = tmp_path_factory.mktemp(prefix)
    db_path = data_dir / "db" / "knowledge_vault_e2e.db"
    vault_dir = data_dir / "vault-e2e"
    vector_dir = data_dir / "vectors-e2e"
    log_dir = data_dir / "logs-e2e"
    tmp_dir = data_dir / "tmp-e2e"

    runtime_overrides = {
        "DB_PATH": db_path,
        "DATA_DIR": data_dir,
        "VECTOR_DIR": vector_dir,
        "VAULT_DIR": vault_dir,
        "LOG_DIR": log_dir,
        "TMP_DIR": tmp_dir,
        "LOG_LEVEL": "WARNING",
    }
    prepare_env = prepare_live_child_env if live else prepare_offline_child_env
    env = prepare_env(
        project_root=PROJECT_ROOT,
        runtime_overrides=runtime_overrides,
    )

    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "db").mkdir(parents=True, exist_ok=True)
    vault_dir.mkdir(parents=True, exist_ok=True)
    vector_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)
    tmp_dir.mkdir(parents=True, exist_ok=True)

    store = SQLiteStore(db_path)
    store.initialize()

    return TestEnv(
        data_dir=data_dir,
        db_path=db_path,
        vault_dir=vault_dir,
        vector_dir=vector_dir,
        log_dir=log_dir,
        tmp_dir=tmp_dir,
        env=env,
    )


@contextmanager
def temporary_test_config(
    test_env: TestEnv,
    *,
    load_local: bool = False,
) -> Iterator[Config]:
    """Expose isolated paths with an explicit base-only or live user config."""
    runtime_overrides = {
        key: test_env.env[key]
        for key in RUNTIME_PATH_ENV_KEYS
        if key in test_env.env
    }
    canonical = validate_test_runtime_paths(
        project_root=PROJECT_ROOT,
        runtime_overrides=runtime_overrides,
    )
    expected_paths = validate_test_runtime_paths(
        project_root=PROJECT_ROOT,
        runtime_overrides={
            "DATA_DIR": test_env.data_dir,
            "DB_PATH": test_env.db_path,
            "VAULT_DIR": test_env.vault_dir,
            "VECTOR_DIR": test_env.vector_dir,
            "LOG_DIR": test_env.log_dir,
            "TMP_DIR": test_env.tmp_dir,
        },
    )
    if canonical != expected_paths:
        raise RuntimeError("test environment paths do not match isolated fixture")

    safe_env = dict(test_env.env)
    safe_env.update(canonical)
    # The formal override is a fixture-owned mirror of the already validated
    # legacy root, never a caller-provided runtime path.  Scope it with the
    # other process state so an ambient wrapper/user root cannot leak in.
    safe_env["PKV_DATA_ROOT"] = canonical["DATA_DIR"]
    expected_mode = {
        "PKV_RUN_LIVE": "1" if load_local else "0",
        OFFLINE_SENTINEL: "0" if load_local else "1",
        LOAD_LOCAL_SENTINEL: "1" if load_local else "0",
        PROJECT_ROOT_SENTINEL: str(PROJECT_ROOT.resolve()),
    }
    actual_mode = {
        key: test_env.env.get(key)
        for key in expected_mode
    }
    if actual_mode != expected_mode:
        mode_name = "live" if load_local else "offline"
        raise RuntimeError(f"inconsistent {mode_name} test environment sentinels")

    previous_config = config_module._config_instance
    previous_env = {
        key: os.environ.get(key)
        for key in TEST_RUNTIME_ENV_KEYS
    }
    try:
        os.environ.update(
            {
                key: safe_env[key]
                for key in TEST_RUNTIME_ENV_KEYS
            }
        )
        base_config_path = PROJECT_ROOT / "config" / "config.yaml"
        user_config_path = str(_live_user_config_path()) if load_local else None
        config = Config(
            str(base_config_path),
            user_config_path=user_config_path,
        )
        assert_config_runtime_paths(config, expected_paths)
        config.ensure_dirs()
        config_module._config_instance = config
        yield config
    finally:
        config_module._config_instance = previous_config
        for key, value in previous_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
