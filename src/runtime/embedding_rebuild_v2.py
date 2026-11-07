"""Private orchestration for confirmed manual rebuild and publication recovery.

Public facades own confirmation capabilities.  This module keeps stable public
identity separate from Core's process-local configuration fence, and only the
existing short writer-lease boundaries mutate accounting or publication facts.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
from dataclasses import dataclass, field
from typing import Any, Callable

from src.ai.provider_factory import create_embedder
from src.runtime import embedding_lifecycle as core
from src.runtime.ai_automation_policy import TokenUsage
from src.runtime.audit import AuditTrace, AuditTraceError
from src.runtime.embedding_completion_audit import append_completion, inspect_completion
from src.runtime.embedding_rebuild_ledger import EmbeddingRebuildLedger, EmbeddingRebuildLedgerState
from src.runtime.embedding_rebuild_policy import inspect_embedding_rebuild_policy, EmbeddingRebuildPolicyState
from src.runtime.embedding_stage_proof import PROOF_NAME, ValidatedStageProof, validate_publish_ready
from src.runtime.errors import ErrorCode, PKVRuntimeError
from src.runtime.write_lease import write_lease_scope
from src.utils.config import Config, get_config


def _error(code: ErrorCode, message: str, *, details: dict | None = None) -> PKVRuntimeError:
    return PKVRuntimeError(code, message, stage="embedding_publication", recoverable=True, details=details)


def _hash(domain: str, payload: dict) -> str:
    raw = json.dumps(payload, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(domain.encode("utf-8") + b"\x00" + raw).hexdigest()


def _config(config: Config | None) -> Config:
    value = get_config() if config is None else config
    if not isinstance(value, Config):
        raise TypeError("config 必须是 Config 或 None")
    return value


def _contract(inspection: core.EmbeddingIndexInspection) -> dict:
    contract = inspection.contract
    if contract is None:
        raise _error(ErrorCode.REPAIR_REQUIRED, "当前 Embedding contract 无法验证。")
    return {"provider": contract.provider, "model": contract.model, "dimension": contract.dimension, "endpoint_fingerprint": contract.endpoint_sha256}


def _contract_sha(inspection: core.EmbeddingIndexInspection) -> str:
    return _hash("pkv.embedding.contract.v2", _contract(inspection))


def _period(policy) -> dict:
    from src.runtime.embedding_rebuild_ledger import current_period
    return current_period(policy)


def _require_policy(policy) -> None:
    if policy.state is not EmbeddingRebuildPolicyState.READY or policy.token_quota is None or policy.policy_fingerprint is None:
        raise _error(ErrorCode.EMBEDDING_REBUILD_AUTHORIZATION_REQUIRED, "手动 Embedding 重建需要有效的独立预算策略。")


def _material(inspection: core.EmbeddingIndexInspection, policy) -> dict:
    if inspection.source is None or inspection._snapshot is None or not inspection._snapshot.exists:
        raise _error(ErrorCode.REPAIR_REQUIRED, "当前 Embedding 源或运行态快照无法验证。")
    return {
        "identity_schema": "pkv.embedding.transition.v2",
        "data_root_identity_sha256": core._data_root_identity_sha256(inspection._config),
        "runtime_snapshot_sha256": inspection._snapshot.raw_sha256,
        "index_state": inspection.state.value,
        "source": inspection.source.to_dict(),
        "pointer": {"active_generation_id": inspection.active_generation, "previous_generation_id": inspection.previous_generation, "active_manifest_sha256": inspection.active_manifest_sha256},
        "contract_sha256": _contract_sha(inspection),
        "policy_fingerprint": policy.policy_fingerprint,
    }


def _estimate(inspection: core.EmbeddingIndexInspection) -> int:
    if inspection._captured_source is None:
        raise _error(ErrorCode.REPAIR_REQUIRED, "当前 Embedding 源捕获无法验证。")
    return math.ceil(sum(len(record.content) + sum(len(chunk) for _, chunk in record.chunks) for record in inspection._captured_source.records) / 4)


def _issue(code: ErrorCode, message: str) -> dict:
    return {"code": code.value, "message": message, "stage": "embedding_publication", "recoverable": True}


@dataclass(frozen=True)
class _Handle:
    _payload: dict = field(repr=False)
    _config: Config = field(repr=False)
    _core: Any = field(default=None, repr=False)
    _policy: Any = field(default=None, repr=False)
    _ledger: Any = field(default=None, repr=False)
    _operation: Any = field(default=None, repr=False)
    _proof: ValidatedStageProof | None = field(default=None, repr=False)
    _material: dict | None = field(default=None, repr=False)
    _replay_key: str | None = field(default=None, repr=False)
    _manifest_sha: str | None = field(default=None, repr=False)
    _publishable: bool = field(default=False, repr=False)
    _network_error: PKVRuntimeError | None = field(default=None, repr=False)

    def to_dict(self) -> dict:
        return copy.deepcopy(self._payload)


def inspect_rebuild(config: Config | None = None) -> _Handle:
    resolved = _config(config)
    inspection = core.inspect_embedding_index(resolved, source=core.SQLiteEmbeddingSource())
    policy = inspect_embedding_rebuild_policy(resolved)
    _require_policy(policy)
    material = _material(inspection, policy)
    ledger = EmbeddingRebuildLedger(resolved.layout).inspect()
    issues = [issue.to_dict() for issue in inspection.issues]
    state = inspection.state.value
    network_error = None
    if ledger.blocking_operation_count:
        network_error = _error(ErrorCode.REPAIR_REQUIRED, "存在尚未结算且需要修复的手动重建操作。")
        candidates = () if any(item.state is EmbeddingRebuildLedgerState.REPAIR_REQUIRED for item in ledger.operations) else ledger.operations
        for operation in candidates:
            if operation.record_origin != "native_v3" or operation.provider_effect not in {"unknown", "completed"} or operation.reservation is None or operation.reservation.state in {"settled", "released"}:
                continue
            try:
                proof = _proof_for(resolved, operation)
            except PKVRuntimeError:
                continue
            if proof is not None:
                # C evidence explains the otherwise-unknown shared row.  The
                # work still blocks a new network attempt, including a changed
                # force/period/material, until explicit publication or repair.
                network_error = _pending(operation.operation_id)
                break
            same_material = all((operation.data_root_identity_sha256 == material["data_root_identity_sha256"],
                operation.runtime_snapshot_sha256 == material["runtime_snapshot_sha256"], operation.source_revision == inspection.source.digest,
                operation.contract_sha256 == material["contract_sha256"], operation.policy_fingerprint == policy.policy_fingerprint))
            if same_material:
                network_error = _error(ErrorCode.EMBEDDING_PROCESSING, "原手动重建工作尚未完整封存。")
        state = "processing" if network_error.code in {ErrorCode.EMBEDDING_PUBLICATION_PENDING, ErrorCode.EMBEDDING_PROCESSING} else "repair_required"
        issues.append(network_error.to_dict())
    quota = policy.token_quota
    payload = {
        "schema": "pkv.embedding.inspection.v2",
        "index": {"state": state, "source_digest": inspection.source.digest, "source_count": inspection.source.document_count, "chunk_count": inspection.source.chunk_count,
                  "active_generation_id": inspection.active_generation, "previous_generation_id": inspection.previous_generation, "active_manifest_sha256": inspection.active_manifest_sha256,
                  "contract": _contract(inspection), "issues": issues},
        "data_root_identity_sha256": material["data_root_identity_sha256"], "runtime_snapshot_sha256": material["runtime_snapshot_sha256"],
        "policy": {"enabled": True, "authorization_mode": "explicit_confirmation", "timezone": quota.timezone, "daily_embedding_token_limit": quota.daily_total_tokens,
                   "monthly_embedding_token_limit": quota.monthly_total_tokens, "fingerprint": policy.policy_fingerprint},
        "budget_check_period": _period(policy), "ledger_schema_version": ledger.schema_version, "ledger_upgrade": ledger.ledger_upgrade,
    }
    return _Handle(payload, resolved, _core=inspection, _policy=policy, _ledger=ledger, _material=material, _network_error=network_error)


def plan_rebuild(inspection: _Handle, *, force: bool = False) -> _Handle:
    if not isinstance(inspection, _Handle) or inspection._payload.get("schema") != "pkv.embedding.inspection.v2":
        raise TypeError("inspection 必须由 inspect_rebuild 返回")
    if inspection._network_error is not None:
        raise inspection._network_error
    if inspection._payload["index"]["state"] == "repair_required":
        raise _error(ErrorCode.REPAIR_REQUIRED, "当前手动重建状态需要修复。")
    plan = core.plan_embedding_rebuild(inspection._core, force=force)
    plan_id = _hash("pkv.embedding.rebuild-plan.v2", {
        "action": "rebuild", "material": inspection._material, "force": force,
        "ledger_upgrade": inspection._payload["ledger_upgrade"], "budget_check_period": inspection._payload["budget_check_period"],
    })
    return _Handle({"schema": "pkv.embedding.plan.v2", "plan_id": plan_id, "action": "rebuild", "force": force,
                    "inspection": inspection.to_dict(), "estimated_embedding_tokens": _estimate(inspection._core), "requires_confirmation": True, "requires_network": True},
                   inspection._config, _core=plan, _policy=inspection._policy, _ledger=inspection._ledger, _material=inspection._material,
                   _replay_key=_hash("pkv.embedding.transition.v2", inspection._material))


class _Meter:
    def __init__(self, provider) -> None:
        self.adapter = core.PreChunkedEmbeddingAdapter(provider)
        self.client = getattr(provider, "client", None)
        self.provider_attempted = False
        self.provider_call_count = 0
        self.provider_usages: list[TokenUsage] = []
        self.provider_usage_complete = True

    def _call(self, action, value):
        self.provider_attempted = True
        self.provider_call_count += 1
        try:
            return action(value)
        finally:
            try:
                usage = getattr(self.client, "last_usage", None)
                complete = getattr(self.client, "last_usage_complete", False)
                if isinstance(usage, TokenUsage) and usage.source == "provider_reported":
                    self.provider_usages.append(usage)
                    self.provider_usage_complete = self.provider_usage_complete and complete is True
                else:
                    self.provider_usage_complete = False
            except Exception:
                self.provider_usage_complete = False

    def embed_document(self, text):
        return self._call(self.adapter.embed_document, text)

    def embed_stored_chunks(self, chunks):
        return self._call(self.adapter.embed_stored_chunks, tuple(chunks))


def _usage(payload: dict | None) -> tuple[TokenUsage, ...]:
    return () if payload is None else (TokenUsage(**payload),)


def _aggregate(usages) -> TokenUsage | None:
    values = tuple(usages)
    if not values:
        return None
    dimensions = ("uncached_input_tokens", "cached_input_tokens", "generated_tokens", "embedding_input_tokens")
    return TokenUsage(**{name: (sum(getattr(item, name) for item in values if getattr(item, name) is not None) if any(getattr(item, name) is not None for item in values) else None) for name in dimensions}, source="provider_reported")


def _can_record_failure(config: Config, operation) -> bool:
    """Never relabel a possibly committed generation as a failed operation."""
    if operation is None or operation.generation_id is None:
        return True
    try:
        snapshot = core.RuntimeSnapshotStore(config.layout).read()
        extension = core._pointer_extension(snapshot.payload) or {}
        return (extension.get("active_generation") != operation.generation_id
                and operation.generation_id not in extension.get("retained_generations", ()))
    except Exception:
        return False


class ManualRebuildObserver:
    """One manual operation's accounting at Core's A/C/D boundaries."""

    def __init__(self, config: Config, *, public_plan_id: str, replay_key: str,
                 ledger_upgrade: dict | None = None, budget_check_period: dict | None = None) -> None:
        self.config = config
        self.public_plan_id = public_plan_id
        self.replay_key = replay_key
        self.ledger_upgrade = ledger_upgrade
        self.budget_check_period = budget_check_period
        self.ledger = EmbeddingRebuildLedger(config.layout)
        self.policy = inspect_embedding_rebuild_policy(config)
        self.meter = None
        self.operation = None

    def attach_meter(self, meter) -> None:
        self.meter = meter

    def reserve(self, plan, inspection) -> str:
        policy = inspect_embedding_rebuild_policy(self.config)
        _require_policy(policy)
        if policy != self.policy:
            raise _error(ErrorCode.RUNTIME_PLAN_STALE, "手动重建预算策略已经变化。")
        period = _period(policy)
        if self.budget_check_period is not None and period != self.budget_check_period:
            raise _error(ErrorCode.RUNTIME_PLAN_STALE, "手动重建预算期间已经变化。")
        state = self.ledger.inspect()
        if state.ledger_upgrade != self.ledger_upgrade:
            if self.budget_check_period is None and state.ledger_upgrade is not None:
                raise _error(ErrorCode.EMBEDDING_LEDGER_UPGRADE_REQUIRED, "旧手动重建账本需要使用 v2 计划明确升级。")
            raise _error(ErrorCode.RUNTIME_PLAN_STALE, "手动重建账本升级材料已经变化。")
        if self.ledger_upgrade is not None:
            self.ledger.upgrade(self.ledger_upgrade)
        material = _material(inspection, policy)
        self.operation = self.ledger.begin_v3(
            plan_id=plan.plan_id, public_plan_id=self.public_plan_id, replay_key=self.replay_key,
            source_revision=inspection.source.digest, policy=policy, estimated_tokens=_estimate(inspection),
            data_root_identity_sha256=material["data_root_identity_sha256"], runtime_snapshot_sha256=material["runtime_snapshot_sha256"],
            contract_sha256=material["contract_sha256"], budget_check_period=period,
        )
        return self.operation.operation_id

    def stage_metadata(self, plan, inspection) -> dict:
        return {"public_plan_id": self.public_plan_id, "source_revision": inspection.source.digest,
                "data_root_identity_sha256": core._data_root_identity_sha256(self.config),
                "runtime_snapshot_sha256": inspection._snapshot.raw_sha256, "contract_sha256": _contract_sha(inspection)}

    def stage_allocated(self, operation_id, grant) -> None:
        self.ledger.bind_stage(operation_id, stage_key=grant.stage_key, grant_sha256=grant.grant_sha256)
        self.ledger.mark_provider_unknown(operation_id)

    def seal_stage(self, operation_id, grant, generation_id, manifest_sha):
        operation = self.operation
        if operation is None or operation.operation_id != operation_id or operation.reservation is None:
            raise _error(ErrorCode.REPAIR_REQUIRED, "手动重建预留无法关联封存证据。")
        calls = getattr(self.meter, "provider_call_count", 0)
        usages = tuple(getattr(self.meter, "provider_usages", ()))
        aggregate = _aggregate(usages)
        complete = len(usages) == calls and getattr(self.meter, "provider_usage_complete", False)
        completeness = "unknown" if aggregate is None else ("complete" if complete else "partial")
        try:
            return grant.seal_publish_ready(generation_id=generation_id, manifest_sha256=manifest_sha,
                policy_fingerprint=operation.policy_fingerprint, reservation_id=operation.reservation.reservation_id,
                provider_call_count=calls, provider_usage=aggregate.to_dict() if aggregate else None, usage_completeness=completeness)
        except Exception:
            # C has no root writer capability.  A seal may already be durable
            # despite a lost response; recover only by verifying that exact
            # original ledger-selected stage, never by synthesizing a proof.
            recovered = None
            try:
                observed = self.ledger.get_operation(operation_id)
                recovered = _proof_for(self.config, observed) if observed else None
            except Exception:
                pass
            if recovered is not None and recovered.generation_id == generation_id and recovered.manifest_sha256 == manifest_sha:
                return recovered
            raise

    def adopt_stage(self, operation_id, proof) -> None:
        self.ledger.adopt_proof(operation_id, proof_sha256=proof.proof_sha256, generation_id=proof.generation_id,
            provider_usages=_usage(proof.payload["provider_usage"]), usage_completeness=proof.payload["usage_completeness"])

    def mark_activation_intent(self, operation_id, generation_id) -> None:
        self.ledger.mark_activation_intent(operation_id, generation_id=generation_id)

    def mark_completed(self, operation_id) -> None:
        self.ledger.mark_completed(operation_id, provider_attempted=True)

    def mark_failed(self, operation_id, error) -> None:
        if not _can_record_failure(self.config, self.ledger.get_operation(operation_id)):
            return
        attempted = bool(getattr(self.meter, "provider_attempted", False))
        self.ledger.mark_failed(operation_id, error_code=error.code.value,
            provider_usages=tuple(getattr(self.meter, "provider_usages", ())), provider_attempted=attempted, provider_not_started=not attempted)


def _execution(result: core.EmbeddingRebuildExecution, config: Config, *, provider_called: bool) -> _Handle:
    issues = []
    if result.audit_completion_pending:
        issues.append(_issue(ErrorCode.AUDIT_COMPLETION_PENDING, "Embedding 已发布，完成审计仍待对齐。"))
    if result.ledger_completion_pending:
        issues.append(_issue(ErrorCode.REPAIR_REQUIRED, "Embedding 已发布，完成账本仍待对齐。"))
    return _Handle({"schema": "pkv.embedding.execution.v2", "operation_id": result.operation_id, "generation_id": result.generation_id,
                    "previous_generation_id": result.previous_generation, "status": "ready", "provider_called": provider_called,
                    "publication_committed": True, "ledger_completion_pending": result.ledger_completion_pending,
                    "audit_completion_pending": result.audit_completion_pending, "issues": issues}, config)


def execute_rebuild(plan: _Handle, *, embedder_factory: Callable | None = None) -> _Handle:
    if not isinstance(plan, _Handle) or plan._payload.get("schema") != "pkv.embedding.plan.v2":
        raise TypeError("plan 必须由 plan_rebuild 返回")
    current = plan_rebuild(inspect_rebuild(plan._config), force=plan._payload["force"])
    if current._payload["plan_id"] != plan._payload["plan_id"]:
        raise _error(ErrorCode.RUNTIME_PLAN_STALE, "手动重建计划材料或预算期间已经变化。")
    inspection = plan._payload["inspection"]
    observer = ManualRebuildObserver(plan._config, public_plan_id=plan._payload["plan_id"], replay_key=plan._replay_key,
        ledger_upgrade=inspection["ledger_upgrade"], budget_check_period=inspection["budget_check_period"])
    def factory():
        meter = _Meter(create_embedder(plan._config) if embedder_factory is None else embedder_factory())
        observer.attach_meter(meter)
        return meter
    result = core.execute_embedding_rebuild(plan._core, core.confirm_embedding_rebuild(plan._core, allow_network=True),
        embedder_factory=factory, execution_observer=observer)
    return _execution(result, plan._config, provider_called=bool(getattr(observer.meter, "provider_attempted", False)))


def _read_proof_for(config: Config, operation, *, allow_missing: bool = False) -> ValidatedStageProof | None:
    if operation.stage_key is None:
        return None
    path = config.layout.vector_index_dir / "staging" / operation.stage_key / PROOF_NAME
    config.layout.validate_user_file(path, label="Embedding 封存证据", allow_missing=True)
    try:
        os.lstat(path)
    except FileNotFoundError:
        if not allow_missing and (operation.proof_sha256 is not None or operation.state in {EmbeddingRebuildLedgerState.AWAITING_PUBLISH, EmbeddingRebuildLedgerState.ACTIVATION_INTENT, EmbeddingRebuildLedgerState.COMPLETED}):
            raise _error(ErrorCode.REPAIR_REQUIRED, "已封存的 Embedding 证据缺失。")
        return None
    if operation.reservation is None:
        raise _error(ErrorCode.REPAIR_REQUIRED, "Embedding 封存证据缺少原预留。")
    expected = {name: getattr(operation, name) for name in ("operation_id", "plan_id", "public_plan_id", "source_revision", "data_root_identity_sha256", "runtime_snapshot_sha256", "contract_sha256", "policy_fingerprint", "grant_sha256", "proof_sha256")}
    expected["reservation_id"] = operation.reservation.reservation_id
    if operation.generation_id is not None:
        expected["generation_id"] = operation.generation_id
    proof = validate_publish_ready(config.layout, stage_key=operation.stage_key, expected=expected)
    manifest, _ = core._read_json_file(config.layout, proof.stage_dir / core._GENERATION_MANIFEST_NAME, label="Embedding 封存清单")
    contract, source = core._parse_contract(manifest.get("contract")), core._parse_source_summary(manifest.get("source"))
    if contract is None or source is None or source.digest != operation.source_revision:
        raise _error(ErrorCode.REPAIR_REQUIRED, "Embedding 封存来源无法确认。")
    contract_sha = _hash("pkv.embedding.contract.v2", {"provider": contract.provider, "model": contract.model,
        "dimension": contract.dimension, "endpoint_fingerprint": contract.endpoint_sha256})
    if contract_sha != operation.contract_sha256:
        raise _error(ErrorCode.REPAIR_REQUIRED, "Embedding 封存 contract 与原授权不一致。")
    # Strict artifact opening uses the captured generation's contract; current
    # configuration drift is a separate publication gate below.
    core._validate_staged_generation(config, proof.stage_dir, expected_generation_id=proof.generation_id,
        expected_manifest_sha=proof.manifest_sha256, expected_contract=contract, expected_source=source)
    return proof


def _proof_for(config: Config, operation, *, allow_missing: bool = False) -> ValidatedStageProof | None:
    try:
        return _read_proof_for(config, operation, allow_missing=allow_missing)
    except Exception:
        raise _error(ErrorCode.REPAIR_REQUIRED, "Embedding 封存证据无法安全验证。") from None


def _inspect_operation(operation_id: str, *, config: Config | None = None) -> _Handle:
    resolved = _config(config)
    ledger = EmbeddingRebuildLedger(resolved.layout).inspect()
    operation = next((item for item in ledger.operations if item.operation_id == operation_id), None)
    if operation is None:
        raise _error(ErrorCode.EMBEDDING_OPERATION_NOT_FOUND, "当前数据根不存在该手动重建操作。")
    current = core.inspect_embedding_index(resolved, source=core.SQLiteEmbeddingSource())
    policy = inspect_embedding_rebuild_policy(resolved)
    material = _material(current, policy)
    proof = _proof_for(resolved, operation, allow_missing=True)
    generation = proof.generation_id if proof is not None else operation.generation_id
    manifest_sha = proof.manifest_sha256 if proof is not None else None
    committed = False
    audit_done = False
    issues = []
    if proof is not None:
        # A malformed suffix must not be hidden merely because CAS has not yet
        # happened; publication cannot append onto an uncertain audit history.
        audit_done = inspect_completion(resolved.layout, operation_id=operation_id, generation_id=generation,
            manifest_sha256=manifest_sha, publication_verified=False)
    root_matches = operation.data_root_identity_sha256 in (None, material["data_root_identity_sha256"])
    if generation is not None and root_matches:
        extension = core._pointer_extension(current._snapshot.payload) or {}
        active = current.active_generation == generation
        retained = generation in extension.get("retained_generations", ())
        if active or retained:
            manifest, actual_sha = core._validate_manifest(resolved, generation)
            if operation.record_origin == "native_v3":
                manifest_contract = core._parse_contract(manifest.get("contract"))
                manifest_source = core._parse_source_summary(manifest.get("source"))
                if manifest_contract is None or manifest_source is None:
                    raise _error(ErrorCode.REPAIR_REQUIRED, "已发布 generation 缺少原授权绑定。")
                contract_sha = _hash("pkv.embedding.contract.v2", {"provider": manifest_contract.provider, "model": manifest_contract.model,
                    "dimension": manifest_contract.dimension, "endpoint_fingerprint": manifest_contract.endpoint_sha256})
                if contract_sha != operation.contract_sha256 or manifest_source.digest != operation.source_revision:
                    raise _error(ErrorCode.REPAIR_REQUIRED, "已发布 generation 与原授权绑定冲突。")
            if manifest_sha is not None and actual_sha != manifest_sha:
                raise _error(ErrorCode.REPAIR_REQUIRED, "已发布 generation 与封存摘要冲突。")
            manifest_sha = actual_sha
            if active and current.active_manifest_sha256 != actual_sha:
                raise _error(ErrorCode.REPAIR_REQUIRED, "已发布 generation 与当前指针摘要冲突。")
            audit_done = inspect_completion(resolved.layout, operation_id=operation_id, generation_id=generation,
                manifest_sha256=actual_sha, publication_verified=active or retained)
            committed = active or (retained and audit_done)
    if (proof is None and operation.record_origin == "native_v3" and not committed
            and (operation.proof_sha256 is not None or operation.state in {EmbeddingRebuildLedgerState.AWAITING_PUBLISH, EmbeddingRebuildLedgerState.ACTIVATION_INTENT, EmbeddingRebuildLedgerState.COMPLETED})):
        raise _error(ErrorCode.REPAIR_REQUIRED, "缺少封存证据且无法确认原操作已经发布。")
    effective = operation.state.value
    effect = operation.provider_effect
    publishable = False
    if proof is not None:
        effect = "completed"
        if effective not in {"failed", "completed", "repair_required"}:
            effective = "awaiting_publish"
        publishable = all((root_matches, operation.runtime_snapshot_sha256 == material["runtime_snapshot_sha256"],
            operation.source_revision == current.source.digest, operation.contract_sha256 == material["contract_sha256"],
            operation.policy_fingerprint == policy.policy_fingerprint, current.state is not core.EmbeddingIndexState.REPAIR_REQUIRED))
        if not committed and not publishable:
            issues.append(_issue(ErrorCode.RUNTIME_PLAN_STALE, "封存工作的根、源、配置或指针已经变化。"))
    if committed:
        effective = "completed"
    if effective == "repair_required":
        issues.append(_issue(ErrorCode.REPAIR_REQUIRED, "该手动重建操作需要独立修复。"))
    reservation = operation.reservation.to_dict() if operation.reservation else None
    if reservation is not None:
        reservation.pop("reservation_id")
    ledger_pending = committed and (operation.state is not EmbeddingRebuildLedgerState.COMPLETED or operation.reservation is None or operation.reservation.state != "settled")
    payload = {"schema": "pkv.embedding.operation.v2", "operation_id": operation_id, "effective_state": effective,
        "provider_effect": effect, "generation_id": generation, "original_plan_id": operation.public_plan_id,
        "data_root_identity_sha256": material["data_root_identity_sha256"], "runtime_snapshot_sha256": material["runtime_snapshot_sha256"],
        "proof_valid": proof is not None, "proof_sha256": proof.proof_sha256 if proof else None, "reservation": reservation,
        "local_estimate": operation.local_estimate.to_dict() if operation.local_estimate else None,
        "provider_usage": proof.payload["provider_usage"] if proof else (operation.provider_usage.to_dict() if operation.provider_usage else None),
        "publication_committed": committed, "ledger_completion_pending": ledger_pending, "audit_completion_pending": committed and not audit_done,
        "ledger_schema_version": ledger.schema_version, "ledger_upgrade": ledger.ledger_upgrade, "issues": issues}
    return _Handle(payload, resolved, _core=current, _policy=policy, _ledger=ledger, _operation=operation,
        _proof=proof, _material=material, _manifest_sha=manifest_sha, _publishable=publishable)


def inspect_operation(operation_id: str, *, config: Config | None = None) -> _Handle:
    try:
        return _inspect_operation(operation_id, config=config)
    except PKVRuntimeError as error:
        if error.code is ErrorCode.EMBEDDING_OPERATION_NOT_FOUND:
            raise
        code = ErrorCode.RUNTIME_PLAN_STALE if error.code is ErrorCode.RUNTIME_PLAN_STALE else ErrorCode.REPAIR_REQUIRED
        raise _error(code, "Embedding 操作的持久事实无法安全确认。") from None
    except (OSError, ValueError, TypeError):
        raise _error(ErrorCode.REPAIR_REQUIRED, "Embedding 操作的持久事实无法安全确认。") from None


def plan_publication(inspection: _Handle) -> _Handle:
    if not isinstance(inspection, _Handle) or inspection._payload.get("schema") != "pkv.embedding.operation.v2":
        raise TypeError("inspection 必须由 inspect_operation 返回")
    value = inspection._payload
    if value["publication_committed"]:
        if not value["ledger_completion_pending"] and not value["audit_completion_pending"]:
            raise _error(ErrorCode.EMBEDDING_OPERATION_COMPLETED, "该手动重建操作已经完成。",
                details={"operation_id": value["operation_id"], "effective_state": "completed"})
        action = "reconcile_completion"
    elif value["proof_valid"] and inspection._publishable and value["effective_state"] in {"awaiting_publish", "activation_intent"}:
        action = "publish_only"
    else:
        code = ErrorCode.RUNTIME_PLAN_STALE if value["proof_valid"] and not inspection._publishable else ErrorCode.REPAIR_REQUIRED
        raise _error(code, "当前操作没有可安全继续发布的确认材料。")
    identity = {"action": action, "operation_id": value["operation_id"], "original_public_plan_id": value["original_plan_id"],
        "ledger_upgrade": value["ledger_upgrade"], "proof_sha256": value["proof_sha256"], "generation_id": value["generation_id"],
        "material": inspection._material, "publication_committed": value["publication_committed"],
        "ledger_completion_pending": value["ledger_completion_pending"], "audit_completion_pending": value["audit_completion_pending"]}
    return _Handle({"schema": "pkv.embedding.publication-plan.v2", "plan_id": _hash("pkv.embedding.publication-plan.v2", identity),
        "action": action, "operation_id": value["operation_id"], "generation_id": value["generation_id"], "operation_inspection": inspection.to_dict(),
        "ledger_upgrade": value["ledger_upgrade"], "requires_confirmation": True, "requires_network": False, "creates_reservation": False},
        inspection._config, _core=inspection._core, _policy=inspection._policy, _ledger=inspection._ledger,
        _operation=inspection._operation, _proof=inspection._proof, _material=inspection._material, _manifest_sha=inspection._manifest_sha, _publishable=inspection._publishable)


def _pending(operation_id: str) -> PKVRuntimeError:
    return _error(ErrorCode.EMBEDDING_PUBLICATION_PENDING, "Embedding 工作已封存，等待显式继续发布。",
        details={"operation_id": operation_id, "effective_state": "awaiting_publish", "provider_effect": "completed", "publish_only_required": True})


def _align_completed(config, current, *, operation_id, generation_id, manifest_sha, previous_generation, observer) -> core.EmbeddingRebuildExecution:
    audit_pending = ledger_pending = False
    try:
        append_completion(config.layout, operation_id=operation_id, generation_id=generation_id, manifest_sha256=manifest_sha,
            publication_verified=True, context={"operation_id": operation_id, "embedding_contract": current.contract.to_dict()},
            secret_values=core._audit_secret_values(config), previous_generation=previous_generation)
    except Exception:
        # Publication is already committed.  An uncertain audit read or a
        # failed append must never escape into Core's pre-commit failure path.
        audit_pending = True
    try:
        observer.mark_completed(operation_id)
    except Exception:
        ledger_pending = True
    return core.EmbeddingRebuildExecution(current, generation_id, previous_generation, operation_id=operation_id,
        audit_completion_pending=audit_pending, ledger_completion_pending=ledger_pending)


def _publish_stage_impl(config, current, proof, observer) -> core.EmbeddingRebuildExecution:
    operation_id = proof.payload["operation_id"]
    operation = observer.ledger.get_operation(operation_id)
    fresh_proof = _proof_for(config, operation) if operation is not None else None
    if fresh_proof is None or fresh_proof.proof_sha256 != proof.proof_sha256:
        raise _error(ErrorCode.REPAIR_REQUIRED, "Embedding 封存证据在采用前已经变化。")
    proof = fresh_proof
    observer.adopt_stage(operation_id, proof)
    generation_id, manifest_sha = core._publish_generation_directory(config, proof.stage_dir,
        expected_generation_id=proof.generation_id, expected_manifest_sha=proof.manifest_sha256,
        expected_contract=current.contract, expected_source=current.source, preserve_stage=True)
    AuditTrace(config.layout, secret_values=core._audit_secret_values(config)).append({"operation": "embedding_rebuild", "phase": "activation_intent",
        "operation_id": operation_id, "plan_id": proof.payload["plan_id"], "generation_id": generation_id,
        "previous_generation": current.active_generation, "manifest_sha256": manifest_sha})
    observer.mark_activation_intent(operation_id, generation_id)
    try:
        snapshot = core._pointer_update(config, current._snapshot, contract=current.contract, source_digest=current.source.digest,
            generation_id=generation_id, previous_generation=current.active_generation, manifest_sha=manifest_sha)
    except Exception:
        # A durable CAS may have succeeded before its response was lost.  Read
        # back its complete facts; never repeat that CAS to manufacture success.
        observed = core.inspect_embedding_index(config, source=current._source)
        if (observed.active_generation != generation_id or observed.active_manifest_sha256 != manifest_sha
                or observed.state is not core.EmbeddingIndexState.READY or observed.source != current.source
                or observed.contract != current.contract):
            raise
        snapshot = observed._snapshot
    after = core._inspection(state=core.EmbeddingIndexState.READY, contract=current.contract, source=current._captured_source,
        active_generation=generation_id, previous_generation=current.active_generation, manifest_sha=manifest_sha, issues=(),
        config=config, source_reader=current._source, snapshot=snapshot, config_source_revision=current._config_source_revision)
    return _align_completed(config, after, operation_id=operation_id, generation_id=generation_id, manifest_sha=manifest_sha,
        previous_generation=current.active_generation, observer=observer)


def _publish_stage(config, current, proof, observer) -> core.EmbeddingRebuildExecution:
    try:
        return _publish_stage_impl(config, current, proof, observer)
    except PKVRuntimeError:
        raise
    except Exception:
        raise _error(ErrorCode.REPAIR_REQUIRED, "Embedding 发布无法安全完成，已保留原工作与费用事实。") from None


def finish_manual_publication(plan, current, stage_grant, proof, observer):
    """Core's D callback; Core already owns the lease and checked its own fence."""
    return _publish_stage(plan._config, current, proof, observer)


def execute_publication(plan: _Handle) -> _Handle:
    if not isinstance(plan, _Handle) or plan._payload.get("schema") != "pkv.embedding.publication-plan.v2":
        raise TypeError("plan 必须由 plan_publication 返回")
    operation_id = plan._payload["operation_id"]
    try:
        with write_lease_scope(plan._config.layout):
            observer = ManualRebuildObserver(plan._config, public_plan_id=plan._operation.public_plan_id, replay_key=plan._operation.replay_key)
            inspected = None
            try:
                inspected = inspect_operation(operation_id, config=plan._config)
                fresh = plan_publication(inspected)
                if (fresh._payload["plan_id"] != plan._payload["plan_id"]
                        or inspected._core._config_source_revision != plan._core._config_source_revision):
                    raise _error(ErrorCode.RUNTIME_PLAN_STALE, "继续发布计划材料已经变化。")
            except PKVRuntimeError as error:
                # A failed publication gate is not permission to erase paid
                # work.  Preserve its original reservation conservatively.
                operation = observer.ledger.get_operation(operation_id)
                if (operation is not None and operation.state is not EmbeddingRebuildLedgerState.COMPLETED
                        and _can_record_failure(plan._config, operation)
                        and not (inspected is not None and inspected._payload["publication_committed"])):
                    try:
                        observer.ledger.mark_failed(operation_id, error_code=error.code.value, provider_attempted=True,
                            provider_usages=_usage(plan._proof.payload["provider_usage"]) if plan._proof else ())
                    except Exception:
                        pass
                raise
            if plan._payload["ledger_upgrade"] is not None:
                observer.ledger.upgrade(plan._payload["ledger_upgrade"])
            if fresh._payload["action"] == "publish_only":
                try:
                    result = _publish_stage(plan._config, inspected._core, inspected._proof, observer)
                except PKVRuntimeError as error:
                    try:
                        if _can_record_failure(plan._config, observer.ledger.get_operation(operation_id)):
                            observer.ledger.mark_failed(operation_id, error_code=error.code.value, provider_attempted=True,
                                provider_usages=_usage(inspected._proof.payload["provider_usage"]))
                    except Exception:
                        pass
                    raise
            else:
                if inspected._proof is not None:
                    observer.adopt_stage(operation_id, inspected._proof)
                result = _align_completed(plan._config, inspected._core, operation_id=operation_id,
                    generation_id=inspected._payload["generation_id"], manifest_sha=inspected._manifest_sha,
                    previous_generation=(inspected._core.previous_generation if inspected._core.active_generation == inspected._payload["generation_id"] else None), observer=observer)
            return _execution(result, plan._config, provider_called=False)
    except PKVRuntimeError as error:
        if error.code is ErrorCode.WRITE_BUSY and plan._payload["action"] == "publish_only":
            raise _pending(operation_id) from None
        raise
