"""MIG-08: publish a legally sealed generation after its old grant expires."""

from pathlib import Path

import pytest

from pkv_kernel import embedding
from src.runtime import embedding_rebuild_v2 as runtime_v2
from src.runtime import generation_stage
from src.runtime.embedding_rebuild_ledger import EmbeddingRebuildLedger
from src.runtime.errors import ErrorCode, PKVRuntimeError
from src.runtime.generation_stage import GenerationStageGrant
from tests.unit.test_w4_embedding_publication import _config, _pending


def test_public_publication_adopts_expired_legitimate_seal_without_renewing_grant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    # This clock belongs exclusively to the narrow stage grant. The global,
    # writer-lease and budget clocks keep their normal production behavior.
    grant_clock = [1_800_000_000_000]
    monkeypatch.setattr(generation_stage, "_now_millis", lambda: grant_clock[0])
    captured_grants = []
    original_allocate = GenerationStageGrant.allocate

    def capture_grant(cls, *args, **kwargs):
        grant = original_allocate(*args, **kwargs)
        captured_grants.append(grant)
        return grant

    monkeypatch.setattr(GenerationStageGrant, "allocate", classmethod(capture_grant))
    operation_id = _pending(config, monkeypatch)
    assert len(captured_grants) == 1
    old_grant = captured_grants[0]
    ledger = EmbeddingRebuildLedger(config.layout)
    before = ledger.get_operation(operation_id)
    assert before.reservation is not None
    assert before.reservation.state == "reserved"
    reservation_id = before.reservation.reservation_id
    old_expiry = old_grant.expires_at_ms
    sealed_files = {path.name: path.read_bytes() for path in old_grant.stage_dir.iterdir()}
    assert ".publish-ready.json" in sealed_files

    grant_clock[0] = old_expiry + 1
    with pytest.raises(PKVRuntimeError) as rejected:
        old_grant.require_stage_write(config.layout, old_grant.stage_dir)
    assert rejected.value.code is ErrorCode.RUNTIME_PLAN_STALE

    def forbidden_provider(*args, **kwargs):
        pytest.fail("publish-only constructed another Provider after grant expiry")

    monkeypatch.setattr(runtime_v2, "create_embedder", forbidden_provider)
    ledger_bytes = ledger.path.read_bytes()
    inspection = embedding.inspect_embedding_operation_v2(operation_id, config=config)
    assert inspection.to_dict()["effective_state"] == "awaiting_publish"
    assert inspection.to_dict()["proof_valid"] is True
    plan = embedding.plan_embedding_publication_v2(inspection)
    assert plan.to_dict()["action"] == "publish_only"
    assert plan.to_dict()["creates_reservation"] is False
    assert ledger.path.read_bytes() == ledger_bytes
    result = embedding.execute_embedding_publication_v2(
        plan, embedding.confirm_embedding_publication_v2(plan),
    ).to_dict()
    assert result["status"] == "ready"
    assert result["publication_committed"] is True
    assert result["provider_called"] is False
    assert not result["ledger_completion_pending"]
    assert not result["audit_completion_pending"]

    after = ledger.get_operation(operation_id)
    assert len(ledger.read_operations()) == 1
    assert after.reservation.reservation_id == reservation_id
    assert after.reservation.state == "settled"
    assert after.reservation.reserved_tokens == before.reservation.reserved_tokens
    assert after.reservation.local_day == before.reservation.local_day
    assert after.reservation.local_month == before.reservation.local_month
    assert after.reservation.settled_tokens == before.reservation.reserved_tokens
    assert len(captured_grants) == 1
    assert old_grant.expires_at_ms == old_expiry
    assert {path.name: path.read_bytes() for path in old_grant.stage_dir.iterdir()} == sealed_files
    with pytest.raises(PKVRuntimeError) as rejected:
        old_grant.require_stage_write(config.layout, old_grant.stage_dir)
    assert rejected.value.code is ErrorCode.RUNTIME_PLAN_STALE
