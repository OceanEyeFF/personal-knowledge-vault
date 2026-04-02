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
        "007_add_timeline_time_fields.sql",
        "008_align_fts_contract.sql",
    ]
    parsed_versions = [
        manager._parse_version_from_file(MIGRATIONS_DIR / name)
        for name in migration_names
    ]

    assert parsed_versions == [
        "1.0.0",
        "1.1.0",
        "1.1.1",
        "1.1.2",
        "1.2.0",
        "1.2.1",
        "1.2.2",
    ]


def test_get_pending_migrations_keeps_007_when_version_missing_even_if_columns_exist(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "test.db"

    _apply_sql(db_path, "001_initial_schema.sql")
    _apply_sql(db_path, "002_add_cli_tables.sql")

    manager = MigrationManager(db_path, MIGRATIONS_DIR)
    pending = manager.get_pending_migrations()

    assert [version for version, _ in pending] == [
        "1.1.1",
        "1.1.2",
        "1.2.0",
        "1.2.1",
        "1.2.2",
    ]
    assert [path.name for _, path in pending] == [
        "004_add_chat_sessions.sql",
        "005_add_review_system.sql",
        "006_add_relations_foundation.sql",
        "007_add_timeline_time_fields.sql",
        "008_align_fts_contract.sql",
    ]


def test_review_migration_has_standard_headers(tmp_path: Path) -> None:
    manager = MigrationManager(tmp_path / "dummy.db", MIGRATIONS_DIR)
    migration_path = MIGRATIONS_DIR / "005_add_review_system.sql"

    assert manager._parse_version_from_file(migration_path) == "1.1.2"
    assert manager._get_migration_description(migration_path) == (
        "新增审核系统表 review_queue / review_history"
    )


def test_timeline_time_migration_marks_version_when_columns_already_exist(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "test.db"

    _apply_sql(db_path, "001_initial_schema.sql")
    manager = MigrationManager(db_path, MIGRATIONS_DIR)
    manager.apply_migration(
        MIGRATIONS_DIR / "007_add_timeline_time_fields.sql",
        auto_backup=False,
    )

    with sqlite3.connect(str(db_path)) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(knowledge_items)")}
        version = conn.execute(
            "SELECT version FROM schema_version WHERE version = '1.2.1'"
        ).fetchone()

    assert {"event_time", "published_at"}.issubset(columns)
    assert version is not None


def test_get_pending_migrations_keeps_007_for_legacy_schema(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy.db"

    with sqlite3.connect(str(db_path)) as conn:
        conn.executescript(
            """
            CREATE TABLE schema_version (
                version_id INTEGER PRIMARY KEY AUTOINCREMENT,
                version TEXT NOT NULL UNIQUE,
                description TEXT
            );
            INSERT INTO schema_version (version, description)
            VALUES ('1.2.0', 'legacy schema');
            CREATE TABLE knowledge_items (
                knowledge_id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                archived_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )

    manager = MigrationManager(db_path, MIGRATIONS_DIR)
    pending = manager.get_pending_migrations()

    assert [version for version, _ in pending] == ["1.2.1", "1.2.2"]


def test_apply_migration_007_creates_missing_indexes_when_columns_already_exist(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "legacy_with_columns.db"

    with sqlite3.connect(str(db_path)) as conn:
        conn.executescript(
            """
            CREATE TABLE schema_version (
                version_id INTEGER PRIMARY KEY AUTOINCREMENT,
                version TEXT NOT NULL UNIQUE,
                description TEXT
            );
            INSERT INTO schema_version (version, description)
            VALUES ('1.2.0', 'legacy schema');
            CREATE TABLE knowledge_items (
                knowledge_id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                event_time TIMESTAMP,
                published_at TIMESTAMP,
                archived_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )

    manager = MigrationManager(db_path, MIGRATIONS_DIR)
    manager.apply_migration(
        MIGRATIONS_DIR / "007_add_timeline_time_fields.sql",
        auto_backup=False,
    )

    with sqlite3.connect(str(db_path)) as conn:
        indexes = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            )
        }
        version = conn.execute(
            "SELECT version FROM schema_version WHERE version = '1.2.1'"
        ).fetchone()

    assert "idx_knowledge_event_time" in indexes
    assert "idx_knowledge_published_at" in indexes
    assert version is not None
