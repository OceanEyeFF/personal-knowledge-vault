"""R4 Q2 fenced AI derivation orchestration.

Q2 owns no Markdown, SQLite-content, or flat-vector writer.  It turns approved
Provider output into a private :class:`DerivationPatch`, asks Q1′ to apply that
patch, and publishes only a complete Embedding generation.  Every potentially
billable Provider construction is ordered after durable policy/source/budget
checks and a fenced reservation.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Callable, Sequence
from zoneinfo import ZoneInfo

from src.application.r4_lifecycle import R4ContentLifecycle
from src.runtime.ai_automation_policy import (
    AutomationPolicyInspection,
    AutomationPolicyState,
    TokenUsage,
    inspect_ai_automation_policy,
)
from src.runtime.embedding_lifecycle import (
    EmbeddingIndexState,
    PreChunkedEmbeddingAdapter,
    SQLiteEmbeddingSource,
    confirm_embedding_rebuild,
    execute_embedding_rebuild,
    inspect_embedding_index,
    plan_embedding_rebuild,
    publish_embedding_nonready_binding,
)
from src.runtime.errors import ErrorCode, PKVRuntimeError
from src.runtime.price_cards import PriceCard, resolve_price_card
from src.storage.content_lifecycle import (
    AIDerivationState,
    AIDerivationTask,
    ContentLifecycleStore,
    ContentMutationAction,
    ContentMutationState,
)
from src.storage.derivation_ledger import DerivationLedger, DerivationReservation
from src.storage.derivation_patch import (
    DerivationPatch,
    DerivationPatchReference,
    DerivationPatchSpool,
)
from src.storage.sqlite_store import row_projection_sha256

if TYPE_CHECKING:
    from src.application.knowledge_application import KnowledgeApplication
    from src.runtime.embedding_lifecycle import CapturedEmbeddingSource


_STAGE = "r4_derivation_lifecycle"
_Q2_LEASE_SECONDS = 900


@dataclass(frozen=True)
class R4Q2Result:
    """Small internal result used by Application status projection and tests."""

    task: AIDerivationTask | None
    provider_started: bool = False
    error: PKVRuntimeError | None = None


@dataclass(frozen=True)
class _UsageCapture:
    usage: TokenUsage | None
    complete: bool


class _MeteredPreChunkedEmbeddingAdapter:
    """Prefetch Provider vectors before the generation writer lease.

    ``execute_embedding_rebuild`` owns its own short-lived root lease because it
    writes a staged generation and atomically flips the binding.  Its historical
    embedder protocol is invoked from inside that function, so passing a live
    adapter would accidentally perform paid network calls while the root lease
    is held.  This adapter instead fetches every immutable captured record first
    and then replays those vectors to the local generation writer.
    """

    def __init__(self, embedder: Any, captured: "CapturedEmbeddingSource") -> None:
        self._adapter = PreChunkedEmbeddingAdapter(embedder)
        self._client = getattr(embedder, "client", None)
        self._usage_parts: list[TokenUsage] = []
        self._complete = True
        self._records = captured.records
        self._document_vectors: list[Any] = []
        self._chunk_vectors: list[Any] = []
        self._document_cursor = 0
        self._chunk_cursor = 0
        for record in self._records:
            self._document_vectors.append(self._adapter.embed_document(record.content))
            self._capture()
            self._chunk_vectors.append(
                self._adapter.embed_stored_chunks(
                    tuple(chunk for _, chunk in record.chunks)
                )
            )
            self._capture()

    def embed_document(self, text: str) -> Any:
        index = self._document_cursor
        if index >= len(self._records) or text != self._records[index].content:
            raise PKVRuntimeError(
                ErrorCode.RUNTIME_PLAN_STALE,
                "Embedding generation 请求了未预取或已变化的文档内容。",
                stage=_STAGE,
                recoverable=True,
            )
        self._document_cursor += 1
        return self._document_vectors[index]

    def embed_stored_chunks(self, chunks: Sequence[str]) -> Any:
        index = self._chunk_cursor
        expected = (
            tuple(chunk for _, chunk in self._records[index].chunks)
            if index < len(self._records)
            else ()
        )
        if index >= len(self._records) or tuple(chunks) != expected:
            raise PKVRuntimeError(
                ErrorCode.RUNTIME_PLAN_STALE,
                "Embedding generation 请求了未预取或已变化的已存储分块。",
                stage=_STAGE,
                recoverable=True,
            )
        self._chunk_cursor += 1
        return self._chunk_vectors[index]

    def assert_consumed(self) -> None:
        """Prove the local writer replayed exactly the captured Provider work."""

        if (
            self._document_cursor != len(self._records)
            or self._chunk_cursor != len(self._records)
        ):
            raise PKVRuntimeError(
                ErrorCode.RUNTIME_PLAN_STALE,
                "Embedding generation 未完整消费预取向量。",
                stage=_STAGE,
                recoverable=True,
            )

    @property
    def usage_capture(self) -> _UsageCapture:
        if not self._usage_parts:
            return _UsageCapture(None, self._complete)
        return _UsageCapture(
            TokenUsage(
                embedding_input_tokens=sum(
                    usage.embedding_input_tokens or 0 for usage in self._usage_parts
                ),
                source="provider_reported",
            ),
            self._complete,
        )

    def _capture(self) -> None:
        usage = getattr(self._client, "last_usage", None)
        if not isinstance(usage, TokenUsage) or usage.embedding_input_tokens is None:
            self._complete = False
            return
        reported_complete = getattr(self._client, "last_usage_complete", True)
        if reported_complete is not True:
            self._complete = False
        self._usage_parts.append(usage)


class R4DerivationLifecycle:
    """Recover, claim, and execute bounded R4 Q2 work.

    Q2 claims are durable and fenced.  Slow Provider calls occur outside the
    root writer lease; every subsequent durable transition verifies the same
    claim token/fence, so an expired or superseded worker cannot publish a patch
    or a generation.
    """

    def __init__(
        self,
        application: "KnowledgeApplication",
        *,
        q1_lifecycle: R4ContentLifecycle | None = None,
        llm_factory: Callable[[], Any] | None = None,
        embedder_factory: Callable[[], Any] | None = None,
    ) -> None:
        self._application = application
        self._q1 = q1_lifecycle or R4ContentLifecycle(application)
        self._llm_factory = llm_factory or application._create_deepseek_client
        self._embedder_factory = embedder_factory or application._create_embedder

    @property
    def store(self) -> ContentLifecycleStore:
        return ContentLifecycleStore(self._application.config.layout)

    @property
    def ledger(self) -> DerivationLedger:
        return DerivationLedger(self._application.config.layout)

    @property
    def patch_spool(self) -> DerivationPatchSpool:
        return DerivationPatchSpool(self._application.config.layout)

    async def drain_for_operation(self, operation_id: str) -> R4Q2Result:
        task = self.store.get_derivation_task(operation_id)
        if task is None:
            return R4Q2Result(None)
        return await self.drain_task(task.task_id)

    async def recover_and_drain(self, *, max_tasks: int = 8) -> tuple[R4Q2Result, ...]:
        """Perform one bounded recovery pass without introducing a daemon."""

        if type(max_tasks) is not int or max_tasks <= 0:
            raise ValueError("max_tasks 必须是正整数")
        with self._application._write_lease_scope():
            self.store.recover_expired_derivation_claims()
            policy = inspect_ai_automation_policy(self._application.config)
            if policy.retry_max_attempts is not None:
                self.store.mark_retry_exhausted(
                    max_attempts=policy.retry_max_attempts
                )
        results: list[R4Q2Result] = []
        for task in self.store.list_refreshable_derivations():
            if len(results) >= max_tasks:
                break
            results.append(await self.drain_task(task.task_id))
        return tuple(results)

    async def drain_task(self, task_id: str) -> R4Q2Result:
        """Drain one target task, respecting FIFO if an older Q2 task exists."""

        initial = self.store.get_derivation_task_by_id(task_id)
        if initial is None:
            raise PKVRuntimeError(
                ErrorCode.RUNTIME_PLAN_STALE,
                "需要 drain 的 Q2 task 不存在。",
                stage=_STAGE,
                recoverable=True,
            )
        if initial.patch_ref is not None and not initial.patch_applied:
            patch_task = self._q1.store.get_ai_patch_task(initial.patch_ref)
            if patch_task is not None and patch_task.state is not ContentMutationState.COMPLETED:
                with self._application._write_lease_scope():
                    await self._q1.drain_task(patch_task.task_id)
                initial = self.store.get_derivation_task_by_id(task_id) or initial
        refreshed = self._refresh_unclaimed(initial)
        if refreshed.state not in {
            AIDerivationState.PENDING,
            AIDerivationState.RETRY_REQUIRED,
        }:
            return R4Q2Result(refreshed)

        policy = inspect_ai_automation_policy(self._application.config)
        if policy.state is not AutomationPolicyState.READY:
            # ``_refresh_unclaimed`` normally handles this branch.  Recheck at
            # the claim boundary to make config changes fail closed.
            return R4Q2Result(self._pause_for_policy(refreshed, policy))
        assert policy.retry_max_attempts is not None
        with self._application._write_lease_scope():
            self.store.recover_expired_derivation_claims()
            self.store.mark_retry_exhausted(
                max_attempts=policy.retry_max_attempts
            )
            claimed = self.store.claim_next_derivation(
                max_attempts=policy.retry_max_attempts,
                lease_seconds=_Q2_LEASE_SECONDS,
            )
        if claimed is None:
            return R4Q2Result(self.store.get_derivation_task_by_id(task_id))
        result = await self._execute_claimed(claimed, policy)
        if claimed.task_id == task_id:
            return result
        return R4Q2Result(self.store.get_derivation_task_by_id(task_id))

    def _refresh_unclaimed(self, task: AIDerivationTask) -> AIDerivationTask:
        """Recheck source/policy before a task can become Provider-eligible."""

        if task.state in {AIDerivationState.COMPLETED, AIDerivationState.SUPERSEDED}:
            return task
        if task.state is AIDerivationState.PROCESSING:
            return task
        policy = inspect_ai_automation_policy(self._application.config)
        if policy.state is not AutomationPolicyState.READY:
            return self._pause_for_policy(task, policy)
        if policy.policy_fingerprint is None:
            return self._pause_for_policy(task, policy)
        try:
            captured = SQLiteEmbeddingSource().capture(self._application.config)
        except PKVRuntimeError:
            # Source repair is an independent data-root state; do not consume an
            # attempt or construct a Provider while it cannot be proven.
            return task
        if (
            task.source_digest != captured.summary.digest
            and not self._has_committed_patch_waiting_for_ack(task)
        ):
            with self._application._write_lease_scope():
                return self.store.supersede_derivation(
                    task.task_id,
                    error_code=ErrorCode.RUNTIME_PLAN_STALE.value,
                )
        if task.policy_fingerprint not in {None, policy.policy_fingerprint} and task.patch_ref is not None:
            # A patch produced under an old policy must not silently survive a
            # provider/model/pricing change.
            with self._application._write_lease_scope():
                return self.store.supersede_derivation(
                    task.task_id,
                    error_code="policy_changed",
                )
        if task.policy_fingerprint != policy.policy_fingerprint:
            with self._application._write_lease_scope():
                return self.store.replan_derivation(
                    task.task_id,
                    source_digest=captured.summary.digest,
                    policy_fingerprint=policy.policy_fingerprint,
                    state=AIDerivationState.PENDING,
                )
        if task.state in {
            AIDerivationState.AUTHORIZATION_REQUIRED,
            AIDerivationState.BUDGET_PAUSED,
        }:
            with self._application._write_lease_scope():
                return self.store.replan_derivation(
                    task.task_id,
                    source_digest=captured.summary.digest,
                    policy_fingerprint=policy.policy_fingerprint,
                    state=AIDerivationState.PENDING,
                )
        return task

    def _pause_for_policy(
        self,
        task: AIDerivationTask,
        policy: AutomationPolicyInspection,
    ) -> AIDerivationTask:
        if task.state not in {
            AIDerivationState.PENDING,
            AIDerivationState.RETRY_REQUIRED,
            AIDerivationState.BUDGET_PAUSED,
            AIDerivationState.AUTHORIZATION_REQUIRED,
        }:
            return task
        if task.source_digest is None:
            return task
        with self._application._write_lease_scope():
            return self.store.replan_derivation(
                task.task_id,
                source_digest=task.source_digest,
                policy_fingerprint=policy.policy_fingerprint,
                state=AIDerivationState.AUTHORIZATION_REQUIRED,
            )

    async def _execute_claimed(
        self,
        task: AIDerivationTask,
        policy: AutomationPolicyInspection,
    ) -> R4Q2Result:
        if (
            task.state is not AIDerivationState.PROCESSING
            or task.claim_token is None
            or task.owner_fence <= 0
        ):
            raise PKVRuntimeError(
                ErrorCode.RUNTIME_PLAN_STALE,
                "Q2 执行需要当前 fenced claim。",
                stage=_STAGE,
                recoverable=True,
            )
        if (
            policy.state is not AutomationPolicyState.READY
            or policy.policy_fingerprint is None
            or policy.retry_max_attempts is None
            or policy.token_quota is None
        ):
            return R4Q2Result(self._pause_claimed_for_policy(task, policy))
        if task.policy_fingerprint != policy.policy_fingerprint:
            return R4Q2Result(
                self._finish_claimed(
                    task,
                    state=AIDerivationState.AUTHORIZATION_REQUIRED,
                    error_code="policy_changed",
                )
            )

        source = SQLiteEmbeddingSource()
        try:
            if task.patch_ref is not None and not task.patch_applied:
                patch_task = self._q1.store.get_ai_patch_task(task.patch_ref)
                if (
                    patch_task is not None
                    and patch_task.state is ContentMutationState.COMPLETED
                ):
                    with self._application._write_lease_scope():
                        task = self._mark_patch_applied_from_q1_under_lease(
                            task,
                            patch_task,
                            source,
                        )
            captured = source.capture(self._application.config)
            mutation = self.store.get_task_by_operation(task.operation_id)
            if mutation is None:
                raise PKVRuntimeError(
                    ErrorCode.REPAIR_REQUIRED,
                    "Q2 缺少对应的 Q1′ 内容任务。",
                    stage=_STAGE,
                    recoverable=True,
                )
            if task.source_digest != captured.summary.digest:
                return R4Q2Result(
                    self._finish_claimed(
                        task,
                        state=AIDerivationState.SUPERSEDED,
                        error_code=ErrorCode.RUNTIME_PLAN_STALE.value,
                    )
                )
            content = self._target_content_if_patch_needed(task, mutation.action, captured)
        except PKVRuntimeError as error:
            if error.code is ErrorCode.RUNTIME_PLAN_STALE:
                return R4Q2Result(
                    self._finish_claimed(
                        task,
                        state=AIDerivationState.SUPERSEDED,
                        error_code=error.code.value,
                    ),
                    error=error,
                )
            return R4Q2Result(
                self._retry_claimed(task, source, error.code.value), error=error
            )

        try:
            price_card = resolve_price_card(self._application.config, policy.price_policy)
        except PKVRuntimeError as error:
            return R4Q2Result(
                self._finish_claimed(
                    task,
                    state=AIDerivationState.AUTHORIZATION_REQUIRED,
                    error_code=error.code.value,
                ),
                error=error,
            )

        try:
            # Establish a confirmed generation plan before any Q2 Provider is
            # constructed.  After a content patch we inspect/plan again from the
            # final source, but this first gate prevents a paid LLM request when
            # the current runtime pointer/config cannot safely produce one.
            self._validate_generation_preflight(source)
        except PKVRuntimeError as error:
            return R4Q2Result(
                self._retry_claimed(task, source, error.code.value), error=error
            )

        needs_patch = mutation.action is ContentMutationAction.ARCHIVE and not task.patch_applied
        # A recorded patch is durable proof that its LLM work has already run.
        # A Q1′ retry may still be needed, but it must not reserve/estimate that
        # paid work a second time; only later generation remains billable.
        estimates = self._estimate_usage(
            captured,
            content if needs_patch and task.patch_ref is None else None,
        )
        reservation = self._reserve(task, policy, price_card, estimates, source)
        if reservation is None:
            return R4Q2Result(self.store.get_derivation_task_by_id(task.task_id))

        provider_stages: set[str] = set()
        try:
            if needs_patch:
                if task.patch_ref is None:
                    patch = self._derive_patch(
                        task,
                        content or "",
                        on_provider_start=provider_stages.add,
                    )
                    patch_value = patch[0]
                    with self._application._write_lease_scope():
                        self._record_llm_usage(task, reservation, patch[1], price_card)
                        reference = self.patch_spool.write(patch_value)
                        task = self.store.record_derivation_patch(
                            task.task_id,
                            claim_token=task.claim_token,
                            owner_fence=task.owner_fence,
                            patch_ref=reference.patch_id,
                            patch_sha256=reference.payload_sha256,
                        )
                else:
                    patch_value = self.patch_spool.read(
                        DerivationPatchReference(task.patch_ref, task.patch_sha256 or "")
                    )
                    self._validate_recorded_patch(task, patch_value)

                with self._application._write_lease_scope():
                    patch_result = await self._q1.submit_patch_and_drain(patch_value)
                    patch_task = self._q1.store.get_ai_patch_task(patch_value.patch_id)
                    if (
                        patch_task is None
                        or not patch_result.core_committed
                        or patch_task.state.value != "completed"
                    ):
                        settled_tokens, settled_micros = self._settlement_for_stages(
                            estimates,
                            price_card,
                            provider_stages,
                        )
                        return R4Q2Result(
                            self._retry_after_provider(
                                task,
                                reservation,
                                source,
                                ErrorCode.STORAGE_PRIMARY_FAILED.value,
                                provider_started=bool(provider_stages),
                                settled_tokens=settled_tokens,
                                settled_micros=settled_micros,
                            ),
                            provider_started=bool(provider_stages),
                        )
                    task = self._mark_patch_applied_from_q1_under_lease(
                        task,
                        patch_task,
                        source,
                    )

            self._execute_generation(
                task,
                source,
                reservation=reservation,
                price_card=price_card,
                on_provider_start=provider_stages.add,
            )
            with self._application._write_lease_scope():
                if provider_stages:
                    settled_tokens, settled_micros = self._settlement_for_stages(
                        estimates,
                        price_card,
                        provider_stages,
                    )
                    self.ledger.settle(
                        reservation,
                        settled_tokens=settled_tokens,
                        settled_micros=settled_micros,
                    )
                else:
                    self.ledger.release(reservation)
                completed = self.store.mark_derivation_state(
                    task.task_id,
                    claim_token=task.claim_token,
                    owner_fence=task.owner_fence,
                    state=AIDerivationState.COMPLETED,
                )
            return R4Q2Result(completed, provider_started=bool(provider_stages))
        except PKVRuntimeError as error:
            settled_tokens, settled_micros = self._settlement_for_stages(
                estimates,
                price_card,
                provider_stages,
            )
            return R4Q2Result(
                self._retry_after_provider(
                    task,
                    reservation,
                    source,
                    error.code.value,
                    provider_started=bool(provider_stages),
                    settled_tokens=settled_tokens,
                    settled_micros=settled_micros,
                ),
                provider_started=bool(provider_stages),
                error=error,
            )
        except Exception:
            settled_tokens, settled_micros = self._settlement_for_stages(
                estimates,
                price_card,
                provider_stages,
            )
            return R4Q2Result(
                self._retry_after_provider(
                    task,
                    reservation,
                    source,
                    ErrorCode.PROVIDER_UNAVAILABLE.value,
                    provider_started=bool(provider_stages),
                    settled_tokens=settled_tokens,
                    settled_micros=settled_micros,
                ),
                provider_started=bool(provider_stages),
            )

    def _target_content_if_patch_needed(
        self,
        task: AIDerivationTask,
        action: ContentMutationAction,
        captured: "CapturedEmbeddingSource",
    ) -> str | None:
        if action is not ContentMutationAction.ARCHIVE or task.patch_applied:
            return None
        if task.target_knowledge_id is None or task.target_revision_sha256 is None:
            raise PKVRuntimeError(
                ErrorCode.REPAIR_REQUIRED,
                "需要摘要的 Q2 任务缺少 Q1′ target revision。",
                stage=_STAGE,
                recoverable=True,
            )
        row = self._application.sqlite_store.query_by_id(task.target_knowledge_id)
        chunks = self._application.sqlite_store.get_chunks_by_knowledge_id(
            task.target_knowledge_id
        )
        if row is None or row_projection_sha256(row, chunks) != task.target_revision_sha256:
            raise PKVRuntimeError(
                ErrorCode.RUNTIME_PLAN_STALE,
                "Q2 target revision 已在 Provider 前变化。",
                stage=_STAGE,
                recoverable=True,
            )
        for record in captured.records:
            if record.knowledge_id == task.target_knowledge_id:
                return record.content
        raise PKVRuntimeError(
            ErrorCode.REPAIR_REQUIRED,
            "Q2 target 不在可验证的 Embedding source 中。",
            stage=_STAGE,
            recoverable=True,
        )

    @staticmethod
    def _estimate_usage(
        captured: "CapturedEmbeddingSource",
        content: str | None,
    ) -> dict[str, int]:
        embedding_tokens = math.ceil(
            sum(
                len(record.content) + sum(len(chunk) for _, chunk in record.chunks)
                for record in captured.records
            )
            / 4
        )
        if content is None:
            return {
                "summary_input": 0,
                "summary_output": 0,
                "tags_input": 0,
                "tags_output": 0,
                "embedding": embedding_tokens,
            }
        input_tokens = math.ceil(len(content) / 4)
        return {
            "summary_input": input_tokens,
            "summary_output": 600,
            "tags_input": input_tokens,
            "tags_output": 200,
            "embedding": embedding_tokens,
        }

    @staticmethod
    def _settlement_for_stages(
        estimates: dict[str, int],
        price_card: PriceCard | None,
        stages: set[str],
    ) -> tuple[int, int | None]:
        """Return the conservative portion actually exposed to a Provider.

        One reservation protects the whole Q2 attempt before construction.  A
        failure after summary/tag generation but before embedding must settle the
        already-billable LLM portion while releasing the never-started generation
        portion, otherwise a later Q1′ patch retry is charged twice.
        """

        allowed = {"summary", "tags", "embedding"}
        if not stages <= allowed:
            raise ValueError("Q2 Provider stage 无效")
        llm_input = 0
        llm_output = 0
        if "summary" in stages:
            llm_input += estimates["summary_input"]
            llm_output += estimates["summary_output"]
        if "tags" in stages:
            llm_input += estimates["tags_input"]
            llm_output += estimates["tags_output"]
        embedding_tokens = estimates["embedding"] if "embedding" in stages else 0
        tokens = llm_input + llm_output + embedding_tokens
        if price_card is None:
            return tokens, None
        return (
            tokens,
            price_card.conservative_llm_amount(
                uncached_input_tokens=llm_input,
                generated_tokens=llm_output,
            )
            + price_card.conservative_embedding_amount(embedding_tokens),
        )

    def _reserve(
        self,
        task: AIDerivationTask,
        policy: AutomationPolicyInspection,
        price_card: PriceCard | None,
        estimates: dict[str, int],
        source: SQLiteEmbeddingSource,
    ) -> DerivationReservation | None:
        quota = policy.token_quota
        assert quota is not None
        estimated_llm_input = estimates["summary_input"] + estimates["tags_input"]
        estimated_llm_output = estimates["summary_output"] + estimates["tags_output"]
        reserved_tokens = estimated_llm_input + estimated_llm_output + estimates["embedding"]
        reserved_micros: int | None = None
        if price_card is not None:
            reserved_micros = (
                price_card.conservative_llm_amount(
                    uncached_input_tokens=estimated_llm_input,
                    generated_tokens=estimated_llm_output,
                )
                + price_card.conservative_embedding_amount(estimates["embedding"])
            )
        now = datetime.now(ZoneInfo(quota.timezone))
        with self._application._write_lease_scope():
            reservation = self.ledger.reserve(
                task,
                timezone=quota.timezone,
                local_day=now.date().isoformat(),
                local_month=now.strftime("%Y-%m"),
                reserved_tokens=reserved_tokens,
                daily_total_tokens=quota.daily_total_tokens,
                monthly_total_tokens=quota.monthly_total_tokens,
                reserved_micros=reserved_micros,
                daily_cap_micros=(
                    policy.price_policy.daily_cap_micros
                    if policy.price_policy is not None
                    else None
                ),
                monthly_cap_micros=(
                    policy.price_policy.monthly_cap_micros
                    if policy.price_policy is not None
                    else None
                ),
                currency=price_card.currency if price_card is not None else None,
            )
            if reservation is None:
                publish_embedding_nonready_binding(
                    self._application.config,
                    state=EmbeddingIndexState.BUDGET_PAUSED,
                    source=source,
                )
                self.store.mark_derivation_state(
                    task.task_id,
                    claim_token=task.claim_token or "",
                    owner_fence=task.owner_fence,
                    state=AIDerivationState.BUDGET_PAUSED,
                    error_code=ErrorCode.EMBEDDING_BUDGET_PAUSED.value,
                )
                return None
            if estimates["summary_input"] or estimates["summary_output"]:
                self._record_usage(
                    task,
                    reservation,
                    "summary",
                    TokenUsage(
                        uncached_input_tokens=estimates["summary_input"],
                        generated_tokens=estimates["summary_output"],
                        source="local_estimate",
                    ),
                    None,
                    exact_price=False,
                )
            if estimates["tags_input"] or estimates["tags_output"]:
                self._record_usage(
                    task,
                    reservation,
                    "tags",
                    TokenUsage(
                        uncached_input_tokens=estimates["tags_input"],
                        generated_tokens=estimates["tags_output"],
                        source="local_estimate",
                    ),
                    None,
                    exact_price=False,
                )
            if estimates["embedding"]:
                self._record_usage(
                    task,
                    reservation,
                    "embedding",
                    TokenUsage(
                        embedding_input_tokens=estimates["embedding"],
                        source="local_estimate",
                    ),
                    None,
                    exact_price=False,
                )
            return reservation

    def _derive_patch(
        self,
        task: AIDerivationTask,
        content: str,
        *,
        on_provider_start: Callable[[str], None] | None = None,
    ) -> tuple[DerivationPatch, tuple[_UsageCapture, _UsageCapture]]:
        if (
            task.target_knowledge_id is None
            or task.target_revision_sha256 is None
            or task.source_digest is None
        ):
            raise PKVRuntimeError(
                ErrorCode.REPAIR_REQUIRED,
                "Q2 不能为缺失 target/source 的任务生成 DerivationPatch。",
                stage=_STAGE,
                recoverable=True,
            )
        provider = self._llm_factory()
        summarize = getattr(provider, "summarize", None)
        extract_tags = getattr(provider, "extract_tags", None)
        if not callable(summarize) or not callable(extract_tags):
            raise TypeError("Q2 LLM Provider 必须提供 summarize 与 extract_tags")
        if on_provider_start is not None:
            on_provider_start("summary")
        summary = summarize(content)
        summary_usage = self._last_usage(provider)
        if on_provider_start is not None:
            on_provider_start("tags")
        tags = extract_tags(content)
        tags_usage = self._last_usage(provider)
        return (
            DerivationPatch.create(
                derivation_task_id=task.task_id,
                target_knowledge_id=task.target_knowledge_id,
                expected_revision_sha256=task.target_revision_sha256,
                input_digest=task.source_digest,
                summary=summary,
                tags=tags,
            ),
            (summary_usage, tags_usage),
        )

    def _record_llm_usage(
        self,
        task: AIDerivationTask,
        reservation: DerivationReservation,
        captures: tuple[_UsageCapture, _UsageCapture],
        price_card: PriceCard | None,
    ) -> None:
        for stage, capture in zip(("summary", "tags"), captures):
            if capture.usage is not None:
                self._record_usage(
                    task,
                    reservation,
                    stage,
                    capture.usage,
                    price_card,
                    exact_price=capture.complete,
                )

    @staticmethod
    def _last_usage(provider: Any) -> _UsageCapture:
        usage = getattr(provider, "last_usage", None)
        return _UsageCapture(
            usage if isinstance(usage, TokenUsage) else None,
            isinstance(usage, TokenUsage),
        )

    def _validate_recorded_patch(
        self,
        task: AIDerivationTask,
        patch: DerivationPatch,
    ) -> None:
        if (
            patch.derivation_task_id != task.task_id
            or patch.target_knowledge_id != task.target_knowledge_id
            or patch.expected_revision_sha256 != task.target_revision_sha256
            or patch.input_digest != task.source_digest
        ):
            raise PKVRuntimeError(
                ErrorCode.RUNTIME_PLAN_STALE,
                "已记录的 DerivationPatch 与 Q2 task 不一致。",
                stage=_STAGE,
                recoverable=True,
            )

    def _has_committed_patch_waiting_for_ack(self, task: AIDerivationTask) -> bool:
        if task.patch_ref is None or task.patch_applied:
            return False
        patch_task = self._q1.store.get_ai_patch_task(task.patch_ref)
        return patch_task is not None and patch_task.state in {
            ContentMutationState.CORE_COMMITTED,
            ContentMutationState.COMPLETED,
        }

    def _mark_patch_applied_from_q1_under_lease(
        self,
        task: AIDerivationTask,
        patch_task: Any,
        source: SQLiteEmbeddingSource,
    ) -> AIDerivationTask:
        """Acknowledge only the exact Q1′ patch revision and bind its final source."""

        try:
            journal = self._application.storage_coordinator.journal.read(
                patch_task.operation_id
            )
        except (OSError, ValueError, PKVRuntimeError) as exc:
            raise PKVRuntimeError(
                ErrorCode.REPAIR_REQUIRED,
                "已提交 DerivationPatch 缺少可验证的 Q1′ journal。",
                stage=_STAGE,
                recoverable=True,
            ) from exc
        resulting_revision = journal.get("projection_sha256")
        knowledge_id = task.target_knowledge_id
        row = (
            self._application.sqlite_store.query_by_id(knowledge_id)
            if knowledge_id is not None
            else None
        )
        chunks = (
            self._application.sqlite_store.get_chunks_by_knowledge_id(knowledge_id)
            if knowledge_id is not None
            else []
        )
        if (
            patch_task.state is not ContentMutationState.COMPLETED
            or journal.get("operation_id") != patch_task.operation_id
            or journal.get("action") != ContentMutationAction.APPLY_AI_PATCH.value
            or journal.get("core_committed") is not True
            or not isinstance(resulting_revision, str)
            or len(resulting_revision) != 64
            or row is None
            or row_projection_sha256(row, chunks) != resulting_revision
        ):
            raise PKVRuntimeError(
                ErrorCode.RUNTIME_PLAN_STALE,
                "Q1′ DerivationPatch 的最终 revision 无法验证。",
                stage=_STAGE,
                recoverable=True,
            )
        final_source = source.capture(self._application.config)
        return self.store.mark_derivation_patch_applied(
            task.task_id,
            claim_token=task.claim_token or "",
            owner_fence=task.owner_fence,
            source_digest=final_source.summary.digest,
        )

    def _execute_generation(
        self,
        task: AIDerivationTask,
        source: SQLiteEmbeddingSource,
        *,
        reservation: DerivationReservation,
        price_card: PriceCard | None,
        on_provider_start: Callable[[str], None] | None = None,
    ) -> _MeteredPreChunkedEmbeddingAdapter | None:
        inspection = inspect_embedding_index(self._application.config, source=source)
        if inspection.state is EmbeddingIndexState.READY:
            return None
        plan = plan_embedding_rebuild(inspection)
        captured = inspection._captured_source
        if captured is None:
            raise PKVRuntimeError(
                ErrorCode.REPAIR_REQUIRED,
                "Embedding generation 计划缺少已验证 source capture。",
                stage=_STAGE,
                recoverable=True,
            )
        if on_provider_start is not None:
            on_provider_start("embedding")
        embedder = self._embedder_factory()
        # This is the only billable embedding segment.  It deliberately runs
        # outside ``_write_lease_scope``; the next block merely persists
        # provider facts and replays immutable vectors into local files.
        metered = _MeteredPreChunkedEmbeddingAdapter(embedder, captured)
        with self._application._write_lease_scope():
            self.store.assert_live_derivation_claim(
                task.task_id,
                claim_token=task.claim_token or "",
                owner_fence=task.owner_fence,
            )
            usage_capture = metered.usage_capture
            if usage_capture.usage is not None:
                self._record_usage(
                    task,
                    reservation,
                    "embedding",
                    usage_capture.usage,
                    price_card,
                    exact_price=usage_capture.complete,
                )
            execute_embedding_rebuild(
                plan,
                confirm_embedding_rebuild(plan, allow_network=True),
                embedder=metered,
            )
            metered.assert_consumed()
        return metered

    def _validate_generation_preflight(self, source: SQLiteEmbeddingSource) -> None:
        inspection = inspect_embedding_index(self._application.config, source=source)
        if inspection.state is EmbeddingIndexState.READY:
            return
        plan = plan_embedding_rebuild(inspection)
        confirm_embedding_rebuild(plan, allow_network=True)

    def _record_usage(
        self,
        task: AIDerivationTask,
        reservation: DerivationReservation,
        stage: str,
        usage: TokenUsage,
        price_card: PriceCard | None,
        *,
        exact_price: bool,
    ) -> None:
        amount = (
            price_card.amount_for_usage(stage, usage)
            if price_card is not None and exact_price
            else None
        )
        self.ledger.record_usage(
            task,
            reservation_id=reservation.reservation_id,
            stage=stage,
            usage=usage,
            amount_micros=amount,
            currency=price_card.currency if amount is not None and price_card else None,
        )

    def _pause_claimed_for_policy(
        self,
        task: AIDerivationTask,
        policy: AutomationPolicyInspection,
    ) -> AIDerivationTask:
        del policy
        return self._finish_claimed(
            task,
            state=AIDerivationState.AUTHORIZATION_REQUIRED,
            error_code=ErrorCode.EMBEDDING_AUTOMATION_AUTHORIZATION_REQUIRED.value,
        )

    def _finish_claimed(
        self,
        task: AIDerivationTask,
        *,
        state: AIDerivationState,
        error_code: str | None,
    ) -> AIDerivationTask:
        with self._application._write_lease_scope():
            return self.store.mark_derivation_state(
                task.task_id,
                claim_token=task.claim_token or "",
                owner_fence=task.owner_fence,
                state=state,
                error_code=error_code,
            )

    def _retry_claimed(
        self,
        task: AIDerivationTask,
        source: SQLiteEmbeddingSource,
        error_code: str,
    ) -> AIDerivationTask:
        with self._application._write_lease_scope():
            publish_embedding_nonready_binding(
                self._application.config,
                state=EmbeddingIndexState.RETRY_REQUIRED,
                source=source,
            )
            return self.store.mark_derivation_state(
                task.task_id,
                claim_token=task.claim_token or "",
                owner_fence=task.owner_fence,
                state=AIDerivationState.RETRY_REQUIRED,
                error_code=error_code,
                delay_seconds=min(60, 2 ** min(task.attempt_count, 5)),
            )

    def _retry_after_provider(
        self,
        task: AIDerivationTask,
        reservation: DerivationReservation,
        source: SQLiteEmbeddingSource,
        error_code: str,
        *,
        provider_started: bool,
        settled_tokens: int | None = None,
        settled_micros: int | None = None,
    ) -> AIDerivationTask:
        # This branch runs after a potentially slow Provider call, outside the
        # root writer lease.  A delete or newer source may have fenced the claim
        # in the meantime.  The late worker must retain conservative accounting
        # for an already-billed call, but it is not allowed to publish a retry
        # binding or mutate the newer task state.
        with self._application._write_lease_scope():
            try:
                if provider_started:
                    self.ledger.settle(
                        reservation,
                        settled_tokens=(
                            reservation.reserved_tokens
                            if settled_tokens is None
                            else settled_tokens
                        ),
                        settled_micros=(
                            reservation.reserved_micros
                            if settled_micros is None
                            else settled_micros
                        ),
                    )
                else:
                    self.ledger.release(reservation)
            except PKVRuntimeError as stale:
                if stale.code is not ErrorCode.RUNTIME_PLAN_STALE:
                    raise
                if provider_started:
                    self.ledger.settle_detached(
                        reservation,
                        settled_tokens=settled_tokens,
                        settled_micros=settled_micros,
                    )
                else:
                    self.ledger.release_detached(reservation)
                current = self.store.get_derivation_task_by_id(task.task_id)
                return current or task
            # The successful reservation transition above is also the live-claim
            # fence.  While this root lease remains held, a delete/successor
            # cannot interleave, so publishing a retry binding is safe here.
            try:
                # A fault may arrive after generation pointer CAS but before Q2
                # settlement/completion.  Preserve a source-matching READY
                # generation so the next trigger only reconciles the durable
                # task/ledger instead of hiding or rebuilding proven vectors.
                if (
                    inspect_embedding_index(
                        self._application.config,
                        source=source,
                    ).state
                    is not EmbeddingIndexState.READY
                ):
                    publish_embedding_nonready_binding(
                        self._application.config,
                        state=EmbeddingIndexState.RETRY_REQUIRED,
                        source=source,
                    )
            except PKVRuntimeError:
                # The durable Q2 retry state remains the recovery authority even
                # if an auxiliary binding refresh needs its own repair.
                pass
            return self.store.mark_derivation_state(
                task.task_id,
                claim_token=task.claim_token or "",
                owner_fence=task.owner_fence,
                state=AIDerivationState.RETRY_REQUIRED,
                error_code=error_code,
                delay_seconds=min(60, 2 ** min(task.attempt_count, 5)),
            )


__all__ = ["R4DerivationLifecycle", "R4Q2Result"]
