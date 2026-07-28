"""Process-isolated helpers shared by E2E fixtures and their tests."""

from __future__ import annotations

import os
import shutil
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterator, Protocol

from src.storage.sqlite_store import SQLiteStore
from src.utils import config as config_module
from src.utils.config import Config


TEST_RUNTIME_ENV_KEYS = (
    "DB_PATH",
    "DATA_DIR",
    "VECTOR_DIR",
    "VAULT_DIR",
    "LOG_DIR",
    "TMP_DIR",
    "LOG_LEVEL",
    "PYTHONDONTWRITEBYTECODE",
)


class TempPathFactory(Protocol):
    """Minimal protocol needed from pytest's temporary-path factory."""

    def mktemp(self, basename: str, numbered: bool = True) -> Path:
        """Create and return a temporary directory."""


@dataclass(frozen=True)
class TestEnv:
    data_dir: Path
    db_path: Path
    vault_dir: Path
    vector_dir: Path
    log_dir: Path
    tmp_dir: Path
    env: Dict[str, str]


def _clean_path(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
    elif path.exists():
        path.unlink(missing_ok=True)


def build_test_env(
    tmp_path_factory: TempPathFactory,
    *,
    prefix: str = "pkv-e2e",
) -> TestEnv:
    """Build isolated paths and a child-process environment without globals."""
    data_dir = tmp_path_factory.mktemp(prefix)
    db_path = data_dir / "db" / "knowledge_vault_e2e.db"
    vault_dir = data_dir / "vault-e2e"
    vector_dir = data_dir / "vectors-e2e"
    log_dir = data_dir / "logs-e2e"
    tmp_dir = data_dir / "tmp-e2e"

    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "db").mkdir(parents=True, exist_ok=True)

    _clean_path(db_path)
    _clean_path(vault_dir)
    _clean_path(vector_dir)

    vault_dir.mkdir(parents=True, exist_ok=True)
    vector_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)
    tmp_dir.mkdir(parents=True, exist_ok=True)

    store = SQLiteStore(db_path)
    store.initialize()

    env = os.environ.copy()
    env.update(
        {
            "DB_PATH": str(db_path),
            "DATA_DIR": str(data_dir),
            "VECTOR_DIR": str(vector_dir),
            "VAULT_DIR": str(vault_dir),
            "LOG_DIR": str(log_dir),
            "TMP_DIR": str(tmp_dir),
            "LOG_LEVEL": "WARNING",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )

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
def temporary_test_config(test_env: TestEnv) -> Iterator[Config]:
    """Temporarily expose isolated paths to in-process config consumers."""
    previous_config = config_module._config_instance
    previous_env = {
        key: os.environ.get(key)
        for key in TEST_RUNTIME_ENV_KEYS
    }
    try:
        os.environ.update(
            {
                key: test_env.env[key]
                for key in TEST_RUNTIME_ENV_KEYS
            }
        )
        config_module._config_instance = None
        yield config_module.get_config()
    finally:
        config_module._config_instance = previous_config
        for key, value in previous_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
