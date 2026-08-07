"""SQLiteStore.initialize explicit-db-path compatibility regression tests.

``SQLiteStore.initialize`` is a documented low-level compatibility wrapper:
tests/maintenance callers pass an explicit ``db_path`` (often a tmp_path) and
must never be validated against an ambient user-data root.  Production
startup containment stays with ``bootstrap_runtime``; the optional ``layout``
argument is the explicit authority seam.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.runtime import ErrorCode, PKVRuntimeError, RuntimeLayout  # noqa: E402
from src.storage.migration_manager import DatabaseState, MigrationManager  # noqa: E402
from src.storage.sqlite_store import SQLiteStore  # noqa: E402

MIGRATIONS_DIR = PROJECT_ROOT / "scripts" / "migrations"


def test_initialize_builds_explicit_tmp_path_db_without_ambient_layout(
    tmp_path: Path,
) -> None:
    """Regression: explicit tmp_path db must not hit ambient data-root checks."""
    db_path = tmp_path / "data" / "db" / "vault.db"
    store = SQLiteStore(db_path)

    store.initialize()

    assert db_path.is_file()
    assert store.table_exists("knowledge_items")
    assert store.table_exists("chat_sessions")
    assert store.count_entries() == 0
    # 兼容模式不写任何环境用户数据根下的路径 (备份目录不得被隐式创建)。
    assert not (tmp_path / "data" / "backups").exists()


def test_initialize_is_idempotent_for_ready_database(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "test.db")

    store.initialize()
    store.initialize()

    assert store.table_exists("knowledge_items")
    assert store.count_entries() == 0


def test_initialize_keeps_fail_closed_rejection_for_upgrade_required_db(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "old.db"
    manager = MigrationManager(db_path, MIGRATIONS_DIR)
    manager.apply_migration(MIGRATIONS_DIR / "001_initial_schema.sql", auto_backup=False)

    with pytest.raises(PKVRuntimeError) as exc_info:
        SQLiteStore(db_path).initialize()

    assert exc_info.value.code is ErrorCode.DATABASE_UPGRADE_REQUIRED


def test_initialize_accepts_explicit_layout_authority(tmp_path: Path) -> None:
    # 显式环境/storage 权威: 排除 wrapper 注入的 DB_PATH/DATA_DIR 等运行时
    # 环境变量, 使 user_data_root 成为唯一路径权威, 真实测试显式 layout seam。
    layout = RuntimeLayout.resolve(
        user_data_root=tmp_path / "data",
        environment={},
        storage_config={},
    )
    store = SQLiteStore(layout.db_path)

    store.initialize(layout=layout)

    assert layout.db_path.is_file()
    assert store.table_exists("knowledge_items")
    assert store.count_entries() == 0


def test_initialize_with_explicit_layout_rejects_out_of_root_db(
    tmp_path: Path,
) -> None:
    # 同样使用干净环境/storage 权威: 否则 wrapper 注入的 DB_PATH 会覆盖
    # 显式 user_data_root, 无法测试 out-of-root 拒绝 seam。
    layout = RuntimeLayout.resolve(
        user_data_root=tmp_path / "data",
        environment={},
        storage_config={},
    )
    outside = tmp_path / "outside" / "vault.db"
    store = SQLiteStore(outside)

    with pytest.raises(PKVRuntimeError) as exc_info:
        store.initialize(layout=layout)

    assert exc_info.value.code is ErrorCode.DATA_ROOT_UNSAFE
    assert not outside.exists()


def test_initialize_created_db_reaches_migration_chain_latest(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    SQLiteStore(db_path).initialize()

    inspection = MigrationManager(db_path, MIGRATIONS_DIR).inspect_database()
    latest = MigrationManager(tmp_path / "unused.db", MIGRATIONS_DIR).latest_version()

    assert inspection.state is DatabaseState.READY
    assert inspection.current_version == latest
