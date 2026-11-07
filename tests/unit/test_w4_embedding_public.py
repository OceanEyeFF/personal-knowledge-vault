"""W4 SDK boundary tests; internal seam fakes never stand in for Core acceptance."""

from __future__ import annotations

import copy
import hashlib
import json
import pickle
import sys
from types import ModuleType, SimpleNamespace

import pytest

from pkv_kernel import embedding
from src.runtime.errors import ErrorCode, PKVRuntimeError
from src.runtime.ai_automation_policy import TokenUsage
from tests.unit.test_embedding_rebuild_ledger import _configured
from tests.unit.test_kernel_embedding_rebuild import _seed_r2


OPERATION_ID = "embedding-rebuild-" + "a" * 32
GENERATION_ID = "g-" + "b" * 20 + "-" + "c" * 12
HANDLES = (
    "EmbeddingRebuildInspectionV2", "EmbeddingRebuildPlanV2",
    "EmbeddingRebuildConfirmationV2", "EmbeddingRebuildExecutionV2",
    "EmbeddingOperationInspectionV2", "EmbeddingPublicationPlanV2",
    "EmbeddingPublicationConfirmationV2",
)


def _inspection():
    return {
        "schema": "pkv.embedding.inspection.v2",
        "index": {
            "state": "initial_generation_pending", "source_digest": "a" * 64,
            "source_count": 1, "chunk_count": 1, "active_generation_id": None,
            "previous_generation_id": None, "active_manifest_sha256": None,
            "contract": {"provider": "openai", "model": "synthetic", "dimension": 3,
                         "endpoint_fingerprint": "b" * 64}, "issues": [],
        },
        "data_root_identity_sha256": "c" * 64, "runtime_snapshot_sha256": "d" * 64,
        "policy": {"enabled": True, "authorization_mode": "explicit_confirmation",
                   "timezone": "UTC", "daily_embedding_token_limit": 100,
                   "monthly_embedding_token_limit": 1000, "fingerprint": "e" * 64},
        "budget_check_period": {"timezone": "UTC", "local_day": "2026-10-25", "local_month": "2026-10"},
        "ledger_schema_version": None, "ledger_upgrade": None,
    }


def _operation():
    return {
        "schema": "pkv.embedding.operation.v2", "operation_id": OPERATION_ID,
        "effective_state": "awaiting_publish", "provider_effect": "completed",
        "generation_id": GENERATION_ID, "original_plan_id": "a" * 64,
        "data_root_identity_sha256": "c" * 64, "runtime_snapshot_sha256": "d" * 64,
        "proof_valid": True, "proof_sha256": "e" * 64,
        "reservation": {"state": "reserved", "timezone": "UTC", "local_day": "2026-10-25",
                        "local_month": "2026-10", "reserved_tokens": 12, "settled_tokens": None},
        "local_estimate": {"uncached_input_tokens": None, "cached_input_tokens": None,
                           "generated_tokens": None, "embedding_input_tokens": 12, "source": "local_estimate"},
        "provider_usage": None, "publication_committed": False,
        "ledger_completion_pending": False, "audit_completion_pending": False,
        "ledger_schema_version": 3, "ledger_upgrade": None, "issues": [],
    }


def _execution(provider_called):
    return {
        "schema": "pkv.embedding.execution.v2", "operation_id": OPERATION_ID,
        "generation_id": GENERATION_ID, "previous_generation_id": None, "status": "ready",
        "provider_called": provider_called, "publication_committed": True,
        "ledger_completion_pending": False, "audit_completion_pending": False, "issues": [],
    }


@pytest.fixture
def facade_seam(tmp_path, monkeypatch):
    """Exercise only public validation against a deliberately narrow Core seam."""
    config = _configured(tmp_path)
    calls = []
    state = {"inspection": _inspection(), "operation": _operation()}
    module = ModuleType("src.runtime.embedding_rebuild_v2")

    def result(payload):
        return SimpleNamespace(to_dict=lambda: copy.deepcopy(payload))

    def inspect_rebuild(selected):
        assert selected is config
        calls.append("inspect")
        return result(state["inspection"])

    def plan_rebuild(inspection, *, force):
        calls.append("plan")
        return result({
            "schema": "pkv.embedding.plan.v2", "plan_id": ("f" if force else "a") * 64,
            "action": "rebuild", "force": force, "inspection": inspection.to_dict(),
            "estimated_embedding_tokens": 12, "requires_confirmation": True, "requires_network": True,
        })

    def inspect_operation(operation_id, *, config):
        assert operation_id == OPERATION_ID
        calls.append("operation")
        return result(state["operation"])

    def plan_publication(inspection):
        calls.append("publication_plan")
        operation = inspection.to_dict()
        return result({
            "schema": "pkv.embedding.publication-plan.v2", "plan_id": "b" * 64,
            "action": "publish_only", "operation_id": OPERATION_ID, "generation_id": GENERATION_ID,
            "operation_inspection": operation, "ledger_upgrade": operation["ledger_upgrade"],
            "requires_confirmation": True, "requires_network": False, "creates_reservation": False,
        })

    def execute_rebuild(plan):
        calls.append("execute")
        return result(_execution(True))

    def execute_publication(plan):
        calls.append("publish")
        return result(_execution(False))

    for name, function in locals().copy().items():
        if name in {"inspect_rebuild", "plan_rebuild", "inspect_operation", "plan_publication",
                    "execute_rebuild", "execute_publication"}:
            setattr(module, name, function)
    monkeypatch.setitem(sys.modules, module.__name__, module)
    return SimpleNamespace(config=config, calls=calls, state=state, module=module)


def _plans(seam):
    inspection = embedding.inspect_embedding_rebuild_v2(seam.config)
    rebuild = embedding.plan_embedding_rebuild_v2(inspection)
    operation = embedding.inspect_embedding_operation_v2(OPERATION_ID, config=seam.config)
    publication = embedding.plan_embedding_publication_v2(operation)
    return inspection, rebuild, operation, publication


@pytest.mark.parametrize("name", HANDLES)
def test_public_handles_cannot_be_directly_constructed_or_deserialized(name):
    handle = getattr(embedding, name)
    with pytest.raises(TypeError, match="factory-only"):
        handle()
    with pytest.raises(TypeError):
        handle(**{"schema": "pkv.embedding.plan.v2", "plan_id": "a" * 64})


def test_all_seven_handles_expose_independent_closed_display_copies(facade_seam):
    inspection, plan, operation, publication = _plans(facade_seam)
    confirmation = embedding.confirm_embedding_rebuild_v2(plan, allow_network=True)
    publication_confirmation = embedding.confirm_embedding_publication_v2(publication)
    # Inspect, plan and confirm do not reach either mutation seam.
    assert facade_seam.calls == ["inspect", "plan", "operation", "publication_plan"]
    execution = embedding.execute_embedding_rebuild_v2(plan, confirmation)
    for handle in (inspection, plan, operation, publication, confirmation, publication_confirmation, execution):
        before = handle.to_dict()
        display = handle.to_dict()
        display["schema"] = "replaced"
        assert handle.to_dict() == before
        with pytest.raises(AttributeError):
            handle.schema = "replaced"
        with pytest.raises(TypeError, match="cannot be serialized"):
            pickle.dumps(handle)
        with pytest.raises(TypeError):
            copy.deepcopy(handle)
        assert not hasattr(handle, "__dict__")
    inspection.index["contract"]["dimension"] = 99
    plan.inspection["policy"]["enabled"] = False
    operation.reservation["settled_tokens"] = 0
    assert inspection.index["contract"]["dimension"] == 3
    assert plan.inspection["policy"]["enabled"] is True
    assert operation.reservation["settled_tokens"] is None
    assert operation.provider_usage is None
    assert execution.publication_committed is True


@pytest.mark.parametrize("value", [None, 0, 1, "true", [], {}])
def test_bool_inputs_are_not_coerced(facade_seam, value):
    inspection, plan, _, publication = _plans(facade_seam)
    for operation in (
        lambda: embedding.plan_embedding_rebuild_v2(inspection, force=value),
        lambda: embedding.confirm_embedding_rebuild_v2(plan, approved=value),
        lambda: embedding.confirm_embedding_rebuild_v2(plan, allow_network=value),
        lambda: embedding.confirm_embedding_publication_v2(publication, approved=value),
    ):
        with pytest.raises(TypeError):
            operation()
    assert "execute" not in facade_seam.calls and "publish" not in facade_seam.calls


def test_missing_rejected_cross_plan_and_mixed_confirmations_never_execute(facade_seam):
    inspection, plan, _, publication = _plans(facade_seam)
    force_plan = embedding.plan_embedding_rebuild_v2(inspection, force=True)
    wrong = embedding.confirm_embedding_rebuild_v2(force_plan, allow_network=True)
    for confirmation, expected in (
        (None, ErrorCode.CONFIRMATION_REQUIRED),
        (embedding.confirm_embedding_rebuild_v2(plan, approved=False), ErrorCode.CONFIRMATION_REQUIRED),
        (embedding.confirm_embedding_rebuild_v2(plan), ErrorCode.EMBEDDING_REBUILD_AUTHORIZATION_REQUIRED),
        (wrong, ErrorCode.RUNTIME_PLAN_STALE),
    ):
        with pytest.raises(PKVRuntimeError) as caught:
            embedding.execute_embedding_rebuild_v2(plan, confirmation)
        assert caught.value.code is expected
    publication_confirmation = embedding.confirm_embedding_publication_v2(publication)
    with pytest.raises(TypeError):
        embedding.execute_embedding_rebuild_v2(plan, publication_confirmation)
    with pytest.raises(TypeError):
        embedding.execute_embedding_publication_v2(publication, wrong)
    with pytest.raises(TypeError):
        embedding.execute_embedding_publication_v2(plan, publication_confirmation)
    with pytest.raises(TypeError):
        embedding.execute_embedding_rebuild_v2(plan, wrong.to_dict())
    with pytest.raises(PKVRuntimeError) as caught:
        embedding.execute_embedding_publication_v2(publication)
    assert caught.value.code is ErrorCode.CONFIRMATION_REQUIRED
    assert "execute" not in facade_seam.calls and "publish" not in facade_seam.calls


def test_publication_execution_is_provider_free_and_never_accepts_network_parameters(facade_seam):
    _, _, _, plan = _plans(facade_seam)
    confirmation = embedding.confirm_embedding_publication_v2(plan)
    execution = embedding.execute_embedding_publication_v2(plan, confirmation)
    assert execution.provider_called is False
    assert facade_seam.calls[-1] == "publish"
    with pytest.raises(TypeError):
        embedding.confirm_embedding_publication_v2(plan, allow_network=True)


@pytest.mark.parametrize("value", [None, 1, True, "", "../private", "embedding-rebuild-" + "A" * 32,
                                  "embedding-rebuild-" + "a" * 31, OPERATION_ID + "\n"])
def test_invalid_operation_identity_is_rejected_before_loading_configuration(facade_seam, monkeypatch, value):
    monkeypatch.setattr(embedding, "get_config", lambda: pytest.fail("invalid ID loaded config"))
    with pytest.raises(ValueError):
        embedding.inspect_embedding_operation_v2(value)
    assert facade_seam.calls == []


@pytest.mark.parametrize("path", [(), ("index",), ("index", "contract"), ("policy",), ("budget_check_period",)])
def test_closed_inspection_rejects_unknown_sensitive_fields(facade_seam, path):
    payload = facade_seam.state["inspection"]
    for field in path:
        payload = payload[field]
    payload["secret_config_path"] = "do-not-project"
    with pytest.raises(ValueError, match="invalid public contract") as caught:
        embedding.inspect_embedding_rebuild_v2(facade_seam.config)
    assert "do-not-project" not in str(caught.value)


@pytest.mark.parametrize("field,value", [
    ("source_count", True), ("chunk_count", -1), ("source_digest", "A" * 64),
    ("active_generation_id", "../../private"), ("active_manifest_sha256", "bad"),
])
def test_invalid_core_scalars_fail_closed(facade_seam, field, value):
    facade_seam.state["inspection"]["index"][field] = value
    with pytest.raises(ValueError):
        embedding.inspect_embedding_rebuild_v2(facade_seam.config)


def test_legacy_missing_public_id_remains_null_without_private_hash_substitution(facade_seam):
    facade_seam.state["operation"].update(original_plan_id=None, proof_valid=False, proof_sha256=None)
    operation = embedding.inspect_embedding_operation_v2(OPERATION_ID, config=facade_seam.config)
    assert operation.original_plan_id is None
    assert operation.proof_sha256 is None


def test_invalid_proof_never_projects_an_unverified_digest(facade_seam):
    facade_seam.state["operation"]["proof_valid"] = False
    with pytest.raises(ValueError):
        embedding.inspect_embedding_operation_v2(OPERATION_ID, config=facade_seam.config)


def _pending_details():
    return {"operation_id": OPERATION_ID, "effective_state": "awaiting_publish",
            "provider_effect": "completed", "publish_only_required": True}


def test_pending_error_preserves_only_approved_detached_details():
    details = _pending_details()
    error = PKVRuntimeError(ErrorCode.EMBEDDING_PUBLICATION_PENDING, "Publication awaits a writer.",
                            stage="embedding_publication", recoverable=True, details=details)
    details["private_path"] = "do-not-project"
    error.details["private_path"] = "do-not-project"
    projected = error.to_dict()
    assert projected["details"] == _pending_details()
    projected["details"]["operation_id"] = "changed"
    assert error.to_dict()["details"]["operation_id"] == OPERATION_ID
    assert "details" not in PKVRuntimeError(ErrorCode.WRITE_BUSY, "busy").to_dict()


@pytest.mark.parametrize("patch", [
    {"private_path": "do-not-project"}, {"provider_effect": "unknown"},
    {"effective_state": "processing"}, {"publish_only_required": 1},
    {"operation_id": "../private"},
])
def test_pending_error_rejects_unknown_or_inconsistent_details(patch):
    with pytest.raises(ValueError):
        PKVRuntimeError(ErrorCode.EMBEDDING_PUBLICATION_PENDING, "safe", details={**_pending_details(), **patch})


def test_details_are_not_an_arbitrary_exception_context_channel():
    with pytest.raises(ValueError, match="requires"):
        PKVRuntimeError(ErrorCode.EMBEDDING_PUBLICATION_PENDING, "safe")
    with pytest.raises(ValueError):
        PKVRuntimeError(ErrorCode.WRITE_BUSY, "safe", details={"operation_id": OPERATION_ID, "effective_state": "processing"})
    error = PKVRuntimeError(ErrorCode.EMBEDDING_OPERATION_COMPLETED, "safe",
                            details={"operation_id": OPERATION_ID, "effective_state": "completed"})
    error._details["private_path"] = "do-not-project"
    with pytest.raises(ValueError):
        error.to_dict()


def _hash_domain(domain, payload):
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(domain.encode("utf-8") + b"\0" + encoded).hexdigest()


def test_real_public_plan_uses_frozen_hash_material_and_reads_do_not_write(tmp_path):
    config = _configured(tmp_path)
    _seed_r2(config)
    before = {str(path.relative_to(config.data_root)): hashlib.sha256(path.read_bytes()).hexdigest()
              for path in config.data_root.rglob("*") if path.is_file()}
    inspection = embedding.inspect_embedding_rebuild_v2(config)
    plan = embedding.plan_embedding_rebuild_v2(inspection, force=True)
    confirmation = embedding.confirm_embedding_rebuild_v2(plan, allow_network=True)
    display = inspection.to_dict()
    index = display["index"]
    material = {
        "identity_schema": "pkv.embedding.transition.v2",
        "data_root_identity_sha256": display["data_root_identity_sha256"],
        "runtime_snapshot_sha256": display["runtime_snapshot_sha256"],
        "index_state": index["state"],
        "source": {"digest": index["source_digest"], "document_count": index["source_count"], "chunk_count": index["chunk_count"]},
        "pointer": {name: index[name] for name in ("active_generation_id", "previous_generation_id", "active_manifest_sha256")},
        "contract_sha256": _hash_domain("pkv.embedding.contract.v2", index["contract"]),
        "policy_fingerprint": display["policy"]["fingerprint"],
    }
    assert plan.plan_id == _hash_domain("pkv.embedding.rebuild-plan.v2", {
        "action": "rebuild", "material": material, "force": True,
        "ledger_upgrade": display["ledger_upgrade"], "budget_check_period": display["budget_check_period"],
    })
    assert confirmation.plan_id == plan.plan_id
    after = {str(path.relative_to(config.data_root)): hashlib.sha256(path.read_bytes()).hexdigest()
             for path in config.data_root.rglob("*") if path.is_file()}
    assert after == before
    assert embedding.plan_embedding_rebuild_v2(embedding.inspect_embedding_rebuild_v2(config), force=True).plan_id == plan.plan_id
    assert embedding.plan_embedding_rebuild_v2(inspection, force=False).plan_id != plan.plan_id
    serialized = json.dumps(plan.to_dict(), ensure_ascii=False)
    assert str(config.data_root) not in serialized
    assert "embedding-test-secret" not in serialized and "embedding.invalid" not in serialized


def test_real_v2_confirmation_is_bound_to_root_before_provider_use(tmp_path):
    first = _configured(tmp_path / "first")
    second = _configured(tmp_path / "second")
    _seed_r2(first)
    _seed_r2(second)
    plan_a = embedding.plan_embedding_rebuild_v2(embedding.inspect_embedding_rebuild_v2(first))
    plan_b = embedding.plan_embedding_rebuild_v2(embedding.inspect_embedding_rebuild_v2(second))
    assert plan_a.plan_id != plan_b.plan_id
    confirmation = embedding.confirm_embedding_rebuild_v2(plan_a, allow_network=True)
    with pytest.raises(PKVRuntimeError) as caught:
        embedding.execute_embedding_rebuild_v2(plan_b, confirmation)
    assert caught.value.code is ErrorCode.RUNTIME_PLAN_STALE


@pytest.mark.parametrize("complete", [False, None, 1, "true"])
def test_v1_meter_does_not_infer_batch_completeness_from_numeric_usage(complete):
    class Client:
        last_usage = TokenUsage(embedding_input_tokens=7)
        last_usage_complete = complete

        def embed_batch_numpy(self, chunks):
            return "vectors"

    provider = SimpleNamespace(client=Client(), embed_document=lambda text: "vector")
    meter = embedding._MeteredPreChunkedEmbeddingAdapter(provider)
    assert meter.embed_document("document") == "vector"
    assert meter.embed_stored_chunks(["stored chunk"]) == "vectors"
    assert meter.provider_call_count == 2
    assert len(meter.provider_usages) == 2
    assert meter.provider_usage_complete is False


def test_v1_meter_preserves_reported_usage_when_completeness_getter_fails():
    class Client:
        last_usage = TokenUsage(embedding_input_tokens=7)

        @property
        def last_usage_complete(self):
            raise RuntimeError("private provider failure")

        def embed_batch_numpy(self, chunks):
            raise ValueError("original provider failure")

    provider = SimpleNamespace(client=Client(), embed_document=lambda text: "vector")
    meter = embedding._MeteredPreChunkedEmbeddingAdapter(provider)
    with pytest.raises(ValueError, match="original provider failure"):
        meter.embed_stored_chunks(["stored chunk"])
    assert meter.provider_usages == (Client.last_usage,)
    assert meter.provider_usage_complete is False
