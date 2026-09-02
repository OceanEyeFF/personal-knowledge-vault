"""R4 archive boundary: Q1′ commits before fenced Q2 Provider work."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import numpy as np
import pytest

from src.application import KnowledgeApplication
from src.kernel import KnowledgeKernel
from src.runtime.bootstrap import bootstrap_runtime
from src.runtime.ai_automation_policy import (
    AutomationPolicyState,
    inspect_ai_automation_policy,
)
from src.runtime.embedding_lifecycle import (
    EmbeddingIndexState,
    inspect_embedding_index,
    resolve_embedding_index_binding,
)
from src.runtime.errors import ErrorCode, PKVRuntimeError
from src.runtime.layout import RuntimeLayout
from src.runtime.runtime_snapshot import RuntimeSnapshotStore
from src.runtime.write_lease import has_active_write_lease, write_lease_scope
from src.storage.vector_store import VectorStore
from src.utils.config import Config


PROJECT_ROOT = Path(__file__).parent.parent.parent


def _configured(tmp_path: Path) -> Config:
    layout = RuntimeLayout.resolve(
        resources_root=PROJECT_ROOT,
        user_data_root=tmp_path / "data",
        profile_root=tmp_path / "profile",
        environment={},
    )
    layout.user_config_path.parent.mkdir(parents=True, exist_ok=True)
    layout.user_config_path.write_text(
        json.dumps(
            {
                "ai": {
                    "llm": {
                        "api_key": "test-llm-secret",
                        "base_url": "https://llm.invalid/v1",
                        "model": "r4-fake-llm",
                    },
                    "embedding": {
                        "api_key": "test-embedding-secret",
                        "base_url": "https://embedding.invalid/v1",
                        "model": "r4-fake-embedding",
                        "dim": 3,
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    return Config(layout=layout)


def _seed_r2_snapshot(config: Config) -> None:
    assert config.embedding_dim is not None
    payload = {
        "schema_version": 1,
        "database": {"schema_version": "1.2.6"},
        "embedding": {
            "provider": config.embd_provider,
            "fingerprint": config.embedding_index_fingerprint(config.embedding_dim),
        },
    }
    store = RuntimeSnapshotStore(config.layout)
    observed = store.read()
    with write_lease_scope(config.layout):
        store.publish(observed, payload)


def _authorized_automation_config(
    tmp_path: Path,
    *,
    daily_total_tokens: int = 1000,
) -> Config:
    config = _configured(tmp_path)
    payload = json.loads(config.user_config_path.read_text(encoding="utf-8"))
    payload["ai"]["automation"] = {
        "schema_version": 1,
        "enabled": True,
        "authorization": {"policy_sha256": None},
        "token_budget": {
            "timezone": "UTC",
            "daily_total_tokens": daily_total_tokens,
            "monthly_total_tokens": daily_total_tokens * 5,
        },
        "retry": {"max_attempts": 2},
    }
    config.user_config_path.write_text(json.dumps(payload), encoding="utf-8")
    pending = Config(layout=config.layout)
    inspection = inspect_ai_automation_policy(pending)
    assert inspection.state is AutomationPolicyState.AUTHORIZATION_REQUIRED
    assert inspection.policy_fingerprint is not None
    payload["ai"]["automation"]["authorization"] = {
        "policy_sha256": inspection.policy_fingerprint
    }
    config.user_config_path.write_text(json.dumps(payload), encoding="utf-8")
    authorized = Config(layout=config.layout)
    assert inspect_ai_automation_policy(authorized).state is AutomationPolicyState.READY
    return authorized


class _FakeEmbeddingClient:
    def embed_batch_numpy(self, texts: list[str]) -> np.ndarray:
        return np.asarray(
            [[float(len(text)), 2.0, 3.0] for text in texts],
            dtype=np.float32,
        )


class _FakeEmbedder:
    def __init__(self) -> None:
        self.client = _FakeEmbeddingClient()

    def embed_document(self, text: str) -> np.ndarray:
        return np.asarray([float(len(text)), 2.0, 3.0], dtype=np.float32)


class _FakeLLM:
    def summarize(self, content: str) -> str:
        del content
        return "Automated R4 summary."

    def extract_tags(self, content: str) -> list[str]:
        del content
        return ["r4", "automated"]


def test_archive_safely_persists_then_defers_ai_and_revokes_vector_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _configured(tmp_path)
    bootstrap_runtime(config)
    _seed_r2_snapshot(config)
    app = KnowledgeApplication(config)

    # The R4 P2 boundary must not even reach either factory during import.
    monkeypatch.setattr(
        app,
        "_create_deepseek_client",
        lambda: (_ for _ in ()).throw(AssertionError("LLM must be deferred")),
    )
    monkeypatch.setattr(
        app,
        "_create_embedder",
        lambda: (_ for _ in ()).throw(AssertionError("embedder must be deferred")),
    )

    result = asyncio.run(app.archive_text("R4 archive body", title="R4 document"))

    assert result.success is True
    assert result.terminal == "degraded"
    assert result.data["core_committed"] is True
    assert result.data["knowledge_id"] is not None
    assert result.data["ai_automation"] == {
        "status": "rebuild_required",
        "task_state": "authorization_required",
    }
    assert result.issues[-1]["code"] == ErrorCode.EMBEDDING_REBUILD_REQUIRED.value
    assert not VectorStore.has_index_artifacts(config.vector_index_dir)

    inspection = inspect_embedding_index(config)
    assert inspection.state is EmbeddingIndexState.REBUILD_REQUIRED
    assert inspection.source is not None and inspection.source.document_count == 1
    with pytest.raises(PKVRuntimeError) as unavailable:
        resolve_embedding_index_binding(config)
    assert unavailable.value.code is ErrorCode.EMBEDDING_REBUILD_REQUIRED
    with sqlite3.connect(config.layout.db_path) as connection:
        task = connection.execute(
            "SELECT state FROM ai_derivation_tasks"
        ).fetchone()
    assert task == ("authorization_required",)


def test_authorized_archive_automatically_builds_generation_after_reservation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _authorized_automation_config(tmp_path, daily_total_tokens=20_000)
    bootstrap_runtime(config)
    _seed_r2_snapshot(config)
    app = KnowledgeApplication(config)
    monkeypatch.setattr(app, "_create_deepseek_client", _FakeLLM)
    monkeypatch.setattr(
        app,
        "_create_embedder",
        _FakeEmbedder,
    )

    result = asyncio.run(app.archive_text("authorized R4 body"))

    assert result.success is True
    assert result.terminal == "success"
    assert result.data["ai_automation"] == {
        "status": "ready",
        "task_state": "completed",
    }
    with sqlite3.connect(config.layout.db_path) as connection:
        row = connection.execute(
            "SELECT state, policy_fingerprint FROM ai_derivation_tasks"
        ).fetchone()
        usage = connection.execute(
            """
                SELECT uncached_input_tokens, cached_input_tokens,
                       generated_tokens, embedding_input_tokens, source
                FROM ai_derivation_usage
                WHERE stage = 'embedding' AND source = 'local_estimate'
            """
        ).fetchone()
    assert row == ("completed", inspect_ai_automation_policy(config).policy_fingerprint)
    assert usage is not None
    assert usage[:3] == (None, None, None)
    assert usage[3] is not None and usage[3] > 0
    assert usage[4] == "local_estimate"
    assert inspect_embedding_index(config).state is EmbeddingIndexState.READY


def test_q2_provider_calls_run_outside_the_root_writer_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Q2 may hold its lease for local commits, never for paid Provider I/O."""

    config = _authorized_automation_config(tmp_path, daily_total_tokens=20_000)
    bootstrap_runtime(config)
    _seed_r2_snapshot(config)
    app = KnowledgeApplication(config)
    provider_events: list[str] = []

    def _assert_provider_outside_lease(stage: str) -> None:
        assert not has_active_write_lease(config.layout), stage
        provider_events.append(stage)

    class _LeaseAwareLLM:
        def summarize(self, content: str) -> str:
            del content
            _assert_provider_outside_lease("summary")
            return "Lease-safe summary."

        def extract_tags(self, content: str) -> list[str]:
            del content
            _assert_provider_outside_lease("tags")
            return ["lease", "safe"]

    class _LeaseAwareEmbeddingClient:
        def embed_batch_numpy(self, texts: list[str]) -> np.ndarray:
            _assert_provider_outside_lease("embedding_chunks")
            return np.asarray(
                [[float(len(text)), 2.0, 3.0] for text in texts],
                dtype=np.float32,
            )

    class _LeaseAwareEmbedder:
        def __init__(self) -> None:
            self.client = _LeaseAwareEmbeddingClient()

        def embed_document(self, text: str) -> np.ndarray:
            _assert_provider_outside_lease("embedding_document")
            return np.asarray([float(len(text)), 2.0, 3.0], dtype=np.float32)

    def _llm_factory() -> _LeaseAwareLLM:
        _assert_provider_outside_lease("llm_factory")
        return _LeaseAwareLLM()

    def _embedder_factory() -> _LeaseAwareEmbedder:
        _assert_provider_outside_lease("embedding_factory")
        return _LeaseAwareEmbedder()

    monkeypatch.setattr(app, "_create_deepseek_client", _llm_factory)
    monkeypatch.setattr(app, "_create_embedder", _embedder_factory)

    result = asyncio.run(app.archive_text("lease-safe R4 provider body"))

    assert result.success is True
    assert result.terminal == "success"
    assert len(provider_events) == 6
    assert set(provider_events) == {
        "llm_factory",
        "summary",
        "tags",
        "embedding_factory",
        "embedding_document",
        "embedding_chunks",
    }


def test_auto_automation_pauses_before_provider_when_token_budget_is_exhausted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _authorized_automation_config(tmp_path, daily_total_tokens=20_000)
    payload = json.loads(config.user_config_path.read_text(encoding="utf-8"))
    payload["ai"]["automation"]["token_budget"]["daily_total_tokens"] = 0
    payload["ai"]["automation"]["authorization"] = {"policy_sha256": None}
    config.user_config_path.write_text(json.dumps(payload), encoding="utf-8")
    pending = Config(layout=config.layout)
    policy = inspect_ai_automation_policy(pending)
    assert policy.policy_fingerprint is not None
    payload["ai"]["automation"]["authorization"] = {
        "policy_sha256": policy.policy_fingerprint
    }
    config.user_config_path.write_text(json.dumps(payload), encoding="utf-8")
    config = Config(layout=config.layout)
    bootstrap_runtime(config)
    _seed_r2_snapshot(config)
    app = KnowledgeApplication(config)
    monkeypatch.setattr(
        app,
        "_create_deepseek_client",
        lambda: (_ for _ in ()).throw(AssertionError("budget must precede LLM")),
    )
    monkeypatch.setattr(
        app,
        "_create_embedder",
        lambda: (_ for _ in ()).throw(AssertionError("budget must precede Provider")),
    )

    result = asyncio.run(app.archive_text("budget-paused R4 body"))

    assert result.success is True
    assert result.terminal == "degraded"
    assert result.data["ai_automation"] == {
        "status": "budget_paused",
        "task_state": "budget_paused",
    }
    assert inspect_embedding_index(config).state is EmbeddingIndexState.BUDGET_PAUSED
    with sqlite3.connect(config.layout.db_path) as connection:
        row = connection.execute("SELECT state FROM ai_derivation_tasks").fetchone()
        reservations = connection.execute(
            "SELECT COUNT(*) FROM ai_derivation_reservations"
        ).fetchone()
    assert row == ("budget_paused",)
    assert reservations == (0,)


def test_auto_automation_provider_failure_keeps_document_and_marks_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _authorized_automation_config(tmp_path)
    bootstrap_runtime(config)
    _seed_r2_snapshot(config)
    app = KnowledgeApplication(config)
    monkeypatch.setattr(app, "_create_deepseek_client", _FakeLLM)
    monkeypatch.setattr(
        app,
        "_create_embedder",
        lambda: (_ for _ in ()).throw(RuntimeError("offline fake failure")),
    )

    result = asyncio.run(app.archive_text("retry-required R4 body"))

    assert result.success is True
    assert result.terminal == "degraded"
    assert result.data["core_committed"] is True
    assert result.data["ai_automation"] == {
        "status": "retry_required",
        "task_state": "retry_required",
    }
    assert inspect_embedding_index(config).state is EmbeddingIndexState.RETRY_REQUIRED
    with sqlite3.connect(config.layout.db_path) as connection:
        row = connection.execute(
            "SELECT state, last_error_code FROM ai_derivation_tasks"
        ).fetchone()
        reservation = connection.execute(
            "SELECT state FROM ai_derivation_reservations"
        ).fetchone()
    assert row == ("retry_required", ErrorCode.PROVIDER_UNAVAILABLE.value)
    assert reservation == ("settled",)


def test_nonready_binding_blocks_related_and_hybrid_without_flat_or_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _configured(tmp_path)
    bootstrap_runtime(config)
    _seed_r2_snapshot(config)
    app = KnowledgeApplication(config)
    monkeypatch.setattr(
        app,
        "_create_embedder",
        lambda: (_ for _ in ()).throw(AssertionError("binding must precede Provider")),
    )

    archived = asyncio.run(app.archive_text("binding reader archive body"))
    knowledge_id = archived.data["knowledge_id"]
    assert isinstance(knowledge_id, int)
    assert archived.data["ai_automation"]["status"] == "rebuild_required"
    assert not VectorStore.has_index_artifacts(config.vector_index_dir)

    related = app.related(knowledge_id)
    vector = app.search("binding reader query", strategy="vector")
    hybrid = app.search("binding reader query", strategy="hybrid")

    assert related["status"] == "degraded"
    assert related["issues"][0]["code"] == ErrorCode.EMBEDDING_REBUILD_REQUIRED.value
    assert vector.status == "error"
    assert vector.issues[0].code is ErrorCode.EMBEDDING_REBUILD_REQUIRED
    assert hybrid.status == "degraded"
    assert hybrid.issues[0].code is ErrorCode.EMBEDDING_REBUILD_REQUIRED
    assert not VectorStore.has_index_artifacts(config.vector_index_dir)


def test_kernel_delete_routes_real_r4_application_through_q1_and_never_flat_vectors(
    tmp_path: Path,
) -> None:
    config = _configured(tmp_path)
    bootstrap_runtime(config)
    _seed_r2_snapshot(config)
    app = KnowledgeApplication(config)
    archived = asyncio.run(app.archive_text("kernel R4 delete body"))
    knowledge_id = archived.data["knowledge_id"]
    assert isinstance(knowledge_id, int)
    archive_operation_id = archived.data["operation_id"]
    assert isinstance(archive_operation_id, str)
    kernel = KnowledgeKernel._from_application(app)

    with pytest.raises(PKVRuntimeError) as vectors:
        kernel.delete_vectors_for_entry(knowledge_id)
    assert vectors.value.code is ErrorCode.EMBEDDING_REBUILD_REQUIRED

    deleted = kernel.delete_entry(knowledge_id)

    assert deleted.status.value == "deleted"
    assert app.sqlite_store.query_by_id(knowledge_id) is None
    assert not VectorStore.has_index_artifacts(config.vector_index_dir)
    record = app.storage_coordinator.journal.read(deleted.operation_id)
    assert record["action"] == "delete"
    with sqlite3.connect(config.layout.db_path) as connection:
        q1 = connection.execute(
            "SELECT state FROM content_mutation_tasks WHERE operation_id = ?",
            (deleted.operation_id,),
        ).fetchone()
        old_archive_derivation = connection.execute(
            "SELECT state FROM ai_derivation_tasks WHERE operation_id = ?",
            (archive_operation_id,),
        ).fetchone()
        delete_derivation = connection.execute(
            "SELECT state FROM ai_derivation_tasks WHERE operation_id = ?",
            (deleted.operation_id,),
        ).fetchone()
    assert q1 == ("completed",)
    assert old_archive_derivation == ("superseded",)
    assert delete_derivation == ("authorization_required",)


def test_ready_binding_serves_related_from_generation_not_flat_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _authorized_automation_config(tmp_path, daily_total_tokens=20_000)
    bootstrap_runtime(config)
    _seed_r2_snapshot(config)
    app = KnowledgeApplication(config)
    monkeypatch.setattr(app, "_create_deepseek_client", _FakeLLM)
    monkeypatch.setattr(app, "_create_embedder", _FakeEmbedder)

    first = asyncio.run(app.archive_text("first generation related document"))
    second = asyncio.run(app.archive_text("second generation related document"))
    first_id = first.data["knowledge_id"]
    second_id = second.data["knowledge_id"]
    assert isinstance(first_id, int) and isinstance(second_id, int)
    assert first.data["ai_automation"]["status"] == "ready"
    assert second.data["ai_automation"]["status"] == "ready"
    assert not VectorStore.has_index_artifacts(config.vector_index_dir)

    binding = resolve_embedding_index_binding(config)
    retriever = app._new_vector_retriever()
    related = app.related(first_id)

    assert retriever.vector_index_dir == binding.index_dir
    assert binding.index_dir.parent == config.layout.vector_index_dir / "generations"
    assert related["status"] == "success"
    assert [item["knowledge_id"] for item in related["results"]] == [second_id]
