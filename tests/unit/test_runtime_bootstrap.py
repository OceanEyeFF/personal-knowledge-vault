"""Single bootstrap contract shared by release adapters."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from src.runtime import ErrorCode, PKVRuntimeError, RuntimeLayout, bootstrap_runtime
from src.runtime.write_lease import VaultWriteLease
from src.storage.coordinator import StorageOperationJournal
from src.storage.markdown_store import Entry, MarkdownStore
from src.storage.migration_manager import DatabaseState, MigrationManager
from src.storage.sqlite_store import (
    SQLiteStore,
    entry_projection_sha256,
)
from src.utils.config import Config


PROJECT_ROOT = Path(__file__).parent.parent.parent


def _layout(tmp_path: Path) -> RuntimeLayout:
    return RuntimeLayout.resolve(
        resources_root=PROJECT_ROOT,
        user_data_root=tmp_path / "user-data",
        environment={},
    )


def _journal(layout: RuntimeLayout) -> StorageOperationJournal:
    return StorageOperationJournal(layout.runtime_state_dir / "operations")


def _archive_record(
    layout: RuntimeLayout,
    operation_id: str,
    *,
    stage: str,
    relative: str,
    knowledge_id: object = None,
    vector_required: bool = False,
    projection_sha256: str | None = None,
    primary_identity: tuple[int, int] | None = None,
    primary_sha256: str | None = None,
) -> dict:
    checkpoints = {
        "preparing": "archive_planned",
        "primary_committed": "primary_committed",
        "index_committed": "index_committed",
        "vector_committed": "vector_committed",
    }
    return {
        "action": "archive",
        "status": "in_progress",
        "stage": stage,
        "journal_schema_version": 3,
        "checkpoint": checkpoints[stage],
        "title": "crashed archive",
        "knowledge_id": knowledge_id,
        "planned_file_path": str(layout.vault_dir / relative),
        "planned_relative_file_path": relative,
        "vector_required": vector_required,
        "projection_sha256": projection_sha256 or ("0" * 64),
        "primary_st_dev": primary_identity[0] if primary_identity else None,
        "primary_st_ino": primary_identity[1] if primary_identity else None,
        "primary_sha256": primary_sha256,
        "errors": [],
        "repair_actions": [],
    }



def test_config_resolution_is_read_only_and_bootstrap_creates_declared_tree(
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)

    config = Config(layout=layout)
    assert not layout.user_data_root.exists()

    context = bootstrap_runtime(config)

    assert context.database.state is DatabaseState.READY
    assert layout.db_path.is_file()
    assert layout.vault_dir.is_dir()
    assert layout.vector_index_dir.is_dir()
    assert layout.local_config_path.parent.is_dir()
    assert layout.log_dir.is_dir()
    assert layout.tmp_dir.is_dir()
    assert layout.backup_dir.is_dir()
    assert layout.runtime_state_dir.is_dir()


def test_bootstrap_is_idempotent_for_ready_fresh_install(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    first = bootstrap_runtime(Config(layout=layout))
    before = layout.db_path.stat()

    second = bootstrap_runtime(Config(layout=layout))

    assert first.database.current_version == second.database.current_version
    assert second.database.state is DatabaseState.READY
    assert layout.db_path.stat().st_size == before.st_size


def test_bootstrap_rejects_busy_before_initializing_database_or_recovery(
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    config = Config(layout=layout)

    with VaultWriteLease(layout):
        with pytest.raises(PKVRuntimeError) as captured:
            bootstrap_runtime(config)

    assert captured.value.code is ErrorCode.WRITE_BUSY
    assert captured.value.stage == "write_lease"
    assert captured.value.recoverable is True
    assert not layout.db_path.exists()
    assert not layout.vault_dir.exists()
    assert not (layout.runtime_state_dir / "operations").exists()


def test_bootstrap_rejects_old_database_without_auto_upgrade_or_mutation(
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    layout.ensure_user_directories()
    manager = MigrationManager(layout.db_path, layout.migrations_dir)
    manager.apply_migration(
        layout.migrations_dir / "001_initial_schema.sql", auto_backup=False
    )
    before = layout.db_path.read_bytes()

    with pytest.raises(PKVRuntimeError) as exc_info:
        bootstrap_runtime(Config(layout=layout))

    assert exc_info.value.code is ErrorCode.DATABASE_UPGRADE_REQUIRED
    assert layout.db_path.read_bytes() == before


def test_missing_bundled_resource_rejects_before_user_tree_creation(
    tmp_path: Path,
) -> None:
    resources = tmp_path / "resources"
    (resources / "config").mkdir(parents=True)
    (resources / "config" / "config.yaml").write_text("{}\n", encoding="utf-8")
    user_root = tmp_path / "user-data"
    layout = RuntimeLayout.resolve(
        resources_root=resources,
        user_data_root=user_root,
        environment={},
    )
    config = Config(layout=layout)

    with pytest.raises(PKVRuntimeError) as exc_info:
        bootstrap_runtime(config)

    assert exc_info.value.code is ErrorCode.RESOURCE_MISSING
    assert not user_root.exists()


# ============================================================
# Crash-boundary restart protocol (journal recovery)
# ============================================================


def _initialized(tmp_path: Path) -> tuple[RuntimeLayout, Config]:
    layout = _layout(tmp_path)
    config = Config(layout=layout)
    bootstrap_runtime(config)
    return layout, config


@pytest.mark.parametrize("action", ["archive", "delete"])
def test_bootstrap_recovers_journal_created_before_any_plan(
    tmp_path: Path,
    action: str,
) -> None:
    """A crash immediately after journal creation is provably side-effect free."""
    layout, config = _initialized(tmp_path)
    journal = _journal(layout)
    operation_id = ("7" if action == "archive" else "8") * 32
    payload = {
        "action": action,
        "status": "in_progress",
        "stage": "preparing",
        "journal_schema_version": 1,
        "checkpoint": "journal_created",
        "errors": [],
        "repair_actions": [],
    }
    if action == "delete":
        payload["knowledge_id"] = 999
    journal.write(operation_id, payload)

    context = bootstrap_runtime(config)

    assert context.database.state is DatabaseState.READY
    assert journal.read(operation_id)["status"] == "rejected"
    assert context.repair_records == ()


def test_bootstrap_recovers_plan_only_archive_record(tmp_path: Path) -> None:
    """A crashed archive with only the journaled plan has no side effects."""
    layout, config = _initialized(tmp_path)
    journal = _journal(layout)
    operation_id = "1" * 32
    journal.write(
        operation_id,
        _archive_record(
            layout,
            operation_id,
            stage="preparing",
            relative="text/crashed.md",
        ),
    )

    context = bootstrap_runtime(config)

    assert context.database.state is DatabaseState.READY
    assert journal.read(operation_id)["status"] == "rejected"
    assert not (layout.vault_dir / "text" / "crashed.md").exists()
    assert context.repair_records == ()


def test_bootstrap_correlates_primary_checkpoint_by_operation_commit_proof(
    tmp_path: Path,
) -> None:
    """SQLite may commit before its knowledge_id reaches the next journal record."""
    layout, config = _initialized(tmp_path)
    markdown = MarkdownStore(layout.vault_dir)
    sqlite = SQLiteStore(layout.db_path)
    entry = Entry(title="Primary checkpoint", source_type="text", content="body")
    plan = markdown.plan_save(entry)
    saved = markdown.save_planned(plan, entry)
    journal = _journal(layout)
    operation_id = "9" * 32
    chunks = ["chunk"]
    projection_sha256 = entry_projection_sha256(entry, plan.relative_path, chunks)
    knowledge_id = sqlite.insert_entry_with_chunks(
        entry,
        plan.relative_path,
        chunks,
        operation_id=operation_id,
        projection_sha256=projection_sha256,
    )
    primary_identity, primary_sha256 = markdown.gateway.file_fingerprint(saved)
    journal.write(
        operation_id,
        _archive_record(
            layout,
            operation_id,
            stage="primary_committed",
            relative=plan.relative_path,
            vector_required=True,
            projection_sha256=projection_sha256,
            primary_identity=primary_identity,
            primary_sha256=primary_sha256,
        ),
    )

    context = bootstrap_runtime(config)

    record = journal.read(operation_id)
    assert record["status"] == "degraded"
    assert record["knowledge_id"] == knowledge_id
    assert "rebuild_vectors_for_entry" in record["repair_actions"]
    assert any(item["operation_id"] == operation_id for item in context.repair_records)


def test_bootstrap_fails_closed_on_ambiguous_orphan_markdown(tmp_path: Path) -> None:
    """Orphan markdown without a committed row must fail closed, not delete facts."""
    layout, config = _initialized(tmp_path)
    markdown = MarkdownStore(layout.vault_dir)
    journal = _journal(layout)
    operation_id = "2" * 32
    target = layout.vault_dir / "text" / "orphan.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# user fact\n", encoding="utf-8")
    primary_identity, primary_sha256 = markdown.gateway.file_fingerprint(target)
    journal.write(
        operation_id,
        _archive_record(
            layout,
            operation_id,
            stage="primary_committed",
            relative="text/orphan.md",
            primary_identity=primary_identity,
            primary_sha256=primary_sha256,
        ),
    )

    with pytest.raises(PKVRuntimeError) as exc_info:
        bootstrap_runtime(config)

    assert exc_info.value.code is ErrorCode.STORAGE_REPAIR_REQUIRED
    assert "2" * 32 in str(exc_info.value)
    assert target.read_text(encoding="utf-8") == "# user fact\n"


def test_bootstrap_recovers_committed_archive_to_degraded_when_vectors_required(
    tmp_path: Path,
) -> None:
    """Provable Markdown+SQLite commit with unknown vectors recovers to DEGRADED."""
    layout, config = _initialized(tmp_path)
    markdown = MarkdownStore(layout.vault_dir)
    sqlite = SQLiteStore(layout.db_path)
    entry = Entry(title="Committed", source_type="text", content="body")
    plan = markdown.plan_save(entry)
    saved = markdown.save_planned(plan, entry)
    operation_id = "3" * 32
    chunks = ["chunk"]
    projection_sha256 = entry_projection_sha256(entry, plan.relative_path, chunks)
    knowledge_id = sqlite.insert_entry_with_chunks(
        entry,
        plan.relative_path,
        chunks,
        operation_id=operation_id,
        projection_sha256=projection_sha256,
    )
    primary_identity, primary_sha256 = markdown.gateway.file_fingerprint(saved)
    journal = _journal(layout)
    journal.write(
        operation_id,
        _archive_record(
            layout,
            operation_id,
            stage="index_committed",
            relative=plan.relative_path,
            knowledge_id=knowledge_id,
            vector_required=True,
            projection_sha256=projection_sha256,
            primary_identity=primary_identity,
            primary_sha256=primary_sha256,
        ),
    )

    context = bootstrap_runtime(config)

    assert context.database.state is DatabaseState.READY
    record = journal.read(operation_id)
    assert record["status"] == "degraded"
    assert "rebuild_vectors_for_entry" in record["repair_actions"]
    assert any(record["operation_id"] == operation_id for record in context.repair_records)


def test_bootstrap_recovers_restorable_quarantined_delete(tmp_path: Path) -> None:
    """Delete quarantined before SQLite commit: restore is provable and idempotent."""
    layout, config = _initialized(tmp_path)
    markdown = MarkdownStore(layout.vault_dir)
    sqlite = SQLiteStore(layout.db_path)
    entry = Entry(title="Del", source_type="text", content="body")
    plan = markdown.plan_save(entry)
    saved = markdown.save_planned(plan, entry)
    chunks = ["c"]
    knowledge_id = sqlite.insert_entry_with_chunks(entry, plan.relative_path, chunks)
    projection_sha256 = entry_projection_sha256(entry, plan.relative_path, chunks)
    primary_identity, primary_sha256 = markdown.gateway.file_fingerprint(saved)
    journal = _journal(layout)
    operation_id = "4" * 32
    quarantine_path = markdown.plan_quarantine(plan.relative_path, operation_id)
    quarantine_path.parent.mkdir(parents=True, exist_ok=True)
    os.replace(saved, quarantine_path)  # simulate the move that happened before the crash
    journal.write(
        operation_id,
        {
            "action": "delete",
            "status": "in_progress",
            "stage": "delete_quarantined",
            "journal_schema_version": 3,
            "checkpoint": "primary_quarantined",
            "knowledge_id": knowledge_id,
            "relative_file_path": plan.relative_path,
            "original_path": str(saved),
            "quarantine_path": str(quarantine_path),
            "planned_quarantine_path": str(quarantine_path),
            "primary_missing": False,
            "primary_st_dev": primary_identity[0],
            "primary_st_ino": primary_identity[1],
            "quarantine_st_dev": primary_identity[0],
            "quarantine_st_ino": primary_identity[1],
            "primary_sha256": primary_sha256,
            "quarantine_sha256": primary_sha256,
            "projection_sha256": projection_sha256,
            "vector_required": False,
            "errors": [],
            "repair_actions": [],
        },
    )

    context = bootstrap_runtime(config)

    assert context.database.state is DatabaseState.READY
    assert journal.read(operation_id)["status"] == "rejected"
    assert Path(saved).is_file()
    assert not quarantine_path.exists()
    assert sqlite.query_by_id(knowledge_id) is not None
    assert context.repair_records == ()


def test_bootstrap_refuses_replaced_quarantine_identity(tmp_path: Path) -> None:
    """A journal path cannot authorize restoring a different regular file."""

    layout, config = _initialized(tmp_path)
    markdown = MarkdownStore(layout.vault_dir)
    sqlite = SQLiteStore(layout.db_path)
    entry = Entry(title="Replaced quarantine", source_type="text", content="body")
    plan = markdown.plan_save(entry)
    saved = markdown.save_planned(plan, entry)
    chunks = ["c"]
    knowledge_id = sqlite.insert_entry_with_chunks(entry, plan.relative_path, chunks)
    projection_sha256 = entry_projection_sha256(entry, plan.relative_path, chunks)
    primary_identity, primary_sha256 = markdown.gateway.file_fingerprint(saved)
    operation_id = "e" * 32
    quarantine_path = markdown.plan_quarantine(plan.relative_path, operation_id)
    quarantine_path.parent.mkdir(parents=True, exist_ok=True)
    os.replace(saved, quarantine_path)
    replacement = layout.vault_dir / "replacement.tmp"
    replacement.write_text("replacement", encoding="utf-8")
    os.replace(replacement, quarantine_path)
    journal = _journal(layout)
    journal.write(
        operation_id,
        {
            "action": "delete",
            "status": "in_progress",
            "stage": "delete_quarantined",
            "journal_schema_version": 3,
            "checkpoint": "primary_quarantined",
            "knowledge_id": knowledge_id,
            "relative_file_path": plan.relative_path,
            "original_path": str(saved),
            "quarantine_path": str(quarantine_path),
            "planned_quarantine_path": str(quarantine_path),
            "primary_missing": False,
            "primary_st_dev": primary_identity[0],
            "primary_st_ino": primary_identity[1],
            "quarantine_st_dev": primary_identity[0],
            "quarantine_st_ino": primary_identity[1],
            "primary_sha256": primary_sha256,
            "quarantine_sha256": primary_sha256,
            "projection_sha256": projection_sha256,
            "vector_required": False,
            "errors": [],
            "repair_actions": [],
        },
    )

    with pytest.raises(PKVRuntimeError) as exc_info:
        bootstrap_runtime(config)

    assert exc_info.value.code is ErrorCode.STORAGE_REPAIR_REQUIRED
    assert not saved.exists()
    assert quarantine_path.read_text(encoding="utf-8") == "replacement"
    assert sqlite.query_by_id(knowledge_id) is not None


def test_bootstrap_never_restores_another_operations_quarantine(
    tmp_path: Path,
) -> None:
    layout, config = _initialized(tmp_path)
    markdown = MarkdownStore(layout.vault_dir)
    sqlite = SQLiteStore(layout.db_path)
    entry = Entry(title="Wrong quarantine", source_type="text", content="body")
    plan = markdown.plan_save(entry)
    saved = markdown.save_planned(plan, entry)
    chunks = ["c"]
    knowledge_id = sqlite.insert_entry_with_chunks(entry, plan.relative_path, chunks)
    projection_sha256 = entry_projection_sha256(entry, plan.relative_path, chunks)
    primary_identity, primary_sha256 = markdown.gateway.file_fingerprint(saved)
    operation_id = "d" * 32
    foreign_quarantine = layout.vault_dir / ".pkv-quarantine" / f"foreign-{saved.name}"
    foreign_quarantine.parent.mkdir(parents=True, exist_ok=True)
    os.replace(saved, foreign_quarantine)
    journal = _journal(layout)
    journal.write(
        operation_id,
        {
            "action": "delete",
            "status": "in_progress",
            "stage": "delete_quarantined",
            "journal_schema_version": 3,
            "checkpoint": "primary_quarantined",
            "knowledge_id": knowledge_id,
            "relative_file_path": plan.relative_path,
            "original_path": str(saved),
            "quarantine_path": str(foreign_quarantine),
            "planned_quarantine_path": str(foreign_quarantine),
            "primary_missing": False,
            "primary_st_dev": primary_identity[0],
            "primary_st_ino": primary_identity[1],
            "quarantine_st_dev": primary_identity[0],
            "quarantine_st_ino": primary_identity[1],
            "primary_sha256": primary_sha256,
            "quarantine_sha256": primary_sha256,
            "projection_sha256": projection_sha256,
            "vector_required": False,
            "errors": [],
            "repair_actions": [],
        },
    )

    with pytest.raises(PKVRuntimeError) as exc_info:
        bootstrap_runtime(config)

    assert exc_info.value.code is ErrorCode.STORAGE_REPAIR_REQUIRED
    assert foreign_quarantine.read_text(encoding="utf-8")
    assert not saved.exists()
    assert sqlite.query_by_id(knowledge_id) is not None


def test_bootstrap_recovers_committed_delete_quarantine_to_degraded_purge(
    tmp_path: Path,
) -> None:
    """SQLite committed but quarantine remains: DEGRADED + purge, never auto-delete."""
    layout, config = _initialized(tmp_path)
    markdown = MarkdownStore(layout.vault_dir)
    sqlite = SQLiteStore(layout.db_path)
    entry = Entry(title="Del2", source_type="text", content="body")
    plan = markdown.plan_save(entry)
    saved = markdown.save_planned(plan, entry)
    operation_id = "6" * 32
    chunks = ["c"]
    knowledge_id = sqlite.insert_entry_with_chunks(entry, plan.relative_path, chunks)
    projection_sha256 = entry_projection_sha256(entry, plan.relative_path, chunks)
    primary_identity, primary_sha256 = markdown.gateway.file_fingerprint(saved)
    sqlite.delete_entry(
        knowledge_id,
        operation_id=operation_id,
        projection_sha256=projection_sha256,
        relative_file_path=plan.relative_path,
    )  # SQLite already committed with an operation-bound proof
    journal = _journal(layout)
    quarantine_path = markdown.plan_quarantine(plan.relative_path, operation_id)
    quarantine_path.parent.mkdir(parents=True, exist_ok=True)
    os.replace(saved, quarantine_path)
    journal.write(
        operation_id,
        {
            "action": "delete",
            "status": "in_progress",
            "stage": "delete_quarantined",
            "journal_schema_version": 3,
            "checkpoint": "primary_quarantined",
            "knowledge_id": knowledge_id,
            "relative_file_path": plan.relative_path,
            "original_path": str(saved),
            "quarantine_path": str(quarantine_path),
            "planned_quarantine_path": str(quarantine_path),
            "primary_missing": False,
            "primary_st_dev": primary_identity[0],
            "primary_st_ino": primary_identity[1],
            "quarantine_st_dev": primary_identity[0],
            "quarantine_st_ino": primary_identity[1],
            "primary_sha256": primary_sha256,
            "quarantine_sha256": primary_sha256,
            "projection_sha256": projection_sha256,
            "vector_required": False,
            "errors": [],
            "repair_actions": [],
        },
    )

    context = bootstrap_runtime(config)

    assert context.database.state is DatabaseState.READY
    record = journal.read(operation_id)
    assert record["status"] == "degraded"
    assert "purge_committed_quarantine" in record["repair_actions"]
    # the quarantined user file is NOT deleted automatically
    assert quarantine_path.exists()
    assert any(record["operation_id"] == operation_id for record in context.repair_records)


def test_bootstrap_fails_closed_on_valid_non_object_journal_json(tmp_path: Path) -> None:
    """Valid JSON list/null/scalar must become a repair record, never crash."""
    layout, config = _initialized(tmp_path)
    journal_dir = layout.runtime_state_dir / "operations"
    payloads = {
        "a" * 32: "[1, 2, 3]",
        "b" * 32: "null",
        "c" * 32: '"scalar"',
    }
    for operation_id, payload in payloads.items():
        (journal_dir / f"{operation_id}.json").write_text(payload, encoding="utf-8")

    with pytest.raises(PKVRuntimeError) as exc_info:
        bootstrap_runtime(config)

    assert exc_info.value.code is ErrorCode.STORAGE_REPAIR_REQUIRED
    assert "需要人工修复" in str(exc_info.value)


def test_bootstrap_keeps_terminal_degraded_records_startup_usable(tmp_path: Path) -> None:
    """Ordinary terminal DEGRADED vector repairs stay usable and visible."""
    layout, config = _initialized(tmp_path)
    journal = _journal(layout)
    operation_id = "5" * 32
    journal.write(
        operation_id,
        {
            "action": "archive",
            "status": "degraded",
            "stage": "index_committed",
            "title": "degraded",
            "knowledge_id": 1,
            "errors": [
                {
                    "code": "storage_vector_failed",
                    "message": "vector unavailable",
                    "recoverable": True,
                }
            ],
            "repair_actions": ["rebuild_vectors_for_entry"],
        },
    )

    context = bootstrap_runtime(config)

    assert context.database.state is DatabaseState.READY
    assert any(record["operation_id"] == operation_id for record in context.repair_records)
