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
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from datetime import datetime
import math
from threading import RLock
from typing import Any
from zoneinfo import ZoneInfo

from src.retrieval.result import RetrievalIssue, SearchResponse
from src.runtime.errors import ErrorCode, PKVRuntimeError
from src.workflow.models import WorkflowResult


@dataclass(frozen=True)
class ApplicationSnapshot:
    """Immutable identity captured by every operation of one application graph."""

    generation: int
    config: Any


@dataclass(frozen=True)
class _UnavailableVectorRetriever:
    """Generation-binding failure projected as a vector branch response.

    Hybrid retrieval may still return healthy BM25 candidates, but it must carry
    the exact non-ready binding reason rather than construct a Provider or read
    a flat index directory.
    """

    error: PKVRuntimeError

    def search(self, query: str, limit: int = 10) -> SearchResponse:
        del query, limit
        return SearchResponse.failed_response(
            RetrievalIssue(
                code=self.error.code,
                message=str(self.error),
                stage=self.error.stage or "embedding_index",
                recoverable=self.error.recoverable,
            ),
            strategy="vector",
        )


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
            from src.runtime.writer_inventory import require_active_data_root_writer

            require_active_data_root_writer(
                self.config.layout,
                owner="application_vector_mutation",
            )
            factory = self.vector_store_factory or self._default_vector_store
            candidate = factory(self.config)
            self._vector_store_checked = candidate is not None
            if candidate is not None:
                self._vector_store = candidate
        return self._vector_store

    @property
    def readonly_vector_store(self) -> Any | None:
        """Open the current ready generation in strict no-write/no-lock mode."""

        store, _ = self.resolve_readonly_vector_store()
        return store

    def resolve_readonly_vector_store(self) -> tuple[Any | None, PKVRuntimeError | None]:
        """Resolve exactly one ready generation for a complete read operation.

        Product composition never probes ``config.vector_index_dir`` here.  A
        non-ready lifecycle binding returns its stable typed reason so CLI/MCP
        can distinguish processing/retry/budget/authorization from true empty
        search results.  Lightweight legacy test graphs keep their injected
        read-only seam until their callers are migrated to this Application port.
        """

        if self._uses_internal_ai_automation():
            try:
                from src.runtime.embedding_lifecycle import resolve_embedding_index_binding
                from src.storage.vector_store import VectorStore

                binding = resolve_embedding_index_binding(self.config)
                return (
                    VectorStore.open_readonly(
                        binding.index_dir,
                        dim=binding.contract.dimension,
                        runtime_config=self.config,
                        layout=self.config.layout,
                    ),
                    None,
                )
            except PKVRuntimeError as error:
                return None, error
            except Exception as exc:
                return (
                    None,
                    PKVRuntimeError(
                        ErrorCode.RETRIEVAL_INDEX_UNAVAILABLE,
                        "Embedding generation 无法以只读方式打开。",
                        stage="embedding_index",
                        recoverable=True,
                    ),
                )

        if self._readonly_vector_store is None:
            candidate = self._default_readonly_vector_store(self.config)
            self._readonly_vector_store_checked = candidate is not None
            if candidate is not None:
                self._readonly_vector_store = candidate
        return self._readonly_vector_store, None

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
            # Constructing the coordinator creates its durable operation-journal
            # directory, so even lazy composition is a mutation-owned action.
            from src.runtime.writer_inventory import require_active_data_root_writer

            require_active_data_root_writer(
                self.config.layout,
                owner="storage_coordinator",
            )
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
            try:
                return self._new_vector_retriever().search(query, limit)
            except PKVRuntimeError as error:
                return self._generation_unavailable_search_response(error, strategy="vector")
        if normalized_strategy == "hybrid":
            return self._new_hybrid_retriever().search(query, limit)
        raise ValueError(f"不支持的检索策略: {strategy}")

    def related(self, knowledge_id: int, limit: int = 5) -> dict[str, Any]:
        """Return read-only vector neighbours from one active generation binding.

        This is the shared CLI/MCP port.  In particular, a non-ready binding is
        a degradable lifecycle state, never a flat-index probe and never an
        otherwise-successful ``no_hits`` response.
        """

        if isinstance(knowledge_id, bool) or not isinstance(knowledge_id, int) or knowledge_id <= 0:
            return self._related_invalid("无效的 knowledge_id，需要正整数", "related_entry_lookup")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            return self._related_invalid("limit 必须是正整数", "limit_validation")
        safe_limit = min(limit, 20)
        try:
            seed = self.sqlite_store.query_by_id(knowledge_id)
        except PKVRuntimeError as error:
            return self._related_error(error, message="条目读取失败")
        except Exception:
            return self._related_error(
                PKVRuntimeError(
                    ErrorCode.RETRIEVAL_BACKEND_FAILED,
                    "条目读取失败。",
                    stage="related_entry_lookup",
                    recoverable=True,
                ),
                message="条目读取失败",
            )
        if seed is None:
            return {
                "status": "no_hits",
                "strategy": "vector_related",
                "total": 0,
                "results": [],
                "issues": [],
                "message": "未找到条目",
            }

        if self._uses_internal_ai_automation():
            vector_store, unavailable = self.resolve_readonly_vector_store()
        else:
            # Compatibility graph for injected test/legacy adapters.  Product
            # Config snapshots always take the binding-aware branch above.
            vector_store, unavailable = self.readonly_vector_store, None
        if unavailable is not None or vector_store is None:
            error = unavailable or PKVRuntimeError(
                ErrorCode.RETRIEVAL_INDEX_UNAVAILABLE,
                "检索索引不可用",
                stage="vector_index",
                recoverable=True,
            )
            return self._related_degraded(error, message="向量索引不可用，无法获取关联知识")

        try:
            document_vector = vector_store.get_doc_vector(knowledge_id)
            if document_vector is None:
                return self._related_degraded(
                    PKVRuntimeError(
                        ErrorCode.RETRIEVAL_INDEX_UNAVAILABLE,
                        "当前 generation 缺少该条目的文档向量。",
                        stage="document_vector",
                        recoverable=True,
                    ),
                    message="该条目暂无向量，无法获取关联知识",
                )
            raw_results = vector_store.search_doc(document_vector, k=safe_limit + 1)
            if (
                type(raw_results) is not list
                or len(raw_results) > safe_limit + 1
                or not all(
                    type(item) is tuple
                    and len(item) == 2
                    and type(item[0]) is int
                    and item[0] > 0
                    and type(item[1]) in {int, float}
                    and math.isfinite(item[1])
                    for item in raw_results
                )
                or len({item[0] for item in raw_results}) != len(raw_results)
            ):
                raise PKVRuntimeError(
                    ErrorCode.RETRIEVAL_BACKEND_FAILED,
                    "关联向量查询返回无效结果。",
                    stage="vector_related",
                    recoverable=True,
                )
            results: list[dict[str, Any]] = []
            for related_id, distance in raw_results:
                if related_id == knowledge_id:
                    continue
                if len(results) >= safe_limit:
                    break
                entry = self.sqlite_store.query_by_id(related_id)
                if not isinstance(entry, Mapping):
                    raise PKVRuntimeError(
                        ErrorCode.RETRIEVAL_METADATA_INCONSISTENT,
                        "关联向量与知识元数据不一致。",
                        stage="vector_metadata_read",
                        recoverable=False,
                    )
                tags = entry.get("tags")
                if tags is None:
                    tags_value: list[str] = []
                elif isinstance(tags, str):
                    tags_value = [value.strip() for value in tags.split(",") if value.strip()]
                else:
                    raise PKVRuntimeError(
                        ErrorCode.RETRIEVAL_METADATA_INCONSISTENT,
                        "关联条目的标签元数据不一致。",
                        stage="vector_metadata_read",
                        recoverable=False,
                    )
                results.append(
                    {
                        "knowledge_id": related_id,
                        "title": entry.get("title") or f"条目 {related_id}",
                        "abstract": entry.get("summary_one_sentence") or "",
                        "tags": tags_value,
                        "source_type": entry.get("source_type") or "",
                        "score": round(min(1.0, max(0.0, 1.0 - distance)), 4),
                    }
                )
        except PKVRuntimeError as error:
            return self._related_error(error, message="向量关联查询不可用")
        except Exception:
            return self._related_error(
                PKVRuntimeError(
                    ErrorCode.RETRIEVAL_BACKEND_FAILED,
                    "向量关联查询不可用。",
                    stage="vector_related",
                    recoverable=True,
                ),
                message="向量关联查询不可用",
            )
        return {
            "status": "success" if results else "no_hits",
            "strategy": "vector_related",
            "total": len(results),
            "results": results,
            "issues": [],
            **({"message": "未找到关联条目"} if not results else {}),
        }

    @staticmethod
    def _related_invalid(message: str, stage: str) -> dict[str, Any]:
        return {
            "status": "invalid",
            "strategy": "vector_related",
            "total": 0,
            "results": [],
            "issues": [
                {
                    "code": ErrorCode.RETRIEVAL_INVALID_QUERY.value,
                    "message": message,
                    "stage": stage,
                    "recoverable": True,
                }
            ],
            "message": message,
        }

    @staticmethod
    def _related_degraded(error: PKVRuntimeError, *, message: str) -> dict[str, Any]:
        return {
            "status": "degraded",
            "strategy": "vector_related",
            "total": 0,
            "results": [],
            "issues": [
                {
                    "code": error.code.value,
                    "message": str(error),
                    "stage": error.stage or "embedding_index",
                    "recoverable": error.recoverable,
                }
            ],
            "message": message,
        }

    @staticmethod
    def _related_error(error: PKVRuntimeError, *, message: str) -> dict[str, Any]:
        return {
            "status": "error",
            "strategy": "vector_related",
            "total": 0,
            "results": [],
            "issues": [
                {
                    "code": error.code.value,
                    "message": str(error),
                    "stage": error.stage or "vector_related",
                    "recoverable": error.recoverable,
                }
            ],
            "message": message,
        }

    @staticmethod
    def _generation_unavailable_search_response(
        error: PKVRuntimeError,
        *,
        strategy: str,
    ) -> SearchResponse:
        """Keep a non-ready generation distinct from a completed empty query."""

        return SearchResponse.failed_response(
            RetrievalIssue(
                code=error.code,
                message=str(error),
                stage=error.stage or "embedding_index",
                recoverable=error.recoverable,
            ),
            strategy=strategy,
        )

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
        if self._uses_internal_ai_automation():
            # Archive persists the primary Markdown/SQLite facts first.  AI
            # analysis and Embedding become one internal lifecycle task after
            # that commit, so a Provider is never created in this request.
            data["defer_ai_automation"] = True

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
        if self._uses_internal_ai_automation():
            data["defer_ai_automation"] = True

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
            workflow_data = {
                "text": text,
                "title": entry.title,
                "entry": entry,
                "content": entry.content,
                "skip_sharpen": bool(skip_sharpen),
                "skip_review": bool(skip_review),
            }
            if self._uses_internal_ai_automation():
                workflow_data["defer_ai_automation"] = True
            return await self._new_workflow().execute_async("archive-text", workflow_data)

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
                result = self._schedule_archive_ai_automation(result)
                result = self._drain_scheduled_ai_automation(result)
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
                with self._runtime_file_log_scope(owner="application_archive"):
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

    def _uses_internal_ai_automation(self) -> bool:
        """Whether this graph has a real Config snapshot with the R4 policy port.

        Lightweight application doubles remain an intentionally narrow unit-test
        seam.  Product composition always provides ``Config.get`` and therefore
        always defers paid AI work until the internal lifecycle has admitted it.
        """

        # ``Config`` is the production immutable snapshot type.  A number of
        # focused adapter tests intentionally provide a get()-shaped lightweight
        # object; treating that object as an authorized product lifecycle would
        # turn an isolated fake workflow into a data-root task writer.
        from src.utils.config import Config

        return isinstance(Config, type) and isinstance(self.config, Config)

    def _schedule_archive_ai_automation(self, result: WorkflowResult) -> WorkflowResult:
        """Invalidate stale retrieval and queue internal AI work after core commit.

        This runs inside the archive's existing data-root lease and audit scope.
        It neither creates a Provider nor starts a worker; P3 owns that later
        lifecycle drain.  A post-commit binding/queue fault degrades the result
        instead of inviting a duplicate archive retry.
        """

        if not self._uses_internal_ai_automation() or result.terminal == "error":
            return result
        payload = result.data if isinstance(result.data, Mapping) else {}
        if (
            payload.get("core_committed") is not True
            or payload.get("ai_automation_deferred") is not True
        ):
            return result
        mutation_id = payload.get("operation_id")
        if not isinstance(mutation_id, str) or not mutation_id:
            return self._automation_degraded_result(
                result,
                code=ErrorCode.REPAIR_REQUIRED,
                message="文档已入库，但缺少内部 AI 任务标识；请先修复运行态。",
                stage="ai_automation_lifecycle",
                status="repair_required",
                task_state=None,
            )

        try:
            from src.runtime.ai_automation_policy import (
                AutomationPolicyState,
                inspect_ai_automation_policy,
            )
            from src.runtime.embedding_lifecycle import (
                EmbeddingIndexState,
                SQLiteEmbeddingSource,
                publish_embedding_nonready_binding,
            )
            from src.storage.ai_automation_store import (
                AIAutomationTaskState,
                AIAutomationTaskStore,
            )

            policy = inspect_ai_automation_policy(self.config)
            source = SQLiteEmbeddingSource()
            captured = source.capture(self.config)
            task_state: str | None = None
            binding_state = EmbeddingIndexState.REBUILD_REQUIRED
            status = "rebuild_required"
            message = "文档已入库，正在等待内部 AI 生命周期处理。"
            code = ErrorCode.EMBEDDING_REBUILD_REQUIRED

            if policy.state is AutomationPolicyState.READY:
                assert policy.policy_fingerprint is not None
                task = AIAutomationTaskStore(self.config.layout).enqueue_rebuild(
                    mutation_id=mutation_id,
                    source_digest=captured.summary.digest,
                    policy_fingerprint=policy.policy_fingerprint,
                    state=AIAutomationTaskState.PENDING,
                )
                task_state = task.state.value
                status = "processing"
                message = "文档已入库，自动 AI 任务已排队处理。"
                binding_state = EmbeddingIndexState.PROCESSING
            elif policy.state in {
                AutomationPolicyState.AUTHORIZATION_REQUIRED,
                AutomationPolicyState.INVALID,
            }:
                binding_state = EmbeddingIndexState.AUTHORIZATION_REQUIRED
                status = "authorization_required"
                code = ErrorCode.EMBEDDING_AUTOMATION_AUTHORIZATION_REQUIRED
                message = "文档已入库，自动 AI 任务等待当前策略确认。"
                if policy.policy_fingerprint is not None:
                    task = AIAutomationTaskStore(self.config.layout).enqueue_rebuild(
                        mutation_id=mutation_id,
                        source_digest=captured.summary.digest,
                        policy_fingerprint=policy.policy_fingerprint,
                        state=AIAutomationTaskState.AUTHORIZATION_REQUIRED,
                    )
                    task_state = task.state.value
            else:
                message = "文档已入库；AI 自动化尚未启用，Embedding 等待后续内部处理。"

            binding = publish_embedding_nonready_binding(
                self.config,
                state=binding_state,
                source=source,
            )
            # A second capture would be a logic error under the same writer
            # lease; never report that a queue represents a different source.
            if binding.source is None or binding.source.digest != captured.summary.digest:
                raise PKVRuntimeError(
                    ErrorCode.RUNTIME_PLAN_STALE,
                    "知识源在内部 AI 任务登记期间发生变化。",
                    stage="ai_automation_lifecycle",
                    recoverable=True,
                )
            return self._automation_degraded_result(
                result,
                code=code,
                message=message,
                stage="ai_automation_lifecycle",
                status=status,
                task_state=task_state,
            )
        except PKVRuntimeError as error:
            return self._automation_degraded_result(
                result,
                code=error.code,
                message="文档已入库，但内部 AI 生命周期未能安全登记；请勿直接重试文档导入。",
                stage=error.stage or "ai_automation_lifecycle",
                status="retry_required",
                task_state=None,
            )

    @staticmethod
    def _automation_degraded_result(
        result: WorkflowResult,
        *,
        code: ErrorCode,
        message: str,
        stage: str,
        status: str,
        task_state: str | None,
    ) -> WorkflowResult:
        """Keep the committed archive visible while projecting AI lifecycle status."""

        issue = {
            "code": code.value,
            "message": message,
            "severity": "warning",
            "stage": stage,
            "recoverable": True,
        }
        return WorkflowResult(
            success=True,
            data={
                **(dict(result.data) if isinstance(result.data, Mapping) else {}),
                "ai_automation": {
                    "status": status,
                    "task_state": task_state,
                },
                "do_not_retry": True,
            },
            errors=[],
            logs=list(result.logs),
            warnings=[*result.warnings, message],
            issues=[*result.issues, issue],
            terminal="degraded",
        )

    def _schedule_committed_mutation_ai_automation(
        self,
        mutation_id: str,
    ) -> Mapping[str, Any] | None:
        """Register a non-workflow source mutation (currently Kernel delete).

        The Kernel intentionally keeps its historical storage-result DTO.  This
        private bridge shares the archive registration transaction without
        adding a public rebuild/status method or widening the Kernel API.
        """

        outcome = self._schedule_archive_ai_automation(
            WorkflowResult(
                success=True,
                data={
                    "core_committed": True,
                    "operation_id": mutation_id,
                    "ai_automation_deferred": True,
                },
            )
        )
        outcome = self._drain_scheduled_ai_automation(outcome)
        status = outcome.data.get("ai_automation")
        return status if isinstance(status, Mapping) else None

    def _drain_scheduled_ai_automation(self, result: WorkflowResult) -> WorkflowResult:
        """Run one bounded, internal AI drain while the mutation lease is live.

        This is deliberately not a daemon and is never reached from a read
        path.  Archive/delete have already committed their core data before this
        point.  Therefore every non-ready outcome is projected as a recoverable
        lifecycle state rather than asking callers to repeat the import.
        """

        if not self._uses_internal_ai_automation():
            return result
        payload = result.data if isinstance(result.data, Mapping) else {}
        automation = payload.get("ai_automation")
        if not isinstance(automation, Mapping) or automation.get("task_state") != "pending":
            return result

        try:
            outcome = self._drain_one_ai_automation_task()
        except PKVRuntimeError as error:
            return self._automation_degraded_result(
                result,
                code=error.code,
                message="文档已入库，但自动 AI 任务需要重试。",
                stage=error.stage or "ai_automation_lifecycle",
                status="retry_required",
                task_state="retry_required",
            )

        if outcome is None:
            return result
        status, task_state, code, message = outcome
        if status == "ready":
            return WorkflowResult(
                success=True,
                data={
                    **dict(payload),
                    "ai_automation": {"status": status, "task_state": task_state},
                    "do_not_retry": True,
                },
                errors=[],
                logs=list(result.logs),
                warnings=[],
                issues=[],
                terminal="success",
            )
        return self._automation_degraded_result(
            result,
            code=code,
            message=message,
            stage="ai_automation_lifecycle",
            status=status,
            task_state=task_state,
        )

    @staticmethod
    def _estimated_embedding_tokens(captured: Any) -> int:
        """Return a local, price-free conservative input-token estimate.

        Python has no bundled tokenizer for every configured Provider.  The
        estimate is therefore explicitly recorded as a local estimate and is
        used only as a hard-cap reservation; it is never presented as a price or
        as Provider-reported usage.
        """

        text_characters = sum(
            len(record.content) + sum(len(chunk) for _, chunk in record.chunks)
            for record in captured.records
        )
        return math.ceil(text_characters / 4)

    def _drain_one_ai_automation_task(
        self,
    ) -> tuple[str, str, ErrorCode, str] | None:
        """Claim and execute the newest eligible internal generation task.

        Preconditions are intentionally ordered: this method is called only
        from an already-held Application writer lease; it then re-inspects
        policy, source and plan, reserves tokens, and only then constructs an
        Embedding Provider.  ``execute_embedding_rebuild`` receives the active
        lease through a no-op nested scope, so it retains its full stale-plan,
        staging, audit and pointer-CAS checks without attempting a second root
        lock acquisition.
        """

        from src.runtime.ai_automation_policy import (
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
        from src.storage.ai_automation_store import AIAutomationTaskStore

        store = AIAutomationTaskStore(self.config.layout)
        # At most one current-source task is executed per foreground mutation.
        # Older queued tasks may be retired without Provider work, otherwise a
        # failed historical task could permanently hide the newly committed one.
        for _ in range(8):
            task = store.claim_next()
            if task is None:
                return None
            assert task.claim_token is not None
            claim_token = task.claim_token
            policy = inspect_ai_automation_policy(self.config)
            source = SQLiteEmbeddingSource()

            if policy.state in {
                AutomationPolicyState.AUTHORIZATION_REQUIRED,
                AutomationPolicyState.INVALID,
            }:
                store.mark_authorization_required(task.task_id, claim_token=claim_token)
                publish_embedding_nonready_binding(
                    self.config,
                    state=EmbeddingIndexState.AUTHORIZATION_REQUIRED,
                    source=source,
                )
                return (
                    "authorization_required",
                    "authorization_required",
                    ErrorCode.EMBEDDING_AUTOMATION_AUTHORIZATION_REQUIRED,
                    "文档已入库，自动 AI 任务等待当前策略确认。",
                )
            if policy.state is not AutomationPolicyState.READY:
                store.mark_retry(
                    task.task_id,
                    claim_token=claim_token,
                    error_code=ErrorCode.EMBEDDING_REBUILD_REQUIRED.value,
                )
                publish_embedding_nonready_binding(
                    self.config,
                    state=EmbeddingIndexState.REBUILD_REQUIRED,
                    source=source,
                )
                return (
                    "rebuild_required",
                    "retry_required",
                    ErrorCode.EMBEDDING_REBUILD_REQUIRED,
                    "AI 自动化当前未启用；文档已保存，Embedding 等待后续内部处理。",
                )

            assert policy.policy_fingerprint is not None
            inspection = inspect_embedding_index(self.config, source=source)
            if inspection.source is None or inspection.source.digest != task.source_digest:
                store.mark_superseded(task.task_id, claim_token=claim_token)
                continue
            if task.policy_fingerprint != policy.policy_fingerprint:
                # A newer explicitly approved policy may not be silently applied
                # to an old claim.  It remains visible as authorization-required
                # until the next mutation lifecycle records a matching task.
                store.mark_authorization_required(task.task_id, claim_token=claim_token)
                publish_embedding_nonready_binding(
                    self.config,
                    state=EmbeddingIndexState.AUTHORIZATION_REQUIRED,
                    source=source,
                )
                return (
                    "authorization_required",
                    "authorization_required",
                    ErrorCode.EMBEDDING_AUTOMATION_AUTHORIZATION_REQUIRED,
                    "AI 策略已变化；现有任务等待以新策略重新登记。",
                )

            try:
                plan = plan_embedding_rebuild(inspection)
            except PKVRuntimeError as error:
                store.mark_retry(
                    task.task_id,
                    claim_token=claim_token,
                    error_code=error.code.value,
                )
                publish_embedding_nonready_binding(
                    self.config,
                    state=EmbeddingIndexState.RETRY_REQUIRED,
                    source=source,
                )
                return (
                    "retry_required",
                    "retry_required",
                    error.code,
                    "自动 AI 任务的运行态计划需要重试。",
                )

            quota = policy.token_quota
            assert quota is not None
            local_now = datetime.now(ZoneInfo(quota.timezone))
            estimate = self._estimated_embedding_tokens(inspection._captured_source)
            reservation = store.reserve_tokens(
                task.task_id,
                claim_token=claim_token,
                timezone=quota.timezone,
                local_day=local_now.date().isoformat(),
                local_month=local_now.strftime("%Y-%m"),
                reserved_tokens=estimate,
                daily_total_tokens=quota.daily_total_tokens,
                monthly_total_tokens=quota.monthly_total_tokens,
            )
            if reservation is None:
                publish_embedding_nonready_binding(
                    self.config,
                    state=EmbeddingIndexState.BUDGET_PAUSED,
                    source=source,
                )
                return (
                    "budget_paused",
                    "budget_paused",
                    ErrorCode.EMBEDDING_BUDGET_PAUSED,
                    "文档已入库；已达到 token 预算，自动 AI 任务暂停。",
                )

            # Reserve and record before the Provider is constructed.  If a
            # provider can later expose reliable usage, it may append a distinct
            # provider_reported row; unknown cached/generated dimensions remain
            # NULL rather than fabricated zeros.
            store.record_usage(
                task.task_id,
                claim_token=claim_token,
                reservation_id=reservation.reservation_id,
                usage=TokenUsage(
                    embedding_input_tokens=estimate,
                    source="local_estimate",
                ),
            )
            try:
                provider = self._create_embedder()
                execution = execute_embedding_rebuild(
                    plan,
                    confirm_embedding_rebuild(plan, allow_network=True),
                    embedder=PreChunkedEmbeddingAdapter(provider),
                    writer_lease_factory=lambda _config: nullcontext(),
                )
            except PKVRuntimeError as error:
                store.settle_reservation(reservation.reservation_id, claim_token=claim_token)
                store.mark_retry(
                    task.task_id,
                    claim_token=claim_token,
                    error_code=error.code.value,
                )
                publish_embedding_nonready_binding(
                    self.config,
                    state=EmbeddingIndexState.RETRY_REQUIRED,
                    source=source,
                )
                return (
                    "retry_required",
                    "retry_required",
                    error.code,
                    "自动 AI 任务失败；文档已保存，稍后将重试。",
                )
            except Exception:
                store.settle_reservation(reservation.reservation_id, claim_token=claim_token)
                store.mark_retry(
                    task.task_id,
                    claim_token=claim_token,
                    error_code=ErrorCode.PROVIDER_UNAVAILABLE.value,
                )
                publish_embedding_nonready_binding(
                    self.config,
                    state=EmbeddingIndexState.RETRY_REQUIRED,
                    source=source,
                )
                return (
                    "retry_required",
                    "retry_required",
                    ErrorCode.PROVIDER_UNAVAILABLE,
                    "自动 AI 服务暂不可用；文档已保存，稍后将重试。",
                )

            store.settle_reservation(reservation.reservation_id, claim_token=claim_token)
            store.mark_completed(task.task_id, claim_token=claim_token)
            return (
                "ready",
                "completed",
                ErrorCode.EMBEDDING_REBUILD_REQUIRED,
                "自动 AI 任务已完成。",
            )
        raise PKVRuntimeError(
            ErrorCode.RUNTIME_PLAN_STALE,
            "AI 自动化队列包含过多已过期任务；请在下一次写入时继续处理。",
            stage="ai_automation_lifecycle",
            recoverable=True,
        )

    @contextmanager
    def _runtime_file_log_scope(self, *, owner: str) -> Iterator[None]:
        """Bind file logging to this immutable application snapshot's mutation.

        The scope itself opens no file.  If a CLI/MCP process configured a
        matching delayed handler, only records emitted while this exact
        snapshot owns the R3 lease reach ``pkv.log``.
        """

        from src.runtime.file_logging import (
            runtime_file_log_binding,
            runtime_file_log_scope,
        )

        binding = runtime_file_log_binding(
            self.config,
            snapshot_id=f"config-{id(self.config)}",
        )
        with runtime_file_log_scope(binding, owner=owner):
            yield

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

        return TextFallbackProcessor(
            config=self.config,
            allow_ai_enrichment=not self._uses_internal_ai_automation(),
        )

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

        if self._uses_internal_ai_automation():
            from src.runtime.embedding_lifecycle import resolve_embedding_index_binding

            binding = resolve_embedding_index_binding(self.config)
            index_dir = binding.index_dir
        else:
            index_dir = self.config.vector_index_dir
        return VectorRetriever(
            self.config.db_path,
            index_dir,
            embedder_factory=self._create_embedder,
            runtime_config=self.config,
        )

    def _new_hybrid_retriever(self) -> Any:
        from src.retrieval.hybrid_retriever import HybridRetriever

        if self._uses_internal_ai_automation():
            from src.runtime.embedding_lifecycle import resolve_embedding_index_binding

            try:
                binding = resolve_embedding_index_binding(self.config)
            except PKVRuntimeError as error:
                return HybridRetriever(
                    self.config.db_path,
                    self.config.layout.vector_index_dir,
                    runtime_config=self.config,
                    vector_retriever=_UnavailableVectorRetriever(error),
                )
            index_dir = binding.index_dir
        else:
            index_dir = self.config.vector_index_dir
        return HybridRetriever(
            self.config.db_path,
            index_dir,
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

    def _default_query_router(self, config: Any, token_threshold: int) -> Any:
        from src.retrieval.query_router import QueryRouter

        if self._uses_internal_ai_automation():
            return QueryRouter(
                db_path=config.db_path,
                # This placeholder is never opened: the per-search factory
                # resolves one immutable generation binding first.
                vector_index_dir=config.layout.vector_index_dir,
                token_threshold=token_threshold,
                runtime_config=config,
                hybrid_retriever_factory=self._new_hybrid_retriever,
            )
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
        application = _publish_application(config, replace_legacy_config=True)
        # Rebind only an already-opted-in process logger.  Reload never creates
        # pkv.log (the replacement handler remains delayed), while an older
        # shielded write retains its previous binding until it drains.
        from src.runtime.file_logging import runtime_file_log_binding
        from src.utils.logger import LoggerSetup

        LoggerSetup.rebind_runtime_file_handler(
            runtime_file_log_binding(config, snapshot_id=f"config-{id(config)}")
        )
        return application


def reset_application() -> None:
    """Clear the default application singleton for isolated tests."""

    global _default_application
    with _application_lock:
        _default_application = None
