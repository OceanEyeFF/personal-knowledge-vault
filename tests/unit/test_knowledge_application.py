"""Focused contracts for the adapter-neutral KnowledgeApplication boundary."""

from __future__ import annotations

import asyncio
import json
import warnings
from pathlib import Path
import threading
from types import SimpleNamespace

import numpy as np
import pytest

import src.runtime.audit as audit_module
from src.application import KnowledgeApplication
from src.kernel import KnowledgeKernel
from src.retrieval.result import SearchResponse
from src.runtime.errors import ErrorCode, PKVRuntimeError
from src.runtime.layout import RuntimeLayout
from src.runtime.write_lease import VaultWriteLease, write_lease_scope
from src.workflow.models import WorkflowResult


_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _config(tmp_path: Path) -> SimpleNamespace:
    layout = RuntimeLayout.resolve(
        resources_root=_PROJECT_ROOT,
        user_data_root=tmp_path / "data",
        environment={},
    )
    return SimpleNamespace(
        db_path=tmp_path / "pkv.db",
        vault_dir=tmp_path / "vault",
        vector_index_dir=tmp_path / "vector",
        layout=layout,
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


def test_busy_archive_stops_before_processor_workflow_or_audit(tmp_path) -> None:
    config = _config(tmp_path)
    calls: list[str] = []

    class Processor:
        async def process_text(self, text):
            calls.append(f"processor:{text}")
            return SimpleNamespace(title="title", content=text)

    def workflow_factory():
        calls.append("workflow")
        raise AssertionError("busy archive must not construct a workflow")

    app = KnowledgeApplication(
        config,
        text_processor_factory=Processor,
        workflow_factory=workflow_factory,
    )

    with write_lease_scope(config.layout):
        result = asyncio.run(app.archive_text("contended text"))

    assert result.success is False
    assert result.terminal == "error"
    assert result.issues == [
        {
            "code": ErrorCode.WRITE_BUSY.value,
            "message": "知识库当前有其他写入操作",
            "severity": "error",
            "stage": "write_lease",
            "recoverable": True,
        }
    ]
    assert calls == []
    assert not (config.layout.runtime_state_dir / "operations").exists()
    assert not (config.layout.log_dir / "audit.jsonl").exists()


def test_cancelled_archive_keeps_tracked_worker_lease_until_it_finishes(tmp_path) -> None:
    config = _config(tmp_path)
    started = threading.Event()
    allow_finish = threading.Event()
    finished = threading.Event()

    class Processor:
        async def process_text(self, text):
            return SimpleNamespace(title="title", content=text)

    app: KnowledgeApplication

    class Workflow:
        async def execute_async(self, _name, _data):
            def durable_worker() -> None:
                started.set()
                assert allow_finish.wait(timeout=10)
                finished.set()

            await app._run_tracked_write_worker(durable_worker)
            return WorkflowResult(success=True)

    app = KnowledgeApplication(
        config,
        text_processor_factory=Processor,
        workflow_factory=Workflow,
    )

    async def scenario() -> None:
        task = asyncio.create_task(app.archive_text("cancellable text"))
        assert await asyncio.to_thread(started.wait, 10)

        task.cancel()
        await asyncio.sleep(0)
        with pytest.raises(PKVRuntimeError) as captured:
            with VaultWriteLease(config.layout):
                pass
        assert captured.value.code is ErrorCode.WRITE_BUSY

        allow_finish.set()
        assert await asyncio.to_thread(finished.wait, 10)
        with pytest.raises(asyncio.CancelledError):
            await task
        with VaultWriteLease(config.layout):
            pass

    asyncio.run(scenario())


def test_archive_audit_retains_safe_input_and_redacts_configured_secret(tmp_path) -> None:
    config = _config(tmp_path)
    config.llm_api_key = "top-secret-key"
    config.embd_api_key = "embedding-secret"
    config.zhihu_cookie = "cookie-secret"

    class Processor:
        async def process_text(self, text):
            return SimpleNamespace(title="title", content=text)

    class Workflow:
        async def execute_async(self, _name, _data):
            return WorkflowResult(success=True, data={"knowledge_id": 9})

    app = KnowledgeApplication(
        config,
        text_processor_factory=Processor,
        workflow_factory=Workflow,
    )
    result = asyncio.run(
        app.archive_text("safe article; api_key=top-secret-key; keep this body")
    )

    assert result.success is True
    records = [
        json.loads(line)
        for line in (config.layout.log_dir / "audit.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()
    ]
    assert [record["event"]["phase"] for record in records] == [
        "started",
        "completed",
    ]
    serialized = json.dumps(records, ensure_ascii=False)
    assert "safe article" in serialized
    assert "top-secret-key" not in serialized
    assert "embedding-secret" not in serialized
    assert "cookie-secret" not in serialized
    assert "[REDACTED]" in serialized


def test_archive_audit_completion_failure_preserves_committed_outcome(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A final audit fsync failure must not turn a committed archive into an error."""

    config = _config(tmp_path)
    config.llm_api_key = "audit-completion-secret"

    class Processor:
        async def process_text(self, text):
            return SimpleNamespace(title="title", content=text)

    class Workflow:
        async def execute_async(self, _name, _data):
            return WorkflowResult(
                success=True,
                data={
                    "knowledge_id": 19,
                    "status": "ready",
                    "core_committed": True,
                },
            )

    original_append = audit_module.AuditTrace.append

    def fail_only_completed(trace: audit_module.AuditTrace, event: object) -> Path:
        if isinstance(event, dict) and event.get("phase") == "completed":
            raise audit_module.AuditTraceError()
        return original_append(trace, event)  # type: ignore[arg-type]

    monkeypatch.setattr(audit_module.AuditTrace, "append", fail_only_completed)
    app = KnowledgeApplication(
        config,
        text_processor_factory=Processor,
        workflow_factory=Workflow,
    )

    result = asyncio.run(
        app.archive_text("committed article; api_key=audit-completion-secret")
    )

    assert result.success is True
    assert result.terminal == "degraded"
    assert result.data["core_committed"] is True
    assert result.data["do_not_retry"] is True
    assert result.data["audit_completion_pending"] is True
    assert result.issues[-1] == {
        "code": ErrorCode.AUDIT_COMPLETION_PENDING.value,
        "message": "归档已提交，但本地审计完成记录待补；请勿直接重试。",
        "severity": "warning",
        "stage": "audit_trace",
        "recoverable": True,
    }

    records = [
        json.loads(line)
        for line in (config.layout.log_dir / "audit.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()
    ]
    # ``mark_completion_pending_after_commit`` suppresses a false failed event;
    # later reconciliation can see the original started operation safely.
    assert [record["event"]["phase"] for record in records] == ["started"]
    serialized = json.dumps(records, ensure_ascii=False)
    assert "audit-completion-secret" not in serialized
    assert "[REDACTED]" in serialized


def test_archive_audit_redacts_provider_error_text_from_workflow_result(tmp_path) -> None:
    """Workflow terminal diagnostics must not bypass AuditTrace redaction."""

    config = _config(tmp_path)
    config.llm_api_key = "provider-error-secret"

    class Processor:
        async def process_text(self, text):
            return SimpleNamespace(title="title", content=text)

    class Workflow:
        async def execute_async(self, _name, _data):
            return WorkflowResult(
                success=False,
                errors=[
                    "provider diagnostic: api_key=provider-error-secret; retry later"
                ],
                issues=[
                    {
                        "code": "provider_unavailable",
                        "message": "Authorization: Bearer provider-error-secret",
                        "severity": "error",
                        "recoverable": True,
                        "stage": "provider_request",
                    }
                ],
            )

    app = KnowledgeApplication(
        config,
        text_processor_factory=Processor,
        workflow_factory=Workflow,
    )

    result = asyncio.run(app.archive_text("article body stays traceable"))

    assert result.success is False
    records = [
        json.loads(line)
        for line in (config.layout.log_dir / "audit.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()
    ]
    assert [record["event"]["phase"] for record in records] == ["started", "failed"]
    serialized = json.dumps(records, ensure_ascii=False)
    assert "provider-error-secret" not in serialized
    assert "[REDACTED]" in serialized


def test_kernel_delete_and_chat_mutations_reject_busy_before_store_calls(tmp_path) -> None:
    config = _config(tmp_path)
    calls: list[str] = []
    vector_store = SimpleNamespace(
        delete_vectors_for_entry=lambda _knowledge_id: calls.append("vector")
    )
    sqlite_store = SimpleNamespace(
        create_session=lambda _session_id, _title: calls.append("chat")
    )
    app = KnowledgeApplication(
        config,
        sqlite_store_factory=lambda _config: sqlite_store,
        vector_store_factory=lambda _config: vector_store,
    )
    app._storage_coordinator = SimpleNamespace(
        delete=lambda *_args, **_kwargs: calls.append("delete")
    )
    kernel = KnowledgeKernel._from_application(app)

    with VaultWriteLease(config.layout):
        with pytest.raises(PKVRuntimeError) as delete_error:
            kernel.delete_entry(41)
        with pytest.raises(PKVRuntimeError) as chat_error:
            kernel.chat_sessions.create_session("session-1", "title")

    assert delete_error.value.code is ErrorCode.WRITE_BUSY
    assert chat_error.value.code is ErrorCode.WRITE_BUSY
    assert calls == []


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

    with write_lease_scope(config.layout):
        assert app.vector_store is None
        assert app.vector_store is writable
    assert app.readonly_vector_store is None
    assert app.readonly_vector_store is readonly

    # A successfully opened index is process-cached; only absence is re-probed.
    with write_lease_scope(config.layout):
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
    assert store._write_worker_runner.__self__ is app
    assert (
        store._write_worker_runner.__func__
        is KnowledgeApplication._run_tracked_write_worker
    )
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

    with write_lease_scope(config_b.layout):
        store = KnowledgeApplication(config_b)._create_writer_vector_store(4)

    assert store._runtime_config is config_b
    assert store.embedding_fingerprint["embedding_model"] == "embedding-model-b"
    assert store.embedding_fingerprint["embedding_dim"] == "4"


def test_vector_retriever_binds_explicit_config_b_not_ambient_global(
    tmp_path,
    monkeypatch,
) -> None:
    """Vector reads keep B's db/index/provider factory after A exists globally."""

    import src.retrieval.vector_retriever as vector_retriever_module

    config_b = _config(tmp_path)
    captured: dict[str, object] = {}

    class VectorRetriever:
        def __init__(self, db_path, vector_index_dir, *, embedder_factory, runtime_config):
            captured.update(
                db_path=db_path,
                vector_index_dir=vector_index_dir,
                embedder_factory=embedder_factory,
                runtime_config=runtime_config,
            )

    def reject_ambient_config(*_args, **_kwargs):
        raise AssertionError("ambient config A must not be consulted")

    monkeypatch.setattr(vector_retriever_module, "VectorRetriever", VectorRetriever)
    monkeypatch.setattr("src.storage.vector_store.get_config", reject_ambient_config)

    retriever = KnowledgeApplication(config_b)._new_vector_retriever()

    assert isinstance(retriever, VectorRetriever)
    assert captured["db_path"] == config_b.db_path
    assert captured["vector_index_dir"] == config_b.vector_index_dir
    assert captured["runtime_config"] is config_b
    assert callable(captured["embedder_factory"])


def test_archive_vector_write_uses_explicit_config_b_not_ambient_config_a(
    tmp_path,
    monkeypatch,
) -> None:
    """One archive operation keeps Store, Provider and VectorStore on B."""

    from src.runtime.errors import OperationStatus

    config_b = _config(tmp_path)
    config_b.embedding_dim = 2
    config_b.embd_base_url = "https://embedding-b.example/v1"
    config_b.embd_model = "embedding-model-b"
    config_b.embedding_index_fingerprint = lambda dim: {
        "base_url": config_b.embd_base_url,
        "embedding_model": config_b.embd_model,
        "embedding_dim": str(dim),
    }
    config_b.get_workflow_config = lambda name: {
        "schema_version": 1,
        "name": name,
        "description": "config B vector workflow",
        "steps": [
            {
                "id": "store_entry",
                "type": "store_entry",
                "config": {"targets": ["markdown", "sqlite", "vector_index"]},
                "on_error": "fail",
            }
        ],
    }
    entry = SimpleNamespace(title="B title", content="B body")
    calls: list[object] = []
    index_available = False

    class Processor:
        async def process_text(self, text):
            assert text == "B body"
            return entry

    class Embedder:
        dim = 2

        def embed_document(self, content):
            assert content == "B body"
            calls.append("embed-document")
            return np.array([1.0, 0.0], dtype=np.float32)

        def embed_chunks(self, content, _include_chunks):
            assert content == "B body"
            calls.append("embed-chunks")
            return np.array([[0.0, 1.0]], dtype=np.float32), ["B body"]

    class VectorStore:
        def add_doc_vector(self, knowledge_id, vector):
            calls.append(("doc", knowledge_id, vector.tolist()))

        def add_chunk_vectors(self, knowledge_id, chunk_indices, vectors):
            calls.append(("chunks", knowledge_id, chunk_indices, vectors.tolist()))

        def delete_vectors_for_entry(self, knowledge_id):
            calls.append(("delete", knowledge_id))

    vector_store = VectorStore()
    operation = SimpleNamespace(
        status=OperationStatus.READY,
        errors=(),
        retry_safe=True,
        to_dict=lambda: {
            "status": "ready",
            "knowledge_id": 73,
            "operation_id": "b" * 32,
            "core_committed": True,
            "errors": [],
        },
    )

    def archive(entry_arg, *, chunks, vector_operation, vector_error, vector_required):
        assert entry_arg is entry
        assert chunks == ["B body"]
        assert vector_required is True
        assert vector_error is None
        vector_operation(73)
        return operation

    app = KnowledgeApplication(
        config_b,
        sqlite_store_factory=lambda config: object(),
        markdown_store_factory=lambda config: object(),
        vector_store_factory=lambda config: vector_store if index_available else None,
        text_processor_factory=Processor,
    )
    app._default_readonly_vector_store = (
        lambda config: vector_store if index_available else None
    )
    app._storage_coordinator = SimpleNamespace(archive=archive)
    app._create_embedder = lambda: Embedder()

    def create_writer_vector_store(dim):
        nonlocal index_available
        index_available = True
        calls.append(("vector-store", dim, config_b.vector_index_dir))
        return vector_store

    app._create_writer_vector_store = create_writer_vector_store

    def reject_config_a(*_args, **_kwargs):
        raise AssertionError("ambient config A must not be consulted")

    monkeypatch.setattr("src.workflow.steps.get_config", reject_config_a)
    monkeypatch.setattr("src.workflow.engine.get_workflow_config", reject_config_a)

    from src.kernel import KnowledgeKernel

    kernel = KnowledgeKernel._from_application(app)
    assert kernel.has_vector_index() is False
    assert app.readonly_vector_store is None

    result = asyncio.run(app.archive_text("B body"))

    assert result.terminal == "success"
    assert calls == [
        "embed-document",
        "embed-chunks",
        ("vector-store", 2, config_b.vector_index_dir),
        ("doc", 73, [1.0, 0.0]),
        ("chunks", 73, [0], [[0.0, 1.0]]),
    ]
    # A missing pair is re-probed after the archive-created writer publishes it;
    # related (strict read) and delete then use the same application snapshot.
    assert kernel.has_vector_index() is True
    assert app.readonly_vector_store is vector_store
    kernel.delete_vectors_for_entry(73)
    assert calls[-1] == ("delete", 73)


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


def test_kernel_reload_publishes_new_snapshot_without_mixing_inflight_operation(
    monkeypatch,
    tmp_path,
) -> None:
    """An old task retains A while a task begun after reload uses only B."""

    import src.utils.config as config_module
    from src.kernel import configure_kernel, reload_kernel, reset_kernel

    config_a = _config(tmp_path / "a")
    config_b = _config(tmp_path / "b")
    previous_config = config_module._config_instance
    started = asyncio.Event()
    release_old = asyncio.Event()
    seen: list[tuple[str, object, int]] = []

    class Processor:
        async def process_text(self, text):
            return SimpleNamespace(title=text, content=text)

    class Workflow:
        def __init__(self, application, label, wait_for_release):
            self._application = application
            self._label = label
            self._wait_for_release = wait_for_release

        async def execute_async(self, _name, _data):
            seen.append(
                (
                    self._label,
                    self._application.snapshot.config,
                    self._application.snapshot.generation,
                )
            )
            if self._wait_for_release:
                started.set()
                await release_old.wait()
                seen.append(
                    (
                        f"{self._label}-after-reload",
                        self._application.snapshot.config,
                        self._application.snapshot.generation,
                    )
                )
            return WorkflowResult(success=True, terminal="success")

    try:
        old_kernel = configure_kernel(config_a)
        old_app = old_kernel._application
        old_app.text_processor_factory = Processor
        old_app.workflow_factory = lambda: Workflow(old_app, "old", True)

        async def scenario():
            old_task = asyncio.create_task(old_kernel.archive_text("old"))
            await started.wait()

            monkeypatch.setattr(config_module, "Config", lambda: config_b)
            new_kernel = reload_kernel()
            new_app = new_kernel._application
            new_app.text_processor_factory = Processor
            new_app.workflow_factory = lambda: Workflow(new_app, "new", False)

            new_result = await new_kernel.archive_text("new")
            release_old.set()
            old_result = await old_task
            return new_kernel, old_result, new_result

        new_kernel, old_result, new_result = asyncio.run(scenario())

        assert old_result.terminal == "success"
        assert new_result.terminal == "success"
        assert new_kernel.configuration_generation > old_kernel.configuration_generation
        assert seen == [
            ("old", config_a, old_kernel.configuration_generation),
            ("new", config_b, new_kernel.configuration_generation),
            ("old-after-reload", config_a, old_kernel.configuration_generation),
        ]
        assert config_module.get_config() is config_b
    finally:
        reset_kernel()
        config_module._config_instance = previous_config


def _runtime_config(tmp_path: Path):
    """Build a real, isolated Config without consulting a user Vault."""

    from src.runtime.layout import RuntimeLayout
    from src.utils.config import Config

    layout = RuntimeLayout.resolve(
        resources_root=Path(__file__).resolve().parents[2],
        user_data_root=tmp_path,
        environment={},
    )
    return Config(layout=layout)


def test_explicit_kernel_b_archive_defers_ai_without_consulting_global_a(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real B archive keeps primary storage and deferred AI on B only.

    Config A is kept as the process-global compatibility identity but every
    global lookup is a failure sentinel.  R4 P2 must persist B's documents,
    revoke stale vector readiness, and avoid a flat-vector or Provider fallback.
    """

    import src.cli.commands as commands
    import src.storage.vector_store as vector_store_module
    import src.utils.config as config_module
    import src.workflow.engine as workflow_engine_module
    import src.workflow.steps as workflow_steps_module
    from src.runtime.bootstrap import bootstrap_runtime
    from src.runtime.embedding_lifecycle import EmbeddingIndexState, inspect_embedding_index
    from src.runtime.runtime_snapshot import RuntimeSnapshotStore
    from src.runtime.write_lease import write_lease_scope
    from src.storage.markdown_store import Entry

    config_a = _runtime_config(tmp_path / "config-a")
    config_b = _runtime_config(tmp_path / "config-b")
    config_b.user_config_path.parent.mkdir(parents=True, exist_ok=True)
    config_b.user_config_path.write_text(
        json.dumps(
            {
                "ai": {
                    "llm": {"api_key": "test-llm-secret"},
                    "embedding": {"api_key": "test-embedding-secret"},
                }
            }
        ),
        encoding="utf-8",
    )
    config_b = config_b.reload_snapshot()
    # Bootstrap is an isolated B-only fixture setup: it creates B's schema and
    # writer-owned tokenizer cache before the read/FTS portions of this test.
    bootstrap_runtime(config_b, recover_interrupted=False)
    assert config_b.embedding_dim is not None
    snapshot_store = RuntimeSnapshotStore(config_b.layout)
    with write_lease_scope(config_b.layout):
        snapshot_store.publish(
            snapshot_store.read(),
            {
                "schema_version": 1,
                "database": {"schema_version": "1.2.5"},
                "embedding": {
                    "provider": config_b.embd_provider,
                    "fingerprint": config_b.embedding_index_fingerprint(
                        config_b.embedding_dim
                    ),
                },
            },
        )

    def reject_global_config_a(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("global Config A must not be consulted by explicit Config B")

    # Dynamic and module-imported compatibility aliases both become sentinels.
    # The actual B graph must use only its injected runtime_config fields.
    monkeypatch.setattr(config_module, "_config_instance", config_a)
    monkeypatch.setattr(config_module, "get_config", reject_global_config_a)
    monkeypatch.setattr(workflow_engine_module, "get_workflow_config", reject_global_config_a)
    monkeypatch.setattr(workflow_steps_module, "get_config", reject_global_config_a)
    monkeypatch.setattr(vector_store_module, "get_config", reject_global_config_a)

    def b_workflow(workflow_name: str) -> dict[str, object]:
        assert workflow_name == "archive-text"
        return {
            "schema_version": 1,
            "name": "archive-text",
            "description": "isolated B vector transition",
            "steps": [
                {
                    "id": "store_entry",
                    "type": "store_entry",
                    "config": {"targets": ["markdown", "sqlite", "vector_index"]},
                    "on_error": "fail",
                }
            ],
        }

    monkeypatch.setattr(config_b, "get_workflow_config", b_workflow)

    class FakeTextProcessor:
        async def process_text(self, text: str) -> Entry:
            return Entry(
                title=f"B {text}",
                source_type="text",
                source_url=None,
                tags=["config-b"],
                keywords=["config-b"],
                abstract=f"B {text}",
                summary_one_sentence=f"B {text}",
                summary_100_words=f"B {text}",
                content=text,
            )

    app = KnowledgeApplication(
        config_b,
        text_processor_factory=FakeTextProcessor,
    )
    app._create_embedder = lambda: (_ for _ in ()).throw(
        AssertionError("Embedding must be deferred")
    )
    kernel = KnowledgeKernel._from_application(app)

    assert kernel.config is config_b
    assert not config_a.data_root.exists()
    assert not vector_store_module.VectorStore.has_index_artifacts(
        config_b.vector_index_dir
    )
    # R4 P2 never writes a flat index during archive.
    assert kernel.has_vector_index() is False

    alpha = asyncio.run(kernel.archive_text("B alpha"))
    beta = asyncio.run(kernel.archive_text("B beta"))
    assert alpha.terminal == "degraded", alpha.errors
    assert beta.terminal == "degraded", beta.errors
    assert alpha.data["ai_automation"]["status"] == "rebuild_required"
    assert beta.data["ai_automation"]["status"] == "rebuild_required"
    alpha_id = alpha.data["knowledge_id"]
    beta_id = beta.data["knowledge_id"]
    assert type(alpha_id) is int and type(beta_id) is int
    assert alpha_id != beta_id

    assert not vector_store_module.VectorStore.has_index_artifacts(config_b.vector_index_dir)
    assert kernel.has_vector_index() is False

    # Exercise the production strict-read related adapter with an explicit B.
    # It creates neither a Provider nor a writer VectorStore.
    monkeypatch.setattr(commands, "_load_config", lambda: config_b)
    related = commands._related_payload(str(alpha_id), 5)
    assert related["status"] == "degraded"
    assert related["issues"][0]["code"] == ErrorCode.EMBEDDING_REBUILD_REQUIRED.value

    deleted = kernel.delete_entry(alpha_id)
    assert deleted.successful is True
    assert kernel.get_entry(alpha_id) is None
    assert kernel.has_vector_index() is False
    after_delete = inspect_embedding_index(config_b)
    assert after_delete.state is EmbeddingIndexState.REBUILD_REQUIRED
    assert after_delete.source is not None and after_delete.source.document_count == 1
    assert commands._related_payload(str(alpha_id), 5)["status"] == "no_hits"
    assert not config_a.data_root.exists()


def test_explicit_kernel_config_update_reloads_only_its_own_snapshot(tmp_path) -> None:
    """B may update/reload itself without changing default/global A."""

    import pkv_kernel
    import src.utils.config as config_module

    config_a = _runtime_config(tmp_path / "a")
    config_b = _runtime_config(tmp_path / "b")
    previous_config = config_module._config_instance
    original_a_level = config_a.get("logging.level")

    try:
        default_kernel = pkv_kernel.configure_kernel(config_a)
        explicit_kernel = pkv_kernel.get_kernel(config_b)

        # The public Kernel compatibility method may retain its historical
        # spelling, but it must not call deprecated Config aliases internally.
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            reloaded_b = explicit_kernel.update_local_config(
                {"logging.level": "DEBUG"}
            )

        assert config_module.get_config() is config_a
        assert pkv_kernel.get_kernel() is default_kernel
        assert config_a.get("logging.level") == original_a_level
        assert not config_a.user_config_path.exists()
        assert config_b.user_config_path.exists()
        assert reloaded_b is not explicit_kernel
        assert reloaded_b.config is not config_b
        # B is re-parsed into a fresh, internally coherent layout rather than
        # carrying an old layout under newly loaded settings.
        assert reloaded_b.config.layout is not config_b.layout
        assert reloaded_b.config.data_root == config_b.data_root
        assert reloaded_b.config.db_path == config_b.db_path
        assert reloaded_b.config.vector_index_dir == config_b.vector_index_dir
        assert reloaded_b.config.get("logging.level") == "DEBUG"
        assert reloaded_b.configuration_generation == (
            explicit_kernel.configuration_generation + 1
        )
    finally:
        pkv_kernel.reset_kernel()
        config_module._config_instance = previous_config


def test_stale_default_kernel_cannot_overwrite_newer_default_config(tmp_path) -> None:
    """An old default reference must not publish settings into a newer graph."""

    import pkv_kernel
    import src.utils.config as config_module

    config_a = _runtime_config(tmp_path / "a")
    config_b = _runtime_config(tmp_path / "b")
    previous_config = config_module._config_instance

    try:
        stale_default = pkv_kernel.configure_kernel(config_a)
        current_default = pkv_kernel.configure_kernel(config_b)

        with pytest.raises(RuntimeError, match="过期的默认 Kernel"):
            stale_default.update_local_config({"logging.level": "DEBUG"})

        assert config_module.get_config() is config_b
        assert pkv_kernel.get_kernel() is current_default
        assert not config_a.user_config_path.exists()
    finally:
        pkv_kernel.reset_kernel()
        config_module._config_instance = previous_config


def test_default_kernel_rejects_root_update_before_persisting(tmp_path) -> None:
    """A default settings update cannot leave a dormant root switch on disk."""

    import pkv_kernel
    import src.utils.config as config_module
    from src.runtime.errors import ErrorCode, PKVRuntimeError

    config = _runtime_config(tmp_path / "current")
    previous_config = config_module._config_instance
    try:
        kernel = pkv_kernel.configure_kernel(config)

        with pytest.raises(PKVRuntimeError) as captured:
            kernel.update_local_config(
                {"storage.data_root": str(tmp_path / "different-root")}
            )

        assert captured.value.code is ErrorCode.DATA_ROOT_SWITCH_REQUIRED
        assert captured.value.stage == "config_update"
        assert not config.user_config_path.exists()
        assert pkv_kernel.get_kernel() is kernel
        assert config_module.get_config() is config
    finally:
        pkv_kernel.reset_kernel()
        config_module._config_instance = previous_config


def test_explicit_kernel_rejects_root_update_before_persisting_and_keeps_a(tmp_path) -> None:
    """B cannot retarget itself or contaminate default A through a settings write."""

    import pkv_kernel
    import src.utils.config as config_module
    from src.runtime.errors import ErrorCode, PKVRuntimeError

    config_a = _runtime_config(tmp_path / "a")
    config_b = _runtime_config(tmp_path / "b")
    previous_config = config_module._config_instance
    try:
        default_kernel = pkv_kernel.configure_kernel(config_a)
        explicit_kernel = pkv_kernel.get_kernel(config_b)

        with pytest.raises(PKVRuntimeError) as captured:
            explicit_kernel.update_local_config(
                {"storage.data_root": str(tmp_path / "different-b-root")}
            )

        assert captured.value.code is ErrorCode.DATA_ROOT_SWITCH_REQUIRED
        assert captured.value.stage == "config_update"
        assert not config_b.user_config_path.exists()
        assert pkv_kernel.get_kernel() is default_kernel
        assert config_module.get_config() is config_a
    finally:
        pkv_kernel.reset_kernel()
        config_module._config_instance = previous_config


def test_default_kernel_reload_rejects_manual_root_change_without_publication(tmp_path) -> None:
    """An externally edited user config cannot silently retarget the live Kernel."""

    import pkv_kernel
    import src.utils.config as config_module
    import yaml
    from src.runtime.errors import ErrorCode, PKVRuntimeError

    config = _runtime_config(tmp_path / "current")
    previous_config = config_module._config_instance
    try:
        kernel = pkv_kernel.configure_kernel(config)
        config.user_config_path.parent.mkdir(parents=True)
        config.user_config_path.write_text(
            yaml.safe_dump(
                {"storage": {"data_root": str(tmp_path / "external-root")}},
                allow_unicode=True,
                sort_keys=False,
            ),
            encoding="utf-8",
        )

        with pytest.raises(PKVRuntimeError) as captured:
            pkv_kernel.reload_kernel()

        assert captured.value.code is ErrorCode.DATA_ROOT_SWITCH_REQUIRED
        assert captured.value.stage == "config_reload"
        assert pkv_kernel.get_kernel() is kernel
        assert config_module.get_config() is config
    finally:
        pkv_kernel.reset_kernel()
        config_module._config_instance = previous_config
