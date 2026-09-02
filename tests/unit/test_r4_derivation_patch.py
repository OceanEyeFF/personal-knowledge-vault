"""R4-D Q2 output must return to the sole Q1′ Markdown/SQLite writer."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

from src.application import KnowledgeApplication
from src.application.r4_lifecycle import R4ContentLifecycle
from src.runtime.bootstrap import bootstrap_runtime
from src.runtime.errors import ErrorCode, PKVRuntimeError
from src.runtime.runtime_snapshot import RuntimeSnapshotStore
from src.runtime.write_lease import write_lease_scope
from src.storage.derivation_patch import DerivationPatch, DerivationPatchSpool
from src.storage.content_lifecycle import ContentMutationState, PreparedDocument
from src.storage.markdown_store import Entry
from src.utils.config import Config
from src.runtime.layout import RuntimeLayout


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
                        "api_key": "r4-patch-llm-secret",
                        "base_url": "https://llm.invalid/v1",
                        "model": "r4-patch-llm",
                    },
                    "embedding": {
                        "api_key": "r4-patch-embedding-secret",
                        "base_url": "https://embedding.invalid/v1",
                        "model": "r4-patch-embedding",
                        "dim": 3,
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    config = Config(layout=layout)
    bootstrap_runtime(config)
    assert config.embedding_dim is not None
    snapshots = RuntimeSnapshotStore(config.layout)
    with write_lease_scope(config.layout):
        snapshots.publish(
            snapshots.read(),
            {
                "schema_version": 1,
                "database": {"schema_version": "1.2.6"},
                "embedding": {
                    "provider": config.embd_provider,
                    "fingerprint": config.embedding_index_fingerprint(
                        config.embedding_dim
                    ),
                },
            },
        )
    return config


async def _archive(lifecycle: R4ContentLifecycle):
    with lifecycle._application._write_lease_scope():
        return await lifecycle.submit_and_drain(
            PreparedDocument.for_archive(
                Entry(title="patch target", source_type="text", content="patch body")
            )
        )


async def _apply(lifecycle: R4ContentLifecycle, patch: DerivationPatch):
    with lifecycle._application._write_lease_scope():
        return await lifecycle.submit_patch_and_drain(patch)


def test_derivation_patch_is_atomically_applied_once_through_q1(tmp_path: Path) -> None:
    config = _configured(tmp_path)
    app = KnowledgeApplication(config)
    lifecycle = R4ContentLifecycle(app)
    archived = asyncio.run(_archive(lifecycle))
    source_task = lifecycle.store.get_derivation_task(archived.task.operation_id)
    assert source_task is not None
    assert source_task.target_knowledge_id is not None
    assert source_task.target_revision_sha256 is not None
    assert source_task.source_digest is not None
    patch = DerivationPatch.create(
        derivation_task_id=source_task.task_id,
        target_knowledge_id=source_task.target_knowledge_id,
        expected_revision_sha256=source_task.target_revision_sha256,
        input_digest=source_task.source_digest,
        summary="AI generated summary. Second sentence.",
        tags=["r4", "patch"],
    )

    applied = asyncio.run(_apply(lifecycle, patch))
    repeated = asyncio.run(_apply(lifecycle, patch))

    assert applied.core_committed is True
    assert repeated.core_committed is True
    row = app.sqlite_store.query_by_id(source_task.target_knowledge_id)
    assert row is not None
    assert row["summary_100_words"] == patch.summary
    assert row["summary_one_sentence"] == "AI generated summary"
    assert row["tags"] == "r4,patch"
    markdown = app.markdown_store.load(
        app.markdown_store.gateway.resolve(row["file_path"], must_exist=True, require_file=True)
    )
    assert markdown.summary_100_words == patch.summary
    assert markdown.tags == ["r4", "patch"]
    with sqlite3.connect(config.db_path) as connection:
        proof_count = connection.execute(
            "SELECT COUNT(*) FROM r4_content_operation_commits"
        ).fetchone()[0]
        patch_q2_children = connection.execute(
            """
            SELECT COUNT(*) FROM ai_derivation_tasks d
            JOIN content_mutation_tasks c ON c.operation_id = d.operation_id
            WHERE c.action = 'apply_ai_patch'
            """
        ).fetchone()[0]
    assert proof_count == 1
    assert patch_q2_children == 0


@pytest.mark.parametrize(
    "changed_field,changed_value",
    [
        ("derivation_task_id", "f" * 32),
        ("target_knowledge_id", 999_999),
        ("expected_revision_sha256", "a" * 64),
        ("input_digest", "b" * 64),
    ],
)
def test_derivation_patch_identity_mismatch_fails_before_spool_or_write(
    tmp_path: Path,
    changed_field: str,
    changed_value: str | int,
) -> None:
    config = _configured(tmp_path)
    app = KnowledgeApplication(config)
    lifecycle = R4ContentLifecycle(app)
    archived = asyncio.run(_archive(lifecycle))
    source_task = lifecycle.store.get_derivation_task(archived.task.operation_id)
    assert source_task is not None
    assert source_task.target_knowledge_id is not None
    assert source_task.target_revision_sha256 is not None
    assert source_task.source_digest is not None
    values: dict[str, object] = {
        "derivation_task_id": source_task.task_id,
        "target_knowledge_id": source_task.target_knowledge_id,
        "expected_revision_sha256": source_task.target_revision_sha256,
        "input_digest": source_task.source_digest,
    }
    values[changed_field] = changed_value
    patch = DerivationPatch.create(
        **values,
        summary="must not be committed",
        tags=["invalid"],
    )
    before = app.sqlite_store.query_by_id(source_task.target_knowledge_id)

    with pytest.raises(PKVRuntimeError) as captured:
        asyncio.run(_apply(lifecycle, patch))

    assert captured.value.code is ErrorCode.RUNTIME_PLAN_STALE
    assert app.sqlite_store.query_by_id(source_task.target_knowledge_id) == before
    assert lifecycle.store.get_ai_patch_task(patch.patch_id) is None
    assert not (
        config.layout.runtime_state_dir / "r4" / "patches" / f"{patch.patch_id}.json"
    ).exists()


def test_tampered_derivation_patch_spool_fails_closed_before_content_write(
    tmp_path: Path,
) -> None:
    config = _configured(tmp_path)
    app = KnowledgeApplication(config)
    lifecycle = R4ContentLifecycle(app)
    archived = asyncio.run(_archive(lifecycle))
    source_task = lifecycle.store.get_derivation_task(archived.task.operation_id)
    assert source_task is not None
    assert source_task.target_knowledge_id is not None
    assert source_task.target_revision_sha256 is not None
    assert source_task.source_digest is not None
    patch = DerivationPatch.create(
        derivation_task_id=source_task.task_id,
        target_knowledge_id=source_task.target_knowledge_id,
        expected_revision_sha256=source_task.target_revision_sha256,
        input_digest=source_task.source_digest,
        summary="must not survive tampering",
        tags=["tampered"],
    )
    with app._write_lease_scope():
        reference = DerivationPatchSpool(config.layout).write(patch)
        task = lifecycle.store.enqueue_ai_patch(
            patch_ref=reference.patch_id,
            patch_sha256=reference.payload_sha256,
            target_knowledge_id=patch.target_knowledge_id,
            target_revision_sha256=patch.expected_revision_sha256,
        )
    patch_path = (
        config.layout.runtime_state_dir / "r4" / "patches" / f"{patch.patch_id}.json"
    )
    patch_path.write_bytes(patch_path.read_bytes() + b"tampered")
    before = app.sqlite_store.query_by_id(source_task.target_knowledge_id)

    async def drain():
        with app._write_lease_scope():
            return await lifecycle.drain_task(task.task_id)

    result = asyncio.run(drain())

    assert result.core_committed is False
    assert result.task.state is ContentMutationState.RETRY_REQUIRED
    assert app.sqlite_store.query_by_id(source_task.target_knowledge_id) == before
    assert patch_path.exists()
    with sqlite3.connect(config.db_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM r4_content_operation_commits WHERE operation_id = ?",
            (task.operation_id,),
        ).fetchone() == (0,)
