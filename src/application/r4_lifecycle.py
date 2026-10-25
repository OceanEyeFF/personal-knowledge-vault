"""Internal R4 Q1′ lifecycle orchestration.

This module is intentionally below adapters and contains no Q0 crawler or Q2
Provider execution.  R4-A uses deterministic ``PreparedDocument`` payloads to
prove the durable content-submission/handoff contract before R4-B moves ingress
and R4-C/R4-D add scheduling and AI derivation.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from src.runtime.ai_automation_policy import (
    AutomationPolicyState,
    inspect_ai_automation_policy,
)
from src.runtime.embedding_lifecycle import (
    EmbeddingIndexState,
    SQLiteEmbeddingSource,
    publish_embedding_nonready_binding,
)
from src.runtime.errors import ErrorCode, OperationStatus, PKVRuntimeError
from src.storage.content_lifecycle import (
    AIDerivationState,
    ContentAIHandoffState,
    ContentLifecycleStore,
    ContentMutationAction,
    ContentMutationState,
    ContentMutationTask,
    PreparedDocument,
    PreparedDocumentSpool,
)
from src.storage.coordinator import recover_interrupted_operations

if TYPE_CHECKING:
    from src.application.knowledge_application import KnowledgeApplication


_STAGE = "r4_content_lifecycle"
_RETRY_DELAY_SECONDS = 30
_EXECUTABLE_ACTIONS = (
    ContentMutationAction.ARCHIVE,
    ContentMutationAction.DELETE,
    ContentMutationAction.APPLY_AI_PATCH,
)


class R4LifecycleFault(RuntimeError):
    """Deterministic fake-only crash seam used by R4-A characterization tests."""


@dataclass(frozen=True)
class R4Q1Result:
    task: ContentMutationTask
    core_committed: bool
    handoff_state: str | None = None
    derivation_state: str | None = None
    storage_operation: Any | None = None


class R4ContentLifecycle:
    """Application-owned Q1′ submit/drain/recovery bridge.

    The caller must hold the application's R3 writer lease.  All durable
    metadata mutations happen through ``ContentLifecycleStore``; calls into the
    cross-store coordinator use the application's tracked worker bridge so a
    cancellation cannot release the root lease before a Markdown/SQLite commit
    finishes.
    """

    def __init__(
        self,
        application: "KnowledgeApplication",
        *,
        fault_hook: Callable[[str, ContentMutationTask], None] | None = None,
    ) -> None:
        self._application = application
        self._fault_hook = fault_hook

    @property
    def store(self) -> ContentLifecycleStore:
        return ContentLifecycleStore(self._application.config.layout)

    @property
    def spool(self) -> PreparedDocumentSpool:
        return PreparedDocumentSpool(self._application.config.layout)

    def submit_prepared(self, document: PreparedDocument) -> ContentMutationTask:
        """Persist an immutable payload and its accepted Q1′/blocked-Q2 rows."""

        reference = self.spool.write(document)
        return self.store.enqueue_prepared(document, reference)

    async def submit_and_drain(self, document: PreparedDocument) -> R4Q1Result:
        task = self.submit_prepared(document)
        return await self.drain_task(task.task_id)

    def submit_and_drain_sync(
        self,
        document: PreparedDocument,
        *,
        max_tasks: int = 8,
    ) -> R4Q1Result:
        """Synchronous Q1′ bridge for the stable synchronous Kernel delete port.

        The caller already owns the root writer lease, so this path calls the
        synchronous StorageCoordinator directly instead of creating an event
        loop or re-entering the tracked worker helper.  It intentionally remains
        Q1′ code: Kernel only supplies a PreparedDocument and receives the
        resulting established storage envelope.
        """

        self._validate_max_tasks(max_tasks)
        task = self.submit_prepared(document)
        _, blocking = recover_interrupted_operations(
            self._application.storage_coordinator.journal,
            self._application.markdown_store,
            self._application.sqlite_store,
        )
        for candidate in self._drain_candidates(max_tasks=max_tasks, blocking=blocking):
            result = (
                candidate if isinstance(candidate, R4Q1Result)
                else self._execute_claimed_sync(candidate)
            )
            if result.task.task_id == task.task_id:
                return result
        task = self.store.get_task(task.task_id)
        assert task is not None
        return self._waiting_result(task)

    def _waiting_result(self, task: ContentMutationTask) -> R4Q1Result:
        blocker = self.store.get_target_blocker(task.task_id)
        if blocker is not None and (
            blocker.state is ContentMutationState.REPAIR_REQUIRED
            or blocker.last_error_code in {
                ErrorCode.REPAIR_REQUIRED.value, ErrorCode.STORAGE_REPAIR_REQUIRED.value,
            }
        ):
            raise PKVRuntimeError(
                ErrorCode.REPAIR_REQUIRED,
                "目标的前置内容操作需要修复；当前任务已保留，尚未提交。",
                stage=_STAGE,
                recoverable=True,
            )
        return self._result_for(task)

    @staticmethod
    def _validate_max_tasks(max_tasks: int) -> None:
        if type(max_tasks) is not int or max_tasks <= 0:
            raise ValueError("max_tasks 必须是正整数")

    def _drain_candidates(
        self, *, max_tasks: int, blocking: list[dict[str, Any]],
    ) -> Iterator[ContentMutationTask | R4Q1Result]:
        """Recover predecessors and claim each task at most once in this drain.

        Recovery is local journal/state work.  Executing a yielded claim still
        belongs to the bridge's synchronous or tracked async worker.  The
        exclusion set also protects a long drain whose durable backoff expires.
        """

        self._validate_max_tasks(max_tasks)
        attempted: set[str] = set()
        blocked_operations = {record.get("operation_id") for record in blocking}
        while len(attempted) < max_tasks:
            selected = self.store.next_drain_task(
                actions=_EXECUTABLE_ACTIONS, excluded_task_ids=attempted
            )
            if selected is None:
                return
            # A fresh claim is distinguishable from an old processing task by
            # its identity in the current selection; the store returns that
            # bit explicitly so recovery never steals this drain's own claim.
            task, claimed = selected
            attempted.add(task.task_id)
            if task.operation_id in blocked_operations:
                if claimed:
                    task = self.store.mark_repair_required(
                        task.task_id, claim_token=task.claim_token, owner_fence=task.owner_fence,
                        error_code=ErrorCode.STORAGE_REPAIR_REQUIRED.value,
                    )
                else:
                    task = self.store.mark_recovery_blocked(
                        task.task_id, error_code=ErrorCode.STORAGE_REPAIR_REQUIRED.value,
                    )
                yield self._result_for(task)
                continue
            if claimed:
                yield task
                continue
            try:
                recovered = self._recover_one_sync(task)
            except (PKVRuntimeError, OSError):
                blocked = self.store.mark_recovery_blocked(
                    task.task_id,
                    error_code=ErrorCode.REPAIR_REQUIRED.value,
                )
                recovered = self._result_for(blocked)
            assert recovered is not None
            yield recovered

    async def submit_patch_and_drain(self, patch: Any) -> R4Q1Result:
        """Queue a normalized Q2 patch through the same sole Q1′ writer."""

        from src.storage.derivation_patch import DerivationPatch, DerivationPatchSpool

        if not isinstance(patch, DerivationPatch):
            raise TypeError("patch 必须是 DerivationPatch")
        source_task = self.store.get_derivation_task_by_id(patch.derivation_task_id)
        if (
            source_task is None
            or source_task.target_knowledge_id != patch.target_knowledge_id
            or source_task.target_revision_sha256 != patch.expected_revision_sha256
            or (
                not source_task.patch_applied
                and source_task.source_digest != patch.input_digest
            )
            or (
                source_task.patch_applied
                and source_task.patch_ref != patch.patch_id
            )
        ):
            raise PKVRuntimeError(
                ErrorCode.RUNTIME_PLAN_STALE,
                "DerivationPatch 与来源 Q2 task 的身份或输入不一致。",
                stage=_STAGE,
                recoverable=True,
            )
        reference = DerivationPatchSpool(self._application.config.layout).write(patch)
        task = self.store.get_ai_patch_task(reference.patch_id)
        if task is None:
            task = self.store.enqueue_ai_patch(
                patch_ref=reference.patch_id,
                patch_sha256=reference.payload_sha256,
                target_knowledge_id=patch.target_knowledge_id,
                target_revision_sha256=patch.expected_revision_sha256,
            )
        elif (
            task.patch_sha256 != reference.payload_sha256
            or task.target_knowledge_id != patch.target_knowledge_id
        ):
            raise PKVRuntimeError(
                ErrorCode.RUNTIME_PLAN_STALE,
                "现有 Q1′ DerivationPatch task 与 payload 不一致。",
                stage=_STAGE,
                recoverable=True,
            )
        return await self.drain_task(task.task_id)

    async def recover_and_drain(self, *, max_tasks: int = 8) -> tuple[R4Q1Result, ...]:
        """Idempotently recover journal/task crash boundaries, then drain Q1′.

        There is deliberately no Q2 drain here.  An activated derivation task is
        durable evidence for R4-C/R4-D, but R4-A must prove that no Provider or
        usage work can begin merely because a Q1′ handoff became visible.
        """

        self._validate_max_tasks(max_tasks)
        _, blocking = await self._application._run_tracked_write_worker(
            recover_interrupted_operations,
            self._application.storage_coordinator.journal,
            self._application.markdown_store,
            self._application.sqlite_store,
        )
        # A process can crash after Q1′'s terminal transaction and before its
        # best-effort spool cleanup.  Terminal rows carry the exact private
        # payload digest, so a later permitted lifecycle trigger can sweep only
        # objects no longer needed by recovery.
        with self._application._write_lease_scope():
            for completed in self.store.list_completed_prepared_tasks():
                self._discard_completed_prepared(completed)
        results: list[R4Q1Result] = []
        for candidate in self._drain_candidates(max_tasks=max_tasks, blocking=blocking):
            results.append(
                candidate if isinstance(candidate, R4Q1Result)
                else await self._execute_claimed(candidate)
            )
        return tuple(results)

    async def drain_task(self, task_id: str) -> R4Q1Result:
        """Drain one task by identity, including an interrupted predecessor."""

        task = self.store.get_task(task_id)
        if task is None:
            raise PKVRuntimeError(
                ErrorCode.RUNTIME_PLAN_STALE,
                "需要 drain 的 Q1′ 任务不存在。",
                stage=_STAGE,
                recoverable=True,
            )
        if task.state not in {
            ContentMutationState.ACCEPTED,
            ContentMutationState.RETRY_REQUIRED,
            ContentMutationState.PROCESSING,
            ContentMutationState.CORE_COMMITTED,
            ContentMutationState.AI_HANDOFF_PENDING,
        }:
            self._discard_completed_prepared(task)
            return self._result_for(task)
        _, blocking = await self._application._run_tracked_write_worker(
            recover_interrupted_operations,
            self._application.storage_coordinator.journal,
            self._application.markdown_store,
            self._application.sqlite_store,
        )
        for candidate in self._drain_candidates(max_tasks=8, blocking=blocking):
            result = (
                candidate if isinstance(candidate, R4Q1Result)
                else await self._execute_claimed(candidate)
            )
            if result.task.task_id == task_id:
                return result
        current = self.store.get_task(task_id)
        assert current is not None
        return self._waiting_result(current)

    async def _recover_one(self, task: ContentMutationTask) -> R4Q1Result | None:
        """Advance only states already provable from task + operation journal."""
        return self._recover_one_sync(task)

    async def _execute_claimed(self, task: ContentMutationTask) -> R4Q1Result:
        # Both bridges execute the same action validation, operation proof and
        # completion logic.  The tracked worker retains the root lease until
        # the whole local commit settles even if its async caller is cancelled.
        return await self._application._run_tracked_write_worker(
            self._execute_claimed_sync, task
        )

    def _recover_one_sync(self, task: ContentMutationTask) -> R4Q1Result | None:
        """Synchronous equivalent of the journal-only recovery portion."""

        if task.state is ContentMutationState.PROCESSING:
            if self._journal_proves_core_commit(task):
                task = self.store.reclaim_processing_task(task.task_id)
                task = self._record_core_from_journal(task)
                self._fault("core_committed", task)
            else:
                task = self.store.recover_processing_task(task.task_id)
                return self._result_for(task)
        elif task.state is ContentMutationState.CORE_COMMITTED:
            task = self._record_core_from_journal(task)
            self._fault("core_committed", task)
        if (
            task.action is ContentMutationAction.APPLY_AI_PATCH
            and task.state is ContentMutationState.CORE_COMMITTED
        ):
            return self._result_for(self.store.complete_patch_task(task.task_id))
        if task.state in {
            ContentMutationState.CORE_COMMITTED,
            ContentMutationState.AI_HANDOFF_PENDING,
        }:
            return self._advance_handoff(task)
        return None

    def _execute_claimed_sync(self, task: ContentMutationTask) -> R4Q1Result:
        """Execute one Q1′ claim without an async worker bridge."""

        if task.state is not ContentMutationState.PROCESSING or not task.claim_token:
            raise PKVRuntimeError(
                ErrorCode.RUNTIME_PLAN_STALE,
                "Q1′ 同步 drain 需要当前 processing claim。",
                stage=_STAGE,
                recoverable=True,
            )
        try:
            document = (
                self.spool.read(task.prepared_reference)
                if task.prepared_reference
                else None
            )
            if task.action is ContentMutationAction.ARCHIVE:
                if document is None or document.entry is None:
                    raise PKVRuntimeError(
                        ErrorCode.REPAIR_REQUIRED,
                        "archive Q1′ 任务缺少可验证 PreparedDocument。",
                        stage=_STAGE,
                        recoverable=True,
                    )
                operation = self._application.storage_coordinator.archive(
                    document.entry,
                    operation_id=task.operation_id,
                    vector_required=False,
                )
            elif task.action is ContentMutationAction.DELETE:
                if document is None or task.target_knowledge_id is None:
                    raise PKVRuntimeError(
                        ErrorCode.REPAIR_REQUIRED,
                        "delete Q1′ 任务缺少可验证目标。",
                        stage=_STAGE,
                        recoverable=True,
                    )
                operation = self._application.storage_coordinator.delete(
                    task.target_knowledge_id,
                    operation_id=task.operation_id,
                    vector_operation=None,
                )
            elif task.action is ContentMutationAction.APPLY_AI_PATCH:
                if (
                    task.patch_ref is None
                    or task.patch_sha256 is None
                    or task.target_knowledge_id is None
                    or task.target_revision_sha256 is None
                ):
                    raise PKVRuntimeError(
                        ErrorCode.REPAIR_REQUIRED,
                        "apply_ai_patch Q1′ task 缺少可验证 DerivationPatch。",
                        stage=_STAGE,
                        recoverable=True,
                    )
                from src.storage.derivation_patch import (
                    DerivationPatchReference,
                    DerivationPatchSpool,
                )

                patch = DerivationPatchSpool(self._application.config.layout).read(
                    DerivationPatchReference(task.patch_ref, task.patch_sha256)
                )
                if (
                    patch.target_knowledge_id != task.target_knowledge_id
                    or patch.expected_revision_sha256 != task.target_revision_sha256
                ):
                    raise PKVRuntimeError(
                        ErrorCode.RUNTIME_PLAN_STALE,
                        "DerivationPatch 与 Q1′ task target revision 不一致。",
                        stage=_STAGE,
                        recoverable=True,
                    )
                operation = self._application.storage_coordinator.apply_ai_patch(
                    patch, operation_id=task.operation_id
                )
            else:
                raise PKVRuntimeError(
                    ErrorCode.RUNTIME_PLAN_STALE,
                    "Q1′ action 不受支持。",
                    stage=_STAGE,
                    recoverable=True,
                )
        except PKVRuntimeError as error:
            if self._journal_proves_core_commit(task):
                task = self._record_core_from_journal(task)
                self._fault("core_committed", task)
                if task.action is ContentMutationAction.APPLY_AI_PATCH:
                    return self._result_for(self.store.complete_patch_task(task.task_id))
                return self._result_with_operation(self._advance_handoff(task), operation=None)
            return self._result_for(
                self.store.mark_retry_required(
                    task.task_id,
                    claim_token=task.claim_token,
                    owner_fence=task.owner_fence,
                    error_code=error.code.value,
                    retry_delay_seconds=_RETRY_DELAY_SECONDS,
                )
            )
        except Exception:
            if self._journal_proves_core_commit(task):
                task = self._record_core_from_journal(task)
                self._fault("core_committed", task)
                if task.action is ContentMutationAction.APPLY_AI_PATCH:
                    return self._result_for(self.store.complete_patch_task(task.task_id))
                return self._result_with_operation(self._advance_handoff(task), operation=None)
            return self._result_for(
                self.store.mark_retry_required(
                    task.task_id,
                    claim_token=task.claim_token,
                    owner_fence=task.owner_fence,
                    error_code=ErrorCode.STORAGE_PRIMARY_FAILED.value,
                    retry_delay_seconds=_RETRY_DELAY_SECONDS,
                )
            )
        if not operation.core_committed:
            finish_claim = (
                self.store.mark_repair_required
                if operation.status is OperationStatus.REPAIR_REQUIRED
                else self.store.mark_rejected
            )
            return self._result_with_operation(
                self._result_for(
                    finish_claim(
                        task.task_id,
                        claim_token=task.claim_token,
                        owner_fence=task.owner_fence,
                        error_code=(
                            operation.errors[0]["code"]
                            if operation.errors
                            else ErrorCode.STORAGE_PRIMARY_FAILED.value
                        ),
                    )
                ),
                operation,
            )
        if operation.operation_id != task.operation_id:
            raise PKVRuntimeError(
                ErrorCode.REPAIR_REQUIRED,
                "StorageCoordinator 返回了不匹配的 operation identity。",
                stage=_STAGE,
                recoverable=True,
            )
        task = self._record_core_from_journal(task)
        self._fault("core_committed", task)
        if task.action is ContentMutationAction.APPLY_AI_PATCH:
            return self._result_with_operation(
                self._result_for(self.store.complete_patch_task(task.task_id)),
                operation,
            )
        return self._result_with_operation(self._advance_handoff(task), operation)

    @staticmethod
    def _result_with_operation(result: R4Q1Result, operation: Any | None) -> R4Q1Result:
        return R4Q1Result(
            result.task,
            core_committed=result.core_committed,
            handoff_state=result.handoff_state,
            derivation_state=result.derivation_state,
            storage_operation=operation,
        )

    def _advance_handoff(self, task: ContentMutationTask) -> R4Q1Result:
        """Complete Q1′→Q2 durable handoff without claiming/doing Q2 work."""

        if (
            task.action is ContentMutationAction.DELETE
            and task.target_knowledge_id is not None
        ):
            # A delete is a content revision boundary too.  Any prior archive
            # derivation for the removed target loses its claim/fence before it
            # can publish a late patch or generation outcome.
            self.store.supersede_derivations_for_target(
                task.target_knowledge_id,
                excluding_operation_id=task.operation_id,
            )
        handoff = self.store.ensure_handoff(task.operation_id)
        self._fault("handoff_recorded", task)
        source = SQLiteEmbeddingSource()
        try:
            policy = inspect_ai_automation_policy(self._application.config)
            if policy.state is AutomationPolicyState.READY:
                binding_state = EmbeddingIndexState.PROCESSING
                derivation_state = AIDerivationState.PENDING
            elif policy.state in {
                AutomationPolicyState.AUTHORIZATION_REQUIRED,
                AutomationPolicyState.INVALID,
            }:
                binding_state = EmbeddingIndexState.AUTHORIZATION_REQUIRED
                derivation_state = AIDerivationState.AUTHORIZATION_REQUIRED
            else:
                # Disabled automation still needs to revoke a stale ready
                # generation, but it cannot implicitly start a Provider.
                binding_state = EmbeddingIndexState.REBUILD_REQUIRED
                derivation_state = AIDerivationState.AUTHORIZATION_REQUIRED
            if handoff.state in {
                ContentAIHandoffState.PENDING,
                ContentAIHandoffState.RETRY_REQUIRED,
            }:
                binding = publish_embedding_nonready_binding(
                    self._application.config,
                    state=binding_state,
                    source=source,
                )
                if binding.source is None:
                    raise PKVRuntimeError(
                        ErrorCode.REPAIR_REQUIRED,
                        "Embedding non-ready binding 缺少 source 摘要。",
                        stage=_STAGE,
                        recoverable=True,
                    )
                handoff = self.store.mark_binding_published(
                    task.operation_id,
                    source_digest=binding.source.digest,
                    binding_state=binding_state.value,
                )
                self._fault("binding_published", task)
            if handoff.state is ContentAIHandoffState.BINDING_PUBLISHED:
                if handoff.source_digest is None:
                    raise PKVRuntimeError(
                        ErrorCode.REPAIR_REQUIRED,
                        "已发布的 handoff 缺少 source digest。",
                        stage=_STAGE,
                        recoverable=True,
                    )
                derivation = self.store.activate_derivation(
                    task.operation_id,
                    source_digest=handoff.source_digest,
                    policy_fingerprint=policy.policy_fingerprint,
                    state=derivation_state,
                )
                handoff = self.store.get_handoff(task.operation_id) or handoff
                self._fault("q2_activated", task)
                completed_task = self.store.get_task(task.task_id) or task
                self._discard_completed_prepared(completed_task)
                return R4Q1Result(
                    completed_task,
                    core_committed=True,
                    handoff_state=handoff.state.value,
                    derivation_state=derivation.state.value,
                )
        except PKVRuntimeError as error:
            handoff = self.store.mark_handoff_retry(
                task.operation_id,
                error_code=error.code.value,
                retry_delay_seconds=_RETRY_DELAY_SECONDS,
            )
            derivation = self.store.get_derivation_task(task.operation_id)
            return R4Q1Result(
                self.store.get_task(task.task_id) or task,
                core_committed=True,
                handoff_state=handoff.state.value,
                derivation_state=derivation.state.value if derivation is not None else None,
            )
        derivation = self.store.get_derivation_task(task.operation_id)
        completed_task = self.store.get_task(task.task_id) or task
        self._discard_completed_prepared(completed_task)
        return R4Q1Result(
            completed_task,
            core_committed=True,
            handoff_state=handoff.state.value,
            derivation_state=derivation.state.value if derivation is not None else None,
        )

    def _journal_proves_core_commit(self, task: ContentMutationTask) -> bool:
        return self._journal_core_details(task) is not None

    def _discard_completed_prepared(self, task: ContentMutationTask) -> None:
        """Release private Q0 body only after terminal Q1′ handoff proof."""

        if (
            task.state is ContentMutationState.COMPLETED
            and task.prepared_reference is not None
        ):
            self.spool.discard(task.prepared_reference)

    def _record_core_from_journal(self, task: ContentMutationTask) -> ContentMutationTask:
        details = self._journal_core_details(task)
        if details is None:
            raise PKVRuntimeError(
                ErrorCode.RUNTIME_PLAN_STALE,
                "Q1′ operation journal 未提供可验证的 core commit 详情。",
                stage=_STAGE,
                recoverable=True,
            )
        knowledge_id, projection_sha256 = details
        return self.store.record_core_committed(
            task.task_id,
            operation_id=task.operation_id,
            knowledge_id=knowledge_id,
            # apply_ai_patch keeps its *expected previous* revision in the Q1′
            # task so a retry can still validate the immutable patch input.  The
            # journal remains the authority for its resulting revision/proof.
            target_revision_sha256=(
                None
                if task.action is ContentMutationAction.APPLY_AI_PATCH
                else projection_sha256
            ),
            claim_token=task.claim_token,
            owner_fence=(task.owner_fence if task.claim_token is not None else None),
        )

    def _journal_core_details(
        self, task: ContentMutationTask
    ) -> tuple[int, str] | None:
        try:
            record = self._application.storage_coordinator.journal.read(task.operation_id)
        except (OSError, ValueError, PKVRuntimeError):
            return None
        knowledge_id = record.get("knowledge_id")
        projection_sha256 = record.get("projection_sha256")
        if (
            record.get("operation_id") != task.operation_id
            or record.get("action") != task.action.value
            or record.get("core_committed") is not True
            or type(knowledge_id) is not int
            or isinstance(knowledge_id, bool)
            or knowledge_id <= 0
            or not isinstance(projection_sha256, str)
            or len(projection_sha256) != 64
        ):
            return None
        return knowledge_id, projection_sha256

    def _fault(self, phase: str, task: ContentMutationTask) -> None:
        if self._fault_hook is not None:
            self._fault_hook(phase, task)

    def _result_for(self, task: ContentMutationTask) -> R4Q1Result:
        handoff = self.store.get_handoff(task.operation_id)
        derivation = self.store.get_derivation_task(task.operation_id)
        return R4Q1Result(
            task,
            core_committed=task.state
            in {
                ContentMutationState.CORE_COMMITTED,
                ContentMutationState.AI_HANDOFF_PENDING,
                ContentMutationState.COMPLETED,
            },
            handoff_state=handoff.state.value if handoff is not None else None,
            derivation_state=derivation.state.value if derivation is not None else None,
        )


__all__ = ["R4ContentLifecycle", "R4LifecycleFault", "R4Q1Result"]
