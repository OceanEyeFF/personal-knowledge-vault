"""W1 R-07 red contracts for Q1 action completeness and bounded progress.

These are in-process, persistent-state reopen tests, not the new-OS-process
acceptance required by Q1-01.  Real spools, ledgers, Markdown, SQLite and the
Kernel delete port are exercised; no product behavior is replaced by a fake.
The current implementation is expected to fail the progress oracles normally,
without xfail/skip, until W2 fixes the synchronous Q1 consumer.
"""

from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from src.application import KnowledgeApplication
from src.application.r4_lifecycle import R4ContentLifecycle
from src.kernel.facade import KnowledgeKernel
from src.runtime.errors import ErrorCode, PKVRuntimeError
from src.storage.content_lifecycle import (
    ContentMutationState,
    ContentMutationTask,
    PreparedDocument,
)
from src.storage.derivation_patch import DerivationPatch, DerivationPatchSpool
from src.storage.markdown_store import Entry
from src.utils.config import Config
from tests.unit.test_r4_content_lifecycle import _configured


@dataclass(frozen=True)
class _Scenario:
    application: KnowledgeApplication
    lifecycle: R4ContentLifecycle
    patch: DerivationPatch
    patch_task: ContentMutationTask
    delete_target: int
    provider_calls: list[str]


def _guard_providers(
    application: KnowledgeApplication,
    monkeypatch: pytest.MonkeyPatch,
    calls: list[str],
) -> None:
    def forbidden_factory(name: str):
        def construct(*args: Any, **kwargs: Any) -> Any:
            calls.append(name)
            raise AssertionError("Q1 local work must not construct a Provider")

        return construct

    monkeypatch.setattr(application, "_create_embedder", forbidden_factory("embedding"))
    monkeypatch.setattr(application, "_create_deepseek_client", forbidden_factory("llm"))


async def _archive(lifecycle: R4ContentLifecycle, title: str) -> Any:
    with lifecycle._application._write_lease_scope():
        return await lifecycle.submit_and_drain(
            PreparedDocument.for_archive(
                Entry(title=title, source_type="text", content=f"Synthetic {title} body")
            )
        )


def _scenario(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    initial_state: ContentMutationState,
    same_target: bool,
) -> _Scenario:
    config = _configured(tmp_path)
    calls: list[str] = []
    application = KnowledgeApplication(config)
    _guard_providers(application, monkeypatch, calls)
    lifecycle = R4ContentLifecycle(application)
    archived = asyncio.run(_archive(lifecycle, "W1 patch target"))
    assert archived.core_committed is True
    source = lifecycle.store.get_derivation_task(archived.task.operation_id)
    assert source is not None
    assert source.target_knowledge_id is not None
    assert source.target_revision_sha256 is not None
    assert source.source_digest is not None
    delete_target = source.target_knowledge_id
    if not same_target:
        # Create this target before enqueueing the patch: fixture preparation
        # must not itself drain the patch from the global FIFO.
        independent = asyncio.run(_archive(lifecycle, "W1 independent delete target"))
        assert independent.core_committed is True
        assert independent.task.target_knowledge_id is not None
        delete_target = independent.task.target_knowledge_id

    patch = DerivationPatch.create(
        derivation_task_id=source.task_id,
        target_knowledge_id=source.target_knowledge_id,
        expected_revision_sha256=source.target_revision_sha256,
        input_digest=source.source_digest,
        summary="Complete synthetic W1 summary. No Provider request is needed.",
        tags=["w1", "synthetic"],
    )
    with application._write_lease_scope():
        reference = DerivationPatchSpool(config.layout).write(patch)
        patch_task = lifecycle.store.enqueue_ai_patch(
            patch_ref=reference.patch_id,
            patch_sha256=reference.payload_sha256,
            target_knowledge_id=patch.target_knowledge_id,
            target_revision_sha256=patch.expected_revision_sha256,
        )
        if initial_state is ContentMutationState.RETRY_REQUIRED:
            claimed = lifecycle.store.claim_next()
            assert claimed is not None and claimed.task_id == patch_task.task_id
            assert claimed.claim_token is not None
            patch_task = lifecycle.store.mark_retry_required(
                claimed.task_id,
                claim_token=claimed.claim_token,
                owner_fence=claimed.owner_fence,
                error_code="worker_restarted",
            )
        # Only fixture ordering is set directly.  The SQL timestamp has second
        # precision; relying on random task IDs or sleeps would make the test
        # sometimes place the later delete in front of this earlier patch.
        with sqlite3.connect(config.db_path) as connection:
            connection.execute(
                "UPDATE content_mutation_tasks SET created_at = ? WHERE task_id = ?",
                ("2000-01-01 00:00:00", patch_task.task_id),
            )
    assert patch_task.state is initial_state
    assert calls == []

    # Reopen the persisted graph without a recovery helper that could hide the
    # synchronous consumer defect.  This is intentionally not an OS restart.
    reopened = KnowledgeApplication(Config(layout=config.layout))
    _guard_providers(reopened, monkeypatch, calls)
    reopened_lifecycle = R4ContentLifecycle(reopened)
    persisted = reopened_lifecycle.store.get_task(patch_task.task_id)
    assert persisted is not None and persisted.state is initial_state
    return _Scenario(reopened, reopened_lifecycle, patch, persisted, delete_target, calls)


def _patch_proof_count(scenario: _Scenario) -> int:
    with sqlite3.connect(scenario.application.config.db_path) as connection:
        return connection.execute(
            "SELECT COUNT(*) FROM r4_content_operation_commits WHERE operation_id = ?",
            (scenario.patch_task.operation_id,),
        ).fetchone()[0]


@pytest.mark.parametrize(
    "initial_state",
    [ContentMutationState.ACCEPTED, ContentMutationState.RETRY_REQUIRED],
    ids=["accepted", "retry_required"],
)
@pytest.mark.parametrize("same_target", [True, False], ids=["same_target", "independent_target"])
def test_q1_01_sync_delete_progresses_past_materialized_patch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    initial_state: ContentMutationState,
    same_target: bool,
) -> None:
    """R-07: a valid queued patch must not starve every subsequent delete."""
    scenario = _scenario(
        tmp_path, monkeypatch, initial_state=initial_state, same_target=same_target
    )
    kernel = KnowledgeKernel._from_application(scenario.application)
    errors: list[str] = []
    attempts: list[int] = []
    # With only a patch and this delete eligible, two bounded calls are ample
    # for a bridge that returns after processing one earlier task.  Stop once
    # the requested deletion actually commits; never issue a duplicate then.
    for _ in range(2):
        try:
            kernel.delete_entry(scenario.delete_target)
        except PKVRuntimeError as error:
            errors.append(error.code.value)
            assert error.code is ErrorCode.EMBEDDING_PROCESSING
        current = scenario.lifecycle.store.get_task(scenario.patch_task.task_id)
        assert current is not None
        attempts.append(current.attempt_count)
        if scenario.application.sqlite_store.query_by_id(scenario.delete_target) is None:
            break

    assert scenario.provider_calls == []
    assert scenario.application.sqlite_store.query_by_id(scenario.delete_target) is None, (
        "Q1-01 / R-07: synchronous delete made no bounded progress past a complete patch; "
        f"errors={errors}, patch_attempts={attempts}, patch_state={current.state.value}"
    )
    proof_count = _patch_proof_count(scenario)
    assert proof_count <= 1
    if current.state is ContentMutationState.COMPLETED:
        assert proof_count == 1
        if not same_target:
            patched_row = scenario.application.sqlite_store.query_by_id(
                scenario.patch.target_knowledge_id
            )
            assert patched_row is not None
            assert patched_row["summary_100_words"] == scenario.patch.summary
    elif same_target:
        assert current.state is ContentMutationState.SUPERSEDED
        with sqlite3.connect(scenario.application.config.db_path) as connection:
            assert connection.execute(
                "SELECT COUNT(*) FROM storage_operation_commits "
                "WHERE action = 'delete' AND knowledge_id = ?",
                (scenario.delete_target,),
            ).fetchone()[0] == 1
    else:
        assert current.state is ContentMutationState.RETRY_REQUIRED
        assert current.not_before is not None, "Unfinished patch needs explicit backoff"
        assert current.last_error_code is not None


def test_q1_03_single_drain_does_not_repeat_one_no_progress_task(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R-07: claim/retry state cycling is not useful bounded-drain progress."""
    scenario = _scenario(
        tmp_path,
        monkeypatch,
        initial_state=ContentMutationState.RETRY_REQUIRED,
        same_target=False,
    )
    before_attempts = scenario.patch_task.attempt_count
    with scenario.application._write_lease_scope():
        scenario.lifecycle.submit_and_drain_sync(
            PreparedDocument.for_delete(scenario.delete_target), max_tasks=8
        )
    current = scenario.lifecycle.store.get_task(scenario.patch_task.task_id)
    assert current is not None
    assert scenario.provider_calls == []
    attempts_added = current.attempt_count - before_attempts
    assert attempts_added <= 1, (
        "Q1-03 / R-07: one drain retried the same task without a content commit; "
        f"attempts_added={attempts_added}, patch_state={current.state.value}"
    )


@pytest.mark.parametrize(
    "initial_state",
    [ContentMutationState.ACCEPTED, ContentMutationState.RETRY_REQUIRED],
    ids=["accepted", "retry_required"],
)
def test_q1_02_async_control_applies_same_patch_once_then_sync_delete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    initial_state: ContentMutationState,
) -> None:
    """Green control: the durable patch fixture is legal and locally applicable."""
    scenario = _scenario(
        tmp_path, monkeypatch, initial_state=initial_state, same_target=True
    )

    async def drain_patch():
        with scenario.application._write_lease_scope():
            return await scenario.lifecycle.drain_task(scenario.patch_task.task_id)

    first = asyncio.run(drain_patch())
    repeated = asyncio.run(drain_patch())
    assert first.core_committed is True and repeated.core_committed is True
    assert repeated.task.state is ContentMutationState.COMPLETED
    row = scenario.application.sqlite_store.query_by_id(scenario.patch.target_knowledge_id)
    assert row is not None and row["summary_100_words"] == scenario.patch.summary
    markdown = scenario.application.markdown_store.load(
        scenario.application.markdown_store.gateway.resolve(
            row["file_path"], must_exist=True, require_file=True
        )
    )
    assert markdown.summary_100_words == scenario.patch.summary
    assert _patch_proof_count(scenario) == 1
    result = KnowledgeKernel._from_application(scenario.application).delete_entry(
        scenario.delete_target
    )
    assert result.core_committed is True
    assert scenario.application.sqlite_store.query_by_id(scenario.delete_target) is None
    assert _patch_proof_count(scenario) == 1
    assert scenario.provider_calls == []
