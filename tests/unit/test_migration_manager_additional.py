"""
MigrationManager 额外白盒覆盖测试。
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


def _write_migration(path: Path, version: str, description: str = "desc") -> None:
    path.write_text(
        "\n".join(
            [
                f"-- Migration: {path.name}",
                f"-- Version: {version}",
                f"-- Description: {description}",
                "CREATE TABLE IF NOT EXISTS sample_table (id INTEGER PRIMARY KEY);",
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def test_run_health_check_reports_duplicate_unknown_version_and_table_drift(
    tmp_path: Path,
) -> None:
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    _write_migration(migrations_dir / "001_base.sql", "1.0.0", "base")
    _write_migration(migrations_dir / "002_next.sql", "1.0.1", "next")
    _write_migration(migrations_dir / "003_duplicate.sql", "1.0.0", "duplicate")

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
            VALUES ('1.2.0', 'relations ready');
            INSERT INTO schema_version (version, description)
            VALUES ('9.9.9', 'unknown');
            CREATE TABLE chat_sessions (session_id TEXT PRIMARY KEY);
            """
        )

    report = MigrationManager(db_path, migrations_dir).run_health_check()

    assert report["healthy"] is False
    assert any("重复版本号 1.0.0" in issue for issue in report["issues"])
    assert any("未严格高于前一个脚本版本 1.0.1" in issue for issue in report["issues"])
    assert any("数据库当前版本 9.9.9 不在当前迁移链定义中" in issue for issue in report["issues"])
    assert any("chat_sessions" in issue and "1.1.1" in issue for issue in report["issues"])
    assert any("knowledge_relations" in issue and "1.2.0" in issue for issue in report["issues"])


def test_apply_all_pending_rebuilds_fts_when_alignment_versions_present(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = MigrationManager(tmp_path / "test.db", tmp_path / "migrations")
    migration_file = tmp_path / "migrations" / "008_align_fts_contract.sql"
    migration_file.parent.mkdir(exist_ok=True)
    _write_migration(migration_file, "1.2.2")

    monkeypatch.setattr(
        manager,
        "get_pending_migrations",
        lambda: [("1.2.2", migration_file)],
    )

    applied: list[Path] = []
    monkeypatch.setattr(
        manager,
        "apply_migration",
        lambda path, auto_backup=True: applied.append(path),
    )

    rebuilt: list[Path] = []

    class StubStore:
        def __init__(self, db_path: Path) -> None:
            rebuilt.append(db_path)

        def rebuild_fts5_index(self) -> None:
            rebuilt.append(Path("done"))

    monkeypatch.setattr("src.storage.migration_manager.SQLiteStore", StubStore)

    assert manager.apply_all_pending(auto_backup=False) == 1
    assert applied == [migration_file]
    assert rebuilt == [manager.db_path, Path("done")]


def test_apply_all_pending_returns_zero_when_no_pending(tmp_path: Path) -> None:
    manager = MigrationManager(tmp_path / "test.db", tmp_path / "migrations")
    manager.migrations_dir.mkdir(exist_ok=True)

    assert manager.apply_all_pending(auto_backup=False) == 0


def test_apply_migration_warns_on_backup_failure_and_rolls_back_on_sql_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    good_migration = migrations_dir / "010_good.sql"
    bad_migration = migrations_dir / "011_bad.sql"
    _write_migration(good_migration, "1.3.0")
    bad_migration.write_text(
        "\n".join(
            [
                "-- Migration: 011_bad.sql",
                "-- Version: 1.3.1",
                "-- Description: bad",
                "CREATE TABL broken_sql (id INTEGER PRIMARY KEY);",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    manager = MigrationManager(tmp_path / "test.db", migrations_dir)
    monkeypatch.setattr(
        manager,
        "_backup_database",
        lambda migration_name: (_ for _ in ()).throw(RuntimeError("backup failed")),
    )

    manager.apply_migration(good_migration, auto_backup=True)
    with sqlite3.connect(str(manager.db_path)) as conn:
        assert (
            conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='sample_table'"
            ).fetchone()
            is not None
        )
    assert "自动备份失败: backup failed" in caplog.text

    with pytest.raises(sqlite3.Error):
        manager.apply_migration(bad_migration, auto_backup=False)


def test_check_and_prompt_upgrade_prints_pending_descriptions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    migration_file = migrations_dir / "012_prompt.sql"
    _write_migration(migration_file, "2.0.0", "show prompt")
    manager = MigrationManager(tmp_path / "test.db", migrations_dir)
    monkeypatch.setattr(
        manager,
        "get_pending_migrations",
        lambda: [("2.0.0", migration_file)],
    )

    assert manager.check_and_prompt_upgrade() is True
    output = capsys.readouterr().out
    assert "012_prompt.sql" in output
    assert "版本: v2.0.0" in output
    assert "说明: show prompt" in output


def test_backup_and_parse_helper_methods_cover_success_fallbacks_and_invalid_version(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = MigrationManager(tmp_path / "test.db", tmp_path / "migrations")
    manager.migrations_dir.mkdir(exist_ok=True)

    calls: list[list[str]] = []

    def fake_run(command, check, capture_output, text):
        calls.append(command)

        class Result:
            stdout = "backup ok"

        return Result()

    monkeypatch.setattr(subprocess, "run", fake_run)
    manager._backup_database("010_good.sql")
    assert calls and calls[0][0] == "powershell"

    def failing_run(command, check, capture_output, text):
        raise subprocess.CalledProcessError(1, command, stderr="backup error")

    monkeypatch.setattr(subprocess, "run", failing_run)
    with pytest.raises(subprocess.CalledProcessError):
        manager._backup_database("011_bad.sql")

    fallback_file = manager.migrations_dir / "012_no_header.sql"
    fallback_file.write_text("CREATE TABLE noop(id INTEGER PRIMARY KEY);\n", encoding="utf-8")
    invalid_header_file = manager.migrations_dir / "013_invalid_header.sql"
    invalid_header_file.write_text(
        "-- Migration: 013_invalid_header.sql\n"
        "-- Version: invalid\n"
        "CREATE TABLE noop2(id INTEGER PRIMARY KEY);\n",
        encoding="utf-8",
    )

    assert manager._parse_version_from_file(fallback_file) == "0.0.12"
    assert manager._get_migration_description(fallback_file) is None
    assert manager._read_migration_metadata(invalid_header_file)["description"] is None
    assert manager._version_to_tuple("invalid") == (0, 0, 0)
