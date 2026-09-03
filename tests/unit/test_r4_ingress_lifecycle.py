"""R4-B Q0 characterization: admission precedes parser work and is recoverable."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from src.application import KnowledgeApplication
from src.application.r4_ingress_lifecycle import R4IngressLifecycle
from src.application.r4_lifecycle import R4ContentLifecycle, R4LifecycleFault
from src.runtime.bootstrap import bootstrap_runtime
from src.runtime.errors import ErrorCode, PKVRuntimeError
from src.runtime.layout import RuntimeLayout
from src.runtime.runtime_snapshot import RuntimeSnapshotStore
from src.runtime.write_lease import has_active_write_lease, write_lease_scope
from src.storage.content_lifecycle import ContentMutationState
from src.storage.ingress_lifecycle import (
    IngressKind,
    IngressRequest,
    IngressState,
    IngressTaskSpool,
)
from src.storage.markdown_store import Entry
from src.utils.config import Config


PROJECT_ROOT = Path(__file__).parent.parent.parent


def _tree_snapshot(root: Path) -> dict[str, str]:
    if not root.exists():
        return {}
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.name != "write.lease"
    }


def _r4_row_counts(database_path: Path) -> dict[str, int]:
    tables = (
        "knowledge_items",
        "content_chunks",
        "ingress_tasks",
        "content_mutation_tasks",
        "content_ai_handoffs",
        "ai_derivation_tasks",
        "ai_derivation_reservations",
        "ai_derivation_usage",
        "storage_operation_commits",
        "r4_content_operation_commits",
    )
    with sqlite3.connect(database_path) as connection:
        return {
            table: connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            for table in tables
        }


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
                        "api_key": "r4-q0-llm-secret",
                        "base_url": "https://llm.invalid/v1",
                        "model": "r4-q0-llm",
                    },
                    "embedding": {
                        "api_key": "r4-q0-embedding-secret",
                        "base_url": "https://embedding.invalid/v1",
                        "model": "r4-q0-embedding",
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
    snapshot = RuntimeSnapshotStore(config.layout)
    with write_lease_scope(config.layout):
        snapshot.publish(
            snapshot.read(),
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


def test_q0_admission_happens_before_fake_preparer_and_hands_one_prepared_contract(
    tmp_path: Path,
) -> None:
    config = _configured(tmp_path)
    app = KnowledgeApplication(config)
    observed: list[IngressRequest] = []

    async def fake_preparer(request: IngressRequest) -> Entry:
        with sqlite3.connect(config.db_path) as connection:
            admitted = connection.execute(
                """
                SELECT state, claim_token, claimed_until, owner_fence, request_ref
                FROM ingress_tasks WHERE request_ref = ?
                """,
                (request.request_id,),
            ).fetchone()
        assert admitted is not None
        assert admitted[0] == IngressState.PROCESSING.value
        assert isinstance(admitted[1], str) and len(admitted[1]) == 32
        assert admitted[2] is not None
        assert admitted[3] > 0
        assert admitted[4] == request.request_id
        observed.append(request)
        return Entry(
            title=f"{request.kind.value}-title",
            source_type="text",
            content=f"parsed:{request.source}",
        )

    lifecycle = R4IngressLifecycle(app, preparer=fake_preparer)
    requests = (
        IngressRequest.create(IngressKind.URL, "https://example.invalid/article"),
        IngressRequest.create(IngressKind.TEXT, "literal source"),
        IngressRequest.create(IngressKind.FILE, "C:/authorized/note.md"),
    )

    results = [asyncio.run(lifecycle.submit_and_drain(request)) for request in requests]

    assert [request.kind for request in observed] == [
        IngressKind.URL,
        IngressKind.TEXT,
        IngressKind.FILE,
    ]
    assert all(result.task.state is IngressState.SUBMITTED for result in results)
    assert all(result.core_committed for result in results)
    with sqlite3.connect(config.db_path) as connection:
        rows = [
            connection.execute(
                """
                SELECT request_kind, request_ref, request_sha256, state
                FROM ingress_tasks WHERE request_ref = ?
                """,
                (request.request_id,),
            ).fetchone()
            for request in requests
        ]
        ledger_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(ingress_tasks)").fetchall()
        }
        raw_source_matches = connection.execute(
            "SELECT COUNT(*) FROM ingress_tasks WHERE request_ref = ?",
            ("literal source",),
        ).fetchone()[0]
    assert [row[0] for row in rows] == ["url", "text", "file"]
    assert all(len(row[2]) == 64 and row[3] == "submitted" for row in rows)
    assert "source" not in ledger_columns
    assert raw_source_matches == 0
    for request, result in zip(requests, results):
        assert not (
            config.layout.runtime_state_dir / "r4" / "ingress" / f"{request.request_id}.json"
        ).exists()
        assert result.q1_result is not None
        prepared_id = result.q1_result.task.prepared_ref
        assert prepared_id is not None
        assert not (
            config.layout.runtime_state_dir / "r4" / "prepared" / f"{prepared_id}.json"
        ).exists()


def test_busy_q0_returns_before_fake_preparer_or_spool_side_effect(tmp_path: Path) -> None:
    config = _configured(tmp_path)
    app = KnowledgeApplication(config)
    calls: list[str] = []

    async def fake_preparer(_: IngressRequest) -> Entry:
        calls.append("preparer")
        return Entry(title="unexpected", source_type="text", content="unexpected")

    lifecycle = R4IngressLifecycle(app, preparer=fake_preparer)
    request = IngressRequest.create(IngressKind.URL, "https://example.invalid/busy")

    with write_lease_scope(config.layout):
        counts_before = _r4_row_counts(config.db_path)
        trees_before = {
            "runtime": _tree_snapshot(config.layout.runtime_state_dir),
            "vault": _tree_snapshot(config.vault_dir),
            "vectors": _tree_snapshot(config.vector_index_dir),
            "logs": _tree_snapshot(config.log_dir),
            "tmp": _tree_snapshot(config.tmp_dir),
        }
        with pytest.raises(PKVRuntimeError) as error:
            asyncio.run(lifecycle.submit_and_drain(request))
        counts_after = _r4_row_counts(config.db_path)
        trees_after = {
            "runtime": _tree_snapshot(config.layout.runtime_state_dir),
            "vault": _tree_snapshot(config.vault_dir),
            "vectors": _tree_snapshot(config.vector_index_dir),
            "logs": _tree_snapshot(config.log_dir),
            "tmp": _tree_snapshot(config.tmp_dir),
        }

    assert error.value.code is ErrorCode.WRITE_BUSY
    assert calls == []
    assert counts_after == counts_before
    assert trees_after == trees_before
    assert not (config.layout.runtime_state_dir / "r4" / "ingress").exists()


def test_path_shaped_text_uses_production_text_preparer_without_reading_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _configured(tmp_path)
    app = KnowledgeApplication(config)
    sensitive_path = tmp_path / "must-not-read.txt"
    sensitive_path.write_text("PRIVATE-FILE-CANARY", encoding="utf-8")
    original_read_text = Path.read_text

    def guarded_read_text(path: Path, *args, **kwargs):
        if path == sensitive_path:
            raise AssertionError("path-shaped literal text must not be read as a file")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded_read_text)
    request = IngressRequest.create(IngressKind.TEXT, str(sensitive_path))

    result = asyncio.run(R4IngressLifecycle(app).submit_and_drain(request))

    assert result.core_committed
    with sqlite3.connect(config.db_path) as connection:
        stored = connection.execute(
            "SELECT content FROM knowledge_items"
        ).fetchone()
    assert stored == (str(sensitive_path),)


def test_q0_prepare_failure_is_retryable_without_submitting_q1(tmp_path: Path) -> None:
    config = _configured(tmp_path)
    app = KnowledgeApplication(config)

    async def fake_preparer(_: IngressRequest) -> Entry:
        raise PKVRuntimeError(
            ErrorCode.WORKFLOW_STEP_FAILED,
            "fake crawler unavailable",
            stage="fake_crawler",
            recoverable=True,
        )

    lifecycle = R4IngressLifecycle(app, preparer=fake_preparer)
    result = asyncio.run(
        lifecycle.submit_and_drain(
            IngressRequest.create(IngressKind.URL, "https://example.invalid/retry")
        )
    )

    assert result.error is not None
    assert result.task.state is IngressState.RETRY_REQUIRED
    with sqlite3.connect(config.db_path) as connection:
        q1_count = connection.execute(
            "SELECT COUNT(*) FROM content_mutation_tasks"
        ).fetchone()[0]
    assert q1_count == 0


def test_q0_tampered_request_spool_fails_closed_before_preparer_or_q1(
    tmp_path: Path,
) -> None:
    config = _configured(tmp_path)
    app = KnowledgeApplication(config)
    calls: list[str] = []

    async def fake_preparer(_: IngressRequest) -> Entry:
        calls.append("preparer")
        return Entry(title="unexpected", source_type="text", content="unexpected")

    lifecycle = R4IngressLifecycle(app, preparer=fake_preparer)
    request = IngressRequest.create(IngressKind.TEXT, "private request body")
    with write_lease_scope(config.layout):
        task = lifecycle.admit(request)
    spool_path = (
        config.layout.runtime_state_dir / "r4" / "ingress" / f"{request.request_id}.json"
    )
    spool_path.write_bytes(spool_path.read_bytes() + b"tampered")

    result = asyncio.run(lifecycle.drain_task(task.task_id))

    assert result.error is not None
    assert result.error.code is ErrorCode.REPAIR_REQUIRED
    assert result.task.state is IngressState.RETRY_REQUIRED
    assert calls == []
    assert spool_path.exists()
    with sqlite3.connect(config.db_path) as connection:
        q1_count = connection.execute(
            "SELECT COUNT(*) FROM content_mutation_tasks"
        ).fetchone()[0]
    assert q1_count == 0


def test_cancelled_q0_preparer_recovers_once_after_claim_expiry_and_reload(
    tmp_path: Path,
) -> None:
    config = _configured(tmp_path)
    app = KnowledgeApplication(config)
    started = asyncio.Event()
    calls: list[str] = []

    async def cancellable_preparer(request: IngressRequest) -> Entry:
        calls.append(request.request_id)
        if len(calls) == 1:
            started.set()
            await asyncio.Event().wait()
        return Entry(title="recovered", source_type="text", content="recovered once")

    lifecycle = R4IngressLifecycle(app, preparer=cancellable_preparer)
    request = IngressRequest.create(IngressKind.TEXT, "cancelled body")

    async def cancel_first_worker() -> None:
        worker = asyncio.create_task(lifecycle.submit_and_drain(request))
        await started.wait()
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker

    asyncio.run(cancel_first_worker())
    interrupted = lifecycle.store.list_recoverable()[0]
    assert interrupted.state is IngressState.PROCESSING
    with sqlite3.connect(config.db_path) as connection:
        connection.execute(
            """
            UPDATE ingress_tasks SET claimed_until = datetime('now', '-1 second')
            WHERE request_ref = ?
            """,
            (request.request_id,),
        )
        connection.commit()

    reloaded_config = Config(layout=config.layout)
    bootstrap_runtime(reloaded_config)
    recovered = R4IngressLifecycle(
        KnowledgeApplication(reloaded_config),
        preparer=cancellable_preparer,
    )
    results = asyncio.run(recovered.recover_and_drain())

    assert calls == [request.request_id, request.request_id]
    assert any(result.core_committed for result in results)
    with sqlite3.connect(reloaded_config.db_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM knowledge_items"
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT state FROM ingress_tasks WHERE request_ref = ?",
            (request.request_id,),
        ).fetchone() == (IngressState.SUBMITTED.value,)


def test_q0_targeted_drain_never_claims_a_different_pending_request(tmp_path: Path) -> None:
    config = _configured(tmp_path)
    app = KnowledgeApplication(config)
    observed: list[str] = []

    async def fake_preparer(request: IngressRequest) -> Entry:
        observed.append(request.source)
        return Entry(title=request.source, source_type="text", content=request.source)

    lifecycle = R4IngressLifecycle(app, preparer=fake_preparer)
    first_request = IngressRequest.create(IngressKind.URL, "https://example.invalid/first")
    second_request = IngressRequest.create(IngressKind.URL, "https://example.invalid/second")
    with write_lease_scope(config.layout):
        first = lifecycle.admit(first_request)
        second = lifecycle.admit(second_request)

    result = asyncio.run(lifecycle.drain_task(second.task_id))

    assert result.core_committed
    assert observed == [second_request.source]
    assert lifecycle.store.get_task(first.task_id).state is IngressState.ACCEPTED
    assert lifecycle.store.get_task(second.task_id).state is IngressState.SUBMITTED


def test_q0_expired_claim_is_recovered_before_restarting_fake_preparer(tmp_path: Path) -> None:
    config = _configured(tmp_path)
    app = KnowledgeApplication(config)
    calls: list[str] = []

    async def fake_preparer(request: IngressRequest) -> Entry:
        calls.append(request.request_id)
        return Entry(title="recovered", source_type="text", content="recovered")

    lifecycle = R4IngressLifecycle(app, preparer=fake_preparer)
    request = IngressRequest.create(IngressKind.URL, "https://example.invalid/recover")
    with write_lease_scope(config.layout):
        accepted = lifecycle.admit(request)
        claim = lifecycle.store.claim_task(accepted.task_id)
    assert claim is not None and claim.state is IngressState.PROCESSING
    with sqlite3.connect(config.db_path) as connection:
        connection.execute(
            """
            UPDATE ingress_tasks
            SET claimed_until = datetime('now', '-1 second')
            WHERE task_id = ?
            """,
            (accepted.task_id,),
        )
        connection.commit()

    reloaded_config = Config(layout=config.layout)
    bootstrap_runtime(reloaded_config)
    reloaded = R4IngressLifecycle(
        KnowledgeApplication(reloaded_config),
        preparer=fake_preparer,
    )
    results = asyncio.run(reloaded.recover_and_drain())

    assert calls == [request.request_id]
    assert any(
        result.task.task_id == accepted.task_id
        and result.task.state is IngressState.SUBMITTED
        and result.core_committed
        for result in results
    )


def test_q0_submitted_q1_crash_recovers_on_next_internal_trigger(tmp_path: Path) -> None:
    config = _configured(tmp_path)
    app = KnowledgeApplication(config)
    fired = False

    def fail_once(phase: str, _task) -> None:
        nonlocal fired
        if phase == "binding_published" and not fired:
            fired = True
            raise R4LifecycleFault(phase)

    async def fake_preparer(_: IngressRequest) -> Entry:
        return Entry(title="crash ingress", source_type="text", content="body")

    crashing = R4IngressLifecycle(
        app,
        preparer=fake_preparer,
        q1_lifecycle=R4ContentLifecycle(app, fault_hook=fail_once),
    )
    with pytest.raises(R4LifecycleFault, match="binding_published"):
        asyncio.run(
            crashing.submit_and_drain(
                IngressRequest.create(IngressKind.TEXT, "crash input")
            )
        )

    reloaded_config = Config(layout=config.layout)
    bootstrap_runtime(reloaded_config)
    recovering = R4IngressLifecycle(
        KnowledgeApplication(reloaded_config),
        preparer=fake_preparer,
    )
    asyncio.run(recovering.recover_and_drain())
    with sqlite3.connect(config.db_path) as connection:
        row = connection.execute(
            "SELECT state FROM content_mutation_tasks"
        ).fetchone()
        ingress = connection.execute("SELECT state FROM ingress_tasks").fetchone()
    assert row == (ContentMutationState.COMPLETED.value,)
    assert ingress == (IngressState.SUBMITTED.value,)
    submitted = recovering.store.list_submitted()
    assert len(submitted) == 1
    assert not (
        reloaded_config.layout.runtime_state_dir
        / "r4"
        / "ingress"
        / submitted[0].task_id
    ).exists()


def test_q0_task_assets_are_fence_scoped_and_released_after_core_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Slow Q0 work gets only a task-local asset grant, not shared ``tmp/``."""

    config = _configured(tmp_path)
    app = KnowledgeApplication(config)
    grants = []
    observed_relative_paths: list[str] = []
    original_prepare_attempt = IngressTaskSpool.prepare_attempt

    def capture_attempt(self: IngressTaskSpool, task):
        grant = original_prepare_attempt(self, task)
        grants.append(grant)
        return grant

    monkeypatch.setattr(IngressTaskSpool, "prepare_attempt", capture_attempt)

    async def preparer(_: IngressRequest) -> Entry:
        assert not has_active_write_lease(config.layout)
        assert len(grants) == 1
        with pytest.raises(ValueError, match="只允许临时图片"):
            grants[0].write_image_asset("original-page.html", b"must-stay-in-memory")
        asset = grants[0].write_image_asset("wechat_article.png", b"temporary-image")
        observed_relative_paths.append(
            asset.relative_to(config.layout.runtime_state_dir).as_posix()
        )
        assert asset.read_bytes() == b"temporary-image"
        return Entry(title="task scoped", source_type="text", content="committed body")

    request = IngressRequest.create(IngressKind.TEXT, "task-local source")
    result = asyncio.run(R4IngressLifecycle(app, preparer=preparer).submit_and_drain(request))

    assert result.core_committed
    assert len(grants) == 1
    assert observed_relative_paths == [
        f"r4/ingress/{result.task.task_id}/1/assets/wechat_article.png"
    ]
    assert not (
        config.layout.runtime_state_dir / "r4" / "ingress" / result.task.task_id
    ).exists()
    assert list(config.tmp_dir.glob("wechat_*")) == []
    assert not (
        config.layout.runtime_state_dir
        / "r4"
        / "ingress"
        / f"{request.request_id}.json"
    ).exists()


def test_q0_task_asset_grant_fails_closed_after_claim_expiry(tmp_path: Path) -> None:
    """An expired worker cannot publish into either its old or a future attempt."""

    config = _configured(tmp_path)
    lifecycle = R4IngressLifecycle(KnowledgeApplication(config))
    request = IngressRequest.create(IngressKind.TEXT, "stale asset source")
    with write_lease_scope(config.layout):
        task = lifecycle.admit(request)
        claimed = lifecycle.store.claim_task(task.task_id)
        assert claimed is not None
        grant = lifecycle.task_spool.prepare_attempt(claimed)

    with sqlite3.connect(config.db_path) as connection:
        connection.execute(
            "UPDATE ingress_tasks SET claimed_until = datetime('now', '-1 second') "
            "WHERE task_id = ?",
            (task.task_id,),
        )
        connection.commit()

    with pytest.raises(PKVRuntimeError) as exc_info:
        grant.write_image_asset("late.png", b"must-not-publish")

    assert exc_info.value.code is ErrorCode.RUNTIME_PLAN_STALE
    assets = (
        config.layout.runtime_state_dir
        / "r4"
        / "ingress"
        / task.task_id
        / str(claimed.owner_fence)
        / "assets"
    )
    assert list(assets.iterdir()) == []


def test_q0_retry_reclaims_prior_fence_assets_before_task_cleanup(
    tmp_path: Path,
) -> None:
    """Repeated expired claims never accumulate enough assets to retain Q0 space."""

    config = _configured(tmp_path)
    lifecycle = R4IngressLifecycle(KnowledgeApplication(config))
    request = IngressRequest.create(IngressKind.TEXT, "retrying task assets")
    with write_lease_scope(config.layout):
        task = lifecycle.admit(request)

    task_root = config.layout.runtime_state_dir / "r4" / "ingress" / task.task_id
    for expected_fence in range(1, 10):
        with write_lease_scope(config.layout):
            if expected_fence > 1:
                assert lifecycle.store.recover_expired_claims() == 1
            claimed = lifecycle.store.claim_task(task.task_id)
            assert claimed is not None
            assert claimed.owner_fence == expected_fence
            grant = lifecycle.task_spool.prepare_attempt(claimed)

        grant.write_image_asset(
            f"attempt-{expected_fence}.png",
            b"temporary-image",
        )
        assert sorted(path.name for path in task_root.iterdir()) == [
            str(expected_fence)
        ]

        if expected_fence < 9:
            with sqlite3.connect(config.db_path) as connection:
                connection.execute(
                    "UPDATE ingress_tasks SET claimed_until = datetime('now', '-1 second') "
                    "WHERE task_id = ?",
                    (task.task_id,),
                )
                connection.commit()

    with write_lease_scope(config.layout):
        assert lifecycle.task_spool.discard_task(task.task_id)
    assert not task_root.exists()
