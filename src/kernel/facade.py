"""Stable headless Kernel facade used by peripheral adapters and wrappers."""

from __future__ import annotations

from typing import Any

from src.application import (
    KnowledgeApplication,
    configure_application,
    get_application,
    reload_application,
    reset_application,
)
from src.kernel.preview import PreviewOutcome, load_preview_with_store
from src.runtime.bootstrap import bootstrap_runtime
from src.utils.config import Config


class KernelChatSessions:
    """Narrow chat-session port; no storage implementation escapes the Kernel."""

    def __init__(self, application: KnowledgeApplication) -> None:
        self._application = application

    @property
    def _store(self) -> Any:
        return self._application.sqlite_store

    def create_session(self, session_id: str, title: str) -> Any:
        return self._store.create_session(session_id, title)

    def get_session(self, session_id: str) -> Any:
        return self._store.get_session(session_id)

    def list_sessions(self, *, is_archived: bool = False) -> Any:
        return self._store.list_sessions(is_archived=is_archived)

    def update_session(self, **kwargs: Any) -> Any:
        return self._store.update_session(**kwargs)

    def delete_session(self, session_id: str) -> Any:
        return self._store.delete_session(session_id)

    def archive_session(self, session_id: str, *, is_archived: bool) -> Any:
        return self._store.archive_session(session_id, is_archived=is_archived)

    def query_by_url(self, url: str) -> Any:
        return self._store.query_by_url(url)

    def query_by_id(self, knowledge_id: int) -> Any:
        return self._store.query_by_id(knowledge_id)


class KnowledgeKernel:
    """Headless PKV engine with stable operations for external wrappers.

    A desktop wrapper depends on this facade in the same direction that a shell
    depends on a headless inference engine: framework objects never become Kernel
    dependencies, and Store/Workflow/Provider implementations never escape the
    facade's public operations.
    """

    def __init__(self, application: KnowledgeApplication) -> None:
        self._application = application
        self._chat_sessions = KernelChatSessions(application)

    @property
    def config(self) -> Any:
        return self._application.config

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
        return self._application.vector_store is not None

    def delete_vectors_for_entry(self, knowledge_id: int) -> None:
        vector_store = self._application.vector_store
        if vector_store is not None:
            vector_store.delete_vectors_for_entry(knowledge_id)

    def delete_entry(self, knowledge_id: int) -> Any:
        def delete_vectors(entry_id: int) -> None:
            vector_store = self._application.vector_store
            if vector_store is not None:
                vector_store.delete_vectors_for_entry(entry_id)

        return self._application.storage_coordinator.delete(
            knowledge_id,
            vector_operation=delete_vectors,
        )

    def update_local_config(self, updates: dict[str, Any]) -> "KnowledgeKernel":
        self.config.update_local_config(updates)
        return reload_kernel()


_default_kernel: KnowledgeKernel | None = None
_default_application_identity: int | None = None


def _wrap_default(application: KnowledgeApplication) -> KnowledgeKernel:
    global _default_kernel, _default_application_identity
    identity = id(application)
    if _default_kernel is None or _default_application_identity != identity:
        _default_kernel = KnowledgeKernel(application)
        _default_application_identity = identity
    return _default_kernel


def configure_kernel(config: Any) -> KnowledgeKernel:
    """Install one process Kernel after the shared runtime bootstrap succeeds."""

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
        return KnowledgeKernel(get_application(config))
    return _wrap_default(get_application())


def reload_kernel() -> KnowledgeKernel:
    return _wrap_default(reload_application())


def reset_kernel() -> None:
    global _default_kernel, _default_application_identity
    reset_application()
    _default_kernel = None
    _default_application_identity = None
