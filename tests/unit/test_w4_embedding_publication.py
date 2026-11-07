"""W4 manual publication recovery with durable synthetic stage and audit facts."""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.runtime import embedding_lifecycle as core
from src.runtime import embedding_rebuild_v2 as v2
from src.runtime.audit import AuditTrace, AuditTraceError
from src.runtime.embedding_rebuild_ledger import EmbeddingRebuildLedger
from src.runtime.errors import ErrorCode, PKVRuntimeError
from src.runtime.write_lease import write_lease_scope
from tests.unit.test_embedding_rebuild_ledger import _configured
from tests.unit.test_kernel_embedding_rebuild import _seed_r2


class _NoDocumentsProvider:
    def __init__(self):
        self.client = self

    def embed_document(self, text):
        pytest.fail("empty source called Provider")

    def embed_batch_numpy(self, chunks):
        pytest.fail("empty source called Provider")


def _config(tmp_path: Path):
    config = _configured(tmp_path)
    _seed_r2(config)
    return config


def _plan(config):
    return v2.plan_rebuild(v2.inspect_rebuild(config))


def _execute(plan, provider_factory=_NoDocumentsProvider):
    return v2.execute_rebuild(plan, embedder_factory=provider_factory)


def _pending(config, monkeypatch, provider_factory=_NoDocumentsProvider):
    count = 0
    @contextmanager
    def leases(layout, **kwargs):
        nonlocal count
        count += 1
        if count == 2:
            raise PKVRuntimeError(ErrorCode.WRITE_BUSY, "competing synthetic owner")
        with write_lease_scope(layout, **kwargs):
            yield
    with monkeypatch.context() as patch:
        patch.setattr("src.runtime.write_lease.write_lease_scope", leases)
        with pytest.raises(PKVRuntimeError) as captured:
            _execute(_plan(config), provider_factory)
    assert captured.value.code is ErrorCode.EMBEDDING_PUBLICATION_PENDING
    operation_id = captured.value.to_dict()["details"]["operation_id"]
    return operation_id


def test_public_identity_is_stable_and_force_does_not_change_replay(tmp_path: Path) -> None:
    config = _config(tmp_path)
    first = _plan(config)
    again = _plan(config)
    forced = v2.plan_rebuild(v2.inspect_rebuild(config), force=True)
    assert first.to_dict()["plan_id"] == again.to_dict()["plan_id"]
    assert first.to_dict()["plan_id"] != forced.to_dict()["plan_id"]
    assert first._replay_key == forced._replay_key
    assert "_config_source_revision" not in json.dumps(first.to_dict())
    assert not EmbeddingRebuildLedger(config.layout).path.exists()


def test_period_changes_network_plan_but_not_material_replay(tmp_path: Path, monkeypatch) -> None:
    config = _config(tmp_path)
    monkeypatch.setattr("src.runtime.embedding_rebuild_ledger.current_utc", lambda: datetime(2026, 10, 31, tzinfo=timezone.utc))
    first = _plan(config)
    monkeypatch.setattr("src.runtime.embedding_rebuild_ledger.current_utc", lambda: datetime(2026, 11, 1, tzinfo=timezone.utc))
    next_period = _plan(config)
    assert first.to_dict()["plan_id"] != next_period.to_dict()["plan_id"]
    assert first._replay_key == next_period._replay_key
    with pytest.raises(PKVRuntimeError) as error:
        _execute(first)
    assert error.value.code is ErrorCode.RUNTIME_PLAN_STALE
    assert not EmbeddingRebuildLedger(config.layout).path.exists()


def test_success_preserves_private_stage_and_only_five_final_files(tmp_path: Path) -> None:
    config = _config(tmp_path)
    result = _execute(_plan(config)).to_dict()
    assert result["status"] == "ready"
    assert result["publication_committed"]
    ledger = EmbeddingRebuildLedger(config.layout)
    operation = ledger.get_operation(result["operation_id"])
    stage = config.layout.vector_index_dir / "staging" / operation.stage_key
    final = config.layout.vector_index_dir / "generations" / result["generation_id"]
    assert (stage / ".publish-ready.json").is_file()
    assert (stage / ".generation-stage-grant.json").is_file()
    assert {path.name for path in final.iterdir()} == {*core._PAIR_FILENAMES, core._GENERATION_MANIFEST_NAME}
    status = v2.inspect_operation(result["operation_id"], config=config)
    assert status.to_dict()["effective_state"] == "completed"
    assert not status.to_dict()["audit_completion_pending"]
    assert not status.to_dict()["ledger_completion_pending"]
    with pytest.raises(PKVRuntimeError) as error:
        v2.plan_publication(status)
    assert error.value.code is ErrorCode.EMBEDDING_OPERATION_COMPLETED


def test_lookup_and_plan_after_d_busy_are_read_only_and_publish_reuses_reservation(tmp_path: Path, monkeypatch) -> None:
    config = _config(tmp_path)
    operation_id = _pending(config, monkeypatch)
    ledger = EmbeddingRebuildLedger(config.layout)
    before = ledger.path.read_bytes()
    initial = ledger.get_operation(operation_id)
    status = v2.inspect_operation(operation_id, config=config)
    assert status.to_dict()["effective_state"] == "awaiting_publish"
    assert status.to_dict()["provider_effect"] == "completed"
    assert status.to_dict()["proof_valid"]
    publication = v2.plan_publication(status)
    assert publication.to_dict()["action"] == "publish_only"
    assert ledger.path.read_bytes() == before
    monkeypatch.setattr(v2, "create_embedder", lambda _: pytest.fail("publication constructed Provider"))
    result = v2.execute_publication(publication).to_dict()
    assert result["provider_called"] is False
    assert result["publication_committed"]
    final = ledger.get_operation(operation_id)
    assert final.reservation.reservation_id == initial.reservation.reservation_id
    assert len(ledger.read_operations()) == 1
    assert final.reservation.state == "settled"


def test_lookup_does_not_find_other_stage_when_ledger_selected_proof_is_missing(tmp_path: Path, monkeypatch) -> None:
    config = _config(tmp_path)
    operation_id = _pending(config, monkeypatch)
    ledger = EmbeddingRebuildLedger(config.layout)
    operation = ledger.get_operation(operation_id)
    path = config.layout.vector_index_dir / "staging" / operation.stage_key / ".publish-ready.json"
    detached = path.parent.parent / "unbound-proof.json"
    detached.write_bytes(path.read_bytes())
    path.unlink()
    before = ledger.path.read_bytes()
    status = v2.inspect_operation(operation_id, config=config).to_dict()
    assert not status["proof_valid"]
    assert status["provider_effect"] == "unknown"
    assert status["effective_state"] == "processing"
    assert ledger.path.read_bytes() == before


def test_invalid_sealed_proof_is_repair_without_ledger_write(tmp_path: Path, monkeypatch) -> None:
    config = _config(tmp_path)
    operation_id = _pending(config, monkeypatch)
    ledger = EmbeddingRebuildLedger(config.layout)
    operation = ledger.get_operation(operation_id)
    path = config.layout.vector_index_dir / "staging" / operation.stage_key / ".publish-ready.json"
    path.write_bytes(path.read_bytes() + b" ")
    before = ledger.path.read_bytes()
    with pytest.raises(PKVRuntimeError) as error:
        v2.inspect_operation(operation_id, config=config)
    assert error.value.code is ErrorCode.REPAIR_REQUIRED
    assert ledger.path.read_bytes() == before


def test_post_cas_audit_failure_reconciles_without_provider_or_pointer_cas(tmp_path: Path, monkeypatch) -> None:
    config = _config(tmp_path)
    original = AuditTrace.append
    def fail_completion(self, event):
        if event.get("phase") == "completed":
            raise AuditTraceError()
        return original(self, event)
    with monkeypatch.context() as patch:
        patch.setattr(AuditTrace, "append", fail_completion)
        result = _execute(_plan(config)).to_dict()
    assert result["audit_completion_pending"]
    status = v2.inspect_operation(result["operation_id"], config=config)
    assert status.to_dict()["publication_committed"]
    publication = v2.plan_publication(status)
    assert publication.to_dict()["action"] == "reconcile_completion"
    monkeypatch.setattr(core, "_pointer_update", lambda *a, **k: pytest.fail("reconciliation repeated CAS"))
    monkeypatch.setattr(v2, "create_embedder", lambda _: pytest.fail("reconciliation constructed Provider"))
    result = v2.execute_publication(publication).to_dict()
    assert not result["provider_called"]
    assert not result["audit_completion_pending"]
    assert not result["ledger_completion_pending"]


def test_post_cas_ledger_failure_is_durable_completed_lookup_and_reconciles(tmp_path: Path, monkeypatch) -> None:
    config = _config(tmp_path)
    def fail_completion(*args, **kwargs):
        raise OSError("synthetic settlement response failure")
    with monkeypatch.context() as patch:
        patch.setattr(EmbeddingRebuildLedger, "mark_completed", fail_completion)
        result = _execute(_plan(config)).to_dict()
    assert result["ledger_completion_pending"]
    inspection = v2.inspect_operation(result["operation_id"], config=config)
    assert inspection.to_dict()["effective_state"] == "completed"
    assert inspection.to_dict()["ledger_completion_pending"]
    monkeypatch.setattr(core, "_pointer_update", lambda *a, **k: pytest.fail("reconciliation repeated CAS"))
    result = v2.execute_publication(v2.plan_publication(inspection)).to_dict()
    assert not result["ledger_completion_pending"]


def test_snapshot_drift_rejects_publication_and_conservatively_settles(tmp_path: Path, monkeypatch) -> None:
    from src.runtime.runtime_snapshot import RuntimeSnapshotStore
    config = _config(tmp_path)
    operation_id = _pending(config, monkeypatch)
    publication = v2.plan_publication(v2.inspect_operation(operation_id, config=config))
    store = RuntimeSnapshotStore(config.layout)
    with write_lease_scope(config.layout):
        document = store.read()
        store.publish(document, document.merged({"w4_synthetic_drift": {"revision": 1}}))
    with pytest.raises(PKVRuntimeError) as error:
        v2.execute_publication(publication)
    assert error.value.code is ErrorCode.RUNTIME_PLAN_STALE
    operation = EmbeddingRebuildLedger(config.layout).get_operation(operation_id)
    assert operation.reservation.state == "settled"
    assert operation.settlement_basis == "conservative_reservation"


def test_partial_final_generation_is_repair_and_stage_is_retained(tmp_path: Path, monkeypatch) -> None:
    config = _config(tmp_path)
    operation_id = _pending(config, monkeypatch)
    status = v2.inspect_operation(operation_id, config=config)
    publication = v2.plan_publication(status)
    final = config.layout.vector_index_dir / "generations" / status.to_dict()["generation_id"]
    final.mkdir(parents=True)
    (final / "doc_vectors.idx").write_bytes(b"interrupted-copy")
    with pytest.raises(PKVRuntimeError) as error:
        v2.execute_publication(publication)
    assert error.value.code is ErrorCode.REPAIR_REQUIRED
    assert status._proof.stage_dir.is_dir()
    assert (status._proof.stage_dir / ".publish-ready.json").is_file()


def test_seal_fsync_response_loss_recovers_exact_proof_without_second_provider(tmp_path: Path, monkeypatch) -> None:
    from src.runtime.generation_stage import GenerationStageGrant
    config = _config(tmp_path)
    original = GenerationStageGrant.seal_publish_ready
    seals = []
    def lose_response(self, **kwargs):
        proof = original(self, **kwargs)
        seals.append(proof.proof_sha256)
        raise OSError("synthetic lost seal response")
    monkeypatch.setattr(GenerationStageGrant, "seal_publish_ready", lose_response)
    result = _execute(_plan(config)).to_dict()
    assert len(seals) == 1
    assert result["publication_committed"]
    status = v2.inspect_operation(result["operation_id"], config=config).to_dict()
    assert status["proof_sha256"] == seals[0]
    assert status["effective_state"] == "completed"


def test_pointer_cas_response_loss_is_recognized_without_second_cas(tmp_path: Path, monkeypatch) -> None:
    config = _config(tmp_path)
    original = core._pointer_update
    calls = []
    def lose_response(*args, **kwargs):
        original(*args, **kwargs)
        calls.append(kwargs["generation_id"])
        raise OSError("synthetic lost CAS response")
    monkeypatch.setattr(core, "_pointer_update", lose_response)
    result = _execute(_plan(config)).to_dict()
    assert calls == [result["generation_id"]]
    assert result["publication_committed"]
    assert not result["audit_completion_pending"]
    assert not result["ledger_completion_pending"]


def test_d_revalidates_proof_after_c_before_pointer_mutation(tmp_path: Path, monkeypatch) -> None:
    config = _config(tmp_path)
    original = v2.finish_manual_publication
    def changed_proof(plan, current, grant, proof, observer):
        (proof.stage_dir / ".publish-ready.json").write_bytes(b"corrupted-seal")
        return original(plan, current, grant, proof, observer)
    monkeypatch.setattr(v2, "finish_manual_publication", changed_proof)
    with pytest.raises(PKVRuntimeError) as error:
        _execute(_plan(config))
    assert error.value.code is ErrorCode.REPAIR_REQUIRED
    assert core.inspect_embedding_index(config).active_generation is None
    operation, = EmbeddingRebuildLedger(config.layout).read_operations()
    assert operation.reservation.state in {"released", "settled"}


def test_committed_pointer_can_reconcile_missing_proof_but_never_republish(tmp_path: Path, monkeypatch) -> None:
    config = _config(tmp_path)
    original = AuditTrace.append
    def fail_completion(self, event):
        if event.get("phase") == "completed":
            raise AuditTraceError()
        return original(self, event)
    with monkeypatch.context() as patch:
        patch.setattr(AuditTrace, "append", fail_completion)
        result = _execute(_plan(config)).to_dict()
    operation = EmbeddingRebuildLedger(config.layout).get_operation(result["operation_id"])
    (config.layout.vector_index_dir / "staging" / operation.stage_key / ".publish-ready.json").unlink()
    status = v2.inspect_operation(result["operation_id"], config=config)
    assert status.to_dict()["publication_committed"]
    assert status.to_dict()["proof_sha256"] is None
    assert not status.to_dict()["proof_valid"]
    publication = v2.plan_publication(status)
    assert publication.to_dict()["action"] == "reconcile_completion"
    monkeypatch.setattr(core, "_pointer_update", lambda *a, **k: pytest.fail("missing-proof reconciliation repeated CAS"))
    assert not v2.execute_publication(publication).to_dict()["audit_completion_pending"]


def test_partial_provider_usage_survives_seal_and_settles_known_overrun(tmp_path: Path, monkeypatch) -> None:
    import numpy as np
    from src.runtime.ai_automation_policy import TokenUsage
    from src.storage.markdown_store import Entry, MarkdownStore
    from src.storage.sqlite_store import SQLiteStore
    config = _config(tmp_path)
    with write_lease_scope(config.layout):
        markdown = MarkdownStore(config.layout.vault_dir)
        entry = Entry(title="partial use", source_type="text", content="synthetic source")
        write_plan = markdown.plan_save(entry)
        markdown.save_planned(write_plan, entry)
        SQLiteStore(config.layout.db_path).insert_entry_with_chunks(entry, write_plan.relative_path, ["synthetic chunk"])
    class PartialProvider:
        def __init__(self):
            self.client = self
            self.last_usage = None
            self.last_usage_complete = False
            self.calls = 0
        def embed_document(self, text):
            self.calls += 1
            self.last_usage = TokenUsage(embedding_input_tokens=250, source="provider_reported")
            self.last_usage_complete = True
            return np.asarray([1, 2, 3], dtype=np.float32)
        def embed_batch_numpy(self, chunks):
            self.calls += 1
            self.last_usage = None
            self.last_usage_complete = False
            return np.asarray([[1, 2, 3]], dtype=np.float32)
    provider = PartialProvider()
    operation_id = _pending(config, monkeypatch, lambda: provider)
    assert provider.calls == 2
    status = v2.inspect_operation(operation_id, config=config)
    assert status._proof.payload["usage_completeness"] == "partial"
    assert status.to_dict()["provider_usage"]["embedding_input_tokens"] == 250
    v2.execute_publication(v2.plan_publication(status))
    settled = EmbeddingRebuildLedger(config.layout).get_operation(operation_id)
    assert settled.reservation.settled_tokens == 250
    assert settled.settlement_basis == "conservative_reservation"
    assert provider.calls == 2


def test_meter_honors_partial_internal_batch_and_ignores_usage_getter_failure() -> None:
    from src.runtime.ai_automation_policy import TokenUsage
    class Provider:
        def __init__(self):
            self.client = self
            self.last_usage_complete = False
        @property
        def last_usage(self):
            return TokenUsage(embedding_input_tokens=900, source="provider_reported")
        def embed_document(self, text):
            return "vector"
        def embed_batch_numpy(self, chunks):
            return "vectors"
    meter = v2._Meter(Provider())
    meter.embed_document("test")
    assert meter.provider_usages[0].embedding_input_tokens == 900
    assert not meter.provider_usage_complete
    class BadUsageProvider(Provider):
        @property
        def last_usage(self):
            raise RuntimeError("private provider property")
        def embed_document(self, text):
            raise ValueError("original provider failure")
    meter = v2._Meter(BadUsageProvider())
    with pytest.raises(ValueError, match="original provider failure"):
        meter.embed_document("test")
    assert not meter.provider_usage_complete


def test_sealed_work_is_pending_in_network_inspection_and_blocks_force(tmp_path: Path, monkeypatch) -> None:
    config = _config(tmp_path)
    operation_id = _pending(config, monkeypatch)
    ledger = EmbeddingRebuildLedger(config.layout)
    before = ledger.path.read_bytes()
    inspection = v2.inspect_rebuild(config)
    payload = inspection.to_dict()
    assert payload["index"]["state"] == "processing"
    assert payload["index"]["issues"][-1]["code"] == "embedding_publication_pending"
    with pytest.raises(PKVRuntimeError) as error:
        v2.plan_rebuild(inspection, force=True)
    assert error.value.code is ErrorCode.EMBEDDING_PUBLICATION_PENDING
    assert error.value.details["operation_id"] == operation_id
    assert ledger.path.read_bytes() == before


def test_repeated_publication_contention_preserves_proof_and_ledger(tmp_path: Path, monkeypatch) -> None:
    config = _config(tmp_path)
    operation_id = _pending(config, monkeypatch)
    publication = v2.plan_publication(v2.inspect_operation(operation_id, config=config))
    ledger = EmbeddingRebuildLedger(config.layout)
    before = ledger.path.read_bytes()
    @contextmanager
    def busy(*args, **kwargs):
        raise PKVRuntimeError(ErrorCode.WRITE_BUSY, "synthetic contention")
        yield
    monkeypatch.setattr(v2, "write_lease_scope", busy)
    for _ in range(2):
        with pytest.raises(PKVRuntimeError) as error:
            v2.execute_publication(publication)
        assert error.value.code is ErrorCode.EMBEDDING_PUBLICATION_PENDING
        assert error.value.details["operation_id"] == operation_id
    assert ledger.path.read_bytes() == before


def test_corrupt_audit_after_committed_pointer_never_marks_operation_failed(tmp_path: Path, monkeypatch) -> None:
    config = _config(tmp_path)
    with monkeypatch.context() as patch:
        patch.setattr(EmbeddingRebuildLedger, "mark_completed", lambda *a, **k: (_ for _ in ()).throw(OSError("synthetic")))
        result = _execute(_plan(config)).to_dict()
    publication = v2.plan_publication(v2.inspect_operation(result["operation_id"], config=config))
    ledger = EmbeddingRebuildLedger(config.layout)
    before = ledger.path.read_bytes()
    with (config.layout.log_dir / "audit.jsonl").open("ab") as handle:
        handle.write(b"truncated-private-audit-body")
    with pytest.raises(PKVRuntimeError) as error:
        v2.execute_publication(publication)
    assert error.value.code is ErrorCode.REPAIR_REQUIRED
    assert "private-audit-body" not in str(error.value)
    assert ledger.path.read_bytes() == before


def test_current_contract_drift_keeps_original_proof_valid_but_refuses_publication(tmp_path: Path, monkeypatch) -> None:
    from src.utils.config import Config
    config = _config(tmp_path)
    operation_id = _pending(config, monkeypatch)
    original = v2.inspect_operation(operation_id, config=config)
    contents = json.loads(config.layout.user_config_path.read_text(encoding="utf-8"))
    contents["ai"]["embedding"]["model"] = "different-embedding-contract"
    config.layout.user_config_path.write_text(json.dumps(contents), encoding="utf-8")
    reloaded = Config(layout=config.layout)
    inspection = v2.inspect_operation(operation_id, config=reloaded)
    assert inspection.to_dict()["proof_valid"]
    assert inspection.to_dict()["proof_sha256"] == original.to_dict()["proof_sha256"]
    with pytest.raises(PKVRuntimeError) as error:
        v2.plan_publication(inspection)
    assert error.value.code is ErrorCode.RUNTIME_PLAN_STALE


def test_in_process_publication_handle_detects_config_file_change_without_old_hmac_replay(tmp_path: Path, monkeypatch) -> None:
    config = _config(tmp_path)
    operation_id = _pending(config, monkeypatch)
    publication = v2.plan_publication(v2.inspect_operation(operation_id, config=config))
    contents = json.loads(config.layout.user_config_path.read_text(encoding="utf-8"))
    contents["ai"]["embedding"]["api_key"] = "replacement-test-only-credential"
    config.layout.user_config_path.write_text(json.dumps(contents), encoding="utf-8")
    with pytest.raises(PKVRuntimeError) as error:
        v2.execute_publication(publication)
    assert error.value.code is ErrorCode.RUNTIME_PLAN_STALE
    assert core.inspect_embedding_index(config).active_generation is None
    operation = EmbeddingRebuildLedger(config.layout).get_operation(operation_id)
    assert operation.reservation.state == "settled"
    assert operation.settlement_basis == "conservative_reservation"
