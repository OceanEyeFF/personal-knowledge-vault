"""
MigrationManager 运行时与异常路径测试。
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.runtime.errors import ErrorCode, PKVRuntimeError  # noqa: E402
from src.storage.migration_manager import (  # noqa: E402
    DatabaseState,
    MigrationManager,
)


SOURCE_MIGRATIONS_DIR = PROJECT_ROOT / "scripts" / "migrations"


def _write_migration(
    path: Path,
    *,
    version: str | None,
    description: str | None = "test migration",
    include_migration: bool = True,
) -> None:
    lines: list[str] = []
    if include_migration:
        lines.append(f"-- Migration: {path.name}")
    if version is not None:
        lines.append(f"-- Version: {version}")
    if description is not None:
        lines.append(f"-- Description: {description}")
    lines.append("SELECT 1;")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _apply_schema_version_only(db_path: Path, version: str) -> None:
    with sqlite3.connect(str(db_path)) as conn:
        conn.executescript(
            """
            CREATE TABLE schema_version (
                version_id INTEGER PRIMARY KEY AUTOINCREMENT,
                version TEXT NOT NULL UNIQUE,
                description TEXT
            );
            """
        )
        conn.execute(
            "INSERT INTO schema_version (version, description) VALUES (?, ?)",
            (version, "current"),
        )


def test_init_does_not_create_missing_bundled_migrations_dir(tmp_path: Path) -> None:
    migrations_dir = tmp_path / "missing" / "migrations"

    manager = MigrationManager(tmp_path / "test.db", migrations_dir)

    assert not migrations_dir.exists()
    with pytest.raises(PKVRuntimeError) as error:
        manager.latest_version()
    assert error.value.code is ErrorCode.RESOURCE_MISSING


def test_get_current_version_returns_zero_only_when_database_is_absent(tmp_path: Path) -> None:
    manager = MigrationManager(tmp_path / "test.db", SOURCE_MIGRATIONS_DIR)

    assert manager.get_current_version() == "0.0.0"


def test_get_current_version_rejects_existing_database_without_version_table(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "test.db"
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute("CREATE TABLE unrelated (id INTEGER PRIMARY KEY)")

    manager = MigrationManager(db_path, SOURCE_MIGRATIONS_DIR)
    with pytest.raises(PKVRuntimeError) as error:
        manager.get_current_version()

    assert error.value.code is ErrorCode.DATABASE_VERSION_TABLE_MISSING


def test_run_health_check_reports_invalid_duplicate_and_unknown_version(
    tmp_path: Path,
) -> None:
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    _write_migration(migrations_dir / "001_init.sql", version="1.0.0")
    _write_migration(migrations_dir / "002_duplicate.sql", version="1.0.0")
    _write_migration(migrations_dir / "003_invalid.sql", version="bad.version")
    _write_migration(migrations_dir / "004_non_monotonic.sql", version="0.9.0")

    db_path = tmp_path / "test.db"
    _apply_schema_version_only(db_path, "9.9.9")

    manager = MigrationManager(db_path, migrations_dir)
    report = manager.run_health_check()

    issues = "\n".join(report["issues"])
    assert report["healthy"] is False
    assert "重复版本号 1.0.0" in issues
    assert "版本号格式无效: bad.version" in issues
    assert "未严格高于前一个脚本版本 1.0.0" in issues
    assert "数据库当前版本 9.9.9 不在当前迁移链定义中" in issues


def test_run_health_check_reports_missing_expected_tables(tmp_path: Path) -> None:
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    for name in [
        "001_initial_schema.sql",
        "002_add_cli_tables.sql",
        "004_add_chat_sessions.sql",
        "005_add_review_system.sql",
        "006_add_relations_foundation.sql",
    ]:
        (migrations_dir / name).write_text(
            (SOURCE_MIGRATIONS_DIR / name).read_text(encoding="utf-8"),
            encoding="utf-8",
        )

    db_path = tmp_path / "test.db"
    with sqlite3.connect(str(db_path)) as conn:
        conn.executescript(
            """
            CREATE TABLE schema_version (
                version_id INTEGER PRIMARY KEY AUTOINCREMENT,
                version TEXT NOT NULL UNIQUE,
                description TEXT
            );
            INSERT INTO schema_version (version, description)
            VALUES ('1.1.2', 'review added');
            """
        )

    manager = MigrationManager(db_path, migrations_dir)
    report = manager.run_health_check()

    assert report["healthy"] is False
    assert any(
        "schema_version 已记录 1.1.2" in issue and "review_queue, review_history" in issue
        for issue in report["issues"]
    )


def test_apply_migration_stops_when_auto_backup_fails(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    (tmp_path / "migrations").mkdir()
    with sqlite3.connect(str(db_path)) as conn:
        conn.executescript(
            """
            CREATE TABLE schema_version (
                version_id INTEGER PRIMARY KEY AUTOINCREMENT,
                version TEXT NOT NULL UNIQUE,
                description TEXT
            );
            """
        )

    migration_file = tmp_path / "migrations" / "010_test.sql"
    migration_file.write_text(
        "\n".join(
            [
                "-- Migration: 010_test.sql",
                "-- Version: 9.0.0",
                "-- Description: create smoke table",
                "CREATE TABLE smoke (id INTEGER PRIMARY KEY);",
                "INSERT INTO schema_version (version, description) VALUES ('9.0.0', 'create smoke table');",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    manager = MigrationManager(db_path, tmp_path / "migrations")
    manager._backup_database = lambda _: (_ for _ in ()).throw(RuntimeError("backup failed"))

    with pytest.raises(PKVRuntimeError) as error:
        manager.apply_migration(migration_file, auto_backup=True)

    assert error.value.code is ErrorCode.MIGRATION_BACKUP_FAILED

    with sqlite3.connect(str(db_path)) as conn:
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='smoke'"
        ).fetchone() is None
        assert conn.execute(
            "SELECT version FROM schema_version WHERE version='9.0.0'"
        ).fetchone() is None


def test_apply_migration_rolls_back_and_raises_on_sql_error(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            """
            CREATE TABLE schema_version (
                version_id INTEGER PRIMARY KEY AUTOINCREMENT,
                version TEXT NOT NULL UNIQUE,
                description TEXT
            )
            """
        )

    migration_file = migrations_dir / "011_bad.sql"
    migration_file.write_text(
        "\n".join(
            [
                "-- Migration: 011_bad.sql",
                "-- Version: 9.0.1",
                "-- Description: bad sql",
                "CREATE TABLE smoke_data (id INTEGER PRIMARY KEY, value TEXT);",
                "INSERT INTO smoke_data (id, value) VALUES (1, 'pending');",
                "THIS IS NOT SQL;",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    manager = MigrationManager(db_path, migrations_dir)

    with pytest.raises(PKVRuntimeError) as error:
        manager.apply_migration(migration_file, auto_backup=False)
    assert error.value.code is ErrorCode.MIGRATION_FAILED

    with sqlite3.connect(str(db_path)) as conn:
        assert conn.execute(
            "SELECT version FROM schema_version WHERE version = '9.0.1'"
        ).fetchone() is None


def test_apply_all_pending_publishes_only_a_valid_complete_chain(tmp_path: Path) -> None:
    manager = MigrationManager(tmp_path / "test.db", SOURCE_MIGRATIONS_DIR)

    assert manager.apply_all_pending(auto_backup=False) == 11
    assert manager.inspect_database().state is DatabaseState.READY
    assert manager.apply_all_pending(auto_backup=False) == 0


def test_apply_all_pending_rolls_back_fts_versions_when_rebuild_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "test.db"
    manager = MigrationManager(db_path, SOURCE_MIGRATIONS_DIR)
    for name in (
        "001_initial_schema.sql",
        "002_add_cli_tables.sql",
        "004_add_chat_sessions.sql",
        "005_add_review_system.sql",
        "006_add_relations_foundation.sql",
        "007_add_timeline_time_fields.sql",
    ):
        manager.apply_migration(SOURCE_MIGRATIONS_DIR / name, auto_backup=False)

    monkeypatch.setattr(
        "src.storage.migration_manager.SQLiteStore.rebuild_fts5_index",
        lambda instance: (_ for _ in ()).throw(RuntimeError("rebuild failed")),
    )

    with pytest.raises(PKVRuntimeError) as error:
        manager.apply_all_pending(auto_backup=False)
    assert error.value.code is ErrorCode.MIGRATION_FAILED

    with sqlite3.connect(str(db_path)) as conn:
        versions = {
            row[0] for row in conn.execute("SELECT version FROM schema_version")
        }

    assert "1.2.2" not in versions
    assert "1.2.3" not in versions
    assert "1.2.4" not in versions
    assert "1.2.5" not in versions
    assert "1.2.6" not in versions


def test_check_and_prompt_upgrade_prints_pending_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    manager = MigrationManager(tmp_path / "test.db", SOURCE_MIGRATIONS_DIR)

    monkeypatch.setattr(
        manager,
        "get_pending_migrations",
        lambda: [("1.2.3", SOURCE_MIGRATIONS_DIR / "009_repair_fts_storage_contract.sql")],
    )

    assert manager.check_and_prompt_upgrade() is True
    output = capsys.readouterr().out

    assert "检测到 1 个待升级的 Schema 变更" in output
    assert "009_repair_fts_storage_contract.sql" in output
    assert "修复 knowledge_items_fts 存储合同并清理重复索引" in output


def test_check_and_prompt_upgrade_returns_false_when_up_to_date(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = MigrationManager(tmp_path / "test.db", SOURCE_MIGRATIONS_DIR)
    monkeypatch.setattr(manager, "get_pending_migrations", lambda: [])

    assert manager.check_and_prompt_upgrade() is False


def test_backup_database_success_and_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "test.db"
    manager = MigrationManager(
        db_path,
        SOURCE_MIGRATIONS_DIR,
        backup_dir=tmp_path / "backups",
    )
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute("CREATE TABLE durable_data (id INTEGER PRIMARY KEY)")

    backup_path = manager._backup_database("001_initial_schema.sql")
    assert backup_path.parent == tmp_path / "backups"
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


def test_parse_helpers_and_version_tuple_fallbacks(tmp_path: Path) -> None:
    manager = MigrationManager(tmp_path / "test.db", SOURCE_MIGRATIONS_DIR)

    numbered_file = tmp_path / "010_without_headers.sql"
    numbered_file.write_text("SELECT 1;\n", encoding="utf-8")
    unnamed_file = tmp_path / "badname.sql"
    unnamed_file.write_text("SELECT 1;\n", encoding="utf-8")

    assert manager._parse_version_from_file(numbered_file) == "0.0.10"
    assert manager._parse_version_from_file(unnamed_file) == "0.0.0"
    assert manager._get_migration_description(unnamed_file) is None
    assert manager._read_migration_metadata(unnamed_file) == {
        "migration": None,
        "version": None,
        "description": None,
    }
    assert manager._version_to_tuple("bad.version") == (0, 0, 0)


def test_timeline_helpers_and_schema_recording_paths(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    manager = MigrationManager(db_path, SOURCE_MIGRATIONS_DIR)

    assert manager._knowledge_items_has_timeline_columns() is False

    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            """
            CREATE TABLE schema_version (
                version_id INTEGER PRIMARY KEY AUTOINCREMENT,
                version TEXT NOT NULL UNIQUE,
                description TEXT
            )
            """
        )

    assert manager._knowledge_items_has_timeline_columns() is False

    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            """
            CREATE TABLE knowledge_items (
                knowledge_id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                event_time TIMESTAMP,
                published_at TIMESTAMP
            )
            """
        )
        manager._ensure_timeline_indexes(conn)
        manager._record_schema_version(conn, None, "skip")
        manager._record_schema_version(conn, "1.2.1", "timeline")
        conn.commit()

    assert manager._knowledge_items_has_timeline_columns() is True

    with sqlite3.connect(str(db_path)) as conn:
        indexes = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='knowledge_items'"
            )
        }
        versions = [
            row[0]
            for row in conn.execute("SELECT version FROM schema_version ORDER BY version_id")
        ]

    assert {"idx_knowledge_event_time", "idx_knowledge_published_at"}.issubset(indexes)
    assert versions == ["1.2.1"]
