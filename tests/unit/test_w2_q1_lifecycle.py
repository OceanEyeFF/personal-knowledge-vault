"""Q1 local failure, fair scheduling, dependency and proof recovery contracts."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from src.application.r4_lifecycle import R4ContentLifecycle
from src.kernel.facade import KnowledgeKernel
from src.runtime.errors import ErrorCode, OperationStatus, PKVRuntimeError, StorageStage
from src.storage.content_lifecycle import ContentLifecycleStore, ContentMutationAction, ContentMutationState, PreparedDocument
from src.storage.coordinator import StorageOperationResult
from src.storage.markdown_store import Entry
from tests.unit.test_w1_q1_regressions import _patch_proof_count, _scenario


def _drain(scenario, bridge: str, document: PreparedDocument):
    if bridge == "sync":
        with scenario.application._write_lease_scope():
            return scenario.lifecycle.submit_and_drain_sync(document)

    async def run():
        with scenario.application._write_lease_scope():
            task = scenario.lifecycle.submit_prepared(document)
            if bridge == "async":
                return await scenario.lifecycle.drain_task(task.task_id)
            return await scenario.lifecycle.recover_and_drain()

    return asyncio.run(run())


@pytest.mark.parametrize("bridge", ["sync", "async", "recovery"])
@pytest.mark.parametrize("typed_error", [True, False], ids=["runtime_error", "io_error"])
def test_local_patch_failure_backs_off_without_blocking_independent_delete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bridge: str, typed_error: bool,
) -> None:
    scenario = _scenario(
        tmp_path, monkeypatch, initial_state=ContentMutationState.ACCEPTED, same_target=False
    )
    with scenario.application._write_lease_scope():
        coordinator = scenario.application.storage_coordinator
    apply_patch = coordinator.apply_ai_patch
    calls = []

    def fail_patch(*args, **kwargs):
        calls.append(kwargs["operation_id"])
        if typed_error:
            raise PKVRuntimeError(ErrorCode.STORAGE_PRIMARY_FAILED, "synthetic failure")
        raise OSError("synthetic IO failure")

    monkeypatch.setattr(coordinator, "apply_ai_patch", fail_patch)
    with scenario.application._write_lease_scope():
        dependent = scenario.lifecycle.submit_prepared(
            PreparedDocument.for_delete(scenario.patch.target_knowledge_id)
        )
    _drain(scenario, bridge, PreparedDocument.for_delete(scenario.delete_target))
    failed = scenario.lifecycle.store.get_task(scenario.patch_task.task_id)
    waiting = scenario.lifecycle.store.get_task(dependent.task_id)
    assert failed.state is ContentMutationState.RETRY_REQUIRED
    assert failed.attempt_count == 1 and calls == [failed.operation_id]
    assert failed.not_before is not None and failed.last_error_code == "storage_primary_failed"
    assert failed.claim_token is None and failed.claimed_until is None
    assert waiting.state is ContentMutationState.ACCEPTED and waiting.attempt_count == 0
    assert scenario.application.sqlite_store.query_by_id(scenario.delete_target) is None
    assert scenario.application.sqlite_store.query_by_id(scenario.patch.target_knowledge_id) is not None
    assert _patch_proof_count(scenario) == 0

    async def recover():
        with scenario.application._write_lease_scope():
            return await R4ContentLifecycle(scenario.application).recover_and_drain()

    # A new lifecycle sees persisted backoff; no clock sleeps or implicit retry.
    asyncio.run(recover())
    assert calls == [failed.operation_id]
    monkeypatch.setattr(coordinator, "apply_ai_patch", apply_patch)
    with sqlite3.connect(scenario.application.config.db_path) as connection:
        connection.execute(
            "UPDATE content_mutation_tasks SET not_before = CURRENT_TIMESTAMP WHERE task_id = ?",
            (failed.task_id,),
        )
    asyncio.run(recover())
    assert scenario.lifecycle.store.get_task(failed.task_id).state is ContentMutationState.COMPLETED
    assert scenario.lifecycle.store.get_task(dependent.task_id).state is ContentMutationState.COMPLETED
    assert scenario.application.sqlite_store.query_by_id(scenario.patch.target_knowledge_id) is None
    assert _patch_proof_count(scenario) == 1
    assert scenario.provider_calls == []


@pytest.mark.parametrize("head_kind", ["claim", "handoff"])
def test_expired_failures_cannot_consume_every_later_drain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, head_kind: str,
) -> None:
    """More failing heads than quota must still rotate behind existing work."""
    scenario = _scenario(
        tmp_path, monkeypatch, initial_state=ContentMutationState.ACCEPTED, same_target=True
    )
    lifecycle = scenario.lifecycle

    async def settle_patch():
        with scenario.application._write_lease_scope():
            return await lifecycle.drain_task(scenario.patch_task.task_id)

    asyncio.run(settle_patch())
    with scenario.application._write_lease_scope():
        # This test isolates scheduling of work after the fixture patch.
        heads = []
        for number in range(3):
            document = PreparedDocument.for_archive(
                Entry(title=f"blocked-{number}", source_type="text", content="synthetic head")
            )
            task = lifecycle.submit_prepared(document)
            if head_kind == "handoff":
                claimed = lifecycle.store.claim_next()
                assert claimed.task_id == task.task_id
                scenario.application.storage_coordinator.archive(
                    document.entry, operation_id=task.operation_id, vector_required=False
                )
                task = lifecycle._record_core_from_journal(claimed)
                lifecycle.store.ensure_handoff(task.operation_id)
            heads.append(task.task_id)
        target = lifecycle.submit_prepared(PreparedDocument.for_delete(scenario.delete_target))
    with sqlite3.connect(scenario.application.config.db_path) as connection:
        connection.executemany(
            "UPDATE content_mutation_tasks SET created_at = '2000-01-01 00:00:00' WHERE task_id = ?",
            [(task_id,) for task_id in heads] + [(target.task_id,)],
        )

    def fail(*args, **kwargs):
        raise PKVRuntimeError(ErrorCode.STORAGE_PRIMARY_FAILED, "synthetic blocked head")

    if head_kind == "claim":
        monkeypatch.setattr(scenario.application.storage_coordinator, "archive", fail)
    else:
        monkeypatch.setattr("src.application.r4_lifecycle.publish_embedding_nonready_binding", fail)

    async def bounded_drain():
        with scenario.application._write_lease_scope():
            return await lifecycle.recover_and_drain(max_tasks=2)

    first = asyncio.run(bounded_drain())
    assert len(first) == 2
    assert scenario.application.sqlite_store.query_by_id(scenario.delete_target) is not None
    # Expire both failures deterministically.  Their next eligibility remains
    # later than the already queued target; elapsed backoff must not reset FIFO.
    with sqlite3.connect(scenario.application.config.db_path) as connection:
        connection.execute(
            "UPDATE content_mutation_tasks SET not_before = '2001-01-01 00:00:00' "
            "WHERE not_before IS NOT NULL"
        )
    second = asyncio.run(bounded_drain())
    assert len(second) == 2
    assert scenario.application.sqlite_store.query_by_id(scenario.delete_target) is None
    assert lifecycle.store.get_task(target.task_id).state in {
        ContentMutationState.COMPLETED, ContentMutationState.AI_HANDOFF_PENDING,
    }
    assert scenario.provider_calls == []


@pytest.mark.parametrize("boundary", ["processing_expired", "core_committed", "proof_before_journal"])
def test_sync_delete_recovers_patch_commit_without_reapplying(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str,
) -> None:
    scenario = _scenario(
        tmp_path, monkeypatch, initial_state=ContentMutationState.ACCEPTED, same_target=True
    )
    with scenario.application._write_lease_scope():
        claimed = scenario.lifecycle.store.claim_next()
        coordinator = scenario.application.storage_coordinator
        with monkeypatch.context() as fault:
            if boundary == "proof_before_journal":
                write_record = coordinator.journal.write

                def lose_commit_checkpoint(operation_id, payload):
                    if payload.get("core_committed") is not True:
                        return write_record(operation_id, payload)

                fault.setattr(coordinator.journal, "write", lose_commit_checkpoint)
            operation = coordinator.apply_ai_patch(scenario.patch, operation_id=claimed.operation_id)
        assert operation.core_committed
        if boundary == "core_committed":
            scenario.lifecycle._record_core_from_journal(claimed)
        else:
            with sqlite3.connect(scenario.application.config.db_path) as connection:
                connection.execute(
                    "UPDATE content_mutation_tasks SET claimed_until = '2000-01-01 00:00:00' "
                    "WHERE task_id = ?", (claimed.task_id,),
                )

    def forbidden_reapply(*args, **kwargs):
        raise AssertionError("a committed patch must recover its proof, never reapply")

    monkeypatch.setattr(scenario.application.storage_coordinator, "apply_ai_patch", forbidden_reapply)
    result = _drain(scenario, "sync", PreparedDocument.for_delete(scenario.delete_target))
    assert result.core_committed
    assert scenario.application.sqlite_store.query_by_id(scenario.delete_target) is None
    recovered = scenario.lifecycle.store.get_task(claimed.task_id)
    assert recovered.state is ContentMutationState.COMPLETED
    assert recovered.claim_token is None and recovered.claimed_until is None
    if boundary != "core_committed":
        assert recovered.owner_fence > claimed.owner_fence
    assert _patch_proof_count(scenario) == 1
    with scenario.application._write_lease_scope(), pytest.raises(PKVRuntimeError):
        scenario.lifecycle.store.mark_retry_required(
            claimed.task_id, claim_token=claimed.claim_token, owner_fence=claimed.owner_fence,
            error_code="late_worker",
        )
    assert scenario.provider_calls == []


def test_unverifiable_recovery_preserves_commit_and_independent_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = _scenario(
        tmp_path, monkeypatch, initial_state=ContentMutationState.ACCEPTED, same_target=False
    )
    lifecycle = scenario.lifecycle
    with scenario.application._write_lease_scope():
        claimed = lifecycle.store.claim_next()
        scenario.application.storage_coordinator.apply_ai_patch(
            scenario.patch, operation_id=claimed.operation_id
        )
        lifecycle._record_core_from_journal(claimed)
    original_details = lifecycle._journal_core_details
    monkeypatch.setattr(
        lifecycle, "_journal_core_details",
        lambda task: None if task.task_id == claimed.task_id else original_details(task),
    )
    result = _drain(scenario, "sync", PreparedDocument.for_delete(scenario.delete_target))
    assert result.core_committed
    blocked = lifecycle.store.get_task(claimed.task_id)
    assert blocked.state is ContentMutationState.CORE_COMMITTED
    assert blocked.not_before is not None and blocked.last_error_code is not None
    assert R4ContentLifecycle(scenario.application)._result_for(blocked).core_committed
    assert scenario.application.sqlite_store.query_by_id(scenario.delete_target) is None
    assert scenario.application.sqlite_store.query_by_id(scenario.patch.target_knowledge_id) is not None
    assert _patch_proof_count(scenario) == 1 and scenario.provider_calls == []


def test_expired_backoff_during_one_drain_does_not_repeat_a_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = _scenario(
        tmp_path, monkeypatch, initial_state=ContentMutationState.ACCEPTED, same_target=False
    )
    with scenario.application._write_lease_scope():
        coordinator = scenario.application.storage_coordinator
    calls = []

    def fail(*args, **kwargs):
        calls.append(kwargs["operation_id"])
        raise OSError("synthetic local failure")

    original_retry = ContentLifecycleStore.mark_retry_required

    def immediately_due(self, *args, **kwargs):
        task = original_retry(self, *args, **kwargs)
        # A deterministic stand-in for a long drain crossing not_before.
        with sqlite3.connect(scenario.application.config.db_path) as connection:
            connection.execute(
                "UPDATE content_mutation_tasks SET not_before = '2000-01-01 00:00:00' WHERE task_id = ?",
                (task.task_id,),
            )
        return task

    monkeypatch.setattr(coordinator, "apply_ai_patch", fail)
    monkeypatch.setattr(ContentLifecycleStore, "mark_retry_required", immediately_due)
    result = _drain(scenario, "sync", PreparedDocument.for_delete(scenario.delete_target))
    assert result.core_committed
    assert calls == [scenario.patch_task.operation_id]
    assert scenario.lifecycle.store.get_task(scenario.patch_task.task_id).attempt_count == 1
    assert scenario.provider_calls == []


@pytest.mark.parametrize("blocked_kind", ["journal", "repair_result"])
def test_uncertain_patch_blocks_dependents_but_not_independent_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, blocked_kind: str,
) -> None:
    scenario = _scenario(
        tmp_path, monkeypatch, initial_state=ContentMutationState.ACCEPTED, same_target=False
    )
    with scenario.application._write_lease_scope():
        coordinator = scenario.application.storage_coordinator
        if blocked_kind == "journal":
            claimed = scenario.lifecycle.store.claim_next()
            coordinator.journal.write(claimed.operation_id, {
                "operation_id": claimed.operation_id, "action": "apply_ai_patch",
                "status": "in_progress", "stage": "primary_committed",
                "checkpoint": "patch_planned", "core_committed": False,
            })
        dependent = scenario.lifecycle.submit_prepared(
            PreparedDocument.for_delete(scenario.patch.target_knowledge_id)
        )
    calls = []

    def uncertain_patch(*args, **kwargs):
        calls.append(kwargs["operation_id"])
        return StorageOperationResult(
            kwargs["operation_id"], "apply_ai_patch", OperationStatus.REPAIR_REQUIRED,
            StorageStage.COMPENSATING,
            errors=({"code": "storage_repair_required"},),
        )

    monkeypatch.setattr(coordinator, "apply_ai_patch", uncertain_patch)
    result = _drain(scenario, "sync", PreparedDocument.for_delete(scenario.delete_target))
    assert result.core_committed
    blocked = scenario.lifecycle.store.get_task(scenario.patch_task.task_id)
    assert blocked.last_error_code == "storage_repair_required"
    if blocked_kind == "journal":
        assert calls == []
        assert blocked.state is ContentMutationState.PROCESSING and blocked.not_before is not None
    else:
        assert calls == [scenario.patch_task.operation_id]
        assert blocked.state is ContentMutationState.REPAIR_REQUIRED
    assert scenario.lifecycle.store.get_task(dependent.task_id).attempt_count == 0
    assert scenario.application.sqlite_store.query_by_id(scenario.patch.target_knowledge_id) is not None
    assert scenario.application.sqlite_store.query_by_id(scenario.delete_target) is None
    assert _patch_proof_count(scenario) == 0 and scenario.provider_calls == []
    with pytest.raises(PKVRuntimeError) as blocked_delete:
        KnowledgeKernel._from_application(scenario.application).delete_entry(
            scenario.patch.target_knowledge_id
        )
    assert blocked_delete.value.code is ErrorCode.REPAIR_REQUIRED
    assert scenario.lifecycle.store.get_task(dependent.task_id).state is ContentMutationState.ACCEPTED
    assert scenario.application.sqlite_store.query_by_id(scenario.patch.target_knowledge_id) is not None


def test_claim_action_filter_does_not_bypass_same_target_predecessor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = _scenario(
        tmp_path, monkeypatch, initial_state=ContentMutationState.ACCEPTED, same_target=False
    )
    with scenario.application._write_lease_scope():
        dependent = scenario.lifecycle.submit_prepared(
            PreparedDocument.for_delete(scenario.patch.target_knowledge_id)
        )
        independent = scenario.lifecycle.submit_prepared(
            PreparedDocument.for_delete(scenario.delete_target)
        )
        # All three enqueue times tie, and the dependent UUID sorts before
        # its predecessor.  Dependency order must come from insertion order.
        with sqlite3.connect(scenario.application.config.db_path) as connection:
            connection.executemany(
                "UPDATE content_mutation_tasks SET created_at = '2000-01-01 00:00:00' WHERE task_id = ?",
                [(task_id,) for task_id in (
                    scenario.patch_task.task_id, dependent.task_id, independent.task_id
                )],
            )
            connection.execute(
                "UPDATE content_mutation_tasks SET task_id = ? WHERE task_id = ?",
                ("0" * 32, dependent.task_id),
            )
        claimed = scenario.lifecycle.store.claim_next(
            actions=(ContentMutationAction.DELETE,),
            excluded_task_ids=(scenario.patch_task.task_id,),
        )
    assert claimed.task_id == independent.task_id
    assert scenario.lifecycle.store.get_task("0" * 32).attempt_count == 0
    assert scenario.lifecycle.store.get_task(scenario.patch_task.task_id).attempt_count == 0


@pytest.mark.parametrize("q2_state", ["retry_required", "processing"])
@pytest.mark.parametrize("patch_present", [True, False], ids=["complete_patch", "missing_patch"])
def test_local_q1_work_never_resumes_pending_or_unknown_q2_attempts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, q2_state: str, patch_present: bool,
) -> None:
    scenario = _scenario(
        tmp_path, monkeypatch, initial_state=ContentMutationState.ACCEPTED, same_target=False
    )
    with scenario.application._write_lease_scope():
        with sqlite3.connect(scenario.application.config.db_path) as connection:
            # A processing claim models an external attempt with no completed
            # outcome yet; neither this nor retry_required licenses Q1 to run Q2.
            connection.execute(
                "UPDATE ai_derivation_tasks SET state = ?, attempt_count = 1, owner_fence = 1, "
                "claim_token = ?, claimed_until = ?, last_error_code = ? WHERE task_id = ?",
                (
                    q2_state, "e" * 32 if q2_state == "processing" else None,
                    "2099-01-01 00:00:00" if q2_state == "processing" else None,
                    None if q2_state == "processing" else "provider_unavailable",
                    scenario.patch.derivation_task_id,
                ),
            )
            before = connection.execute(
                "SELECT * FROM ai_derivation_tasks WHERE task_id = ?",
                (scenario.patch.derivation_task_id,),
            ).fetchone()
        if not patch_present:
            (
                scenario.application.config.layout.runtime_state_dir
                / "r4" / "patches" / f"{scenario.patch.patch_id}.json"
            ).unlink()
    result = _drain(scenario, "sync", PreparedDocument.for_delete(scenario.delete_target))
    assert result.core_committed
    patch_task = scenario.lifecycle.store.get_task(scenario.patch_task.task_id)
    if patch_present:
        assert patch_task.state is ContentMutationState.COMPLETED
        assert _patch_proof_count(scenario) == 1
    else:
        assert patch_task.state is ContentMutationState.RETRY_REQUIRED
        assert patch_task.last_error_code == ErrorCode.REPAIR_REQUIRED.value
        assert patch_task.not_before is not None and _patch_proof_count(scenario) == 0
        with pytest.raises(PKVRuntimeError) as blocked:
            KnowledgeKernel._from_application(scenario.application).delete_entry(
                scenario.patch.target_knowledge_id
            )
        assert blocked.value.code is ErrorCode.REPAIR_REQUIRED
    with sqlite3.connect(scenario.application.config.db_path) as connection:
        after = connection.execute(
            "SELECT * FROM ai_derivation_tasks WHERE task_id = ?",
            (scenario.patch.derivation_task_id,),
        ).fetchone()
        assert connection.execute("SELECT COUNT(*) FROM ai_derivation_usage").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM ai_derivation_reservations").fetchone() == (0,)
    assert after == before
    assert scenario.provider_calls == []
