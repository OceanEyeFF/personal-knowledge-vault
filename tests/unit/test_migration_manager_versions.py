"""
MigrationManager 版本链自检测试。
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.storage.migration_manager import MigrationManager  # noqa: E402
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
    ]
    assert [path.name for _, path in pending] == [
        "004_add_chat_sessions.sql",
        "005_add_review_system.sql",
        "006_add_relations_foundation.sql",
        "007_add_timeline_time_fields.sql",
        "008_align_fts_contract.sql",
        "009_repair_fts_storage_contract.sql",
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

    assert [version for version, _ in pending] == ["1.2.1", "1.2.2", "1.2.3"]


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
    with sqlite3.connect(str(db_path)) as conn:
        conn.executescript(
            """
            CREATE TABLE schema_version (
                version_id INTEGER PRIMARY KEY AUTOINCREMENT,
                version TEXT NOT NULL UNIQUE,
                description TEXT
            );
            INSERT INTO schema_version (version, description)
            VALUES ('1.2.3', 'latest');
            """
        )

    manager = MigrationManager(db_path, MIGRATIONS_DIR)

    assert manager.apply_all_pending(auto_backup=False) == 0
    assert manager.check_and_prompt_upgrade() is False


def test_apply_all_pending_rebuilds_fts_after_alignment_migrations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = MigrationManager(tmp_path / "pending.db", MIGRATIONS_DIR)
    applied: list[tuple[str, bool]] = []
    rebuild_calls: list[Path] = []

    monkeypatch.setattr(
        manager,
        "get_pending_migrations",
        lambda: [
            ("1.2.2", MIGRATIONS_DIR / "008_align_fts_contract.sql"),
            ("1.2.3", MIGRATIONS_DIR / "009_repair_fts_storage_contract.sql"),
        ],
    )
    monkeypatch.setattr(
        manager,
        "apply_migration",
        lambda migration_file, auto_backup=True: applied.append(
            (migration_file.name, auto_backup)
        ),
    )
    monkeypatch.setattr(
        SQLiteStore,
        "rebuild_fts5_index",
        lambda self: rebuild_calls.append(self.db_path),
    )

    migrated = manager.apply_all_pending(auto_backup=False)

    assert migrated == 2
    assert applied == [
        ("008_align_fts_contract.sql", False),
        ("009_repair_fts_storage_contract.sql", False),
    ]
    assert rebuild_calls == [tmp_path / "pending.db"]


def test_apply_migration_continues_when_auto_backup_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "migrate.db"
    manager = MigrationManager(db_path, MIGRATIONS_DIR)

    def _fail_backup(_: str) -> None:
        raise RuntimeError("backup unavailable")

    monkeypatch.setattr(manager, "_backup_database", _fail_backup)

    manager.apply_migration(MIGRATIONS_DIR / "001_initial_schema.sql", auto_backup=True)

    with sqlite3.connect(str(db_path)) as conn:
        version = conn.execute(
            "SELECT version FROM schema_version ORDER BY version_id DESC LIMIT 1"
        ).fetchone()

    assert version is not None
    assert version[0] == "1.0.0"


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
    manager = MigrationManager(tmp_path / "helpers.db", MIGRATIONS_DIR)
    called: dict[str, object] = {}

    class _Result:
        stdout = "backup ok"

    def _success_run(command, check, capture_output, text):
        called["command"] = command
        called["check"] = check
        called["capture_output"] = capture_output
        called["text"] = text
        return _Result()

    monkeypatch.setattr(subprocess, "run", _success_run)
    manager._backup_database("001_initial_schema.sql")

    assert called["command"] == [
        "powershell",
        "-File",
        "scripts/backup-data.ps1",
        "-Message",
        "自动备份 - Schema 迁移前 (001_initial_schema.sql)",
    ]
    assert called["check"] is True
    assert called["capture_output"] is True
    assert called["text"] is True

    def _fail_run(command, check, capture_output, text):
        raise subprocess.CalledProcessError(
            returncode=1,
            cmd=command,
            stderr="backup failed",
        )

    monkeypatch.setattr(subprocess, "run", _fail_run)
    with pytest.raises(subprocess.CalledProcessError):
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
