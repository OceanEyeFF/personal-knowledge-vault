"""
MigrationManager 版本链自检测试。
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.storage.migration_manager import MigrationManager  # noqa: E402


MIGRATIONS_DIR = PROJECT_ROOT / "scripts" / "migrations"


def _apply_sql(db_path: Path, migration_name: str) -> None:
    sql = (MIGRATIONS_DIR / migration_name).read_text(encoding="utf-8")
    conn = sqlite3.connect(str(db_path))
    conn.executescript(sql)
    conn.close()


def test_migration_versions_are_monotonic_for_active_chain(tmp_path: Path) -> None:
    manager = MigrationManager(tmp_path / "dummy.db", MIGRATIONS_DIR)

    migration_names = [
        "001_initial_schema.sql",
        "002_add_cli_tables.sql",
        "004_add_chat_sessions.sql",
        "005_add_review_system.sql",
        "006_add_relations_foundation.sql",
    ]
    parsed_versions = [
        manager._parse_version_from_file(MIGRATIONS_DIR / name)
        for name in migration_names
    ]

    assert parsed_versions == ["1.0.0", "1.1.0", "1.1.1", "1.1.2", "1.2.0"]


def test_get_pending_migrations_keeps_004_005_006_visible(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"

    _apply_sql(db_path, "001_initial_schema.sql")
    _apply_sql(db_path, "002_add_cli_tables.sql")

    manager = MigrationManager(db_path, MIGRATIONS_DIR)
    pending = manager.get_pending_migrations()

    assert [version for version, _ in pending] == ["1.1.1", "1.1.2", "1.2.0"]
    assert [path.name for _, path in pending] == [
        "004_add_chat_sessions.sql",
        "005_add_review_system.sql",
        "006_add_relations_foundation.sql",
    ]


def test_review_migration_has_standard_headers(tmp_path: Path) -> None:
    manager = MigrationManager(tmp_path / "dummy.db", MIGRATIONS_DIR)
    migration_path = MIGRATIONS_DIR / "005_add_review_system.sql"

    assert manager._parse_version_from_file(migration_path) == "1.1.2"
    assert manager._get_migration_description(migration_path) == (
        "新增审核系统表 review_queue / review_history"
    )
