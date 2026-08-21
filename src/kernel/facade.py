"""Stable headless Kernel facade used by peripheral adapters and wrappers."""

from __future__ import annotations

from threading import RLock
from typing import Any, ClassVar

from src.application import (
    KnowledgeApplication,
    configure_application,
    get_application,
    reload_application,
    reset_application,
)
from src.kernel.preview import PreviewOutcome, load_preview_with_store
from src.runtime.bootstrap import bootstrap_runtime
from src.runtime.errors import PKVRuntimeError
from src.utils.config import Config


class KernelChatSessions:
    """Narrow chat-session port; no storage implementation escapes the Kernel."""

    def __init__(self, application: KnowledgeApplication) -> None:
        self._application = application

    @property
    def _store(self) -> Any:
        return self._application.sqlite_store

    def _write_lease_scope(self) -> Any:
        return self._application._write_lease_scope()

    def create_session(self, session_id: str, title: str) -> Any:
        with self._write_lease_scope():
            with self._application._audit_mutation(
                "chat_session_create",
                {"session_id": session_id, "title": title},
            ) as audit:
                try:
                    result = self._store.create_session(session_id, title)
                except PKVRuntimeError as error:
                    audit.fail_runtime_error(error)
                    raise
                self._application._finish_mutation_audit(audit, result)
                return result

    def get_session(self, session_id: str) -> Any:
        return self._store.get_session(session_id)

    def list_sessions(self, *, is_archived: bool = False) -> Any:
        return self._store.list_sessions(is_archived=is_archived)

    def update_session(self, **kwargs: Any) -> Any:
        with self._write_lease_scope():
            with self._application._audit_mutation(
                "chat_session_update",
                {"updates": kwargs},
            ) as audit:
                try:
                    result = self._store.update_session(**kwargs)
                except PKVRuntimeError as error:
                    audit.fail_runtime_error(error)
                    raise
                self._application._finish_mutation_audit(audit, result)
                return result

    def delete_session(self, session_id: str) -> Any:
        with self._write_lease_scope():
            with self._application._audit_mutation(
                "chat_session_delete",
                {"session_id": session_id},
            ) as audit:
                try:
                    result = self._store.delete_session(session_id)
                except PKVRuntimeError as error:
                    audit.fail_runtime_error(error)
                    raise
                self._application._finish_mutation_audit(audit, result)
                return result

    def archive_session(self, session_id: str, *, is_archived: bool) -> Any:
        with self._write_lease_scope():
            with self._application._audit_mutation(
                "chat_session_archive",
                {"session_id": session_id, "is_archived": is_archived},
            ) as audit:
                try:
                    result = self._store.archive_session(session_id, is_archived=is_archived)
                except PKVRuntimeError as error:
                    audit.fail_runtime_error(error)
                    raise
                self._application._finish_mutation_audit(audit, result)
                return result

    def query_by_url(self, url: str) -> Any:
        return self._store.query_by_url(url)

    def query_by_id(self, knowledge_id: int) -> Any:
        return self._store.query_by_id(knowledge_id)


_KERNEL_FACTORY_TOKEN = object()


class KnowledgeKernel:
    """Headless PKV engine with stable operations for external wrappers.

    A desktop wrapper depends on this facade in the same direction that a shell
    depends on a headless inference engine: framework objects never become Kernel
    dependencies, and Store/Workflow/Provider implementations never escape the
    facade's public operations.
    """

    _FACTORY_TOKEN: ClassVar[object] = _KERNEL_FACTORY_TOKEN

    def __init__(
        self,
        application: KnowledgeApplication,
        *,
        _factory_token: object | None = None,
        _is_process_default: bool = False,
    ) -> None:
        if _factory_token is not self._FACTORY_TOKEN:
            raise TypeError(
                "KnowledgeKernel 是 factory-only 公开端口；请通过 "
                "pkv_kernel.lifecycle 的已确认执行结果，或兼容的 "
                "bootstrap_kernel()/get_kernel() 获取实例。"
            )
        self._application = application
        # ``get_kernel(config=...)`` creates an isolated graph.  Only the
        # singleton published by ``_wrap_default`` may replace the legacy
        # process Config identity during a settings update.
        self._is_process_default = _is_process_default
        self._chat_sessions = KernelChatSessions(application)

    @classmethod
    def _from_application(
        cls,
        application: KnowledgeApplication,
        *,
        is_process_default: bool = False,
    ) -> "KnowledgeKernel":
        """Internal-only factory that keeps public construction graph-private."""

        return cls(
            application,
            _factory_token=cls._FACTORY_TOKEN,
            _is_process_default=is_process_default,
        )

    @property
    def config(self) -> Any:
        return self._application.config

    @property
    def configuration_generation(self) -> int:
        """The immutable config-graph generation captured by this Kernel.

        A successful reload returns a new Kernel with a greater generation.
        Existing Kernel references and in-flight operations retain their prior
        generation and never switch dependencies mid-operation.
        """

        return self._application.snapshot.generation

    @property
    def chat_sessions(self) -> KernelChatSessions:
        return self._chat_sessions

    async def archive_url(self, input_data: dict[str, Any]) -> Any:
        return await self._application.archive_url(input_data)

    async def archive_text(self, text: str, **kwargs: Any) -> Any:
        return await self._application.archive_text(text, **kwargs)

    def search(self, query: str, strategy: str = "auto", limit: int = 10, **kwargs: Any) -> Any:
        return self._application.search(query, strategy, limit, **kwargs)

    def chat_settings(self) -> Any:
        return self._application.chat_settings()

    def create_chat_provider(self, settings: Any | None = None) -> Any:
        return self._application.create_chat_provider(settings)

    def get_entry(self, knowledge_id: int) -> Any:
        return self._application.sqlite_store.query_by_id(knowledge_id)

    def get_entry_by_url(self, url: str) -> Any:
        return self._application.sqlite_store.query_by_url(url)

    def list_entries(self, **kwargs: Any) -> Any:
        return self._application.sqlite_store.list_entries(**kwargs)

    def count_entries(self, *, tag: str | None = None) -> Any:
        return self._application.sqlite_store.count_entries(tag=tag)

    def get_tags(self) -> Any:
        return self._application.sqlite_store.get_all_tags_with_count()

    def get_statistics(self) -> Any:
        return self._application.sqlite_store.get_statistics()

    def load_entry_preview(self, entry: dict[str, Any]) -> PreviewOutcome:
        return load_preview_with_store(entry, self._application.markdown_store)

    def load_markdown_path(self, path: Any) -> Any:
        """Compatibility port for wrappers migrating from Store-shaped seams."""

        return self._application.markdown_store.load(path)

    def has_vector_index(self) -> bool:
        # This is a read predicate, not a mutation preflight.  The strict
        # read-only Application port refuses incomplete/mismatched artifacts
        # without creating a VectorStore writer lock, recovery marker, or index
        # directory.  A missing index is represented by ``None``; other typed
        # safety failures deliberately remain visible to the caller.
        return self._application.readonly_vector_store is not None

    def delete_vectors_for_entry(self, knowledge_id: int) -> None:
        with self._application._write_lease_scope():
            with self._application._audit_mutation(
                "delete_vectors",
                {"knowledge_id": knowledge_id},
            ) as audit:
                try:
                    vector_store = self._application.vector_store
                    if vector_store is not None:
                        vector_store.delete_vectors_for_entry(knowledge_id)
                except PKVRuntimeError as error:
                    audit.fail_runtime_error(error)
                    raise
                self._application._finish_mutation_audit(audit, None)

    def delete_entry(self, knowledge_id: int) -> Any:
        def delete_vectors(entry_id: int) -> None:
            vector_store = self._application.vector_store
            if vector_store is not None:
                vector_store.delete_vectors_for_entry(entry_id)

        with self._application._write_lease_scope():
            with self._application._audit_mutation(
                "delete_entry",
                {"knowledge_id": knowledge_id},
            ) as audit:
                try:
                    result = self._application.storage_coordinator.delete(
                        knowledge_id,
                        vector_operation=delete_vectors,
                    )
                except PKVRuntimeError as error:
                    audit.fail_runtime_error(error)
                    raise
                self._application._finish_mutation_audit(audit, result)
                return result

    def update_local_config(self, updates: dict[str, Any]) -> "KnowledgeKernel":
        """Persist settings and publish a successor for this Kernel's scope.

        The current process-default Kernel writes then atomically publishes a
        new default graph.  An explicit ``get_kernel(config_b)`` graph instead
        reloads *only B* and never replaces the legacy/default Config A.  A
        stale former default is rejected before it can overwrite newer settings.
        """

        with _kernel_lifecycle_lock:
            # User-config updates intentionally remain profile-local.  Acquiring
            # the data-root lease or emitting an AuditTrace here would create a
            # runtime directory before explicit lifecycle setup, violating K1's
            # lazy initialization contract.  A subsequent data-root mutation
            # sees the successor Config snapshot and takes R3 normally.
            if self._is_process_default:
                if self is not _default_kernel:
                    raise RuntimeError(
                        "过期的默认 Kernel 不能更新配置；请先获取当前默认 Kernel"
                    )
                # ``update_local_config`` is the API-major compatibility name
                # for this Kernel method.  Internally settings always use the
                # non-deprecated user-config writer, whose preflight rejects a
                # root switch before it can be persisted.
                self.config.update_user_config(updates)
                return _reload_kernel_locked()

            if not isinstance(self.config, Config):
                raise TypeError(
                    "隔离 Kernel 的配置更新需要 pkv_kernel.Config 实例"
                )
            self.config.update_user_config(updates)
            return _reload_isolated_kernel_locked(self)


_default_kernel: KnowledgeKernel | None = None
_default_application_identity: int | None = None
_kernel_lifecycle_lock = RLock()


def _wrap_default(application: KnowledgeApplication) -> KnowledgeKernel:
    global _default_kernel, _default_application_identity
    identity = id(application)
    if _default_kernel is None or _default_application_identity != identity:
        _default_kernel = KnowledgeKernel._from_application(
            application,
            is_process_default=True,
        )
        _default_application_identity = identity
    return _default_kernel


def configure_kernel(config: Any) -> KnowledgeKernel:
    """Install one process Kernel after the shared runtime bootstrap succeeds."""

    with _kernel_lifecycle_lock:
        return _wrap_default(configure_application(config))


def bootstrap_kernel(config: Config | None = None) -> KnowledgeKernel:
    """Bootstrap and configure the process Kernel through the one runtime gate.

    External wrappers must not compose Stores, Providers, workflows, or runtime
    paths themselves.  This is their cold-start entry point: it creates the
    default configuration when necessary, invokes the shared runtime bootstrap,
    then publishes the exact accepted configuration as the process Kernel.
    """

    selected_config = Config() if config is None else config
    context = bootstrap_runtime(selected_config)
    return configure_kernel(context.config)


def get_kernel(config: Any | None = None) -> KnowledgeKernel:
    """Return the process Kernel or an isolated explicit-config Kernel."""

    if config is not None:
        return KnowledgeKernel._from_application(get_application(config))
    with _kernel_lifecycle_lock:
        return _wrap_default(get_application())


def reload_kernel() -> KnowledgeKernel:
    with _kernel_lifecycle_lock:
        return _reload_kernel_locked()


def _reload_kernel_locked() -> KnowledgeKernel:
    """Publish one new Application/Kernel graph while lifecycle lock is held."""

    return _wrap_default(reload_application())


def _reload_isolated_kernel_locked(kernel: KnowledgeKernel) -> KnowledgeKernel:
    """Reload an explicit Config graph without publishing process-global state."""

    config = kernel.config
    if not isinstance(config, Config):  # guarded by the public method
        raise TypeError("隔离 Kernel 的配置更新需要 pkv_kernel.Config 实例")

    # Rebuild a coherent Config/layout from B's exact bundled/profile/env
    # sources.  Reusing ``config.layout`` here would let YAML say one root while
    # Store/Provider paths still point to another.  ``reload_snapshot`` rejects
    # such a retarget before this isolated graph can be published, without
    # touching default Config A.
    refreshed_config = config.reload_snapshot()
    application = kernel._application
    refreshed_application = KnowledgeApplication(
        refreshed_config,
        sqlite_store_factory=application.sqlite_store_factory,
        markdown_store_factory=application.markdown_store_factory,
        vector_store_factory=application.vector_store_factory,
        bm25_retriever_factory=application.bm25_retriever_factory,
        query_router_factory=application.query_router_factory,
        workflow_factory=application.workflow_factory,
        text_processor_factory=application.text_processor_factory,
        chat_provider_factory=application.chat_provider_factory,
        write_lease_factory=application.write_lease_factory,
        snapshot_generation=kernel.configuration_generation + 1,
    )
    return KnowledgeKernel._from_application(refreshed_application)


def reset_kernel() -> None:
    global _default_kernel, _default_application_identity
    with _kernel_lifecycle_lock:
        reset_application()
        _default_kernel = None
        _default_application_identity = None
