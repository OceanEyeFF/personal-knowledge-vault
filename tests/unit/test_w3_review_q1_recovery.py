"""Review regression: reconcile a repaired Q1 commit without executing it again."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from src.kernel.facade import KnowledgeKernel
from src.application import KnowledgeApplication
from src.application.r4_lifecycle import R4ContentLifecycle
from src.runtime.errors import ErrorCode, PKVRuntimeError
from src.storage.content_lifecycle import ContentMutationState, PreparedDocument
from src.storage.coordinator import recover_interrupted_operations
from src.storage.markdown_store import Entry
from tests.unit.test_r4_content_lifecycle import _configured
from tests.unit.test_w1_q1_regressions import _archive, _guard_providers, _patch_proof_count, _scenario


def _commit_patch_with_uncertain_response(scenario, monkeypatch):
    store = scenario.application.sqlite_store
    real_apply = store.apply_ai_patch
    real_query = store.query_r4_content_operation
    fail_probe = False

    def commit_then_lose_response(**kwargs):
        nonlocal fail_probe
        real_apply(**kwargs)
        fail_probe = True
        raise OSError("synthetic post-commit response loss")

    def transient_unreadable_proof(operation_id):
        nonlocal fail_probe
        if fail_probe:
            fail_probe = False
            raise OSError("synthetic transient commit-probe read failure")
        return real_query(operation_id)

    async def drain_patch():
        with scenario.application._write_lease_scope():
            return await scenario.lifecycle.drain_task(scenario.patch_task.task_id)

    with monkeypatch.context() as fault:
        fault.setattr(store, "apply_ai_patch", commit_then_lose_response)
        fault.setattr(store, "query_r4_content_operation", transient_unreadable_proof)
        first = asyncio.run(drain_patch())
    assert first.task.state is ContentMutationState.REPAIR_REQUIRED
    assert _patch_proof_count(scenario) == 1
    return first.task


def _delete(scenario, bridge, target):
    if bridge == "sync":
        return KnowledgeKernel._from_application(scenario.application).delete_entry(target)

    async def run():
        with scenario.application._write_lease_scope():
            document = PreparedDocument.for_delete(target)
            if bridge == "async":
                return await scenario.lifecycle.submit_and_drain(document)
            task = scenario.lifecycle.submit_prepared(document)
            await scenario.lifecycle.recover_and_drain()
            return scenario.lifecycle._result_for(scenario.lifecycle.store.get_task(task.task_id))

    return asyncio.run(run())


@pytest.mark.parametrize("bridge", ["sync", "async", "recovery", "existing_proof_journal"])
def test_resolved_patch_repair_unblocks_delete_without_reapplying(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bridge: str,
) -> None:
    scenario = _scenario(
        tmp_path, monkeypatch, initial_state=ContentMutationState.ACCEPTED, same_target=True
    )
    failed = _commit_patch_with_uncertain_response(scenario, monkeypatch)
    if bridge == "existing_proof_journal":
        # The coordinator's established idempotent-proof path writes a
        # journal_created record with projection_sha256 only.  Its SQLite
        # proof still binds the full previous/resulting revision pair.
        with scenario.application._write_lease_scope():
            coordinator = scenario.application.storage_coordinator
            assert coordinator.apply_ai_patch(
                scenario.patch, operation_id=failed.operation_id,
            ).core_committed
            record = coordinator.journal.read(failed.operation_id)
        assert record["checkpoint"] == "journal_created"
        assert "resulting_revision_sha256" not in record
        bridge = "sync"
    calls = []

    def forbidden_reapply(*args, **kwargs):
        calls.append("apply")
        raise AssertionError("recovery must reconcile the existing proof")

    monkeypatch.setattr(scenario.application.storage_coordinator, "apply_ai_patch", forbidden_reapply)
    result = _delete(scenario, bridge, scenario.delete_target)
    assert result.core_committed
    current = scenario.lifecycle.store.get_task(failed.task_id)
    assert current.state is ContentMutationState.COMPLETED
    assert current.owner_fence > failed.owner_fence
    assert current.attempt_count == failed.attempt_count
    assert current.claim_token is None and current.claimed_until is None
    assert current.last_error_code is None and current.not_before is None
    assert current.target_revision_sha256 == scenario.patch.expected_revision_sha256
    assert scenario.application.sqlite_store.query_by_id(scenario.delete_target) is None
    assert _patch_proof_count(scenario) == 1 and calls == [] and scenario.provider_calls == []
    with sqlite3.connect(scenario.application.config.db_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM storage_operation_commits WHERE action = 'delete' AND knowledge_id = ?",
            (scenario.delete_target,),
        ).fetchone()[0] == 1


def _settle_journal(scenario):
    with scenario.application._write_lease_scope():
        coordinator = scenario.application.storage_coordinator
        _, blocking = recover_interrupted_operations(
            coordinator.journal, scenario.application.markdown_store, scenario.application.sqlite_store,
        )
        assert not blocking
        record = coordinator.journal.read(scenario.patch_task.operation_id)
    assert record["core_committed"] is True and record["status"] in {"ready", "degraded"}
    return record


@pytest.mark.parametrize("damage", [
    "missing_proof", "other_operation", "other_target", "previous_revision",
    "resulting_revision", "path", "journal_action", "journal_not_committed", "journal_resulting_revision",
    "unreadable_proof",
])
def test_unproven_repair_stays_blocked_without_starving_independent_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, damage: str,
) -> None:
    scenario = _scenario(
        tmp_path, monkeypatch, initial_state=ContentMutationState.ACCEPTED, same_target=False
    )
    failed = _commit_patch_with_uncertain_response(scenario, monkeypatch)
    record = _settle_journal(scenario)
    with scenario.application._write_lease_scope():
        if damage == "unreadable_proof":
            from src.storage import content_lifecycle as module

            connect = module.connect_existing_sqlite

            def deny_proof_reads(*args, **kwargs):
                connection = connect(*args, **kwargs)
                connection.set_authorizer(
                    lambda action, table, *_: sqlite3.SQLITE_DENY
                    if action == sqlite3.SQLITE_READ and table == "r4_content_operation_commits"
                    else sqlite3.SQLITE_OK
                )
                return connection

            monkeypatch.setattr(module, "connect_existing_sqlite", deny_proof_reads)
        elif damage.startswith("journal_"):
            key, value = {
                "journal_action": ("action", "delete"),
                "journal_not_committed": ("core_committed", False),
                "journal_resulting_revision": ("resulting_revision_sha256", "c" * 64),
            }[damage]
            record[key] = value
            scenario.application.storage_coordinator.journal.write(failed.operation_id, record)
        else:
            with sqlite3.connect(scenario.application.config.db_path) as connection:
                if damage == "missing_proof":
                    connection.execute(
                        "DELETE FROM r4_content_operation_commits WHERE operation_id = ?", (failed.operation_id,),
                    )
                else:
                    column, value = {
                        "other_operation": ("operation_id", "f" * 32),
                        "other_target": ("knowledge_id", scenario.delete_target),
                        "previous_revision": ("previous_revision_sha256", "a" * 64),
                        "resulting_revision": ("resulting_revision_sha256", "b" * 64),
                        "path": ("relative_file_path", "synthetic/different.md"),
                    }[damage]
                    connection.execute(
                        f"UPDATE r4_content_operation_commits SET {column} = ? WHERE operation_id = ?",
                        (value, failed.operation_id),
                    )
        dependent = scenario.lifecycle.submit_prepared(
            PreparedDocument.for_delete(scenario.patch.target_knowledge_id)
        )
    calls = []

    def forbidden_reapply(*args, **kwargs):
        calls.append("apply")
        raise AssertionError("uncertain repair must not execute its patch again")

    monkeypatch.setattr(scenario.application.storage_coordinator, "apply_ai_patch", forbidden_reapply)
    independent = _delete(scenario, "sync", scenario.delete_target)
    assert independent.core_committed
    current = scenario.lifecycle.store.get_task(failed.task_id)
    assert current.state is ContentMutationState.REPAIR_REQUIRED
    assert current.not_before is not None and current.last_error_code is not None
    assert current.attempt_count == failed.attempt_count and current.claim_token is None
    assert scenario.lifecycle.store.get_task(dependent.task_id).state is ContentMutationState.ACCEPTED
    with pytest.raises(PKVRuntimeError) as blocked:
        _delete(scenario, "sync", scenario.patch.target_knowledge_id)
    assert blocked.value.code is ErrorCode.REPAIR_REQUIRED
    assert scenario.application.sqlite_store.query_by_id(scenario.patch.target_knowledge_id) is not None
    assert calls == [] and scenario.provider_calls == []


def test_repair_reconciliation_rejects_stale_fence_and_requires_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = _scenario(
        tmp_path, monkeypatch, initial_state=ContentMutationState.ACCEPTED, same_target=True
    )
    failed = _commit_patch_with_uncertain_response(scenario, monkeypatch)
    record = _settle_journal(scenario)
    with pytest.raises(PKVRuntimeError):
        scenario.lifecycle.store.reconcile_repair_committed(failed, record=record)
    assert scenario.lifecycle.store.get_task(failed.task_id) == failed
    with scenario.application._write_lease_scope():
        blocked = scenario.lifecycle.store.mark_recovery_blocked(
            failed.task_id, error_code=ErrorCode.REPAIR_REQUIRED.value,
        )
        with pytest.raises(PKVRuntimeError):
            scenario.lifecycle.store.reconcile_repair_committed(failed, record=record)
        assert scenario.lifecycle.store.get_task(failed.task_id) == blocked
        with sqlite3.connect(scenario.application.config.db_path) as connection:
            connection.execute("""
                CREATE TRIGGER fail_repair_commit BEFORE UPDATE OF state ON content_mutation_tasks
                WHEN OLD.state = 'repair_required' AND NEW.state = 'core_committed'
                BEGIN SELECT RAISE(ABORT, 'synthetic repair commit failure'); END
            """)
        with pytest.raises(PKVRuntimeError):
            scenario.lifecycle.store.reconcile_repair_committed(blocked, record=record)
        assert scenario.lifecycle.store.get_task(failed.task_id) == blocked
        with sqlite3.connect(scenario.application.config.db_path) as connection:
            connection.execute("DROP TRIGGER fail_repair_commit")
        recovered = scenario.lifecycle.store.reconcile_repair_committed(blocked, record=record)
        assert recovered.state is ContentMutationState.CORE_COMMITTED
        assert recovered.owner_fence == blocked.owner_fence + 1
        with pytest.raises(PKVRuntimeError):
            scenario.lifecycle.store.reconcile_repair_committed(blocked, record=record)
        assert scenario.lifecycle.store.get_task(failed.task_id) == recovered
    assert _patch_proof_count(scenario) == 1 and scenario.provider_calls == []


@pytest.mark.parametrize("action", ["archive", "delete"])
@pytest.mark.parametrize("bridge", ["sync", "async"])
def test_original_archive_and_delete_proofs_reconcile_repair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, action: str, bridge: str,
) -> None:
    application = KnowledgeApplication(_configured(tmp_path))
    calls = []
    _guard_providers(application, monkeypatch, calls)
    lifecycle = R4ContentLifecycle(application)
    if action == "delete":
        original = asyncio.run(_archive(lifecycle, "review delete target"))
        document = PreparedDocument.for_delete(original.task.target_knowledge_id)
    else:
        document = PreparedDocument.for_archive(
            Entry(title="review archive", source_type="text", content="synthetic review body")
        )
    with application._write_lease_scope():
        task = lifecycle.submit_prepared(document)
        claimed = lifecycle.store.claim_next()
        assert claimed.task_id == task.task_id
        coordinator = application.storage_coordinator
        if action == "archive":
            result = coordinator.archive(document.entry, operation_id=task.operation_id, vector_required=False)
        else:
            result = coordinator.delete(document.target_knowledge_id, operation_id=task.operation_id, vector_operation=None)
        assert result.core_committed
        failed = lifecycle.store.mark_repair_required(
            task.task_id, claim_token=claimed.claim_token, owner_fence=claimed.owner_fence,
            error_code=ErrorCode.STORAGE_REPAIR_REQUIRED.value,
        )
    execute = getattr(coordinator, action)
    attempts = []

    def reject_original(*args, **kwargs):
        if kwargs.get("operation_id") == task.operation_id:
            attempts.append("reexecute")
            raise AssertionError("repair reconciliation may not repeat the content operation")
        return execute(*args, **kwargs)

    monkeypatch.setattr(coordinator, action, reject_original)
    if bridge == "sync":
        with application._write_lease_scope():
            trigger = lifecycle.submit_and_drain_sync(PreparedDocument.for_archive(
                Entry(title="review recovery trigger", source_type="text", content="independent trigger")
            ))
        assert trigger.core_committed
    else:
        async def recover():
            with application._write_lease_scope():
                return await lifecycle.drain_task(task.task_id)
        assert asyncio.run(recover()).core_committed
        assert asyncio.run(recover()).core_committed
    recovered = lifecycle.store.get_task(task.task_id)
    assert recovered.state is ContentMutationState.COMPLETED
    assert recovered.owner_fence == failed.owner_fence + 1
    assert recovered.attempt_count == failed.attempt_count and recovered.claim_token is None
    assert recovered.target_knowledge_id == result.knowledge_id
    with sqlite3.connect(application.config.db_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM storage_operation_commits WHERE operation_id = ?", (task.operation_id,),
        ).fetchone()[0] == 1
    assert attempts == [] and calls == []

