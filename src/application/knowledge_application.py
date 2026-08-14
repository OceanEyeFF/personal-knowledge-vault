"""Shared application service used by headless adapters and external wrappers.

``KnowledgeApplication`` is the composition boundary for long-lived local
services.  It intentionally has no dependency on an adapter package: callers
bring validated input in and project its domain results back to a UI, CLI, or
protocol response.  Provider-backed dependencies stay lazy, so BM25-only
operations do not require credentials or a vector index.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from src.retrieval.result import RetrievalIssue, SearchResponse
from src.runtime.errors import ErrorCode, PKVRuntimeError
from src.workflow.models import WorkflowResult


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
        return await self._new_workflow().execute_async("archive-url", data)

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
        return await self._new_workflow().execute_async("archive-url", data)

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
        processor = self._new_text_processor()
        entry = await processor.process_text(text)
        title_clean = title.strip() if isinstance(title, str) else ""
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

        return SQLiteStore(config.db_path)

    @staticmethod
    def _default_markdown_store(config: Any) -> Any:
        from src.storage.markdown_store import MarkdownStore

        return MarkdownStore(config.vault_dir)

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

        return BM25Retriever(config.db_path)

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


def configure_application(config: Any) -> KnowledgeApplication:
    """Install the validated runtime config as this process's application root.

    Entry points call this immediately after ``bootstrap_runtime`` succeeds.
    Replacing an older instance is intentional for explicit wrapper test startup;
    normal product processes configure it exactly once.
    """

    global _default_application
    _default_application = KnowledgeApplication(config)
    return _default_application


def get_application(config: Any | None = None) -> KnowledgeApplication:
    """Return the default process application or an explicit-config instance.

    Product adapters normally call this after ``bootstrap_runtime``.  An
    explicit config deliberately creates an isolated application instance,
    making unit tests and per-command config snapshots deterministic without
    contaminating the process singleton used by a long-running adapter process.
    """

    global _default_application
    if config is not None:
        return KnowledgeApplication(config)
    if _default_application is None:
        from src.utils.config import get_config

        _default_application = KnowledgeApplication(get_config())
    return _default_application


def reload_application() -> KnowledgeApplication:
    """Reload configuration for subsequent app operations in this process.

    Settings UI writes the user-local config first; this refresh is deliberately
    explicit so it neither runs bootstrap/migrations nor changes the data-root
    contract.  Existing in-flight operations retain their captured dependencies.
    """

    import src.utils.config as config_module

    config = config_module.Config()
    # Keep one process-wide configuration identity.  Workflow/processor code
    # now receives the Kernel config explicitly, while legacy callers of
    # ``get_config`` must observe the same refreshed snapshot rather than the
    # pre-settings singleton.
    config_module._config_instance = config
    return configure_application(config)


def reset_application() -> None:
    """Clear the default application singleton for isolated tests."""

    global _default_application
    _default_application = None
