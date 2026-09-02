"""R4 Q0 ingress orchestration.

The public application owns admission and status projection, while this module
owns the mechanical split that R4 needs: a short, fenced writer-lease admission;
slow crawler/parser work outside that lease; then the sole Q1′ content writer.
It imports Q0 parser code only and never constructs an AI Provider.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from src.application.r4_lifecycle import R4ContentLifecycle, R4Q1Result
from src.runtime.errors import ErrorCode, PKVRuntimeError
from src.storage.content_lifecycle import (
    PreparedDocument,
    PreparedDocumentReference,
    PreparedDocumentSpool,
)
from src.storage.ingress_lifecycle import (
    IngressKind,
    IngressRequest,
    IngressRequestReference,
    IngressRequestSpool,
    IngressState,
    IngressTask,
    IngressTaskStore,
    PreparedReference,
)
from src.storage.markdown_store import Entry

if TYPE_CHECKING:
    from src.application.knowledge_application import KnowledgeApplication


_STAGE = "r4_ingress_lifecycle"


@dataclass(frozen=True)
class R4IngressResult:
    task: IngressTask
    q1_result: R4Q1Result | None = None
    error: PKVRuntimeError | None = None

    @property
    def core_committed(self) -> bool:
        return self.q1_result is not None and self.q1_result.core_committed


class R4IngressLifecycle:
    """Admission/recovery bridge from Q0 to the prepared-document Q1′ port."""

    def __init__(
        self,
        application: "KnowledgeApplication",
        *,
        preparer: Callable[[IngressRequest], Awaitable[Entry]] | None = None,
        q1_lifecycle: R4ContentLifecycle | None = None,
    ) -> None:
        self._application = application
        self._preparer = preparer
        self._q1 = q1_lifecycle or R4ContentLifecycle(application)

    @property
    def store(self) -> IngressTaskStore:
        return IngressTaskStore(self._application.config.layout)

    @property
    def spool(self) -> IngressRequestSpool:
        return IngressRequestSpool(self._application.config.layout)

    @property
    def prepared_spool(self) -> PreparedDocumentSpool:
        return PreparedDocumentSpool(self._application.config.layout)

    def admit(self, request: IngressRequest) -> IngressTask:
        """Persist a Q0 request while the caller owns the writer lease."""

        reference = self.spool.write(request)
        return self.store.enqueue(request, reference)

    async def submit_and_drain(self, request: IngressRequest) -> R4IngressResult:
        """Admit once, then run one bounded Q0→Q1′ foreground continuation."""

        with self._application._write_lease_scope():
            task = self.admit(request)
        # Recovery starts strictly after this request has acquired and released
        # its short admission lease.  That ordering preserves the Q0 guarantee:
        # a contending writer returns write_busy before crawler/parser/provider
        # work for either the new request or an older abandoned task begins.
        recovered = await self.recover_and_drain(max_tasks=8)
        for result in recovered:
            if result.task.task_id == task.task_id:
                # Preserve the target's concrete Q0 outcome (notably a
                # retryable parser failure) instead of replacing it with a
                # later no-op inspection after backoff has been recorded.
                return result
        return await self.drain_task(task.task_id)

    async def recover_and_drain(self, *, max_tasks: int = 8) -> tuple[R4IngressResult, ...]:
        if type(max_tasks) is not int or max_tasks <= 0:
            raise ValueError("max_tasks 必须是正整数")
        with self._application._write_lease_scope():
            self.store.recover_expired_claims()
        results: list[R4IngressResult] = []
        for task in self.store.list_recoverable():
            if len(results) >= max_tasks:
                break
            result = await self.drain_task(task.task_id)
            results.append(result)
        # A crash after Q0 has durably submitted Q1′ leaves the Q0 row terminal
        # by design.  The Q1′ operation journal remains recoverable, so every
        # permitted foreground trigger also gives it one bounded continuation.
        with self._application._write_lease_scope():
            await self._q1.recover_and_drain(max_tasks=max_tasks)
        return tuple(results)

    async def drain_task(self, task_id: str) -> R4IngressResult:
        """Advance a specific Q0 task without holding a lease during parsing."""

        with self._application._write_lease_scope():
            self.store.recover_expired_claims()
            task = self.store.get_task(task_id)
            if task is None:
                raise PKVRuntimeError(
                    ErrorCode.RUNTIME_PLAN_STALE,
                    "需要 drain 的 Q0 ingress task 不存在。",
                    stage=_STAGE,
                    recoverable=True,
                )
            if task.state is IngressState.PREPARED:
                return await self._submit_prepared_under_lease(task)
            if task.state is IngressState.SUBMITTED:
                q1_task = self._q1.store.get_task_by_operation(task.operation_id)
                if q1_task is None:
                    raise PKVRuntimeError(
                        ErrorCode.REPAIR_REQUIRED,
                        "Q0 已提交但对应 Q1′ task 缺失。",
                        stage=_STAGE,
                        recoverable=True,
                    )
                return R4IngressResult(task, await self._q1.drain_task(q1_task.task_id))
            if task.state not in {IngressState.ACCEPTED, IngressState.RETRY_REQUIRED}:
                return R4IngressResult(task)
            claimed = self.store.claim_task(task_id)
            if claimed is None:
                return R4IngressResult(task)

        # Q0's potentially slow operation deliberately has no root writer
        # lease.  A prior admission is the durable authority for this work.
        try:
            request = self.spool.read(claimed.request_reference)
            entry = await self._prepare(request)
            document = PreparedDocument.for_archive(
                entry,
                provenance={
                    "ingress_kind": request.kind.value,
                    "ingress_request": request.request_id,
                    "parser_version": request.parser_version,
                    **dict(request.provenance),
                },
                parser_version=request.parser_version,
            )
        except PKVRuntimeError as error:
            return self._mark_prepare_failure(claimed, error)
        except Exception as exc:
            return self._mark_prepare_failure(
                claimed,
                PKVRuntimeError(
                    ErrorCode.WORKFLOW_STEP_FAILED,
                    "Q0 前处理失败。",
                    stage=_STAGE,
                    recoverable=True,
                ),
            )

        with self._application._write_lease_scope():
            reference = self.prepared_spool.write(document)
            prepared = self.store.mark_prepared(
                claimed.task_id,
                claim_token=claimed.claim_token or "",
                owner_fence=claimed.owner_fence,
                prepared_reference=PreparedReference(
                    reference.prepared_id,
                    reference.payload_sha256,
                ),
            )
            return await self._submit_prepared_under_lease(prepared)

    def _mark_prepare_failure(
        self,
        claimed: IngressTask,
        error: PKVRuntimeError,
    ) -> R4IngressResult:
        with self._application._write_lease_scope():
            non_retryable = error.code in {
                ErrorCode.URL_INVALID,
                ErrorCode.SSRF_TARGET_FORBIDDEN,
                ErrorCode.SSRF_RESOLUTION_FAILED,
                ErrorCode.SSRF_REDIRECT_LIMIT,
            }
            if non_retryable:
                task = self.store.mark_rejected(
                    claimed.task_id,
                    claim_token=claimed.claim_token or "",
                    owner_fence=claimed.owner_fence,
                    error_code=error.code.value,
                )
                # A terminal safety denial needs no replay body.  Keep its
                # durable state/code for diagnostics while removing the private
                # source payload (which may contain URL credentials).
                self.spool.discard(task.request_reference)
            else:
                # Bounded backoff is intentionally simple at Q0: every retry
                # carries an explicit state and is not immediately re-crawled by
                # the same foreground trigger.
                delay_seconds = min(60, 2 ** min(claimed.attempt_count, 5))
                task = self.store.mark_retry(
                    claimed.task_id,
                    claim_token=claimed.claim_token or "",
                    owner_fence=claimed.owner_fence,
                    error_code=error.code.value,
                    delay_seconds=delay_seconds,
                )
            return R4IngressResult(task, error=error)

    async def _submit_prepared_under_lease(self, task: IngressTask) -> R4IngressResult:
        """Create/rejoin Q1′ using Q0's durable operation identity."""

        reference = task.prepared_reference
        if reference is None:
            raise PKVRuntimeError(
                ErrorCode.REPAIR_REQUIRED,
                "Q0 prepared task 缺少 PreparedDocument 引用。",
                stage=_STAGE,
                recoverable=True,
            )
        document = self.prepared_spool.read(
            PreparedDocumentReference(reference.prepared_id, reference.payload_sha256)
        )
        q1_task = self._q1.store.enqueue_prepared(
            document,
            PreparedDocumentReference(reference.prepared_id, reference.payload_sha256),
            operation_id=task.operation_id,
        )
        task = self.store.mark_submitted(task.task_id)
        # Q1′ now has its own durable immutable payload reference.  Keeping Q0
        # request content is unnecessary; this placement also covers recovery
        # after a crash between ``prepared`` and ``submitted``.
        self.spool.discard(task.request_reference)
        return R4IngressResult(task, await self._q1.drain_task(q1_task.task_id))

    async def _prepare(self, request: IngressRequest) -> Entry:
        if self._preparer is not None:
            entry = await self._preparer(request)
        elif request.kind is IngressKind.TEXT:
            # The production factory disables AI enrichment under the R4 path;
            # this is pure text preprocessing, never a Q2 Provider call.
            entry = await self._application._new_text_processor().process_text(
                request.source
            )
        else:
            entry = await self._fetch_request(request)
        if not isinstance(entry, Entry):
            raise PKVRuntimeError(
                ErrorCode.WORKFLOW_STEP_FAILED,
                "Q0 前处理未产生有效 Entry。",
                stage=_STAGE,
                recoverable=True,
            )
        if request.title_override:
            entry.title = request.title_override
        manual_tags = request.provenance.get("manual_tags")
        if isinstance(manual_tags, str) and manual_tags:
            entry.tags = [tag for tag in manual_tags.split("\u001f") if tag]
        return entry

    async def _fetch_request(self, request: IngressRequest) -> Entry:
        """Use the existing safe fetch selectors without entering a workflow store step."""

        from src.workflow.models import WorkflowContext
        from src.workflow.steps import FetchStep, _grant_cli_local_file_import

        initial: dict[str, Any] = {"url": request.source}
        if request.kind is IngressKind.FILE:
            # Only ``archive_cli_input`` may construct an IngressRequest of this
            # kind after validating its non-serializable one-shot capability.
            # The durable task is therefore the fenced continuation of that
            # admitted authorization, not a public arbitrary-file API.
            _grant_cli_local_file_import(initial, request.source)
        context = WorkflowContext(initial)
        result = await FetchStep(
            "r4_q0_fetch",
            {
                "processor": "auto",
                "url_key": "url",
                "retry": 0,
                "timeout": 30,
            },
            runtime_config=self._application.config,
        ).execute(context)
        entry = result.get("entry")
        if isinstance(entry, Entry):
            return entry
        issues = result.get("issues")
        if isinstance(issues, list) and issues and isinstance(issues[0], dict):
            code_value = issues[0].get("code")
            try:
                code = ErrorCode(code_value)
            except (TypeError, ValueError):
                code = ErrorCode.WORKFLOW_STEP_FAILED
            raise PKVRuntimeError(
                code,
                "Q0 抓取或文件解析失败。",
                stage=str(issues[0].get("stage") or _STAGE),
                recoverable=bool(issues[0].get("recoverable", True)),
            )
        raise PKVRuntimeError(
            ErrorCode.WORKFLOW_STEP_FAILED,
            "Q0 抓取或文件解析失败。",
            stage=_STAGE,
            recoverable=True,
        )


__all__ = ["R4IngressLifecycle", "R4IngressResult"]
