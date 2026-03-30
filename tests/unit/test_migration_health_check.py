"""
迁移链健康检查测试。
"""

from __future__ import annotations

import shutil
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.storage.migration_manager import MigrationManager  # noqa: E402


SOURCE_MIGRATIONS_DIR = PROJECT_ROOT / "scripts" / "migrations"


def _copy_active_migrations(target_dir: Path) -> None:
    for name in [
        "001_initial_schema.sql",
        "002_add_cli_tables.sql",
        "004_add_chat_sessions.sql",
        "005_add_review_system.sql",
        "006_add_relations_foundation.sql",
        "007_add_timeline_time_fields.sql",
    ]:
        shutil.copy2(SOURCE_MIGRATIONS_DIR / name, target_dir / name)


def _apply_sql(db_path: Path, sql_path: Path) -> None:
    sql = sql_path.read_text(encoding="utf-8")
    conn = sqlite3.connect(str(db_path))
    conn.executescript(sql)
    conn.close()


def test_run_health_check_passes_for_clean_chain(tmp_path: Path) -> None:
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    _copy_active_migrations(migrations_dir)

    db_path = tmp_path / "test.db"
    _apply_sql(db_path, migrations_dir / "001_initial_schema.sql")
    _apply_sql(db_path, migrations_dir / "002_add_cli_tables.sql")

    manager = MigrationManager(db_path, migrations_dir)
    report = manager.run_health_check()

    assert report["healthy"] is True
    assert report["issues"] == []
    assert report["database"]["pending_migrations"][-1]["version"] == "1.2.1"


def test_run_health_check_reports_missing_headers(tmp_path: Path) -> None:
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    _copy_active_migrations(migrations_dir)

    bad_file = migrations_dir / "005_add_review_system.sql"
    bad_file.write_text(
        """
-- Migration 005: Add Review System
CREATE TABLE IF NOT EXISTS review_queue (review_id INTEGER PRIMARY KEY);
""".strip()
        + "\n",
        encoding="utf-8",
    )

    manager = MigrationManager(tmp_path / "test.db", migrations_dir)
    report = manager.run_health_check()

    assert report["healthy"] is False
    assert any("005_add_review_system.sql 缺少标准头字段" in issue for issue in report["issues"])


def test_run_health_check_reports_table_version_drift(tmp_path: Path) -> None:
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    _copy_active_migrations(migrations_dir)

    db_path = tmp_path / "test.db"
    _apply_sql(db_path, migrations_dir / "001_initial_schema.sql")
    _apply_sql(db_path, migrations_dir / "002_add_cli_tables.sql")

    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE chat_sessions (session_id TEXT PRIMARY KEY)")
    conn.commit()
    conn.close()

    manager = MigrationManager(db_path, migrations_dir)
    report = manager.run_health_check()

    assert report["healthy"] is False
    assert any("chat_sessions" in issue and "1.1.1" in issue for issue in report["issues"])
