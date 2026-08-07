"""
MigrationManager 额外白盒覆盖测试。
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


MIGRATIONS_DIR = PROJECT_ROOT / "scripts" / "migrations"


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
    """真实迁移链在离线工作副本中完成 FTS 对齐后才发布。"""
    from src.storage.sqlite_store import SQLiteStore

    manager = MigrationManager(tmp_path / "test.db", MIGRATIONS_DIR)
    rebuilt: list[Path] = []
    original_rebuild = SQLiteStore.rebuild_fts5_index

    def spy_rebuild(store: SQLiteStore) -> None:
        rebuilt.append(store.db_path)
        original_rebuild(store)

    monkeypatch.setattr(SQLiteStore, "rebuild_fts5_index", spy_rebuild)

    assert manager.apply_all_pending(auto_backup=False) == 9
    assert len(rebuilt) == 1
    assert rebuilt[0] != manager.db_path
    assert manager.inspect_database().state is DatabaseState.READY


def test_apply_all_pending_rejects_empty_migration_bundle(tmp_path: Path) -> None:
    manager = MigrationManager(tmp_path / "test.db", tmp_path / "migrations")
    manager.migrations_dir.mkdir(exist_ok=True)

    with pytest.raises(PKVRuntimeError) as error:
        manager.apply_all_pending(auto_backup=False)

    assert error.value.code is ErrorCode.RESOURCE_MISSING
    assert not manager.db_path.exists()


def test_read_only_manager_does_not_create_missing_paths(tmp_path: Path) -> None:
    db_path = tmp_path / "missing" / "test.db"
    migrations_dir = tmp_path / "missing" / "migrations"

    manager = MigrationManager(db_path, migrations_dir, read_only=True)

    assert manager.read_only is True
    assert not db_path.exists()
    assert not migrations_dir.exists()
    assert not (tmp_path / "missing").exists()
    assert manager.get_current_version() == "0.0.0"
    with pytest.raises(PKVRuntimeError) as error:
        manager.get_pending_migrations()
    assert error.value.code is ErrorCode.RESOURCE_MISSING
    assert not (tmp_path / "missing").exists()


@pytest.mark.parametrize(
    ("operation", "args"),
    [
        ("apply_migration", (Path("001_test.sql"),)),
        ("apply_all_pending", ()),
        ("_remove_applied_versions", (["1.0.0"],)),
        ("_backup_database", ("001_test.sql",)),
    ],
)
def test_read_only_manager_rejects_mutating_apis(
    tmp_path: Path,
    operation: str,
    args: tuple,
) -> None:
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    manager = MigrationManager(tmp_path / "test.db", migrations_dir, read_only=True)

    with pytest.raises(RuntimeError, match="read-only"):
        getattr(manager, operation)(*args)

    assert not manager.db_path.exists()


def test_apply_migration_rejects_backup_failure_and_rolls_back_on_sql_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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

    db_path = tmp_path / "test.db"
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute("CREATE TABLE existing_data (id INTEGER PRIMARY KEY)")
    manager = MigrationManager(db_path, migrations_dir)
    monkeypatch.setattr(
        manager,
        "_backup_database",
        lambda migration_name: (_ for _ in ()).throw(RuntimeError("backup failed")),
    )

    with pytest.raises(PKVRuntimeError) as backup_error:
        manager.apply_migration(good_migration, auto_backup=True)
    assert backup_error.value.code is ErrorCode.MIGRATION_BACKUP_FAILED
    with sqlite3.connect(str(manager.db_path)) as conn:
        assert (
            conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='sample_table'"
            ).fetchone()
            is None
        )

    with pytest.raises(PKVRuntimeError) as migration_error:
        manager.apply_migration(bad_migration, auto_backup=False)
    assert migration_error.value.code is ErrorCode.MIGRATION_FAILED


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
    db_path = tmp_path / "test.db"
    manager = MigrationManager(
        db_path,
        tmp_path / "migrations",
        backup_dir=tmp_path / "backups",
    )
    manager.migrations_dir.mkdir(exist_ok=True)
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute("CREATE TABLE durable_data (id INTEGER PRIMARY KEY)")

    backup_path = manager._backup_database("010_good.sql")
    assert backup_path.parent == manager.backup_dir
    with sqlite3.connect(str(backup_path)) as conn:
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='durable_data'"
        ).fetchone() is not None

    monkeypatch.setattr(
        manager,
        "_copy_database",
        lambda *_args: (_ for _ in ()).throw(OSError("backup error")),
    )
    with pytest.raises(OSError, match="backup error"):
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
