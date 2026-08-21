"""Shared application service used by headless adapters and external wrappers.

``KnowledgeApplication`` is the composition boundary for long-lived local
services.  It intentionally has no dependency on an adapter package: callers
bring validated input in and project its domain results back to a UI, CLI, or
protocol response.  Provider-backed dependencies stay lazy, so BM25-only
operations do not require credentials or a vector index.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
import math
from threading import RLock
from typing import Any

from src.retrieval.result import RetrievalIssue, SearchResponse
from src.runtime.errors import ErrorCode, PKVRuntimeError
from src.workflow.models import WorkflowResult


@dataclass(frozen=True)
class ApplicationSnapshot:
    """Immutable identity captured by every operation of one application graph."""

    generation: int
    config: Any


@dataclass
class KnowledgeApplication:
    """Compose PKV domain services for one validated runtime configuration.

    Factories are explicit seams for focused application tests.  Production
    construction is delayed until an operation actually needs a dependency;
    notably, neither app creation nor a BM25 lookup constructs a Provider.
    """

    config: Any
    sqlite_store_factory: Callable[[Any], Any] | None = None
    markdown_store_factory: Callable[[Any], Any] | None = None
    vector_store_factory: Callable[[Any], Any] | None = None
    bm25_retriever_factory: Callable[[Any], Any] | None = None
    query_router_factory: Callable[[Any, int], Any] | None = None
    workflow_factory: Callable[[], Any] | None = None
    text_processor_factory: Callable[[], Any] | None = None
    chat_provider_factory: Callable[[Any], Any] | None = None
    write_lease_factory: Callable[[Any], Any] | None = None
    snapshot_generation: int = field(default=0, kw_only=True)
    _snapshot: ApplicationSnapshot = field(init=False, repr=False)
    _sqlite_store: Any = field(default=None, init=False, repr=False)
    _markdown_store: Any = field(default=None, init=False, repr=False)
    _vector_store: Any = field(default=None, init=False, repr=False)
    _vector_store_checked: bool = field(default=False, init=False, repr=False)
    _readonly_vector_store: Any = field(default=None, init=False, repr=False)
    _readonly_vector_store_checked: bool = field(default=False, init=False, repr=False)
    _bm25_retriever: Any = field(default=None, init=False, repr=False)
    _query_routers: dict[int, Any] = field(default_factory=dict, init=False, repr=False)
    _storage_coordinator: Any = field(default=None, init=False, repr=False)
    _relation_query_service: Any = field(default=None, init=False, repr=False)
    _evidence_collection_service: Any = field(default=None, init=False, repr=False)
    _exploration_service: Any = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if type(self.snapshot_generation) is not int or self.snapshot_generation < 0:
            raise ValueError("snapshot_generation 必须是非负整数")
        self._snapshot = ApplicationSnapshot(
            generation=self.snapshot_generation,
            config=self.config,
        )

    def __setattr__(self, name: str, value: Any) -> None:
        # ``config`` is the root of every lazy dependency below this boundary.
        # Rebinding it after construction could make one operation observe an
        # old Store with a new Provider/vector contract.  Reload publishes a
        # new application instead.
        if name == "config" and "config" in self.__dict__:
            raise AttributeError("KnowledgeApplication.config is an immutable snapshot")
        super().__setattr__(name, value)

    @property
    def snapshot(self) -> ApplicationSnapshot:
        """Return this application's immutable config-graph identity."""

        return self._snapshot

    @property
    def sqlite_store(self) -> Any:
        """Return the process-local SQLite store, lazily composed once."""

        if self._sqlite_store is None:
            factory = self.sqlite_store_factory or self._default_sqlite_store
            self._sqlite_store = factory(self.config)
        return self._sqlite_store

    @property
    def markdown_store(self) -> Any:
        """Return the process-local Markdown store, lazily composed once."""

        if self._markdown_store is None:
            factory = self.markdown_store_factory or self._default_markdown_store
            self._markdown_store = factory(self.config)
        return self._markdown_store

    @property
    def vector_store(self) -> Any | None:
        """Open existing vector artifacts for application-owned mutations.

        This is the compatibility handle used by wrapper-driven deletion.  It never creates
        a missing index, but an existing index remains writable for a real
        application operation.  Read-only retrieval must use
        :attr:`readonly_vector_store` so it cannot create lock sidecars.
        """

        if self._vector_store is None:
            factory = self.vector_store_factory or self._default_vector_store
            candidate = factory(self.config)
            self._vector_store_checked = candidate is not None
            if candidate is not None:
                self._vector_store = candidate
        return self._vector_store

    @property
    def readonly_vector_store(self) -> Any | None:
        """Open existing vector artifacts in strict no-write/no-lock mode."""

        if self._readonly_vector_store is None:
            candidate = self._default_readonly_vector_store(self.config)
            self._readonly_vector_store_checked = candidate is not None
            if candidate is not None:
                self._readonly_vector_store = candidate
        return self._readonly_vector_store

    @property
    def bm25_retriever(self) -> Any:
        """Return the credential-free BM25 retriever."""

        if self._bm25_retriever is None:
            factory = self.bm25_retriever_factory or self._default_bm25_retriever
            self._bm25_retriever = factory(self.config)
        return self._bm25_retriever

    @property
    def storage_coordinator(self) -> Any:
        """Return the cross-store write/delete coordinator."""

        if self._storage_coordinator is None:
            from src.storage.coordinator import StorageCoordinator

            self._storage_coordinator = StorageCoordinator(
                self.markdown_store,
                self.sqlite_store,
                self.config.layout.runtime_state_dir / "operations",
            )
        return self._storage_coordinator

    @property
    def relation_query_service(self) -> Any:
        """Return the relation query service on the shared storage root."""

        if self._relation_query_service is None:
            from src.relations.query_service import RelationQueryService
            from src.storage.relation_store import RelationStore

            self._relation_query_service = RelationQueryService(
                RelationStore(self.config.db_path)
            )
        return self._relation_query_service

    @property
    def evidence_collection_service(self) -> Any:
        """Return the evidence service composed from the shared dependencies."""

        if self._evidence_collection_service is None:
            from src.relations.evidence_service import EvidenceCollectionService

            self._evidence_collection_service = EvidenceCollectionService(
                query_router=self.query_router(),
                sqlite_store=self.sqlite_store,
                markdown_store=self.markdown_store,
                relation_query_service=self.relation_query_service,
                runtime_config=self.config,
            )
        return self._evidence_collection_service

    @property
    def exploration_service(self) -> Any:
        """Return the exploration service composed from the shared dependencies."""

        if self._exploration_service is None:
            from src.relations.exploration_service import ExplorationService

            self._exploration_service = ExplorationService(
                query_router=self.query_router(),
                sqlite_store=self.sqlite_store,
                relation_query_service=self.relation_query_service,
                vault_dir=self.markdown_store.vault_dir,
                runtime_config=self.config,
            )
        return self._exploration_service

    def query_router(self, *, token_threshold: int = 5) -> Any:
        """Return a lazy Provider-aware query router for one routing threshold."""

        if isinstance(token_threshold, bool) or not isinstance(token_threshold, int):
            raise ValueError("token_threshold 必须是正整数")
        if token_threshold <= 0:
            raise ValueError("token_threshold 必须是正整数")
        router = self._query_routers.get(token_threshold)
        if router is None:
            factory = self.query_router_factory or self._default_query_router
            router = factory(self.config, token_threshold)
            self._query_routers[token_threshold] = router
        return router

    def search(
        self,
        query: str,
        strategy: str = "auto",
        limit: int = 10,
        *,
        auto_token_threshold: int = 5,
    ) -> SearchResponse:
        """Run a retrieval strategy through the shared composition boundary.

        ``auto_token_threshold`` exists only to preserve published adapter
        behavior while they converge: the historical CLI uses 10, whereas the
        MCP router uses 5.  Each still calls this one operation and shares the
        same Provider factory and response contract.
        """

        normalized_strategy = strategy.lower() if isinstance(strategy, str) else "unknown"
        if not isinstance(query, str) or not query.strip():
            return SearchResponse.invalid("查询文本不能为空", strategy=normalized_strategy)
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            return SearchResponse.invalid(
                "limit 必须是正整数",
                strategy=normalized_strategy,
                stage="limit_validation",
            )
        if normalized_strategy == "auto":
            return self.query_router(token_threshold=auto_token_threshold).search(query, limit)
        if normalized_strategy == "bm25":
            return self.bm25_retriever.search(query, limit)
        if normalized_strategy == "vector":
            return self._new_vector_retriever().search(query, limit)
        if normalized_strategy == "hybrid":
            return self._new_hybrid_retriever().search(query, limit)
        raise ValueError(f"不支持的检索策略: {strategy}")

    async def archive_url(self, input_data: dict[str, Any]) -> WorkflowResult:
        """Validate and archive a published HTTP(S) URL through one workflow."""

        from src.application.validation import validate_url_security_result

        data = dict(input_data)
        raw_url = data.get("url", "")
        url = raw_url.strip() if isinstance(raw_url, str) else ""
        is_http = url.lower().startswith(("http://", "https://"))
        if not is_http:
            return self._workflow_failure(
                PKVRuntimeError(
                    ErrorCode.URL_INVALID,
                    "归档 URL 必须是 HTTP(S) 地址",
                    stage="url_preflight",
                    recoverable=True,
                ),
                stage="url_preflight",
            )
        validation_error = validate_url_security_result(url)
        if validation_error is not None:
            return self._workflow_failure(validation_error, stage="url_preflight")
        data["url"] = url
        data.setdefault("skip_sharpen", True)
        data.setdefault("skip_review", True)

        async def operation() -> WorkflowResult:
            return await self._new_workflow().execute_async("archive-url", data)

        return await self._run_archive_write(
            "archive_url",
            {"input": data},
            operation,
        )

    async def archive_cli_input(self, input_data: dict[str, Any]) -> WorkflowResult:
        """Run the CLI's established URL, literal-text, or granted-file route.

        The CLI is the only published adapter that may present a path-bound,
        one-shot local-file capability.  The application still owns the
        workflow invocation; ``FetchStep`` consumes that identity-bound token
        before reading a file.  Non-URL text preserves the historical literal
        input behavior and is never reinterpreted here as an arbitrary file.
        """

        from src.application.validation import validate_url_security_result

        data = dict(input_data)
        raw_source = data.get("url", "")
        source = raw_source.strip() if isinstance(raw_source, str) else ""
        if not source:
            return self._workflow_failure(
                PKVRuntimeError(
                    ErrorCode.URL_INVALID,
                    "归档输入不能为空",
                    stage="url_preflight",
                    recoverable=True,
                ),
                stage="url_preflight",
            )
        if source.lower().startswith(("http://", "https://")):
            validation_error = validate_url_security_result(source)
            if validation_error is not None:
                return self._workflow_failure(validation_error, stage="url_preflight")
        data["url"] = source
        data.setdefault("skip_sharpen", True)
        data.setdefault("skip_review", True)

        async def operation() -> WorkflowResult:
            return await self._new_workflow().execute_async("archive-url", data)

        return await self._run_archive_write(
            "archive_cli_input",
            {"input": data},
            operation,
        )

    async def archive_text(
        self,
        text: str,
        *,
        title: str = "",
        skip_sharpen: bool = True,
        skip_review: bool = True,
    ) -> WorkflowResult:
        """Parse literal text then run the shared archive-text workflow path."""

        from src.application.validation import validate_text_length

        valid, message = validate_text_length(text)
        if not valid:
            return self._workflow_failure(
                PKVRuntimeError(
                    ErrorCode.WORKFLOW_CONFIG_INVALID,
                    message or "文本内容不可归档",
                    stage="text_validation",
                    recoverable=True,
                ),
                stage="text_validation",
            )
        title_clean = title.strip() if isinstance(title, str) else ""

        async def operation() -> WorkflowResult:
            # Processor construction is intentionally inside the lease.  A
            # contending archive must not consume Provider/processor work before
            # it receives the stable ``write_busy`` outcome.
            processor = self._new_text_processor()
            entry = await processor.process_text(text)
            if title_clean:
                entry.title = title_clean
            return await self._new_workflow().execute_async(
                "archive-text",
                {
                    "text": text,
                    "title": entry.title,
                    "entry": entry,
                    "content": entry.content,
                    "skip_sharpen": bool(skip_sharpen),
                    "skip_review": bool(skip_review),
                },
            )

        return await self._run_archive_write(
            "archive_text",
            {
                "input": {
                    "text": text,
                    "title": title_clean,
                    "skip_sharpen": bool(skip_sharpen),
                    "skip_review": bool(skip_review),
                }
            },
            operation,
        )

    async def _run_archive_write(
        self,
        audit_operation: str,
        audit_context: Mapping[str, Any],
        operation: Callable[[], Awaitable[WorkflowResult]],
    ) -> WorkflowResult:
        """Run one archive operation under the data-root single-writer lease."""

        async def audited_operation() -> WorkflowResult:
            # This function is called only after ``_run_write_operation`` owns
            # the R3 lease, so trace creation cannot make a contending request
            # create logs or consume processor/provider work.
            with self._audit_mutation(audit_operation, audit_context) as audit:
                try:
                    result = await operation()
                except PKVRuntimeError as error:
                    audit.fail_runtime_error(error)
                    raise
                return self._finish_workflow_audit(audit, result)

        try:
            return await self._run_write_operation(audited_operation)
        except PKVRuntimeError as error:
            if error.stage != "write_lease":
                raise
            return self._workflow_failure(error, stage="write_lease")

    async def _run_write_operation(
        self,
        operation: Callable[[], Awaitable[WorkflowResult]],
    ) -> WorkflowResult:
        """Keep the owner lease alive until a cancelled workflow has drained.

        The child task owns both the ContextVar lease identity and all default
        workflow executor calls.  ``shield`` prevents a caller cancellation
        from abandoning a live StoreStep worker; after it settles, cancellation
        is re-raised to the caller rather than transformed into a write result.
        """

        async def owned_operation() -> WorkflowResult:
            with self._write_lease_scope():
                return await operation()

        task = asyncio.create_task(owned_operation())
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await self._drain_cancelled_write_operation(task)
            raise

    @staticmethod
    async def _drain_cancelled_write_operation(task: "asyncio.Task[Any]") -> None:
        """Await a shielded owner task even if cancellation is requested again."""

        while True:
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                if task.done():
                    return
                continue
            except BaseException:
                # The cancelled caller receives its original cancellation.  The
                # child exception was retrieved here, so it cannot become an
                # unobserved task failure after the durable worker has settled.
                return
            return

    def _write_lease_scope(self) -> Any:
        """Return the operation's data-root writer scope without eager runtime IO."""

        if self.write_lease_factory is not None:
            return self.write_lease_factory(self.config)
        from src.runtime.write_lease import write_lease_scope

        return write_lease_scope(self.config.layout)

    async def _run_tracked_write_worker(
        self,
        operation: Callable[..., Any],
        /,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """Run a StoreStep durable worker using the private R3 capability bridge."""

        from src.runtime.write_lease import _run_tracked_write_worker

        return await _run_tracked_write_worker(
            self.config.layout,
            operation,
            *args,
            **kwargs,
        )

    @contextmanager
    def _audit_mutation(
        self,
        operation: str,
        context: Mapping[str, Any],
    ) -> Iterator[Any]:
        """Start one local, credential-redacted audit timeline under a write lease."""

        from src.runtime.audit import AuditTrace

        captured_context: dict[str, Any] = {
            "configuration_generation": self.snapshot.generation,
            "operation_context": self._audit_safe_value(context),
        }
        data_root_identity = getattr(self.config, "data_root_identity", None)
        if isinstance(data_root_identity, str):
            captured_context["data_root_identity"] = data_root_identity
        trace = AuditTrace(
            self.config.layout,
            secret_values=self._audit_secret_values(),
        )
        with trace.operation(operation, context=captured_context) as timeline:
            yield timeline

    def _audit_secret_values(self) -> tuple[str, ...]:
        """Supply all currently supported credential values without logging them."""

        values: list[str] = []
        for attribute in ("llm_api_key", "embd_api_key", "zhihu_cookie"):
            try:
                value = getattr(self.config, attribute, None)
            except Exception:
                continue
            if isinstance(value, str) and value:
                values.append(value)
        return tuple(values)

    @classmethod
    def _audit_safe_value(cls, value: Any) -> Any:
        """Make adapter input/result JSON-safe without rendering opaque objects."""

        if value is None or isinstance(value, (bool, int, str)):
            return value
        if isinstance(value, float):
            return value if math.isfinite(value) else "[NONFINITE_VALUE]"
        if isinstance(value, Mapping):
            return {
                key: cls._audit_safe_value(nested)
                for key, nested in value.items()
                if isinstance(key, str) and not key.startswith("_")
            }
        if isinstance(value, (list, tuple)):
            return [cls._audit_safe_value(item) for item in value]
        # Do not call ``str`` or ``repr``: custom adapters/providers can expose
        # a key there.  The marker records that a value existed without leaking
        # its implementation or contents.
        return "[UNSUPPORTED_VALUE]"

    def _finish_workflow_audit(
        self,
        audit: Any,
        result: WorkflowResult,
    ) -> WorkflowResult:
        """Finalize an archive timeline without misreporting a committed write.

        A non-error workflow terminal has already completed its durable archive
        work.  If only the final audit append/fsync fails, it must remain a
        committed outcome rather than being reflected as an uncommitted
        exception.  The returned degraded result gives adapters an explicit
        reconciliation marker; it never exposes the audit sink error itself.
        """

        payload = {
            "terminal": result.terminal,
            "success": result.success,
            "data": self._audit_safe_value(result.data),
            "errors": self._audit_safe_value(result.errors),
            "warnings": self._audit_safe_value(result.warnings),
            "issues": self._audit_safe_value(result.issues),
        }
        if result.terminal == "error":
            audit.fail(code="workflow_failed", details=payload)
            return result

        from src.runtime.audit import AuditTraceError

        try:
            audit.complete(payload)
        except AuditTraceError:
            # ``AuditTraceError`` is intentionally generic and does not retain
            # an unsafe filesystem/OS payload.  Do not broaden this to unknown
            # exceptions: programming errors in a custom audit seam are not a
            # committed-storage recovery contract.
            # ``complete`` sets its terminal flag only after append+fsync.  Its
            # documented post-commit escape hatch therefore prevents the
            # context manager from appending a false "failed" event on unwind.
            audit.mark_completion_pending_after_commit()
            return self._audit_completion_pending_result(result)
        return result

    @staticmethod
    def _audit_completion_pending_result(result: WorkflowResult) -> WorkflowResult:
        """Project a durable archive whose final local audit record is pending."""

        marker = {
            "code": ErrorCode.AUDIT_COMPLETION_PENDING.value,
            "message": "归档已提交，但本地审计完成记录待补；请勿直接重试。",
            "severity": "warning",
            "stage": "audit_trace",
            "recoverable": True,
        }
        return WorkflowResult(
            success=True,
            data={
                **result.data,
                # A durable archive must never be blindly repeated merely
                # because its independent audit completion record is pending.
                "do_not_retry": True,
                "audit_completion_pending": True,
            },
            errors=[],
            logs=list(result.logs),
            warnings=[*result.warnings, marker["message"]],
            issues=[*result.issues, marker],
            terminal="degraded",
        )

    def _finish_mutation_audit(self, audit: Any, result: Any) -> None:
        """Record a completed Kernel mutation without assuming a Store return type."""

        audit.complete({"result": self._audit_safe_value(result)})

    def chat_settings(self) -> Any:
        """Return a validated immutable chat Provider settings snapshot."""

        from src.ai.provider_factory import chat_settings_from_config

        return chat_settings_from_config(self.config)

    def create_chat_provider(self, settings: Any | None = None) -> Any:
        """Construct a chat Provider only when a chat request is dispatched."""

        if settings is None:
            settings = self.chat_settings()
        if self.chat_provider_factory is not None:
            return self.chat_provider_factory(settings)
        from src.ai.chat_provider import create_chat_provider

        return create_chat_provider(settings)

    def _new_workflow(self) -> Any:
        # Do not cache a workflow: it owns per-run mutable state and keeping this
        # factory call at operation time preserves the established test seam.
        if self.workflow_factory is not None:
            return self.workflow_factory()
        from src.workflow.engine import WorkflowEngine

        return WorkflowEngine(
            runtime_config=self.config,
            step_factory=self._create_workflow_step,
        )

    def _create_workflow_step(
        self,
        step_class: type[Any],
        step_id: str,
        step_config: dict[str, Any],
    ) -> Any:
        """Compose one workflow step from this Kernel's exact dependencies."""

        from src.workflow.steps import AnalyzeStep, FetchStep, ReviewStep, StoreStep

        if issubclass(step_class, FetchStep):
            return step_class(
                step_id=step_id,
                config=step_config,
                runtime_config=self.config,
            )
        if issubclass(step_class, AnalyzeStep):
            return step_class(
                step_id=step_id,
                config=step_config,
                runtime_config=self.config,
                deepseek_client_factory=self._create_deepseek_client,
            )
        if issubclass(step_class, StoreStep):
            return step_class(
                step_id=step_id,
                config=step_config,
                runtime_config=self.config,
                markdown_store=self.markdown_store,
                sqlite_store=self.sqlite_store,
                storage_coordinator=self.storage_coordinator,
                embedder_factory=self._create_embedder,
                vector_store_factory=self._create_writer_vector_store,
                write_worker_runner=self._run_tracked_write_worker,
            )
        if issubclass(step_class, ReviewStep):
            return step_class(
                step_id=step_id,
                config=step_config,
                runtime_config=self.config,
                review_manager_factory=self._create_review_manager,
                deepseek_client_factory=self._create_deepseek_client,
            )
        return step_class(step_id=step_id, config=step_config)

    def _new_text_processor(self) -> Any:
        # See ``_new_workflow``: this must stay operation-scoped.
        if self.text_processor_factory is not None:
            return self.text_processor_factory()
        from src.processors.text_fallback_processor import TextFallbackProcessor

        return TextFallbackProcessor(config=self.config)

    def _create_deepseek_client(self) -> Any:
        from src.ai.deepseek_client import DeepSeekClient

        return DeepSeekClient(config=self.config)

    def _create_review_manager(self) -> Any:
        from src.storage.review_manager import ReviewManager

        return ReviewManager(db_path=self.config.db_path)

    def _create_writer_vector_store(self, dim: int | None) -> Any:
        from src.storage.vector_store import VectorStore

        return VectorStore(
            index_dir=self.config.vector_index_dir,
            dim=dim,
            runtime_config=self.config,
        )

    def _new_vector_retriever(self) -> Any:
        from src.retrieval.vector_retriever import VectorRetriever

        return VectorRetriever(
            self.config.db_path,
            self.config.vector_index_dir,
            embedder_factory=self._create_embedder,
            runtime_config=self.config,
        )

    def _new_hybrid_retriever(self) -> Any:
        from src.retrieval.hybrid_retriever import HybridRetriever

        return HybridRetriever(
            self.config.db_path,
            self.config.vector_index_dir,
            embedder_factory=self._create_embedder,
            runtime_config=self.config,
        )

    def _create_embedder(self) -> Any:
        from src.ai.provider_factory import create_embedder

        return create_embedder(self.config)

    @staticmethod
    def _default_sqlite_store(config: Any) -> Any:
        from src.storage.sqlite_store import SQLiteStore

        return SQLiteStore(config.db_path, runtime_config=config)

    @staticmethod
    def _default_markdown_store(config: Any) -> Any:
        from src.storage.markdown_store import MarkdownStore

        # Application composition is also used by read-only Kernel paths.  The
        # lifecycle/bootstrap writer creates the Vault explicitly; a read must
        # never turn an uninitialized root into a non-empty repair candidate.
        return MarkdownStore(config.vault_dir, create=False)

    @staticmethod
    def _default_vector_store(config: Any) -> Any | None:
        from src.storage.vector_store import VectorStore

        if not VectorStore.has_index_artifacts(config.vector_index_dir):
            return None
        return VectorStore(
            index_dir=config.vector_index_dir,
            dim=None,
            runtime_config=config,
            allow_index_creation=False,
        )

    @staticmethod
    def _default_readonly_vector_store(config: Any) -> Any | None:
        from src.storage.vector_store import VectorStore

        if not VectorStore.has_index_artifacts(config.vector_index_dir):
            return None
        return VectorStore.open_readonly(
            index_dir=config.vector_index_dir,
            dim=None,
            runtime_config=config,
        )

    @staticmethod
    def _default_bm25_retriever(config: Any) -> Any:
        from src.retrieval.bm25_retriever import BM25Retriever

        return BM25Retriever(config.db_path, runtime_config=config)

    @staticmethod
    def _default_query_router(config: Any, token_threshold: int) -> Any:
        from src.retrieval.query_router import QueryRouter

        return QueryRouter(
            db_path=config.db_path,
            vector_index_dir=config.vector_index_dir,
            token_threshold=token_threshold,
            embedder_factory=lambda: KnowledgeApplication._create_embedder_for(config),
            runtime_config=config,
        )

    @staticmethod
    def _create_embedder_for(config: Any) -> Any:
        from src.ai.provider_factory import create_embedder

        return create_embedder(config)

    @staticmethod
    def _workflow_failure(
        error: PKVRuntimeError,
        *,
        stage: str,
    ) -> WorkflowResult:
        issue_stage = error.stage if isinstance(error.stage, str) else stage
        issue = {
            "code": error.code.value,
            "message": str(error),
            "severity": "error",
            "stage": issue_stage,
            "recoverable": error.recoverable is True,
        }
        return WorkflowResult(
            success=False,
            errors=[str(error)],
            issues=[issue],
            terminal="error",
        )


_default_application: KnowledgeApplication | None = None
_application_generation = 0
_application_lock = RLock()


def _publish_application(config: Any, *, replace_legacy_config: bool) -> KnowledgeApplication:
    """Create and atomically publish the next process application snapshot.

    ``_application_lock`` is held by every caller.  When the legacy Config
    identity changes too, take its re-entrant lock for the tiny publication
    window so a newly obtained ``get_config()`` can never be paired with the
    old default application graph.
    """

    global _default_application, _application_generation
    if not replace_legacy_config:
        _application_generation += 1
        _default_application = KnowledgeApplication(
            config,
            snapshot_generation=_application_generation,
        )
        return _default_application

    import src.utils.config as config_module

    with config_module._CONFIG_INSTANCE_LOCK:
        _application_generation += 1
        application = KnowledgeApplication(
            config,
            snapshot_generation=_application_generation,
        )
        config_module.replace_config_instance(config)
        _default_application = application
        return application


def configure_application(config: Any) -> KnowledgeApplication:
    """Install the validated runtime config as this process's application root.

    Entry points call this immediately after ``bootstrap_runtime`` succeeds.
    Replacing an older instance is intentional for explicit wrapper test startup;
    normal product processes configure it exactly once.
    """

    with _application_lock:
        # This config is becoming the process default, so compatibility callers
        # of get_config() must see the same identity as default workflows.
        return _publish_application(config, replace_legacy_config=True)


def get_application(config: Any | None = None) -> KnowledgeApplication:
    """Return the default process application or an explicit-config instance.

    Product adapters normally call this after ``bootstrap_runtime``.  An
    explicit config deliberately creates an isolated application instance,
    making unit tests and per-command config snapshots deterministic without
    contaminating the process singleton used by a long-running adapter process.
    """

    global _default_application
    if config is not None:
        # Explicit config is deliberately isolated: its operation graph must
        # retain B even while process legacy config remains A or is reloaded.
        return KnowledgeApplication(config)
    with _application_lock:
        if _default_application is None:
            from src.utils.config import get_config

            return _publish_application(get_config(), replace_legacy_config=False)
        return _default_application


def reload_application() -> KnowledgeApplication:
    """Reload configuration for subsequent app operations in this process.

    Settings UI writes the user-local config first; this refresh is deliberately
    explicit so it neither runs bootstrap/migrations nor changes the data-root
    contract.  Existing in-flight operations retain their captured dependencies.
    """

    import src.utils.config as config_module

    with _application_lock:
        # Rebuild from the same immutable source snapshot whenever possible.
        # A different data root is a lifecycle operation (impact/backup/explicit
        # confirmation), not an ordinary setting reload.  Existing operations
        # retain their old application object either way.
        current_config = (
            _default_application.config
            if _default_application is not None
            else config_module.get_config()
        )
        reload_snapshot = getattr(current_config, "reload_snapshot", None)
        config = (
            reload_snapshot()
            if callable(reload_snapshot)
            else config_module.Config()
        )
        return _publish_application(config, replace_legacy_config=True)


def reset_application() -> None:
    """Clear the default application singleton for isolated tests."""

    global _default_application
    with _application_lock:
        _default_application = None
