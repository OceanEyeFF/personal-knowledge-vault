"""Legacy and retained-generation recovery use durable publication facts."""

import json
from pathlib import Path

import pytest

from src.runtime import embedding_lifecycle as core
from src.runtime import embedding_rebuild_v2 as v2
from src.runtime.audit import AuditTrace, AuditTraceError
from src.runtime.embedding_rebuild_ledger import EmbeddingRebuildLedger
from src.runtime.errors import ErrorCode, PKVRuntimeError
from src.runtime.runtime_snapshot import RuntimeSnapshotStore
from tests.unit.test_embedding_lifecycle import _FakeEmbedder
from tests.unit.test_w4_embedding_publication import _config, _execute, _plan, _pending


_LEGACY_FIELDS = {
    "operation_id", "plan_id", "source_revision", "policy_fingerprint", "state",
    "reservation", "local_estimate", "provider_usage", "generation_id", "last_error_code",
}


def _rewrite_legacy(config, schema):
    ledger = EmbeddingRebuildLedger(config.layout)
    raw = json.loads(ledger.path.read_text(encoding="utf-8"))
    fields = _LEGACY_FIELDS | ({"public_plan_id", "replay_key"} if schema == 2 else set())
    records = {}
    for operation_id, value in raw["operations"].items():
        records[operation_id] = {name: value[name] for name in fields}
        records[operation_id]["provider_attempted"] = value["provider_effect"] != "not_started"
    ledger.path.write_text(json.dumps({"schema_version": schema, "operations": records}), encoding="utf-8")
    return ledger


@pytest.mark.parametrize("schema", [1, 2])
def test_legacy_completed_reconcile_explicitly_upgrades_without_provider_or_cas(tmp_path: Path, monkeypatch, schema):
    config = _config(tmp_path)
    original_append = AuditTrace.append
    def fail_completion(self, event):
        if event.get("phase") == "completed":
            raise AuditTraceError()
        return original_append(self, event)
    with monkeypatch.context() as patch:
        patch.setattr(AuditTrace, "append", fail_completion)
        result = _execute(_plan(config)).to_dict()
    ledger = _rewrite_legacy(config, schema)
    before = ledger.path.read_bytes()
    inspection = v2.inspect_operation(result["operation_id"], config=config)
    status = inspection.to_dict()
    assert status["publication_committed"] and status["audit_completion_pending"]
    assert not status["proof_valid"] and status["proof_sha256"] is None
    if schema == 1:
        assert status["original_plan_id"] is None
    publication = v2.plan_publication(inspection)
    assert publication.to_dict()["action"] == "reconcile_completion"
    assert publication.to_dict()["ledger_upgrade"]["from_schema"] == schema
    assert ledger.path.read_bytes() == before
    pointer = RuntimeSnapshotStore(config.layout).read()
    monkeypatch.setattr(core, "_pointer_update", lambda *a, **k: pytest.fail("legacy reconcile repeated CAS"))
    monkeypatch.setattr(v2, "create_embedder", lambda *a, **k: pytest.fail("legacy reconcile called Provider"))
    completed = v2.execute_publication(publication).to_dict()
    assert not completed["provider_called"]
    assert not completed["ledger_completion_pending"] and not completed["audit_completion_pending"]
    assert ledger.inspect().schema_version == 3
    assert RuntimeSnapshotStore(config.layout).read() == pointer


@pytest.mark.parametrize("schema", [1, 2])
def test_legacy_processing_cannot_adopt_an_unbound_existing_stage(tmp_path: Path, monkeypatch, schema):
    config = _config(tmp_path)
    operation_id = _pending(config, monkeypatch)
    ledger = _rewrite_legacy(config, schema)
    before = ledger.path.read_bytes()
    status = v2.inspect_operation(operation_id, config=config)
    assert status.to_dict()["effective_state"] == "repair_required"
    assert not status.to_dict()["proof_valid"]
    with pytest.raises(PKVRuntimeError) as rejected:
        v2.plan_publication(status)
    assert rejected.value.code is ErrorCode.REPAIR_REQUIRED
    assert ledger.path.read_bytes() == before


def test_retained_completed_generation_reconciles_accounting_without_reactivating(tmp_path: Path, monkeypatch):
    config = _config(tmp_path)
    with monkeypatch.context() as patch:
        patch.setattr(EmbeddingRebuildLedger, "mark_completed", lambda *a, **k: (_ for _ in ()).throw(OSError("settlement response lost")))
        first = _execute(_plan(config)).to_dict()
    assert first["ledger_completion_pending"]
    # Independent internal lifecycle work can legitimately replace the pointer
    # while this foreground operation's accounting still needs reconciliation.
    inspection = core.inspect_embedding_index(config, source=core.SQLiteEmbeddingSource())
    plan = core.plan_embedding_rebuild(inspection, force=True)
    second = core.execute_embedding_rebuild(
        plan, core.confirm_embedding_rebuild(plan, allow_network=True), embedder=_FakeEmbedder(),
    )
    assert second.generation_id != first["generation_id"]
    pointer = RuntimeSnapshotStore(config.layout).read()
    old = v2.inspect_operation(first["operation_id"], config=config)
    assert old.to_dict()["publication_committed"]
    assert old.to_dict()["ledger_completion_pending"]
    publication = v2.plan_publication(old)
    assert publication.to_dict()["action"] == "reconcile_completion"
    monkeypatch.setattr(core, "_pointer_update", lambda *a, **k: pytest.fail("retained reconcile reactivated old generation"))
    monkeypatch.setattr(v2, "create_embedder", lambda *a, **k: pytest.fail("retained reconcile called Provider"))
    completed = v2.execute_publication(publication).to_dict()
    assert completed["generation_id"] == first["generation_id"]
    assert completed["previous_generation_id"] != first["generation_id"]
    assert not completed["ledger_completion_pending"] and not completed["audit_completion_pending"]
    assert RuntimeSnapshotStore(config.layout).read() == pointer
