"""Fault-injected cross-store terminal-state contracts for W1."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.runtime import ErrorCode, OperationStatus, PKVRuntimeError, StorageStage
import src.storage.vault_paths as vault_paths_module
from src.storage.coordinator import (
    StorageCoordinator,
    StorageOperationJournal,
    recover_interrupted_operations,
)
from src.storage.markdown_store import Entry, MarkdownStore
from src.storage.migration_manager import MigrationManager
from src.storage.sqlite_store import SQLiteStore


PROJECT_ROOT = Path(__file__).parent.parent.parent
MIGRATIONS_DIR = PROJECT_ROOT / "scripts" / "migrations"


@pytest.fixture
def stores(tmp_path: Path) -> tuple[StorageCoordinator, MarkdownStore, SQLiteStore]:
    data_root = tmp_path / "data"
    db_path = data_root / "db" / "vault.db"
    MigrationManager(
        db_path,
        MIGRATIONS_DIR,
        backup_dir=data_root / "backups",
    ).initialize_fresh()
    markdown = MarkdownStore(data_root / "vault")
    sqlite = SQLiteStore(db_path)
    coordinator = StorageCoordinator(
        markdown,
        sqlite,
        data_root / "runtime" / "operations",
    )
    return coordinator, markdown, sqlite


def _entry(title: str = "W1 entry") -> Entry:
    return Entry(
        title=title,
        source_type="text",
        content="第一段知识。\n\n第二段证据。",
        tags=["w1", "safety"],
        keywords=["storage", "repair"],
    )


def test_archive_commits_primary_required_indexes_and_relative_path(
    stores: tuple[StorageCoordinator, MarkdownStore, SQLiteStore],
) -> None:
    coordinator, markdown, sqlite = stores

    result = coordinator.archive(_entry(), chunks=["chunk-a", "chunk-b"])

    assert result.status is OperationStatus.READY
    assert result.stage is StorageStage.COMPLETED
    assert result.knowledge_id is not None
    assert result.file_path is not None and Path(result.file_path).is_file()
    assert result.relative_file_path == "text/W1 entry.md"
    row = sqlite.query_by_id(result.knowledge_id)
    assert row is not None
    assert row["file_path"] == result.relative_file_path
    assert not Path(row["file_path"]).is_absolute()
    assert [
        chunk["chunk_text"]
        for chunk in sqlite.get_chunks_by_knowledge_id(result.knowledge_id)
    ] == ["chunk-a", "chunk-b"]
    assert markdown.load(result.relative_file_path).title == "W1 entry"
    assert coordinator.journal.read(result.operation_id)["status"] == "ready"


def test_required_index_failure_rolls_back_sql_and_compensates_markdown(
    stores: tuple[StorageCoordinator, MarkdownStore, SQLiteStore],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator, markdown, sqlite = stores

    def fail_chunks(*_: object) -> int:
        raise OSError("chunk write failed")

    monkeypatch.setattr(sqlite, "_insert_chunks", fail_chunks)
    result = coordinator.archive(_entry(), chunks=["chunk"])

    assert result.status is OperationStatus.REJECTED
    assert result.stage is StorageStage.COMPENSATING
    assert markdown.list_all() == []
    assert sqlite.list_entries(limit=10) == []
    assert coordinator.pending_repairs() == []


def test_chunk_preparation_failure_rejects_before_primary_write(
    stores: tuple[StorageCoordinator, MarkdownStore, SQLiteStore],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator, markdown, sqlite = stores
    monkeypatch.setattr(
        "src.storage.coordinator.split_text_into_chunks",
        lambda *_: (_ for _ in ()).throw(ValueError("cannot split")),
    )

    result = coordinator.archive(_entry())

    assert result.status is OperationStatus.REJECTED
    assert result.stage is StorageStage.PREPARING
    assert markdown.list_all() == []
    assert sqlite.list_entries(limit=10) == []


def test_compensation_failure_keeps_explicit_repair_record(
    stores: tuple[StorageCoordinator, MarkdownStore, SQLiteStore],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator, markdown, sqlite = stores
    monkeypatch.setattr(
        sqlite,
        "insert_entry_with_chunks",
        lambda *_, **__: (_ for _ in ()).throw(OSError("sqlite unavailable")),
    )
    monkeypatch.setattr(
        markdown.gateway,
        "delete_if_identity",
        lambda *_, **__: (_ for _ in ()).throw(OSError("cannot compensate")),
    )

    result = coordinator.archive(_entry())

    assert result.status is OperationStatus.REPAIR_REQUIRED
    assert result.repair_actions == ("remove_or_reindex_orphan_markdown",)
    assert len(markdown.list_all()) == 1
    assert len(coordinator.pending_repairs()) == 1


def test_archive_compensation_refuses_same_inode_content_rewrite(
    stores: tuple[StorageCoordinator, MarkdownStore, SQLiteStore],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An in-place user edit must not be deleted as operation-owned content."""

    coordinator, markdown, sqlite = stores

    def rewrite_then_fail(*_args, **_kwargs):
        published = markdown.list_all()[0]
        before = markdown.gateway.file_identity(published)
        published.write_text("user edited in place", encoding="utf-8")
        assert markdown.gateway.file_identity(published) == before
        raise OSError("sqlite unavailable")

    monkeypatch.setattr(sqlite, "insert_entry_with_chunks", rewrite_then_fail)

    result = coordinator.archive(_entry())

    assert result.status is OperationStatus.REPAIR_REQUIRED
    assert result.repair_actions == ("remove_or_reindex_orphan_markdown",)
    assert sqlite.list_entries(limit=10) == []
    remaining = markdown.list_all()
    assert len(remaining) == 1
    assert remaining[0].read_text(encoding="utf-8") == "user edited in place"


def test_archive_recovery_refuses_same_inode_content_rewrite(
    stores: tuple[StorageCoordinator, MarkdownStore, SQLiteStore],
) -> None:
    """Recovery cannot promote a hash-diverged Markdown/SQLite pair to READY."""

    coordinator, markdown, sqlite = stores
    archived = coordinator.archive(_entry(), chunks=["chunk"])
    record = coordinator.journal.read(archived.operation_id)
    record.update(
        {
            "status": "in_progress",
            "stage": StorageStage.INDEX_COMMITTED.value,
            "checkpoint": "index_committed",
        }
    )
    coordinator.journal.write(archived.operation_id, record)
    assert archived.file_path is not None
    path = Path(archived.file_path)
    identity = markdown.gateway.file_identity(path)
    path.write_text("user edited in place", encoding="utf-8")
    assert markdown.gateway.file_identity(path) == identity

    usable, blocking = recover_interrupted_operations(
        coordinator.journal,
        markdown,
        sqlite,
    )

    assert usable == []
    assert [item["operation_id"] for item in blocking] == [archived.operation_id]
    assert coordinator.journal.read(archived.operation_id)["status"] == "in_progress"


def test_archive_commit_then_raise_is_reconciled_without_deleting_markdown(
    stores: tuple[StorageCoordinator, MarkdownStore, SQLiteStore],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A post-commit exception must never trigger destructive compensation."""

    coordinator, markdown, sqlite = stores
    original_insert = sqlite.insert_entry_with_chunks

    def commit_then_raise(*args, **kwargs):
        original_insert(*args, **kwargs)
        raise OSError("injected exception after SQLite commit")

    monkeypatch.setattr(sqlite, "insert_entry_with_chunks", commit_then_raise)

    result = coordinator.archive(_entry(), chunks=["chunk"])

    assert result.status is OperationStatus.READY
    assert result.knowledge_id is not None
    assert result.file_path is not None and Path(result.file_path).is_file()
    assert markdown.load(result.relative_file_path or "").title == "W1 entry"
    assert sqlite.query_by_id(result.knowledge_id) is not None
    assert coordinator.journal.read(result.operation_id)["sqlite_commit_reconciled"] is True


def test_vector_failure_is_degraded_but_core_stores_are_explainable(
    stores: tuple[StorageCoordinator, MarkdownStore, SQLiteStore],
) -> None:
    coordinator, _, sqlite = stores

    result = coordinator.archive(
        _entry(),
        chunks=["chunk"],
        vector_required=True,
        vector_operation=lambda _: (_ for _ in ()).throw(OSError("vector disk full")),
    )

    assert result.status is OperationStatus.DEGRADED
    assert result.stage is StorageStage.INDEX_COMMITTED
    assert result.knowledge_id is not None
    assert sqlite.query_by_id(result.knowledge_id) is not None
    assert result.repair_actions == ("rebuild_vectors_for_entry",)
    assert len(coordinator.pending_repairs()) == 1


def test_vector_prepare_failure_also_preserves_required_core(
    stores: tuple[StorageCoordinator, MarkdownStore, SQLiteStore],
) -> None:
    coordinator, _, sqlite = stores

    result = coordinator.archive(
        _entry(),
        vector_required=True,
        vector_error=RuntimeError("provider offline"),
    )

    assert result.status is OperationStatus.DEGRADED
    assert result.knowledge_id is not None
    assert sqlite.query_by_id(result.knowledge_id) is not None
    assert sqlite.get_chunks_by_knowledge_id(result.knowledge_id)


def test_delete_quarantines_primary_then_commits_all_layers(
    stores: tuple[StorageCoordinator, MarkdownStore, SQLiteStore],
) -> None:
    coordinator, _, sqlite = stores
    archived = coordinator.archive(_entry())
    vector_calls: list[int] = []

    deleted = coordinator.delete(
        archived.knowledge_id or 0,
        vector_operation=lambda knowledge_id: vector_calls.append(knowledge_id),
    )

    assert deleted.status is OperationStatus.DELETED
    assert sqlite.query_by_id(archived.knowledge_id or 0) is None
    assert archived.file_path is not None and not Path(archived.file_path).exists()
    assert vector_calls == [archived.knowledge_id]


def test_sqlite_delete_failure_restores_quarantined_primary(
    stores: tuple[StorageCoordinator, MarkdownStore, SQLiteStore],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator, _, sqlite = stores
    archived = coordinator.archive(_entry())
    monkeypatch.setattr(
        sqlite,
        "delete_entry",
        lambda *_: (_ for _ in ()).throw(OSError("database locked")),
    )

    deleted = coordinator.delete(archived.knowledge_id or 0)

    assert deleted.status is OperationStatus.REJECTED
    assert deleted.stage is StorageStage.COMPENSATING
    assert archived.file_path is not None and Path(archived.file_path).is_file()
    assert sqlite.query_by_id(archived.knowledge_id or 0) is not None


def test_delete_commit_then_raise_is_reconciled_without_restoring_markdown(
    stores: tuple[StorageCoordinator, MarkdownStore, SQLiteStore],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A proven delete commit is finalized even if the call raises afterward."""

    coordinator, _, sqlite = stores
    archived = coordinator.archive(_entry())
    original_delete = sqlite.delete_entry

    def commit_then_raise(*args, **kwargs):
        original_delete(*args, **kwargs)
        raise OSError("injected exception after SQLite delete commit")

    monkeypatch.setattr(sqlite, "delete_entry", commit_then_raise)

    deleted = coordinator.delete(archived.knowledge_id or 0)

    assert deleted.status is OperationStatus.DELETED
    assert sqlite.query_by_id(archived.knowledge_id or 0) is None
    assert archived.file_path is not None and not Path(archived.file_path).exists()


def test_delete_lookup_failure_rejects_before_touching_primary(
    stores: tuple[StorageCoordinator, MarkdownStore, SQLiteStore],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator, _, sqlite = stores
    archived = coordinator.archive(_entry())
    assert archived.file_path is not None
    original_query = sqlite.query_by_id
    monkeypatch.setattr(
        sqlite,
        "query_by_id",
        lambda *_: (_ for _ in ()).throw(OSError("database unavailable")),
    )

    deleted = coordinator.delete(archived.knowledge_id or 0)

    assert deleted.status is OperationStatus.REJECTED
    assert deleted.stage is StorageStage.PREPARING
    assert Path(archived.file_path).is_file()
    assert original_query(archived.knowledge_id or 0) is not None


def test_delete_rejects_malicious_db_path_without_touching_row_or_outside_file(
    stores: tuple[StorageCoordinator, MarkdownStore, SQLiteStore],
    tmp_path: Path,
) -> None:
    coordinator, _, sqlite = stores
    outside = tmp_path / "outside.md"
    outside.write_text("must remain", encoding="utf-8")
    knowledge_id = sqlite.insert_entry(_entry("malicious"), "../outside.md")

    deleted = coordinator.delete(knowledge_id)

    assert deleted.status is OperationStatus.REJECTED
    assert sqlite.query_by_id(knowledge_id) is not None
    assert outside.read_text(encoding="utf-8") == "must remain"


def test_delete_vector_failure_is_degraded_after_core_delete(
    stores: tuple[StorageCoordinator, MarkdownStore, SQLiteStore],
) -> None:
    coordinator, _, sqlite = stores
    archived = coordinator.archive(_entry())

    deleted = coordinator.delete(
        archived.knowledge_id or 0,
        vector_operation=lambda _: (_ for _ in ()).throw(OSError("vector busy")),
    )

    assert deleted.status is OperationStatus.DEGRADED
    assert sqlite.query_by_id(archived.knowledge_id or 0) is None
    assert deleted.repair_actions == ("remove_stale_vectors_for_entry",)


def test_missing_primary_before_delete_has_degraded_terminal_state(
    stores: tuple[StorageCoordinator, MarkdownStore, SQLiteStore],
) -> None:
    coordinator, markdown, sqlite = stores
    archived = coordinator.archive(_entry())
    assert archived.file_path is not None
    markdown.delete(Path(archived.file_path))

    deleted = coordinator.delete(archived.knowledge_id or 0)

    assert deleted.status is OperationStatus.DEGRADED
    assert sqlite.query_by_id(archived.knowledge_id or 0) is None
    assert "audit_missing_primary_file" in deleted.repair_actions


def test_journal_failure_before_primary_rejects_without_side_effects(
    stores: tuple[StorageCoordinator, MarkdownStore, SQLiteStore],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator, markdown, sqlite = stores
    monkeypatch.setattr(
        coordinator.journal,
        "write",
        lambda *_: (_ for _ in ()).throw(OSError("journal read-only")),
    )

    result = coordinator.archive(_entry())

    assert result.status is OperationStatus.REJECTED
    assert result.stage is StorageStage.PREPARING
    assert markdown.list_all() == []
    assert sqlite.list_entries(limit=10) == []


def test_terminal_journal_failure_marks_committed_core_repair_required(
    stores: tuple[StorageCoordinator, MarkdownStore, SQLiteStore],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator, _, sqlite = stores
    original_write = coordinator.journal.write
    def fail_terminal_write(operation_id: str, payload: dict):
        if payload.get("status") == OperationStatus.READY.value:
            raise OSError("terminal journal write failed")
        return original_write(operation_id, payload)

    monkeypatch.setattr(coordinator.journal, "write", fail_terminal_write)

    result = coordinator.archive(_entry(), chunks=["chunk"])

    assert result.status is OperationStatus.REPAIR_REQUIRED
    assert result.knowledge_id is not None
    assert sqlite.query_by_id(result.knowledge_id) is not None
    assert "repair_operation_journal" in result.repair_actions
    # Markdown+SQLite are committed: this is committed-needs-repair, NOT a
    # retry-safe generic failure, and the knowledge_id must be retained.
    assert result.core_committed is True
    assert result.retry_safe is False
    assert result.do_not_retry is True
    payload = result.to_dict()
    assert payload["core_committed"] is True
    assert payload["do_not_retry"] is True
    assert coordinator.pending_repairs()[0]["operation_id"] == result.operation_id


def test_archive_plan_is_journaled_before_any_side_effect(
    stores: tuple[StorageCoordinator, MarkdownStore, SQLiteStore],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exact planned target is durable BEFORE the first archive file write."""
    coordinator, markdown, sqlite = stores
    observed: dict = {}
    original_write = markdown.gateway.write_text_atomic_record

    def spy_write(candidate, text):
        records = [
            record
            for record in coordinator.journal.records()
            if record.get("planned_file_path")
        ]
        observed["plan"] = records[-1] if records else None
        return original_write(candidate, text)

    monkeypatch.setattr(markdown.gateway, "write_text_atomic_record", spy_write)

    result = coordinator.archive(_entry())

    assert result.status is OperationStatus.READY
    assert observed["plan"] is not None
    assert observed["plan"]["planned_file_path"] == str(Path(result.file_path))
    assert (
        observed["plan"]["planned_relative_file_path"] == result.relative_file_path
    )
    assert observed["plan"]["status"] == "in_progress"
    # the file exists exactly at the planned target, with the human-friendly name
    assert Path(result.file_path).is_file()
    assert result.relative_file_path == "text/W1 entry.md"


def test_primary_checkpoint_preserves_plan_and_required_vector_contract(
    stores: tuple[StorageCoordinator, MarkdownStore, SQLiteStore],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator, _, sqlite = stores
    original_insert = sqlite.insert_entry_with_chunks
    observed: dict = {}

    def inspect_checkpoint(entry, relative_path, chunks, **kwargs):
        records = [
            record
            for record in coordinator.journal.records()
            if record.get("stage") == StorageStage.PRIMARY_COMMITTED.value
        ]
        observed.update(records[-1])
        return original_insert(entry, relative_path, chunks, **kwargs)

    monkeypatch.setattr(sqlite, "insert_entry_with_chunks", inspect_checkpoint)

    result = coordinator.archive(
        _entry(),
        chunks=["chunk"],
        vector_required=True,
        vector_operation=lambda _: None,
    )

    assert result.status is OperationStatus.READY
    assert observed["checkpoint"] == "primary_committed"
    assert observed["planned_file_path"] == result.file_path
    assert observed["planned_relative_file_path"] == result.relative_file_path
    assert observed["vector_required"] is True
    terminal = coordinator.journal.read(result.operation_id)
    assert terminal["planned_file_path"] == result.file_path
    assert terminal["planned_relative_file_path"] == result.relative_file_path
    assert terminal["vector_required"] is True


def test_archive_same_name_race_rejects_without_overwrite_or_orphan(
    stores: tuple[StorageCoordinator, MarkdownStore, SQLiteStore],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the planned target races into existence, fail without touching it."""
    coordinator, markdown, sqlite = stores
    original_save_planned = markdown.save_planned_record
    raced: dict = {}

    def racing_save_planned(plan, entry):
        # Simulate the race: the planned target appears between plan and write.
        plan.absolute_path.write_text("user fact", encoding="utf-8")
        raced["path"] = plan.absolute_path
        return original_save_planned(plan, entry)

    monkeypatch.setattr(markdown, "save_planned_record", racing_save_planned)

    result = coordinator.archive(_entry())

    assert result.status is OperationStatus.REJECTED
    assert result.stage is StorageStage.PREPARING
    assert raced["path"].read_text(encoding="utf-8") == "user fact"
    assert markdown.list_all() == [raced["path"]]
    assert sqlite.list_entries(limit=10) == []
    leftovers = [
        path
        for path in raced["path"].parent.iterdir()
        if path.name.startswith(".")
    ]
    assert leftovers == []


def test_delete_plan_is_journaled_before_quarantine_move(
    stores: tuple[StorageCoordinator, MarkdownStore, SQLiteStore],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exact original and deterministic quarantine path are durable before the move."""
    coordinator, markdown, sqlite = stores
    archived = coordinator.archive(_entry())
    observed: dict = {}
    original_quarantine = markdown.gateway.quarantine

    def spy_quarantine(
        candidate,
        *,
        operation_id=None,
        expected_identity=None,
        expected_sha256=None,
    ):
        plans = [
            record
            for record in coordinator.journal.records()
            if record.get("stage") == "delete_planned"
        ]
        observed["plan"] = plans[-1] if plans else None
        moved = original_quarantine(
            candidate,
            operation_id=operation_id,
            expected_identity=expected_identity,
            expected_sha256=expected_sha256,
        )
        observed["moved_to"] = str(moved.quarantine_path)
        return moved

    monkeypatch.setattr(markdown.gateway, "quarantine", spy_quarantine)

    deleted = coordinator.delete(archived.knowledge_id or 0)

    assert deleted.status is OperationStatus.DELETED
    plan = observed["plan"]
    assert plan is not None
    assert plan["stage"] == "delete_planned"
    assert plan["original_path"] == archived.file_path
    expected_quarantine = str(
        markdown.gateway.vault_dir
        / ".pkv-quarantine"
        / f"{deleted.operation_id}-{Path(archived.file_path).name}"
    )
    assert plan["planned_quarantine_path"] == expected_quarantine
    # the move used exactly the journaled no-clobber target
    assert observed["moved_to"] == expected_quarantine


def test_delete_quarantine_target_race_rejects_without_moving(
    stores: tuple[StorageCoordinator, MarkdownStore, SQLiteStore],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pre-existing deterministic quarantine target must not be clobbered."""
    coordinator, markdown, sqlite = stores
    archived = coordinator.archive(_entry())
    fixed_op = "a" * 32
    monkeypatch.setattr(
        "src.storage.coordinator.uuid.uuid4",
        lambda: SimpleNamespace(hex=fixed_op),
    )
    quarantine_dir = markdown.gateway.vault_dir / ".pkv-quarantine"
    quarantine_dir.mkdir(exist_ok=True)
    target = quarantine_dir / f"{fixed_op}-{Path(archived.file_path).name}"
    target.write_text("occupied", encoding="utf-8")

    deleted = coordinator.delete(archived.knowledge_id or 0)

    assert deleted.status is OperationStatus.REJECTED
    assert Path(archived.file_path).is_file()
    assert sqlite.query_by_id(archived.knowledge_id or 0) is not None
    assert target.read_text(encoding="utf-8") == "occupied"


def test_delete_post_move_validation_failure_restores_primary(
    stores: tuple[StorageCoordinator, MarkdownStore, SQLiteStore],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A post-move validation failure must restore the primary file."""
    coordinator, markdown, sqlite = stores
    archived = coordinator.archive(_entry())
    original_lstat = vault_paths_module._lstat
    calls = {"n": 0}

    def flaky_lstat(candidate, *, missing_ok=False):
        path = Path(candidate)
        if path.parent.name == ".pkv-quarantine" and calls["n"] == 0:
            calls["n"] += 1
            raise PKVRuntimeError(
                ErrorCode.PATH_STATE_UNDETERMINED,
                "injected post-move validation failure",
            )
        return original_lstat(path, missing_ok=missing_ok)

    monkeypatch.setattr(vault_paths_module, "_lstat", flaky_lstat)

    deleted = coordinator.delete(archived.knowledge_id or 0)

    assert deleted.status is OperationStatus.REJECTED
    assert Path(archived.file_path).is_file()
    assert sqlite.query_by_id(archived.knowledge_id or 0) is not None


def test_delete_post_move_validation_failure_with_failed_restore_is_repair_required(
    stores: tuple[StorageCoordinator, MarkdownStore, SQLiteStore],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If restore also fails, return REPAIR_REQUIRED with recorded paths."""
    coordinator, markdown, sqlite = stores
    archived = coordinator.archive(_entry())
    original_lstat = vault_paths_module._lstat
    calls = {"n": 0}

    def flaky_lstat(candidate, *, missing_ok=False):
        path = Path(candidate)
        if path.parent.name == ".pkv-quarantine" and calls["n"] == 0:
            calls["n"] += 1
            raise PKVRuntimeError(
                ErrorCode.PATH_STATE_UNDETERMINED,
                "injected post-move validation failure",
            )
        return original_lstat(path, missing_ok=missing_ok)

    monkeypatch.setattr(vault_paths_module, "_lstat", flaky_lstat)
    original_move = vault_paths_module._move_no_clobber

    def selective_move(src: Path, dst: Path) -> None:
        if Path(src).parent.name == ".pkv-quarantine":
            raise OSError("injected restore failure")
        original_move(Path(src), Path(dst))

    monkeypatch.setattr(vault_paths_module, "_move_no_clobber", selective_move)

    deleted = coordinator.delete(archived.knowledge_id or 0)

    assert deleted.status is OperationStatus.REPAIR_REQUIRED
    assert deleted.repair_actions == ("restore_quarantined_markdown",)
    # actionable recorded paths survive into the terminal journal record
    assert deleted.original_path == archived.file_path
    assert deleted.quarantine_path is not None
    assert Path(deleted.quarantine_path).exists()
    assert not Path(archived.file_path).exists()
    record = coordinator.journal.read(deleted.operation_id)
    assert record["original_path"] == archived.file_path
    assert record["quarantine_path"] == deleted.quarantine_path
    assert record["repair_actions"] == ["restore_quarantined_markdown"]
    assert sqlite.query_by_id(archived.knowledge_id or 0) is not None


def test_delete_missing_primary_with_sqlite_failure_is_repair_required(
    stores: tuple[StorageCoordinator, MarkdownStore, SQLiteStore],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing primary + SQLite delete failure: REPAIR_REQUIRED, no restore claim."""
    coordinator, markdown, sqlite = stores
    archived = coordinator.archive(_entry())
    assert archived.file_path is not None
    markdown.delete(Path(archived.file_path))
    monkeypatch.setattr(
        sqlite,
        "delete_entry",
        lambda *_: (_ for _ in ()).throw(OSError("database locked")),
    )

    deleted = coordinator.delete(archived.knowledge_id or 0)

    assert deleted.status is OperationStatus.REPAIR_REQUIRED
    assert deleted.stage is StorageStage.COMPENSATING
    assert deleted.repair_actions == ("audit_missing_primary_file",)
    assert "restore_quarantined_markdown" not in deleted.repair_actions
    assert sqlite.query_by_id(archived.knowledge_id or 0) is not None
    # must NOT claim the Markdown file was restored
    assert not any("已恢复" in error.get("message", "") for error in deleted.errors)
    assert not Path(archived.file_path).exists()
    assert deleted.core_committed is False
    assert deleted.retry_safe is False


def test_journal_read_rejects_valid_non_object_json(
    stores: tuple[StorageCoordinator, MarkdownStore, SQLiteStore],
) -> None:
    coordinator, _, _ = stores
    journal = coordinator.journal
    operation_id = "f" * 32
    (journal.journal_dir / f"{operation_id}.json").write_text(
        "[1, 2, 3]", encoding="utf-8"
    )

    with pytest.raises(PKVRuntimeError) as exc_info:
        journal.read(operation_id)

    assert exc_info.value.code is ErrorCode.STORAGE_REPAIR_REQUIRED


def test_journal_records_turn_valid_non_object_json_into_repair_records(
    stores: tuple[StorageCoordinator, MarkdownStore, SQLiteStore],
) -> None:
    """Valid JSON list/null/scalar must become repair records, never crash."""
    coordinator, _, _ = stores
    journal = coordinator.journal
    payloads = {
        "a" * 32: "[1, 2, 3]",
        "b" * 32: "null",
        "c" * 32: '"scalar"',
        "d" * 32: "{}",
        "e" * 32: '{"status": "in_progress"}',
    }
    for operation_id, payload in payloads.items():
        (journal.journal_dir / f"{operation_id}.json").write_text(
            payload, encoding="utf-8"
        )

    records = list(journal.records())

    assert len(records) == len(payloads)
    for record in records:
        assert record["status"] == OperationStatus.REPAIR_REQUIRED.value
        assert record["action"] == "unknown"
        assert record["operation_id"] in payloads
        assert "repair_operation_journal" in record["repair_actions"]


def test_journal_constructor_rejects_redirected_ancestor(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    redirected = tmp_path / "redirected"
    try:
        redirected.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("当前 Windows 主机不允许创建目录 symlink")

    with pytest.raises(PKVRuntimeError):
        StorageOperationJournal(redirected / "operations")

    assert not (outside / "operations").exists()


def test_core_committed_semantics_across_terminals(
    stores: tuple[StorageCoordinator, MarkdownStore, SQLiteStore],
) -> None:
    """core_committed/retry_safe are set separately from overall success."""
    coordinator, _, sqlite = stores

    ready = coordinator.archive(_entry("ready entry"), chunks=["c"])
    assert ready.core_committed is True
    assert ready.retry_safe is False
    assert ready.successful is True

    degraded = coordinator.archive(
        _entry("degraded entry"),
        chunks=["c"],
        vector_required=True,
        vector_operation=lambda _: (_ for _ in ()).throw(OSError("vector down")),
    )
    assert degraded.core_committed is True
    assert degraded.retry_safe is False
    assert degraded.successful is True

    deleted = coordinator.delete(degraded.knowledge_id or 0)
    assert deleted.core_committed is True
    assert deleted.retry_safe is False
    assert deleted.successful is True

    missing = coordinator.delete(
        sqlite.insert_entry(_entry("missing primary"), "text/missing primary.md"),
    )
    assert missing.status is OperationStatus.DEGRADED
    assert missing.core_committed is True

    rejected = coordinator.delete(999999)
    assert rejected.status is OperationStatus.REJECTED
    assert rejected.core_committed is False
    assert rejected.retry_safe is True
    assert rejected.successful is False
