"""W1 fail-closed database classification and migration atomicity contracts."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from src.runtime import ErrorCode, PKVRuntimeError
from src.storage.migration_manager import DatabaseState, MigrationManager
from src.storage.relation_store import RelationStore
from src.storage.sqlite_store import SQLiteStore


PROJECT_ROOT = Path(__file__).parent.parent.parent
MIGRATIONS_DIR = PROJECT_ROOT / "scripts" / "migrations"


def _version_table(db_path: Path, version: str | None = None) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE schema_version (
                version_id INTEGER PRIMARY KEY AUTOINCREMENT,
                version TEXT NOT NULL UNIQUE,
                description TEXT
            )
            """
        )
        if version is not None:
            conn.execute(
                "INSERT INTO schema_version(version, description) VALUES (?, ?)",
                (version, "test"),
            )
        conn.commit()
    finally:
        conn.close()


def _write_migration(path: Path, version: str, body: str) -> None:
    path.write_text(
        "\n".join(
            (
                f"-- Migration: {path.name}",
                f"-- Version: {version}",
                f"-- Description: migration {version}",
                body,
            )
        )
        + "\n",
        encoding="utf-8",
    )


def test_absent_database_is_the_only_fresh_state_and_inspection_is_read_only(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "db" / "vault.db"
    manager = MigrationManager(db_path, MIGRATIONS_DIR, read_only=True)

    inspection = manager.inspect_database()

    assert inspection.state is DatabaseState.FRESH
    assert inspection.current_version == "0.0.0"
    assert not db_path.exists()
    assert not db_path.parent.exists()


def test_existing_database_without_version_table_is_not_fresh(tmp_path: Path) -> None:
    db_path = tmp_path / "vault.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE legacy_data(id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()

    with pytest.raises(PKVRuntimeError) as exc_info:
        MigrationManager(db_path, MIGRATIONS_DIR).inspect_database()

    assert exc_info.value.code is ErrorCode.DATABASE_VERSION_TABLE_MISSING


@pytest.mark.parametrize("store_type", [SQLiteStore, RelationStore])
def test_business_store_never_recreates_a_missing_database(
    tmp_path: Path,
    store_type,
) -> None:
    db_path = tmp_path / "missing" / "vault.db"
    store = store_type(db_path)

    with pytest.raises(PKVRuntimeError) as error:
        if isinstance(store, SQLiteStore):
            store.count_entries()
        else:
            store.table_exists()

    assert error.value.code is ErrorCode.DATABASE_MISSING
    assert not db_path.exists()
    assert not db_path.parent.exists()


def test_corrupt_or_empty_database_is_rejected_as_non_sqlite(tmp_path: Path) -> None:
    db_path = tmp_path / "vault.db"
    db_path.write_bytes(b"not-a-sqlite-database")

    with pytest.raises(PKVRuntimeError) as exc_info:
        MigrationManager(db_path, MIGRATIONS_DIR).inspect_database()

    assert exc_info.value.code is ErrorCode.DATABASE_NOT_SQLITE


def test_existing_database_hardlink_is_rejected_before_open(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    linked = tmp_path / "linked.db"
    _version_table(source, "1.0.0")
    try:
        os.link(source, linked)
    except OSError as exc:
        pytest.skip(f"hard links unavailable: {exc}")

    with pytest.raises(PKVRuntimeError) as exc_info:
        MigrationManager(linked, MIGRATIONS_DIR).inspect_database()

    assert exc_info.value.code is ErrorCode.DATA_ROOT_UNSAFE


@pytest.mark.parametrize("version", [None, "not-semver"])
def test_unreadable_version_state_is_rejected(
    tmp_path: Path, version: str | None
) -> None:
    db_path = tmp_path / "vault.db"
    _version_table(db_path, version)

    with pytest.raises(PKVRuntimeError) as exc_info:
        MigrationManager(db_path, MIGRATIONS_DIR).inspect_database()

    assert exc_info.value.code is ErrorCode.DATABASE_VERSION_TABLE_INVALID


def test_fresh_initialization_is_off_path_and_reaches_ready(tmp_path: Path) -> None:
    db_path = tmp_path / "data" / "db" / "vault.db"
    manager = MigrationManager(
        db_path,
        MIGRATIONS_DIR,
        backup_dir=tmp_path / "data" / "backups",
    )

    inspection = manager.initialize_fresh()

    assert inspection.state is DatabaseState.READY
    assert inspection.current_version == manager.latest_version()
    assert manager.require_ready().state is DatabaseState.READY
    assert not list(db_path.parent.glob("*.initializing"))
    assert not list(db_path.parent.glob("*.migrating"))


def test_old_and_future_versions_have_distinct_startup_rejections(tmp_path: Path) -> None:
    old_db = tmp_path / "old.db"
    old = MigrationManager(old_db, MIGRATIONS_DIR)
    old.apply_migration(MIGRATIONS_DIR / "001_initial_schema.sql", auto_backup=False)
    assert old.inspect_database().state is DatabaseState.UPGRADE_REQUIRED
    with pytest.raises(PKVRuntimeError) as old_error:
        old.require_ready()
    assert old_error.value.code is ErrorCode.DATABASE_UPGRADE_REQUIRED

    future_db = tmp_path / "future.db"
    _version_table(future_db, "99.0.0")
    future = MigrationManager(future_db, MIGRATIONS_DIR)
    assert future.inspect_database().state is DatabaseState.FUTURE_VERSION
    with pytest.raises(PKVRuntimeError) as future_error:
        future.require_ready()
    assert future_error.value.code is ErrorCode.DATABASE_FUTURE_VERSION


def test_old_version_with_missing_declared_tables_is_schema_drift(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "incomplete-old.db"
    _version_table(db_path, "1.0.0")

    with pytest.raises(PKVRuntimeError) as exc_info:
        MigrationManager(db_path, MIGRATIONS_DIR).inspect_database()

    assert exc_info.value.code is ErrorCode.DATABASE_SCHEMA_DRIFT


def test_backup_failure_stops_before_migration_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "vault.db"
    manager = MigrationManager(db_path, MIGRATIONS_DIR)
    manager.apply_migration(MIGRATIONS_DIR / "001_initial_schema.sql", auto_backup=False)
    before = db_path.read_bytes()
    monkeypatch.setattr(
        manager,
        "_backup_database",
        lambda _: (_ for _ in ()).throw(OSError("disk full")),
    )

    with pytest.raises(PKVRuntimeError) as exc_info:
        manager.apply_migration(
            MIGRATIONS_DIR / "002_add_cli_tables.sql", auto_backup=True
        )

    assert exc_info.value.code is ErrorCode.MIGRATION_BACKUP_FAILED
    assert db_path.read_bytes() == before


def test_single_migration_sql_failure_rolls_back_ddl_and_data(tmp_path: Path) -> None:
    db_path = tmp_path / "vault.db"
    _version_table(db_path, "1.0.0")
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    bad = migrations / "002_bad.sql"
    _write_migration(
        bad,
        "1.1.0",
        "CREATE TABLE half_commit(id INTEGER);\nTHIS IS NOT SQL;",
    )
    manager = MigrationManager(db_path, migrations)

    with pytest.raises(PKVRuntimeError) as exc_info:
        manager.apply_migration(bad, auto_backup=False)

    assert exc_info.value.code is ErrorCode.MIGRATION_FAILED
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='half_commit'"
        ).fetchone() is None
    finally:
        conn.close()


def test_migration_chain_failure_never_publishes_partial_fresh_database(
    tmp_path: Path,
) -> None:
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    _write_migration(
        migrations / "001_base.sql",
        "1.0.0",
        """
        CREATE TABLE schema_version (
            version_id INTEGER PRIMARY KEY AUTOINCREMENT,
            version TEXT NOT NULL UNIQUE,
            description TEXT
        );
        INSERT INTO schema_version(version, description) VALUES ('1.0.0', 'base');
        CREATE TABLE primary_data(id INTEGER PRIMARY KEY);
        """,
    )
    _write_migration(
        migrations / "002_bad.sql",
        "1.1.0",
        "CREATE TABLE half_commit(id INTEGER);\nTHIS IS NOT SQL;",
    )
    db_path = tmp_path / "vault.db"
    manager = MigrationManager(db_path, migrations)

    with pytest.raises(PKVRuntimeError) as exc_info:
        manager.apply_all_pending(auto_backup=False)

    assert exc_info.value.code is ErrorCode.MIGRATION_FAILED
    assert not db_path.exists()


def test_online_backup_is_valid_and_contained_in_declared_backup_dir(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "data" / "db" / "vault.db"
    backup_dir = tmp_path / "data" / "backups"
    manager = MigrationManager(db_path, MIGRATIONS_DIR, backup_dir=backup_dir)
    manager.apply_migration(MIGRATIONS_DIR / "001_initial_schema.sql", auto_backup=False)

    backup = manager._backup_database("before-upgrade")

    assert backup.parent == backup_dir
    assert backup.is_file()
    assert MigrationManager(backup, MIGRATIONS_DIR).get_current_version() == "1.0.0"
