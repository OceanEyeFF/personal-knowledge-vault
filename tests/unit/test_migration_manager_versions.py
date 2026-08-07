"""
MigrationManager 版本链自检测试。
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.runtime.errors import ErrorCode, PKVRuntimeError  # noqa: E402
from src.storage.migration_manager import DatabaseState, MigrationManager  # noqa: E402
from src.storage.sqlite_store import SQLiteStore  # noqa: E402


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
        "009_repair_fts_storage_contract.sql",
        "010_add_storage_operation_commits.sql",
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
        "1.2.3",
        "1.2.4",
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
        "1.2.3",
        "1.2.4",
    ]
    assert [path.name for _, path in pending] == [
        "004_add_chat_sessions.sql",
        "005_add_review_system.sql",
        "006_add_relations_foundation.sql",
        "007_add_timeline_time_fields.sql",
        "008_align_fts_contract.sql",
        "009_repair_fts_storage_contract.sql",
        "010_add_storage_operation_commits.sql",
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

    assert [version for version, _ in pending] == [
        "1.2.1",
        "1.2.2",
        "1.2.3",
        "1.2.4",
    ]


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


def test_run_health_check_reports_script_and_database_drift(tmp_path: Path) -> None:
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    (migrations_dir / "001_base.sql").write_text(
        """
        -- Migration: 001_base.sql
        -- Version: 1.0.0
        -- Description: base
        SELECT 1;
        """,
        encoding="utf-8",
    )
    (migrations_dir / "002_duplicate.sql").write_text(
        """
        -- Migration: 002_duplicate.sql
        -- Version: 1.0.0
        -- Description: duplicate
        SELECT 1;
        """,
        encoding="utf-8",
    )
    (migrations_dir / "003_non_monotonic.sql").write_text(
        """
        -- Migration: 003_non_monotonic.sql
        -- Version: 0.9.0
        -- Description: older
        SELECT 1;
        """,
        encoding="utf-8",
    )
    (migrations_dir / "004_missing_header.sql").write_text(
        """
        -- Migration: 004_missing_header.sql
        -- Version: 1.1.0
        SELECT 1;
        """,
        encoding="utf-8",
    )
    (migrations_dir / "005_invalid_version.sql").write_text(
        """
        -- Migration: 005_invalid_version.sql
        -- Version: broken
        -- Description: invalid version
        SELECT 1;
        """,
        encoding="utf-8",
    )

    db_path = tmp_path / "health.db"
    with sqlite3.connect(str(db_path)) as conn:
        conn.executescript(
            """
            CREATE TABLE schema_version (
                version_id INTEGER PRIMARY KEY AUTOINCREMENT,
                version TEXT NOT NULL UNIQUE,
                description TEXT
            );
            INSERT INTO schema_version (version, description)
            VALUES ('1.1.2', 'review migration'),
                   ('9.9.9', 'unknown version');

            CREATE TABLE chat_sessions (
                session_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                messages TEXT NOT NULL DEFAULT '[]'
            );
            """
        )

    manager = MigrationManager(db_path, migrations_dir)
    health = manager.run_health_check()

    assert health["healthy"] is False
    assert any("重复版本号 1.0.0" in issue for issue in health["issues"])
    assert any("未严格高于前一个脚本版本 1.0.0" in issue for issue in health["issues"])
    assert any("缺少标准头字段: description" in issue for issue in health["issues"])
    assert any("版本号格式无效: broken" in issue for issue in health["issues"])
    assert any("数据库当前版本 9.9.9 不在当前迁移链定义中" in issue for issue in health["issues"])
    assert any("已存在 chat_sessions" in issue for issue in health["issues"])
    assert any("已记录 1.1.2" in issue for issue in health["issues"])
    assert health["database"]["schema_version_exists"] is True
    assert health["database"]["current_version"] == "9.9.9"
    assert health["database"]["pending_migrations"] == []


def test_apply_all_pending_and_upgrade_prompt_cover_no_pending_paths(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "latest.db"
    manager = MigrationManager(db_path, MIGRATIONS_DIR)
    assert manager.initialize_fresh().state is DatabaseState.READY

    assert manager.apply_all_pending(auto_backup=False) == 0
    assert manager.check_and_prompt_upgrade() is False


def test_apply_all_pending_rebuilds_fts_after_alignment_migrations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = MigrationManager(tmp_path / "pending.db", MIGRATIONS_DIR)
    rebuild_calls: list[Path] = []
    original_rebuild = SQLiteStore.rebuild_fts5_index

    def spy_rebuild(store: SQLiteStore) -> None:
        rebuild_calls.append(store.db_path)
        original_rebuild(store)

    monkeypatch.setattr(SQLiteStore, "rebuild_fts5_index", spy_rebuild)

    migrated = manager.apply_all_pending(auto_backup=False)

    assert migrated == 9
    assert len(rebuild_calls) == 1
    assert rebuild_calls[0] != manager.db_path
    assert manager.inspect_database().state is DatabaseState.READY


def test_apply_migration_rejects_when_auto_backup_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "migrate.db"
    manager = MigrationManager(db_path, MIGRATIONS_DIR)
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute("CREATE TABLE existing_data (id INTEGER PRIMARY KEY)")

    def _fail_backup(_: str) -> None:
        raise RuntimeError("backup unavailable")

    monkeypatch.setattr(manager, "_backup_database", _fail_backup)

    with pytest.raises(PKVRuntimeError) as error:
        manager.apply_migration(
            MIGRATIONS_DIR / "001_initial_schema.sql", auto_backup=True
        )

    assert error.value.code is ErrorCode.MIGRATION_BACKUP_FAILED

    with sqlite3.connect(str(db_path)) as conn:
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
        ).fetchone() is None


def test_check_and_prompt_upgrade_outputs_pending_migrations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    manager = MigrationManager(tmp_path / "prompt.db", MIGRATIONS_DIR)
    monkeypatch.setattr(
        manager,
        "get_pending_migrations",
        lambda: [("1.0.0", MIGRATIONS_DIR / "001_initial_schema.sql")],
    )

    assert manager.check_and_prompt_upgrade() is True

    captured = capsys.readouterr().out
    assert "001_initial_schema.sql" in captured
    assert "版本: v1.0.0" in captured
    assert "执行升级: python scripts/migrate.py" in captured


def test_backup_database_and_version_helpers_cover_fallback_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "helpers.db"
    manager = MigrationManager(
        db_path,
        MIGRATIONS_DIR,
        backup_dir=tmp_path / "backups",
    )
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute("CREATE TABLE durable_data (id INTEGER PRIMARY KEY)")

    backup_path = manager._backup_database("001_initial_schema.sql")
    assert backup_path.parent == manager.backup_dir
    with sqlite3.connect(str(backup_path)) as conn:
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='durable_data'"
        ).fetchone() is not None

    monkeypatch.setattr(
        manager,
        "_copy_database",
        lambda *_args: (_ for _ in ()).throw(OSError("backup failed")),
    )
    with pytest.raises(OSError, match="backup failed"):
        manager._backup_database("001_initial_schema.sql")

    fallback_file = tmp_path / "010_manual_fix.sql"
    fallback_file.write_text("-- no standard header\nSELECT 1;\n", encoding="utf-8")
    unknown_file = tmp_path / "notes.sql"
    unknown_file.write_text("-- Description: only desc\nSELECT 1;\n", encoding="utf-8")

    assert manager._parse_version_from_file(fallback_file) == "0.0.10"
    assert manager._parse_version_from_file(unknown_file) == "0.0.0"
    assert manager._get_migration_description(unknown_file) == "only desc"
    assert manager._version_compare("1.0.1", "1.0.0") == 1
    assert manager._version_compare("1.0.0", "1.0.1") == -1
    assert manager._version_compare("1.0.0", "1.0.0") == 0
    assert manager._version_to_tuple("bad.version") == (0, 0, 0)
