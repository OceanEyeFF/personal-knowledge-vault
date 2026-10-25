"""W1 red contracts for R-01/R-02/R-06; synthetic roots and no live Provider.

These tests exercise existing production entry points and accounting seams. They
must fail on the observed behavior, not on importing an unimplemented v2 API.
ER case IDs refer to the R4-P1 repair acceptance matrix. Public v2 process tests
will supplement these unit oracles after that surface is implemented.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from threading import Event, Thread

import numpy as np
import pytest

from pkv_kernel import embedding
from src.runtime.ai_automation_policy import TokenUsage
from src.runtime.embedding_rebuild_ledger import (
    EmbeddingRebuildLedger,
    EmbeddingRebuildLedgerState,
)
from src.runtime.embedding_rebuild_policy import inspect_embedding_rebuild_policy
from src.runtime.errors import ErrorCode, PKVRuntimeError
from src.runtime.runtime_snapshot import RuntimeSnapshotStore
from src.runtime.write_lease import write_lease_scope
from src.storage.markdown_store import Entry, MarkdownStore
from src.storage.sqlite_store import SQLiteStore
from tests.unit.test_embedding_rebuild_ledger import _configured
from tests.unit.test_kernel_embedding_rebuild import _seed_r2


def test_r01_er01_public_plan_confirmation_identity_is_bound_to_data_root(
    tmp_path: Path,
) -> None:
    """Identical safe material in two different roots cannot share confirmation."""

    first = _configured(tmp_path / "first")
    second = _configured(tmp_path / "second")
    _seed_r2(first)
    _seed_r2(second)
    assert first.data_root_identity != second.data_root_identity

    first_plan = embedding.plan_embedding_rebuild(
        embedding.inspect_embedding_rebuild(first)
    )
    second_plan = embedding.plan_embedding_rebuild(
        embedding.inspect_embedding_rebuild(second)
    )
    # Both public adapters rebuild a current-root plan before comparing this ID;
    # no later Core confirmation can recover the identity of the original root.
    assert first_plan.plan_id != second_plan.plan_id, (
        "R-01/ER-01: distinct data roots accept the same public confirmation ID"
    )


def test_r01_er02_unchanged_same_root_reinspection_keeps_public_plan_identity(
    tmp_path: Path,
) -> None:
    """Green control: identity binding must retain same-root determinism."""

    config = _configured(tmp_path)
    _seed_r2(config)
    first = embedding.plan_embedding_rebuild(embedding.inspect_embedding_rebuild(config))
    second = embedding.plan_embedding_rebuild(embedding.inspect_embedding_rebuild(config))

    assert first.plan_id == second.plan_id


def test_r02_er04_completed_stage_contention_exposes_publish_only_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real competing writer at D needs a recoverable operation projection.

    The current API is executed to completion/failure; no missing v2 function is
    imported. The assertion freezes the W1 pending-error semantics. W4 must add
    lookup/publish-plan/execute process tests for zero-Provider continuation,
    rather than treating a replay of v1 execute as the intended recovery API.
    """

    config = _configured(tmp_path)
    _seed_r2(config)
    with write_lease_scope(config.layout):
        markdown = MarkdownStore(config.layout.vault_dir)
        entry = Entry(
            title="W1 isolated rebuild",
            source_type="text",
            content="synthetic rebuild source",
        )
        write_plan = markdown.plan_save(entry)
        markdown.save_planned(write_plan, entry)
        SQLiteStore(config.layout.db_path).insert_entry_with_chunks(
            entry,
            write_plan.relative_path,
            ["synthetic stored chunk"],
        )
    plan = embedding.plan_embedding_rebuild(embedding.inspect_embedding_rebuild(config))
    snapshot_before = RuntimeSnapshotStore(config.layout).read()
    ledger = EmbeddingRebuildLedger(config.layout)
    acquired = Event()
    release = Event()
    contender_errors: list[BaseException] = []
    ledger_at_contention: list[bytes] = []

    def hold_competing_writer() -> None:
        try:
            with write_lease_scope(config.layout):
                ledger_at_contention.append(ledger.path.read_bytes())
                acquired.set()
                if not release.wait(timeout=15):
                    raise AssertionError("test owner did not release the contender")
        except BaseException as error:
            contender_errors.append(error)
            acquired.set()

    contender = Thread(target=hold_competing_writer, daemon=True)

    class CountingProvider:
        """Pure vector fixture through the existing public factory seam."""

        def __init__(self) -> None:
            self.client = self
            self.last_usage: TokenUsage | None = None
            self.document_calls = 0
            self.chunk_calls = 0

        def embed_document(self, text: str) -> np.ndarray:
            assert text == "synthetic rebuild source"
            self.document_calls += 1
            self.last_usage = TokenUsage(
                embedding_input_tokens=7,
                source="provider_reported",
            )
            return np.asarray([1.0, 2.0, 3.0], dtype=np.float32)

        def embed_batch_numpy(self, chunks: list[str]) -> np.ndarray:
            assert chunks == ["synthetic stored chunk"]
            self.chunk_calls += 1
            self.last_usage = TokenUsage(
                embedding_input_tokens=11,
                source="provider_reported",
            )
            contender.start()
            assert acquired.wait(timeout=10), "contender did not acquire the root lease"
            assert not contender_errors
            return np.asarray([[1.0, 2.0, 3.0]], dtype=np.float32)

    provider = CountingProvider()
    monkeypatch.setattr(embedding, "create_embedder", lambda _: provider)
    try:
        with pytest.raises(PKVRuntimeError) as rejected:
            embedding.execute_embedding_rebuild(
                plan,
                embedding.confirm_embedding_rebuild(
                    plan,
                    approved=True,
                    allow_network=True,
                ),
            )
        # While the other owner still holds the actual lease, failure handling
        # must not publish a state change through an unleased shared-ledger write.
        assert ledger_at_contention
        assert ledger.path.read_bytes() == ledger_at_contention[0]
    finally:
        release.set()
        if contender.ident is not None:
            contender.join(timeout=10)

    assert not contender.is_alive()
    assert not contender_errors
    assert (provider.document_calls, provider.chunk_calls) == (1, 1)
    assert RuntimeSnapshotStore(config.layout).read() == snapshot_before
    manifests = list(config.layout.vector_index_dir.glob("staging/*/generation-manifest.json"))
    assert len(manifests) == 1, "D must be reached with a complete, private stage"
    manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
    assert len(manifest["files"]) == 4
    assert all((manifests[0].parent / name).is_file() for name in manifest["files"])
    operations = ledger.read_operations()
    assert len(operations) == 1
    assert operations[0].reservation is not None
    assert operations[0].reservation.state == "reserved"

    payload = rejected.value.to_dict()
    observable = {"code": payload.get("code"), "details": payload.get("details")}
    assert observable == {
        "code": "embedding_publication_pending",
        "details": {
            "operation_id": operations[0].operation_id,
            "effective_state": "awaiting_publish",
            "provider_effect": "completed",
            "publish_only_required": True,
        },
    }, (
        "R-02/ER-04: completed paid work is reported as generic write_busy with "
        "no operation identity or publish-only continuation semantics"
    )


@pytest.mark.parametrize(
    ("daily_cap", "monthly_cap", "next_period"),
    [
        pytest.param(10, 100, datetime(2026, 9, 5, tzinfo=timezone.utc), id="day"),
        pytest.param(100, 10, datetime(2026, 10, 1, tzinfo=timezone.utc), id="month"),
    ],
)
def test_r06_er07_budget_rejection_can_be_reauthorized_after_period_recovery(
    tmp_path: Path,
    daily_cap: int,
    monthly_cap: int,
    next_period: datetime,
) -> None:
    """A no-Provider quota refusal cannot permanently consume its replay key."""

    config = _configured(
        tmp_path,
        daily_total_tokens=daily_cap,
        monthly_total_tokens=monthly_cap,
    )
    policy = inspect_embedding_rebuild_policy(config)
    now = [datetime(2026, 9, 4, tzinfo=timezone.utc)]
    ledger = EmbeddingRebuildLedger(config.layout, clock=lambda: now[0])
    common = {
        "source_revision": "b" * 64,
        "policy": policy,
        "estimated_tokens": 6,
    }
    with write_lease_scope(config.layout):
        # This is a ledger-owned accounting fixture, not a real Provider call.
        spent = ledger.begin(
            plan_id="1" * 64,
            public_plan_id="2" * 64,
            replay_key="3" * 64,
            **common,
        )
        ledger.mark_activation_intent(spent.operation_id, generation_id="g-" + "a" * 20)
        ledger.mark_completed(spent.operation_id, provider_attempted=True)
        with pytest.raises(PKVRuntimeError) as exhausted:
            ledger.begin(
                plan_id="4" * 64,
                public_plan_id="5" * 64,
                replay_key="6" * 64,
                **common,
            )

    assert exhausted.value.code is ErrorCode.EMBEDDING_BUDGET_PAUSED
    paused = [
        operation
        for operation in ledger.read_operations()
        if operation.state is EmbeddingRebuildLedgerState.BUDGET_PAUSED
    ]
    assert len(paused) == 1
    assert paused[0].reservation is None
    assert paused[0].provider_attempted is False

    now[0] = next_period
    # Recomputed confirmation IDs may change with budget_check_period, but the
    # material replay key stays fixed so midnight cannot bypass active work.
    with write_lease_scope(config.layout):
        try:
            retried = ledger.begin(
                plan_id="7" * 64,
                public_plan_id="8" * 64,
                replay_key="6" * 64,
                **common,
            )
        except PKVRuntimeError as error:
            pytest.fail(
                "R-06/ER-07: quota recovered with fresh explicit confirmation, "
                f"but the no-Provider rejection still blocks admission: {error.code.value}"
            )

    assert retried.operation_id != paused[0].operation_id
    assert retried.state is EmbeddingRebuildLedgerState.PROCESSING
    assert retried.reservation is not None
    assert retried.reservation.local_day == next_period.date().isoformat()
    assert retried.reservation.local_month == next_period.strftime("%Y-%m")
    assert retried.reservation.reserved_tokens == 6


def test_r06_er08_period_change_does_not_replay_an_active_operation(
    tmp_path: Path,
) -> None:
    """Green control: changing period/plan IDs cannot duplicate active work."""

    config = _configured(tmp_path, daily_total_tokens=10, monthly_total_tokens=100)
    policy = inspect_embedding_rebuild_policy(config)
    now = [datetime(2026, 9, 4, tzinfo=timezone.utc)]
    ledger = EmbeddingRebuildLedger(config.layout, clock=lambda: now[0])
    common = {
        "source_revision": "b" * 64,
        "policy": policy,
        "estimated_tokens": 6,
        "replay_key": "6" * 64,
    }
    with write_lease_scope(config.layout):
        active = ledger.begin(plan_id="1" * 64, public_plan_id="2" * 64, **common)
    now[0] = datetime(2026, 9, 5, tzinfo=timezone.utc)

    with write_lease_scope(config.layout), pytest.raises(PKVRuntimeError) as busy:
        ledger.begin(plan_id="3" * 64, public_plan_id="4" * 64, **common)

    assert busy.value.code is ErrorCode.WRITE_BUSY
    assert ledger.read_operations() == (active,)
