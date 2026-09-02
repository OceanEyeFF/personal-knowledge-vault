"""R4-A characterization: Q1′ commit/handoff before any Q2 Provider work."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

from src.application import KnowledgeApplication
from src.application.r4_lifecycle import R4ContentLifecycle, R4LifecycleFault
from src.runtime.bootstrap import bootstrap_runtime
from src.runtime.runtime_snapshot import RuntimeSnapshotStore
from src.runtime.write_lease import write_lease_scope
from src.storage.content_lifecycle import (
    AIDerivationState,
    ContentMutationState,
    PreparedDocument,
)
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
                        "api_key": "r4-test-llm-secret",
                        "base_url": "https://llm.invalid/v1",
                        "model": "r4-test-llm",
                    },
                    "embedding": {
                        "api_key": "r4-test-embedding-secret",
                        "base_url": "https://embedding.invalid/v1",
                        "model": "r4-test-embedding",
                        "dim": 3,
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    config = Config(layout=layout)
    bootstrap_runtime(config)
    _seed_r2_snapshot(config)
    return config


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
    with write_lease_scope(config.layout):
        store.publish(store.read(), payload)


async def _submit(
    lifecycle: R4ContentLifecycle,
    document: PreparedDocument,
):
    with lifecycle._application._write_lease_scope():
        return await lifecycle.submit_and_drain(document)


async def _recover(lifecycle: R4ContentLifecycle):
    with lifecycle._application._write_lease_scope():
        return await lifecycle.recover_and_drain()


def test_q1_archive_proves_core_commit_then_activates_q2_without_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _configured(tmp_path)
    app = KnowledgeApplication(config)
    lifecycle = R4ContentLifecycle(app)
    monkeypatch.setattr(
        app,
        "_create_embedder",
        lambda: (_ for _ in ()).throw(AssertionError("R4-A must not create Provider")),
    )
    monkeypatch.setattr(
        app,
        "_create_deepseek_client",
        lambda: (_ for _ in ()).throw(AssertionError("R4-A must not create Provider")),
    )

    result = asyncio.run(
        _submit(
            lifecycle,
            PreparedDocument.for_archive(
                Entry(title="Q1′ archive", source_type="text", content="Q1 body"),
                provenance={"source": "fake"},
            ),
        )
    )

    assert result.core_committed is True
    assert result.task.state is ContentMutationState.COMPLETED
    assert result.handoff_state == "q2_activated"
    assert result.derivation_state == AIDerivationState.AUTHORIZATION_REQUIRED.value
    handoff = lifecycle.store.get_handoff(result.task.operation_id)
    derivation = lifecycle.store.get_derivation_task(result.task.operation_id)
    assert handoff is not None and handoff.source_digest is not None
    assert derivation is not None
    assert derivation.state is AIDerivationState.AUTHORIZATION_REQUIRED
    assert derivation.claim_token is None

    journal = app.storage_coordinator.journal.read(result.task.operation_id)
    assert journal["action"] == "archive"
    assert journal["core_committed"] is True
    with sqlite3.connect(config.db_path) as connection:
        proof_count = connection.execute(
            "SELECT COUNT(*) FROM storage_operation_commits WHERE operation_id = ?",
            (result.task.operation_id,),
        ).fetchone()[0]
        legacy_task_count = connection.execute(
            "SELECT COUNT(*) FROM ai_automation_tasks"
        ).fetchone()[0]
        usage_count = connection.execute(
            "SELECT COUNT(*) FROM ai_derivation_usage"
        ).fetchone()[0]
        reservation_count = connection.execute(
            "SELECT COUNT(*) FROM ai_derivation_reservations"
        ).fetchone()[0]
    assert proof_count == 1
    assert legacy_task_count == 0
    assert usage_count == 0
    assert reservation_count == 0


def test_q1_delete_reuses_operation_bound_core_proof(tmp_path: Path) -> None:
    config = _configured(tmp_path)
    app = KnowledgeApplication(config)
    lifecycle = R4ContentLifecycle(app)
    archived = asyncio.run(
        _submit(
            lifecycle,
            PreparedDocument.for_archive(
                Entry(title="delete target", source_type="text", content="delete body")
            ),
        )
    )
    row = app.sqlite_store.query_storage_operation(archived.task.operation_id)
    assert row is not None
    knowledge_id = int(row["knowledge_id"])

    deleted = asyncio.run(
        _submit(lifecycle, PreparedDocument.for_delete(knowledge_id))
    )

    assert deleted.core_committed is True
    assert deleted.task.state is ContentMutationState.COMPLETED
    assert app.sqlite_store.query_by_id(knowledge_id) is None
    journal = app.storage_coordinator.journal.read(deleted.task.operation_id)
    assert journal["action"] == "delete"
    assert journal["core_committed"] is True
    archived_derivation = lifecycle.store.get_derivation_task(
        archived.task.operation_id
    )
    delete_derivation = lifecycle.store.get_derivation_task(
        deleted.task.operation_id
    )
    assert archived_derivation is not None
    assert archived_derivation.state is AIDerivationState.SUPERSEDED
    assert delete_derivation is not None
    assert delete_derivation.state is AIDerivationState.AUTHORIZATION_REQUIRED
    assert lifecycle.store.get_handoff(deleted.task.operation_id).state.value == "q2_activated"


@pytest.mark.parametrize(
    "phase",
    ["core_committed", "handoff_recorded", "binding_published", "q2_activated"],
)
def test_crash_boundaries_converge_idempotently_on_next_q1_trigger(
    tmp_path: Path,
    phase: str,
) -> None:
    config = _configured(tmp_path)
    app = KnowledgeApplication(config)
    fired = False

    def fail_once(observed: str, _task) -> None:
        nonlocal fired
        if observed == phase and not fired:
            fired = True
            raise R4LifecycleFault(phase)

    crashing = R4ContentLifecycle(app, fault_hook=fail_once)
    document = PreparedDocument.for_archive(
        Entry(title=f"crash-{phase}", source_type="text", content="crash body")
    )
    with pytest.raises(R4LifecycleFault, match=phase):
        asyncio.run(_submit(crashing, document))

    reloaded_config = Config(layout=config.layout)
    bootstrap_runtime(reloaded_config)
    recovered_app = KnowledgeApplication(reloaded_config)
    recovered = R4ContentLifecycle(recovered_app)
    asyncio.run(_recover(recovered))

    # The task has normally left the recoverable list already; locate it via
    # the private spool identity through SQLite rather than relying on a
    # terminal-state filter.
    with sqlite3.connect(reloaded_config.db_path) as connection:
        row = connection.execute(
            "SELECT operation_id, state FROM content_mutation_tasks WHERE prepared_ref = ?",
            (document.prepared_id,),
        ).fetchone()
        assert row is not None
        operation_id, state = row
        physical_counts = {
            "knowledge_items": connection.execute(
                "SELECT COUNT(*) FROM knowledge_items"
            ).fetchone()[0],
            "content_mutation_tasks": connection.execute(
                "SELECT COUNT(*) FROM content_mutation_tasks WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()[0],
            "storage_operation_commits": connection.execute(
                "SELECT COUNT(*) FROM storage_operation_commits WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()[0],
            "content_ai_handoffs": connection.execute(
                "SELECT COUNT(*) FROM content_ai_handoffs WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()[0],
            "ai_derivation_tasks": connection.execute(
                "SELECT COUNT(*) FROM ai_derivation_tasks WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()[0],
        }
    assert state == ContentMutationState.COMPLETED.value
    assert physical_counts == {
        "knowledge_items": 1,
        "content_mutation_tasks": 1,
        "storage_operation_commits": 1,
        "content_ai_handoffs": 1,
        "ai_derivation_tasks": 1,
    }
    assert len(list(reloaded_config.vault_dir.rglob("*.md"))) == 1
    assert recovered.store.get_handoff(operation_id).state.value == "q2_activated"
    assert recovered.store.get_derivation_task(operation_id).state is AIDerivationState.AUTHORIZATION_REQUIRED
    assert not (
        config.layout.runtime_state_dir / "r4" / "prepared" / f"{document.prepared_id}.json"
    ).exists()


def test_q2_is_nonclaimable_before_q1_handoff(tmp_path: Path) -> None:
    config = _configured(tmp_path)
    app = KnowledgeApplication(config)
    lifecycle = R4ContentLifecycle(app)
    document = PreparedDocument.for_archive(
        Entry(title="blocked Q2", source_type="text", content="blocked body")
    )

    async def submit_only():
        with app._write_lease_scope():
            return lifecycle.submit_prepared(document)

    task = asyncio.run(submit_only())
    derivation = lifecycle.store.get_derivation_task(task.operation_id)
    assert derivation is not None
    assert derivation.state is AIDerivationState.BLOCKED_HANDOFF
    assert lifecycle.store.get_handoff(task.operation_id) is None
    with sqlite3.connect(config.db_path) as connection:
        usage_count = connection.execute(
            "SELECT COUNT(*) FROM ai_derivation_usage"
        ).fetchone()[0]
        reservation_count = connection.execute(
            "SELECT COUNT(*) FROM ai_derivation_reservations"
        ).fetchone()[0]
    assert usage_count == 0
    assert reservation_count == 0


def test_tampered_prepared_document_fails_closed_before_content_commit(
    tmp_path: Path,
) -> None:
    config = _configured(tmp_path)
    app = KnowledgeApplication(config)
    lifecycle = R4ContentLifecycle(app)
    document = PreparedDocument.for_archive(
        Entry(title="tamper target", source_type="text", content="private body")
    )
    with app._write_lease_scope():
        task = lifecycle.submit_prepared(document)
    prepared_path = (
        config.layout.runtime_state_dir
        / "r4"
        / "prepared"
        / f"{document.prepared_id}.json"
    )
    prepared_path.write_bytes(prepared_path.read_bytes() + b"tampered")

    async def drain():
        with app._write_lease_scope():
            return await lifecycle.drain_task(task.task_id)

    result = asyncio.run(drain())

    assert result.core_committed is False
    assert result.task.state is ContentMutationState.RETRY_REQUIRED
    assert prepared_path.exists()
    derivation = lifecycle.store.get_derivation_task(task.operation_id)
    assert derivation is not None
    assert derivation.state is AIDerivationState.BLOCKED_HANDOFF
    with sqlite3.connect(config.db_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM knowledge_items"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM storage_operation_commits"
        ).fetchone() == (0,)
