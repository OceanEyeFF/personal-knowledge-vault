"""R4 staged Embedding lifecycle contracts (fake Provider only)."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Callable

import numpy as np
import pytest

import src.storage.vector_store as vector_store_module
import src.utils.config as config_module
from src.runtime.embedding_lifecycle import (
    CapturedEmbeddingSource,
    EmbeddingIndexState,
    EmbeddingSourceRecord,
    EmbeddingSourceSummary,
    PreChunkedEmbeddingAdapter,
    SQLiteEmbeddingSource,
    confirm_embedding_rebuild,
    execute_embedding_rebuild,
    inspect_embedding_index,
    plan_embedding_rebuild,
    publish_embedding_nonready_binding,
    resolve_embedding_index_binding,
)
from src.runtime.errors import ErrorCode, PKVRuntimeError
from src.runtime.layout import RuntimeLayout
from src.runtime.runtime_snapshot import RuntimeSnapshotStore
from src.runtime.write_lease import VaultWriteLease, write_lease_scope
from src.storage.markdown_store import Entry, MarkdownStore
from src.storage.sqlite_store import SQLiteStore
from src.utils.config import Config


PROJECT_ROOT = Path(__file__).parent.parent.parent


def _configured(tmp_path: Path, *, name: str = "b") -> tuple[RuntimeLayout, Config]:
    """Build one isolated explicit Config snapshot with no real credentials."""

    profile_root = tmp_path / f"profile-{name}"
    layout = RuntimeLayout.resolve(
        resources_root=PROJECT_ROOT,
        user_data_root=tmp_path / f"data-{name}",
        profile_root=profile_root,
        environment={},
    )
    layout.user_config_path.parent.mkdir(parents=True)
    layout.user_config_path.write_text(
        json.dumps(
            {
                "ai": {
                    "llm": {"api_key": "llm-test-secret"},
                    "embedding": {
                        "api_key": "embedding-test-secret",
                        "base_url": "https://embedding.invalid/v1",
                        "model": "r4-fake-3",
                        "dim": 3,
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    return layout, Config(layout=layout)


def _summary(records: tuple[EmbeddingSourceRecord, ...]) -> EmbeddingSourceSummary:
    payload = [
        {
            "knowledge_id": record.knowledge_id,
            "content": record.content,
            "chunks": list(record.chunks),
        }
        for record in records
    ]
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=True).encode("utf-8")
    ).hexdigest()
    return EmbeddingSourceSummary(
        document_count=len(records),
        chunk_count=sum(len(record.chunks) for record in records),
        digest=digest,
    )


class _MutableSource:
    """A deterministic, content-bearing source seam; never touches a Provider."""

    def __init__(self, records: tuple[EmbeddingSourceRecord, ...]) -> None:
        self.records = records
        self.invalid = False
        self.capture_calls = 0

    def replace(self, records: tuple[EmbeddingSourceRecord, ...]) -> None:
        self.records = records

    def capture(self, config: object) -> CapturedEmbeddingSource:
        self.capture_calls += 1
        if self.invalid:
            raise PKVRuntimeError(
                ErrorCode.REPAIR_REQUIRED,
                "test-only invalid projection",
                stage="embedding_source",
                recoverable=True,
            )
        return CapturedEmbeddingSource(_summary(self.records), self.records)


def _records(body: str = "article body") -> tuple[EmbeddingSourceRecord, ...]:
    return (
        EmbeddingSourceRecord(
            1,
            body,
            ((0, "first stored chunk"), (1, "second stored chunk")),
        ),
    )


class _FakeEmbedder:
    """Pure deterministic vector provider; network access is impossible here."""

    def __init__(
        self,
        *,
        fail: bool = False,
        on_first_document: Callable[[], None] | None = None,
    ) -> None:
        self.fail = fail
        self.on_first_document = on_first_document
        self.calls: list[tuple[str, object]] = []
        self._called_document = False

    @staticmethod
    def _vector(text: str) -> np.ndarray:
        raw = hashlib.sha256(text.encode("utf-8")).digest()
        # Non-zero values make an HNSW index valid while remaining deterministic.
        return np.asarray(
            [1.0 + raw[0] / 255, 1.0 + raw[1] / 255, 1.0 + raw[2] / 255],
            dtype=np.float32,
        )

    def embed_document(self, text: str) -> np.ndarray:
        self.calls.append(("document", text))
        if not self._called_document:
            self._called_document = True
            if self.on_first_document is not None:
                self.on_first_document()
        if self.fail:
            raise RuntimeError("fake provider failure must not leak to audit")
        return self._vector(text)

    def embed_stored_chunks(self, chunks: tuple[str, ...]) -> np.ndarray:
        self.calls.append(("chunks", chunks))
        if self.fail:
            raise RuntimeError("fake provider failure must not leak to audit")
        return np.stack([self._vector(chunk) for chunk in chunks])


def _r2_base(config: Config) -> dict[str, object]:
    assert config.embedding_dim is not None
    return {
        "schema_version": 1,
        "database": {"schema_version": "1.2.5"},
        "embedding": {
            "provider": config.embd_provider,
            "fingerprint": config.embedding_index_fingerprint(config.embedding_dim),
        },
    }


def _seed_r2_snapshot(config: Config) -> None:
    store = RuntimeSnapshotStore(config.layout)
    observed = store.read()
    with write_lease_scope(config.layout):
        store.publish(observed, _r2_base(config))


def _prepare_r2(config: Config) -> None:
    """Initialize an isolated test-only DB, then record the matching R2 base."""

    from src.runtime.bootstrap import bootstrap_runtime

    bootstrap_runtime(config)
    _seed_r2_snapshot(config)


def _publish_v2_nonready_binding(
    config: Config,
    source: _MutableSource,
    state: EmbeddingIndexState | str,
) -> None:
    """Seed only a test fixture; production state writes arrive in R4 P1."""

    inspection = inspect_embedding_index(config, source=source)
    assert inspection.contract is not None
    assert inspection.source is not None
    snapshot_store = RuntimeSnapshotStore(config.layout)
    observed = snapshot_store.read()
    extension = {
        "schema_version": 2,
        "data_root_identity_sha256": hashlib.sha256(
            config.data_root_identity.encode("utf-8")
        ).hexdigest(),
        "state": state.value if isinstance(state, EmbeddingIndexState) else state,
        "source_digest": inspection.source.digest,
        "contract": inspection.contract.to_dict(),
    }
    with write_lease_scope(config.layout):
        snapshot_store.publish(
            observed,
            observed.merged({"embedding_index": extension}),
        )


def _execute(
    config: Config,
    source: _MutableSource,
    embedder: _FakeEmbedder,
):
    inspection = inspect_embedding_index(config, source=source)
    plan = plan_embedding_rebuild(inspection)
    return execute_embedding_rebuild(
        plan,
        confirm_embedding_rebuild(plan, allow_network=True),
        embedder=embedder,
    )


def test_nonready_binding_publish_requires_lease_and_revokes_any_prior_index(
    tmp_path: Path,
) -> None:
    _, config = _configured(tmp_path)
    source = _MutableSource(_records())
    _prepare_r2(config)
    before = RuntimeSnapshotStore(config.layout).read()

    with pytest.raises(PKVRuntimeError) as no_lease:
        publish_embedding_nonready_binding(
            config,
            state=EmbeddingIndexState.REBUILD_REQUIRED,
            source=source,
        )

    assert no_lease.value.code is ErrorCode.WRITE_BUSY
    assert RuntimeSnapshotStore(config.layout).read() == before
    with write_lease_scope(config.layout):
        published = publish_embedding_nonready_binding(
            config,
            state=EmbeddingIndexState.AUTHORIZATION_REQUIRED,
            source=source,
        )

    assert published.state is EmbeddingIndexState.AUTHORIZATION_REQUIRED
    current = inspect_embedding_index(config, source=source)
    assert current.state is EmbeddingIndexState.AUTHORIZATION_REQUIRED
    with pytest.raises(PKVRuntimeError) as unavailable:
        resolve_embedding_index_binding(config, source=source)
    assert unavailable.value.code is ErrorCode.EMBEDDING_AUTOMATION_AUTHORIZATION_REQUIRED


def test_inspection_and_plan_are_zero_write_and_explicit_config_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout, config_b = _configured(tmp_path)
    source = _MutableSource(_records())

    def _unexpected_global_config() -> Config:
        raise AssertionError("R4 must not consult global Config A")

    monkeypatch.setattr(config_module, "get_config", _unexpected_global_config)
    monkeypatch.setattr(vector_store_module, "get_config", _unexpected_global_config)

    inspection = inspect_embedding_index(config_b, source=source)

    assert inspection.state is EmbeddingIndexState.REBUILD_REQUIRED
    with pytest.raises(PKVRuntimeError) as missing_r2:
        plan_embedding_rebuild(inspection)
    assert missing_r2.value.code is ErrorCode.EMBEDDING_REBUILD_REQUIRED
    assert source.capture_calls == 1
    assert not layout.user_data_root.exists()


def test_successful_generation_preserves_r2_snapshot_and_redacts_audit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout, config_b = _configured(tmp_path)
    _prepare_r2(config_b)
    article = (
        "full article kept locally; embedding-test-secret; "
        "https://alice:password@example.invalid/v1"
    )
    source = _MutableSource(_records(article))

    def _unexpected_global_config() -> Config:
        raise AssertionError("explicit Config B must own the entire rebuild")

    monkeypatch.setattr(config_module, "get_config", _unexpected_global_config)
    monkeypatch.setattr(vector_store_module, "get_config", _unexpected_global_config)

    execution = _execute(config_b, source, _FakeEmbedder())
    snapshot = RuntimeSnapshotStore(layout).read().payload
    pointer = snapshot["embedding_index"]
    assert execution.inspection.state is EmbeddingIndexState.READY
    assert pointer["schema_version"] == 2
    assert pointer["state"] == "ready"
    assert pointer["source_digest"] == execution.inspection.source.digest
    assert pointer["active_generation"] == execution.generation_id
    assert pointer["previous_generation"] is None
    assert snapshot["database"] == {"schema_version": "1.2.5"}
    assert snapshot["embedding"] == _r2_base(config_b)["embedding"]
    assert (layout.vector_index_dir / "generations" / execution.generation_id).is_dir()
    assert not (layout.vector_index_dir / "doc_vectors.idx").exists()

    audit = (layout.log_dir / "audit.jsonl").read_text(encoding="utf-8")
    assert "full article kept locally" in audit
    assert "embedding-test-secret" not in audit
    assert "alice:password" not in audit
    assert "[REDACTED]" in audit


def test_legacy_v1_ready_pointer_remains_strictly_readable(tmp_path: Path) -> None:
    layout, config = _configured(tmp_path)
    _prepare_r2(config)
    source = _MutableSource(_records())
    execution = _execute(config, source, _FakeEmbedder())
    snapshot_store = RuntimeSnapshotStore(layout)
    observed = snapshot_store.read()
    legacy = dict(observed.payload["embedding_index"])
    legacy["schema_version"] = 1
    legacy.pop("state")
    legacy.pop("source_digest")
    with write_lease_scope(layout):
        snapshot_store.publish(
            observed,
            observed.merged({"embedding_index": legacy}),
        )

    inspection = inspect_embedding_index(config, source=source)
    binding = resolve_embedding_index_binding(config, source=source)

    assert inspection.state is EmbeddingIndexState.READY
    assert binding.generation_id == execution.generation_id


def test_failure_source_drift_and_busy_never_change_active_pointer(
    tmp_path: Path,
) -> None:
    layout, config = _configured(tmp_path)
    _prepare_r2(config)
    source = _MutableSource(_records("first article"))
    first = _execute(config, source, _FakeEmbedder())
    snapshot_store = RuntimeSnapshotStore(layout)
    active_before = snapshot_store.read()
    active_dir = layout.vector_index_dir / "generations" / first.generation_id
    active_artifacts = {
        path.name: path.read_bytes()
        for path in sorted(active_dir.iterdir())
        if path.is_file()
    }
    audit_path = layout.log_dir / "audit.jsonl"

    # A malformed projection is rejected before a Provider is considered.
    source.invalid = True
    inspection = inspect_embedding_index(config, source=source)
    assert inspection.state is EmbeddingIndexState.REPAIR_REQUIRED
    with pytest.raises(PKVRuntimeError) as source_error:
        plan_embedding_rebuild(inspection)
    assert source_error.value.code is ErrorCode.REPAIR_REQUIRED
    assert snapshot_store.read().raw_sha256 == active_before.raw_sha256
    assert _FakeEmbedder().calls == []

    source.invalid = False
    source.replace(_records("second article"))
    failure_plan = plan_embedding_rebuild(inspect_embedding_index(config, source=source))
    failed = _FakeEmbedder(fail=True)
    with pytest.raises(PKVRuntimeError) as provider_error:
        execute_embedding_rebuild(
            failure_plan,
            confirm_embedding_rebuild(failure_plan, allow_network=True),
            embedder=failed,
        )
    assert provider_error.value.code is ErrorCode.PROVIDER_UNAVAILABLE
    assert failed.calls
    assert snapshot_store.read().raw_sha256 == active_before.raw_sha256
    assert {
        path.name: path.read_bytes()
        for path in sorted(active_dir.iterdir())
        if path.is_file()
    } == active_artifacts

    busy_plan = plan_embedding_rebuild(inspect_embedding_index(config, source=source))
    audit_before_busy = audit_path.read_bytes()
    with VaultWriteLease(layout):
        with pytest.raises(PKVRuntimeError) as busy_error:
            execute_embedding_rebuild(
                busy_plan,
                confirm_embedding_rebuild(busy_plan, allow_network=True),
                embedder=_FakeEmbedder(),
            )
    assert busy_error.value.code is ErrorCode.WRITE_BUSY
    assert snapshot_store.read().raw_sha256 == active_before.raw_sha256
    assert audit_path.read_bytes() == audit_before_busy


@pytest.mark.parametrize(
    ("state", "expected_code"),
    [
        (EmbeddingIndexState.PROCESSING, ErrorCode.EMBEDDING_PROCESSING),
        (EmbeddingIndexState.RETRY_REQUIRED, ErrorCode.EMBEDDING_RETRY_REQUIRED),
        (EmbeddingIndexState.BUDGET_PAUSED, ErrorCode.EMBEDDING_BUDGET_PAUSED),
        (
            EmbeddingIndexState.AUTHORIZATION_REQUIRED,
            ErrorCode.EMBEDDING_AUTOMATION_AUTHORIZATION_REQUIRED,
        ),
    ],
)
def test_v2_nonready_binding_projects_stable_state_without_flat_fallback(
    tmp_path: Path,
    state: EmbeddingIndexState,
    expected_code: ErrorCode,
) -> None:
    layout, config = _configured(tmp_path)
    _prepare_r2(config)
    source = _MutableSource(_records())
    _publish_v2_nonready_binding(config, source, state)
    snapshot_store = RuntimeSnapshotStore(layout)
    before = snapshot_store.read().raw_sha256

    inspection = inspect_embedding_index(config, source=source)

    assert inspection.state is state
    assert inspection.issues[0].code == expected_code.value
    with pytest.raises(PKVRuntimeError) as binding_error:
        resolve_embedding_index_binding(config, source=source)
    assert binding_error.value.code is expected_code
    assert snapshot_store.read().raw_sha256 == before
    assert not (layout.vector_index_dir / "doc_vectors.idx").exists()
    assert not (layout.vector_index_dir / "chunk_vectors.idx").exists()
    assert not (layout.log_dir / "pkv.log").exists()
    assert not (layout.log_dir / "audit.jsonl").exists()


def test_v2_binding_rejects_unknown_state_instead_of_treating_it_as_no_hits(
    tmp_path: Path,
) -> None:
    _, config = _configured(tmp_path)
    _prepare_r2(config)
    source = _MutableSource(_records())
    _publish_v2_nonready_binding(config, source, "no_hits")

    inspection = inspect_embedding_index(config, source=source)

    assert inspection.state is EmbeddingIndexState.REPAIR_REQUIRED
    assert inspection.issues[0].code == ErrorCode.REPAIR_REQUIRED.value


def test_source_change_during_staged_build_leaves_old_generation_active(tmp_path: Path) -> None:
    layout, config = _configured(tmp_path)
    _prepare_r2(config)
    source = _MutableSource(_records("first article"))
    first = _execute(config, source, _FakeEmbedder())
    before = RuntimeSnapshotStore(layout).read()

    source.replace(_records("second article"))
    plan = plan_embedding_rebuild(inspect_embedding_index(config, source=source))
    mutate_source = _FakeEmbedder(
        on_first_document=lambda: source.replace(_records("third article"))
    )
    with pytest.raises(PKVRuntimeError) as stale_error:
        execute_embedding_rebuild(
            plan,
            confirm_embedding_rebuild(plan, allow_network=True),
            embedder=mutate_source,
        )

    assert stale_error.value.code is ErrorCode.RUNTIME_PLAN_STALE
    after = RuntimeSnapshotStore(layout).read()
    assert after.raw_sha256 == before.raw_sha256
    assert after.payload["embedding_index"]["active_generation"] == first.generation_id


def test_sqlite_vault_capture_and_reader_binding_are_strictly_readonly(tmp_path: Path) -> None:
    """The production source/resolver never writes a flat index or sidecar."""

    from src.runtime.bootstrap import bootstrap_runtime

    layout, config = _configured(tmp_path)
    bootstrap_runtime(config)
    _seed_r2_snapshot(config)
    markdown = MarkdownStore(layout.vault_dir)
    entry = Entry(title="R4 readonly", source_type="text", content="sqlite markdown body")
    write_plan = markdown.plan_save(entry)
    markdown.save_planned(write_plan, entry)
    SQLiteStore(layout.db_path).insert_entry_with_chunks(
        entry,
        write_plan.relative_path,
        ["sqlite chunk one", "sqlite chunk two"],
    )

    source = SQLiteEmbeddingSource()
    initial = inspect_embedding_index(config, source=source)
    assert initial.state is EmbeddingIndexState.REBUILD_REQUIRED
    generation_plan = plan_embedding_rebuild(initial)
    execution = execute_embedding_rebuild(
        generation_plan,
        confirm_embedding_rebuild(generation_plan, allow_network=True),
        embedder=_FakeEmbedder(),
    )
    before = {
        path.relative_to(layout.user_data_root): path.read_bytes()
        for path in sorted(layout.vector_index_dir.rglob("*"))
        if path.is_file()
    }

    binding = resolve_embedding_index_binding(config)

    after = {
        path.relative_to(layout.user_data_root): path.read_bytes()
        for path in sorted(layout.vector_index_dir.rglob("*"))
        if path.is_file()
    }
    assert binding.generation_id == execution.generation_id
    assert binding.index_dir.parent.name == "generations"
    assert before == after
    assert not (layout.vector_index_dir / "doc_vectors.idx").exists()


@pytest.mark.parametrize(
    "projection_fault",
    (
        "orphan_chunk",
        "non_contiguous_chunk",
        "empty_chunk",
        "markdown_sqlite_content_drift",
    ),
)
def test_sqlite_embedding_source_bad_projection_never_touches_active_generation_or_provider(
    tmp_path: Path,
    projection_fault: str,
) -> None:
    """Real SQLite/Vault corruption is fail-closed before rebuild Provider work.

    The source boundary is deliberately the production ``SQLiteEmbeddingSource``
    rather than the test-only mutable seam.  Each mutation models a persisted
    projection state which a vector-only rebuild is not allowed to repair or
    silently re-chunk.
    """

    from src.runtime.bootstrap import bootstrap_runtime

    layout, config = _configured(tmp_path)
    bootstrap_runtime(config)
    _seed_r2_snapshot(config)
    markdown = MarkdownStore(layout.vault_dir)
    entry = Entry(
        title="R4 projection source",
        source_type="text",
        content="sqlite markdown body",
    )
    write_plan = markdown.plan_save(entry)
    markdown.save_planned(write_plan, entry)
    knowledge_id = SQLiteStore(layout.db_path).insert_entry_with_chunks(
        entry,
        write_plan.relative_path,
        ["sqlite chunk one", "sqlite chunk two"],
    )
    source = SQLiteEmbeddingSource()
    first = _execute(config, source, _FakeEmbedder())
    active_dir = layout.vector_index_dir / "generations" / first.generation_id
    active_artifacts_before = {
        path.name: path.read_bytes()
        for path in sorted(active_dir.iterdir())
        if path.is_file()
    }
    snapshot_before = layout.runtime_config_path.read_bytes()
    audit_path = layout.log_dir / "audit.jsonl"
    audit_before = audit_path.read_bytes()

    # Keep a valid approved plan from before corruption.  Its execute path must
    # re-inspect the real source before acquiring a lease, audit, staging, or
    # calling the injected fake Provider.
    force_plan = plan_embedding_rebuild(
        inspect_embedding_index(config, source=source),
        force=True,
    )

    if projection_fault == "markdown_sqlite_content_drift":
        markdown_text = write_plan.absolute_path.read_text(encoding="utf-8")
        assert "sqlite markdown body" in markdown_text
        write_plan.absolute_path.write_text(
            markdown_text.replace("sqlite markdown body", "manually drifted body"),
            encoding="utf-8",
        )
    else:
        # These are intentionally raw mutations: SQLiteStore refuses to create
        # these invalid rows in normal operation, while a rebuild must still
        # recognize and fail closed on an already-corrupted isolated database.
        with sqlite3.connect(layout.db_path) as connection:
            if projection_fault == "orphan_chunk":
                connection.execute(
                    """
                    INSERT INTO content_chunks (knowledge_id, chunk_index, chunk_text)
                    VALUES (?, ?, ?)
                    """,
                    (knowledge_id + 10_000, 0, "orphan stored chunk"),
                )
            elif projection_fault == "non_contiguous_chunk":
                connection.execute(
                    """
                    UPDATE content_chunks
                    SET chunk_index = ?
                    WHERE knowledge_id = ? AND chunk_index = ?
                    """,
                    (2, knowledge_id, 1),
                )
            elif projection_fault == "empty_chunk":
                connection.execute(
                    """
                    UPDATE content_chunks
                    SET chunk_text = ?
                    WHERE knowledge_id = ? AND chunk_index = ?
                    """,
                    ("   ", knowledge_id, 1),
                )
            else:  # pragma: no cover - parametrization above is exhaustive.
                raise AssertionError(f"unexpected projection fault: {projection_fault}")

    inspection = inspect_embedding_index(config, source=source)

    assert inspection.state is EmbeddingIndexState.REPAIR_REQUIRED
    assert inspection.issues[0].code == ErrorCode.REPAIR_REQUIRED.value
    with pytest.raises(PKVRuntimeError) as repair_plan_error:
        plan_embedding_rebuild(inspection, force=True)
    assert repair_plan_error.value.code is ErrorCode.REPAIR_REQUIRED

    fake = _FakeEmbedder()
    with pytest.raises(PKVRuntimeError) as stale_execution:
        execute_embedding_rebuild(
            force_plan,
            confirm_embedding_rebuild(force_plan, allow_network=True),
            embedder=fake,
        )

    assert stale_execution.value.code is ErrorCode.RUNTIME_PLAN_STALE
    assert fake.calls == []
    assert layout.runtime_config_path.read_bytes() == snapshot_before
    assert {
        path.name: path.read_bytes()
        for path in sorted(active_dir.iterdir())
        if path.is_file()
    } == active_artifacts_before
    assert audit_path.read_bytes() == audit_before


def test_r2_base_and_user_config_source_revision_gate_provider_work(tmp_path: Path) -> None:
    """A pointer can never be published from a pointer-only/stale R2 snapshot."""

    layout, config = _configured(tmp_path)
    source = _MutableSource(_records("first article"))
    missing_base = inspect_embedding_index(config, source=source)
    assert missing_base.state is EmbeddingIndexState.REBUILD_REQUIRED
    with pytest.raises(PKVRuntimeError) as base_error:
        plan_embedding_rebuild(missing_base)
    assert base_error.value.code is ErrorCode.EMBEDDING_REBUILD_REQUIRED
    assert not layout.runtime_config_path.exists()

    _prepare_r2(config)
    first = _execute(config, source, _FakeEmbedder())
    snapshot_store = RuntimeSnapshotStore(layout)
    before = snapshot_store.read()
    source.replace(_records("second article"))
    plan = plan_embedding_rebuild(inspect_embedding_index(config, source=source))

    # API-key rotation deliberately does not change the embedding *contract*,
    # but it does make an already-approved R2/R4 plan stale before Provider use.
    user_config = json.loads(layout.user_config_path.read_text(encoding="utf-8"))
    user_config["ai"]["embedding"]["api_key"] = "rotated-test-secret"
    layout.user_config_path.write_text(json.dumps(user_config), encoding="utf-8")
    fake = _FakeEmbedder()
    with pytest.raises(PKVRuntimeError) as stale_error:
        execute_embedding_rebuild(
            plan,
            confirm_embedding_rebuild(plan, allow_network=True),
            embedder=fake,
        )
    assert stale_error.value.code is ErrorCode.RUNTIME_PLAN_STALE
    assert fake.calls == []
    assert snapshot_store.read().raw_sha256 == before.raw_sha256
    assert before.payload["embedding_index"]["active_generation"] == first.generation_id


def test_second_generation_retains_previous_and_config_contract_drift_is_visible(
    tmp_path: Path,
) -> None:
    layout, config = _configured(tmp_path)
    _prepare_r2(config)
    source = _MutableSource(_records("first article"))
    first = _execute(config, source, _FakeEmbedder())

    source.replace(_records("second article"))
    second = _execute(config, source, _FakeEmbedder())
    pointer = RuntimeSnapshotStore(layout).read().payload["embedding_index"]
    assert pointer["active_generation"] == second.generation_id
    assert pointer["previous_generation"] == first.generation_id
    assert {first.generation_id, second.generation_id}.issubset(
        set(pointer["retained_generations"])
    )
    assert (layout.vector_index_dir / "generations" / first.generation_id).is_dir()
    assert (layout.vector_index_dir / "generations" / second.generation_id).is_dir()

    # A key-only successor stays compatible; a model successor does not reuse
    # this generation or silently invoke a Provider.
    user_config = json.loads(layout.user_config_path.read_text(encoding="utf-8"))
    user_config["ai"]["embedding"]["api_key"] = "new-key-is-not-a-contract"
    layout.user_config_path.write_text(json.dumps(user_config), encoding="utf-8")
    key_rotated = Config(layout=layout)
    assert inspect_embedding_index(key_rotated, source=source).state is EmbeddingIndexState.READY

    user_config["ai"]["embedding"]["model"] = "different-model"
    layout.user_config_path.write_text(json.dumps(user_config), encoding="utf-8")
    drifted = Config(layout=layout)
    inspection = inspect_embedding_index(drifted, source=source)
    assert inspection.state is EmbeddingIndexState.REBUILD_REQUIRED
    with pytest.raises(PKVRuntimeError) as drift_plan:
        plan_embedding_rebuild(inspection)
    assert drift_plan.value.code is ErrorCode.EMBEDDING_REBUILD_REQUIRED


def test_strict_generation_inspection_rejects_transaction_marker_without_writing(
    tmp_path: Path,
) -> None:
    layout, config = _configured(tmp_path)
    _prepare_r2(config)
    source = _MutableSource(_records())
    execution = _execute(config, source, _FakeEmbedder())
    generation_dir = layout.vector_index_dir / "generations" / execution.generation_id
    transaction = generation_dir / ".doc_vectors.pair-transaction.json"
    transaction.write_text("{}", encoding="utf-8")
    before = {
        path.relative_to(layout.user_data_root): path.read_bytes()
        for path in sorted(generation_dir.iterdir())
        if path.is_file()
    }
    pointer_before = RuntimeSnapshotStore(layout).read().raw_sha256

    inspection = inspect_embedding_index(config, source=source)
    assert inspection.state is EmbeddingIndexState.REPAIR_REQUIRED
    with pytest.raises(PKVRuntimeError) as resolver_error:
        resolve_embedding_index_binding(config, source=source)

    after = {
        path.relative_to(layout.user_data_root): path.read_bytes()
        for path in sorted(generation_dir.iterdir())
        if path.is_file()
    }
    assert resolver_error.value.code is ErrorCode.REPAIR_REQUIRED
    assert after == before
    assert RuntimeSnapshotStore(layout).read().raw_sha256 == pointer_before


def test_confirmation_values_must_be_real_booleans(tmp_path: Path) -> None:
    _, config = _configured(tmp_path)
    _prepare_r2(config)
    plan = plan_embedding_rebuild(inspect_embedding_index(config, source=_MutableSource(_records())))

    with pytest.raises(TypeError):
        confirm_embedding_rebuild(plan, allow_network="false")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        plan_embedding_rebuild(plan.inspection, force="false")  # type: ignore[arg-type]


def test_prechunked_adapter_uses_real_embedder_batch_seam_without_rechunking(
    tmp_path: Path,
) -> None:
    """The historical Embedder public embed_chunks(text) is never invoked."""

    class LegacyClient:
        def __init__(self) -> None:
            self.calls: list[list[str]] = []

        def embed_batch_numpy(self, texts: list[str]) -> np.ndarray:
            self.calls.append(list(texts))
            return np.stack([_FakeEmbedder._vector(text) for text in texts])

    class LegacyEmbedder:
        def __init__(self) -> None:
            self.client = LegacyClient()
            self.rechunk_calls = 0

        def embed_document(self, text: str) -> np.ndarray:
            return _FakeEmbedder._vector(text)

        def embed_chunks(self, text: str, return_chunks: bool = False) -> object:
            self.rechunk_calls += 1
            raise AssertionError("R4 must not call historical embed_chunks(text)")

    _, config = _configured(tmp_path)
    _prepare_r2(config)
    source = _MutableSource(_records("adapter article"))
    legacy = LegacyEmbedder()

    execution = _execute(config, source, PreChunkedEmbeddingAdapter(legacy))

    assert execution.inspection.state is EmbeddingIndexState.READY
    assert legacy.rechunk_calls == 0
    assert legacy.client.calls == [["first stored chunk", "second stored chunk"]]


@pytest.mark.parametrize(
    "malformed",
    (
        "schema_version: 1\nschema_version: 2\n",
        "cycle: &loop [*loop]\n",
        "? [not-a-string-key]\n: value\n",
    ),
)
def test_runtime_snapshot_rejects_ambiguous_or_cyclic_yaml_as_typed_repair(
    tmp_path: Path,
    malformed: str,
) -> None:
    layout, _ = _configured(tmp_path)
    layout.ensure_user_directories()
    layout.runtime_config_path.write_text(malformed, encoding="utf-8")

    with pytest.raises(PKVRuntimeError) as error:
        RuntimeSnapshotStore(layout).read()

    assert error.value.code is ErrorCode.REPAIR_REQUIRED
    assert error.value.stage == "runtime_snapshot"


def test_embedding_lifecycle_clean_import_has_no_runtime_bootstrap_side_effect(
    tmp_path: Path,
) -> None:
    """The R2 comparison is lazy and cannot revive the old import cycle."""

    isolated_home = tmp_path / "clean-profile"
    isolated_root = tmp_path / "clean-data"
    code = (
        "import sys; "
        f"sys.path.insert(0, {str(PROJECT_ROOT)!r}); "
        "import src.runtime.embedding_lifecycle as lifecycle; "
        "print(lifecycle.EmbeddingIndexState.READY.value)"
    )
    environment = dict(os.environ)
    environment.update(
        {
            "HOME": str(isolated_home),
            "USERPROFILE": str(isolated_home),
            "PKV_DATA_ROOT": str(isolated_root),
            "PKV_TEST_OFFLINE": "1",
        }
    )
    completed = subprocess.run(
        [sys.executable, "-I", "-c", code],
        cwd=PROJECT_ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "ready"
    assert not isolated_root.exists()


def test_post_commit_audit_failure_returns_committed_reconciliation_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An audit fsync failure must not misreport a committed pointer as failed."""

    import src.runtime.audit as audit_module

    layout, config = _configured(tmp_path)
    _prepare_r2(config)
    source = _MutableSource(_records("audit reconciliation article"))
    original_append = audit_module.AuditTrace.append

    def fail_only_completed(
        trace: audit_module.AuditTrace,
        event: object,
    ) -> Path:
        if isinstance(event, dict) and event.get("phase") == "completed":
            raise audit_module.AuditTraceError()
        return original_append(trace, event)  # type: ignore[arg-type]

    monkeypatch.setattr(audit_module.AuditTrace, "append", fail_only_completed)

    execution = _execute(config, source, _FakeEmbedder())

    assert execution.audit_completion_pending is True
    pointer = RuntimeSnapshotStore(layout).read().payload["embedding_index"]
    assert pointer["active_generation"] == execution.generation_id
    audit = (layout.log_dir / "audit.jsonl").read_text(encoding="utf-8")
    assert "activation_intent" in audit
    assert '"phase":"completed"' not in audit
