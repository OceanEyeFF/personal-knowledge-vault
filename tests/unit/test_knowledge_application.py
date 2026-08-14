"""Focused contracts for the adapter-neutral KnowledgeApplication boundary."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from src.application import KnowledgeApplication
from src.retrieval.result import SearchResponse
from src.workflow.models import WorkflowResult


def _config(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        db_path=tmp_path / "pkv.db",
        vault_dir=tmp_path / "vault",
        vector_index_dir=tmp_path / "vector",
        layout=SimpleNamespace(runtime_state_dir=tmp_path / "state"),
    )


def test_bm25_search_is_lazy_and_does_not_build_router_or_provider(tmp_path) -> None:
    calls: list[str] = []
    expected = SearchResponse.completed((), strategy="bm25")

    def bm25_factory(config):
        calls.append("bm25")
        return SimpleNamespace(search=lambda query, limit: expected)

    def router_factory(config, threshold):
        calls.append("router")
        raise AssertionError("BM25 must not construct the router")

    app = KnowledgeApplication(
        _config(tmp_path),
        bm25_retriever_factory=bm25_factory,
        query_router_factory=router_factory,
    )

    assert app.search("关键词", "bm25", 3) is expected
    assert calls == ["bm25"]


def test_archive_text_uses_injected_operation_scoped_factories(tmp_path) -> None:
    processed = SimpleNamespace(title="自动标题", content="literal text")
    processor_calls: list[str] = []
    workflow_calls: list[tuple[str, dict]] = []

    class Processor:
        async def process_text(self, text):
            processor_calls.append(text)
            return processed

    class Workflow:
        async def execute_async(self, name, data):
            workflow_calls.append((name, data))
            return WorkflowResult(success=True, data={"knowledge_id": 7})

    app = KnowledgeApplication(
        _config(tmp_path),
        text_processor_factory=Processor,
        workflow_factory=Workflow,
    )

    result = asyncio.run(
        app.archive_text(
            "literal text",
            title="指定标题",
            skip_sharpen=True,
            skip_review=True,
        )
    )

    assert result.terminal == "success"
    assert processor_calls == ["literal text"]
    assert workflow_calls[0][0] == "archive-text"
    assert workflow_calls[0][1]["entry"] is processed
    assert workflow_calls[0][1]["title"] == "指定标题"
    assert workflow_calls[0][1]["skip_sharpen"] is True
    assert workflow_calls[0][1]["skip_review"] is True


def test_archive_url_rejects_unsafe_input_before_creating_workflow(tmp_path) -> None:
    workflow_created = False

    def workflow_factory():
        nonlocal workflow_created
        workflow_created = True
        raise AssertionError("unsafe URL must not reach a workflow")

    app = KnowledgeApplication(_config(tmp_path), workflow_factory=workflow_factory)

    result = asyncio.run(app.archive_url({"url": "http://127.0.0.1/private"}))

    assert result.success is False
    assert result.terminal == "error"
    assert result.issues[0]["code"] == "ssrf_target_forbidden"
    assert workflow_created is False


def test_archive_cli_input_preserves_literal_text_route(tmp_path) -> None:
    calls: list[tuple[str, dict]] = []

    class Workflow:
        async def execute_async(self, name, data):
            calls.append((name, data))
            return WorkflowResult(success=True)

    app = KnowledgeApplication(_config(tmp_path), workflow_factory=Workflow)

    result = asyncio.run(
        app.archive_cli_input({"url": "a literal note, not a path"})
    )

    assert result.success is True
    assert calls == [("archive-url", {
        "url": "a literal note, not a path",
        "skip_sharpen": True,
        "skip_review": True,
    })]


def test_store_dependencies_are_composed_once_per_application(tmp_path) -> None:
    calls: list[str] = []
    store = object()
    markdown = object()

    app = KnowledgeApplication(
        _config(tmp_path),
        sqlite_store_factory=lambda config: calls.append("sqlite") or store,
        markdown_store_factory=lambda config: calls.append("markdown") or markdown,
    )

    assert app.sqlite_store is store
    assert app.sqlite_store is store
    assert app.markdown_store is markdown
    assert app.markdown_store is markdown
    assert calls == ["sqlite", "markdown"]


def test_missing_vector_index_is_reprobed_then_cached_after_it_appears(tmp_path) -> None:
    writable = object()
    readonly = object()
    writable_candidates = iter((None, writable))
    readonly_candidates = iter((None, readonly))
    writable_calls: list[object] = []
    readonly_calls: list[object] = []

    def writable_factory(config):
        writable_calls.append(config)
        return next(writable_candidates)

    def readonly_factory(config):
        readonly_calls.append(config)
        return next(readonly_candidates)

    config = _config(tmp_path)
    app = KnowledgeApplication(config, vector_store_factory=writable_factory)
    app._default_readonly_vector_store = readonly_factory

    assert app.vector_store is None
    assert app.readonly_vector_store is None
    assert app.vector_store is writable
    assert app.readonly_vector_store is readonly

    # A successfully opened index is process-cached; only absence is re-probed.
    assert app.vector_store is writable
    assert app.readonly_vector_store is readonly
    assert writable_calls == [config, config]
    assert readonly_calls == [config, config]


def test_default_workflow_composes_only_the_application_config_and_dependencies(
    tmp_path,
    monkeypatch,
) -> None:
    from src.workflow.steps import AnalyzeStep, FetchStep, ReviewStep, StoreStep

    workflow_config = {
        "steps": [
            {
                "id": "store",
                "type": "store_entry",
                "config": {"targets": ["markdown", "sqlite"]},
                "on_error": "fail",
            }
        ]
    }
    config_b = _config(tmp_path)
    config_b.get_workflow_config = lambda name: workflow_config
    sqlite_store = object()
    markdown_store = object()
    coordinator = object()
    app = KnowledgeApplication(
        config_b,
        sqlite_store_factory=lambda config: sqlite_store,
        markdown_store_factory=lambda config: markdown_store,
    )
    app._storage_coordinator = coordinator

    def reject_global_config(*_args, **_kwargs):
        raise AssertionError("global config A must not be consulted")

    monkeypatch.setattr("src.workflow.engine.get_workflow_config", reject_global_config)
    engine = app._new_workflow()

    assert engine._runtime_config is config_b
    assert engine._load_workflow_config("archive-url") is workflow_config

    fetch = app._create_workflow_step(FetchStep, "fetch", {})
    analyze = app._create_workflow_step(AnalyzeStep, "analyze", {})
    store = app._create_workflow_step(StoreStep, "store", {})
    review = app._create_workflow_step(ReviewStep, "review", {})

    assert fetch._runtime_config is config_b
    assert analyze._runtime_config is config_b
    assert store._runtime_config is config_b
    assert store._sqlite_store is sqlite_store
    assert store._markdown_store is markdown_store
    assert store._storage_coordinator is coordinator
    assert review._runtime_config is config_b


def test_archive_text_executes_with_config_b_when_legacy_global_is_config_a(
    tmp_path,
    monkeypatch,
) -> None:
    from src.runtime.errors import OperationStatus

    entry = SimpleNamespace(title="Kernel title", content="literal Kernel body")

    class Processor:
        async def process_text(self, text):
            assert text == entry.content
            return entry

    operation = SimpleNamespace(
        status=OperationStatus.READY,
        errors=(),
        retry_safe=False,
        to_dict=lambda: {
            "status": "ready",
            "knowledge_id": 73,
            "operation_id": "a" * 32,
            "errors": [],
        },
    )
    coordinator = SimpleNamespace(archive=lambda *args, **kwargs: operation)
    config_b = _config(tmp_path)
    config_b.get_workflow_config = lambda name: {
        "schema_version": 1,
        "name": name,
        "description": "config B workflow",
        "steps": [
            {
                "id": "store_entry",
                "type": "store_entry",
                "config": {"targets": ["markdown", "sqlite"]},
                "on_error": "fail",
            }
        ]
    }
    app = KnowledgeApplication(
        config_b,
        sqlite_store_factory=lambda config: object(),
        markdown_store_factory=lambda config: object(),
        text_processor_factory=Processor,
    )
    app._storage_coordinator = coordinator

    def reject_config_a(*_args, **_kwargs):
        raise AssertionError("legacy global config A was consulted")

    monkeypatch.setattr("src.workflow.steps.get_config", reject_config_a)
    monkeypatch.setattr("src.workflow.engine.get_workflow_config", reject_config_a)

    result = asyncio.run(app.archive_text(entry.content))

    assert result.success is True
    assert result.terminal == "success"
    assert result.data["knowledge_id"] == 73


def test_writer_vector_store_binds_application_config_not_ambient_global(
    tmp_path,
    monkeypatch,
) -> None:
    """An explicit Application must persist B's embedding contract, never A's."""

    import src.storage.vector_store as vector_store_module

    config_b = _config(tmp_path)
    config_b.embedding_dim = 4
    config_b.embd_base_url = "https://embedding-b.example/v1"
    config_b.embd_model = "embedding-model-b"
    config_b.embedding_index_fingerprint = lambda dim: {
        "base_url": config_b.embd_base_url,
        "embedding_model": config_b.embd_model,
        "embedding_dim": str(dim),
    }

    def reject_ambient_config(*_args, **_kwargs):
        raise AssertionError("ambient config A must not influence application B")

    monkeypatch.setattr(vector_store_module, "get_config", reject_ambient_config)

    store = KnowledgeApplication(config_b)._create_writer_vector_store(4)

    assert store._runtime_config is config_b
    assert store.embedding_fingerprint["embedding_model"] == "embedding-model-b"
    assert store.embedding_fingerprint["embedding_dim"] == "4"


def test_reload_application_replaces_the_legacy_config_identity(monkeypatch, tmp_path) -> None:
    import src.application.knowledge_application as application_module
    import src.utils.config as config_module

    config_a = object()
    config_b = _config(tmp_path)
    monkeypatch.setattr(config_module, "_config_instance", config_a)
    monkeypatch.setattr(config_module, "Config", lambda: config_b)
    application_module.reset_application()

    reloaded = application_module.reload_application()

    assert reloaded.config is config_b
    assert config_module._config_instance is config_b
    assert application_module.get_application() is reloaded
