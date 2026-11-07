"""Public, confirmation-bound Embedding generation rebuild façade.

External wrappers use this module instead of importing the R4 Core lifecycle,
Storage, Provider factory, or writer lease directly.  It keeps the familiar
``inspect -> plan -> confirm -> execute`` contract and composes the production
Embedding Provider only after Core preflight has reserved the independent
foreground-rebuild budget and allocated its fenced private stage.

The opaque handles are intentionally process-local.  ``to_dict`` returns safe
display payloads only; it never exposes Config, paths, source bodies, Provider
objects, lease objects, or durable private accounting identities.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from collections.abc import Iterable
from typing import Any

from src.ai.provider_factory import create_embedder
from src.runtime.ai_automation_policy import TokenUsage
from src.runtime.embedding_lifecycle import (
    EmbeddingIndexInspection as _CoreInspection,
    EmbeddingRebuildConfirmation as _CoreConfirmation,
    EmbeddingRebuildExecution as _CoreExecution,
    EmbeddingRebuildPlan as _CorePlan,
    PreChunkedEmbeddingAdapter,
    SQLiteEmbeddingSource,
    _data_root_identity_sha256,
    confirm_embedding_rebuild as _confirm_core,
    execute_embedding_rebuild as _execute_core,
    inspect_embedding_index as _inspect_core,
    plan_embedding_rebuild as _plan_core,
)
from src.runtime.embedding_rebuild_ledger import (
    EmbeddingRebuildLedger,
    EmbeddingRebuildLedgerOperation,
)
from src.runtime.embedding_rebuild_policy import (
    EmbeddingRebuildPolicyInspection,
    inspect_embedding_rebuild_policy,
)
from src.runtime.errors import ErrorCode, PKVRuntimeError
from src.utils.config import Config, get_config


__all__ = [
    "EmbeddingRebuildConfirmation",
    "EmbeddingRebuildExecution",
    "EmbeddingRebuildInspection",
    "EmbeddingRebuildPlan",
    "confirm_embedding_rebuild",
    "execute_embedding_rebuild",
    "inspect_embedding_rebuild",
    "plan_embedding_rebuild",
    "EmbeddingRebuildInspectionV2",
    "EmbeddingRebuildPlanV2",
    "EmbeddingRebuildConfirmationV2",
    "EmbeddingRebuildExecutionV2",
    "EmbeddingOperationInspectionV2",
    "EmbeddingPublicationPlanV2",
    "EmbeddingPublicationConfirmationV2",
    "inspect_embedding_rebuild_v2",
    "plan_embedding_rebuild_v2",
    "confirm_embedding_rebuild_v2",
    "execute_embedding_rebuild_v2",
    "inspect_embedding_operation_v2",
    "plan_embedding_publication_v2",
    "confirm_embedding_publication_v2",
    "execute_embedding_publication_v2",
]


_HANDLE_TOKEN = object()


def _safe_payload(value: Any) -> dict[str, object]:
    payload = value.to_dict()
    if not isinstance(payload, dict):
        raise TypeError("Core Embedding rebuild lifecycle returned a non-mapping payload")
    return copy.deepcopy(payload)


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _public_inspection_material(
    inner: _CorePlan,
    policy: EmbeddingRebuildPolicyInspection,
) -> dict[str, object]:
    """Keep the original v1 material used by durable replay accounting.

    Core plans intentionally contain a process-private Config-source HMAC.  It
    is the right fence for an in-process Core execution, but it cannot be part
    of a public CLI/MCP confirmation that a user carries into another process.
    The process-private revision cannot participate in cross-process identity.
    Root and full snapshot binding are added separately for public confirmation;
    this original material remains stable for already-persisted v1 replay keys.
    """

    inspection = _safe_payload(inner.inspection)
    # ``revision`` includes the process-private HMAC of editable user config.
    # It is safe to display, but deliberately not stable across process
    # boundaries and therefore must not participate in a public confirmation.
    inspection.pop("revision", None)

    return {
        "inspection": inspection,
        "policy": policy.to_dict(),
        "action": "kernel_embedding_rebuild_v1",
    }


def _public_identity_material(
    inner: _CorePlan,
    policy: EmbeddingRebuildPolicyInspection,
) -> dict[str, object]:
    """Bind confirmation to its root and exact validated runtime snapshot.

    Core captured this snapshot through RuntimeSnapshotStore's secret-free read
    before creating the plan.  Use that same raw-byte CAS revision, not a new
    read or a hash of editable user configuration.  These stable facts survive
    a process restart while the private Config-source HMAC deliberately does not.
    """

    snapshot = inner.inspection._snapshot
    if snapshot is None or not snapshot.exists:
        raise PKVRuntimeError(
            ErrorCode.REPAIR_REQUIRED,
            "Embedding rebuild 计划缺少可验证的运行态快照。",
            stage="embedding_plan",
            recoverable=True,
        )
    return {
        **_public_inspection_material(inner, policy),
        "data_root_identity_sha256": _data_root_identity_sha256(inner._config),
        "runtime_snapshot_sha256": snapshot.raw_sha256,
    }


def _public_plan_id(
    inner: _CorePlan,
    policy: EmbeddingRebuildPolicyInspection,
) -> str:
    """Return the externally confirmed identity, including the force decision."""

    return _canonical_sha256(
        {
            **_public_identity_material(inner, policy),
            "force": inner.force,
        }
    )


def _public_replay_key(
    inner: _CorePlan,
    policy: EmbeddingRebuildPolicyInspection,
) -> str:
    """Fence concurrent work on the same material generation transition.

    ``force`` affects confirmation semantics and remains in the public plan ID,
    but it must not let a force/non-force race send the same source to a
    Provider twice before the pointer CAS can reject the loser.
    """

    # Strengthening confirmation must not silently change the keys of existing
    # processing operations and admit a second paid attempt.  The root already
    # scopes this v1 ledger; retaining its snapshot-independent key is conservative.
    # A new replay identity/ledger version belongs to the explicit v2 upgrade.
    return _canonical_sha256(_public_inspection_material(inner, policy))


def _require_config(config: Config | None) -> Config:
    if config is None:
        resolved = get_config()
    else:
        resolved = config
    if not isinstance(resolved, Config):
        raise TypeError("config 必须是 pkv_kernel.Config 或 None")
    return resolved


def _public_ledger_payload(
    operation: EmbeddingRebuildLedgerOperation | None,
) -> dict[str, object] | None:
    """Project private durable accounting onto a body/path-free public fact set."""

    if operation is None:
        return None
    reservation = operation.reservation.to_dict() if operation.reservation else None
    if reservation is not None:
        # Reservation identity is durable accounting internals, not a public
        # resume/cancel capability.  The settled state, periods and amounts are
        # sufficient for the observable maintenance result.
        reservation.pop("reservation_id", None)
    return {
        "state": operation.state.value,
        "reservation": reservation,
        "local_estimate": (
            operation.local_estimate.to_dict()
            if operation.local_estimate is not None
            else None
        ),
        "provider_usage": (
            operation.provider_usage.to_dict()
            if operation.provider_usage is not None
            else None
        ),
        "generation_id": operation.generation_id,
        "last_error_code": operation.last_error_code,
        "provider_attempted": operation.provider_attempted,
    }


class EmbeddingRebuildInspection:
    """Opaque, zero-write inspection of the index and manual policy."""

    __slots__ = ("__inner", "__config", "__policy")

    def __init__(
        self,
        inner: _CoreInspection,
        config: Config,
        policy: EmbeddingRebuildPolicyInspection,
        *,
        _token: object | None = None,
    ) -> None:
        if _token is not _HANDLE_TOKEN:
            raise TypeError(
                "EmbeddingRebuildInspection 必须由 pkv_kernel.embedding.inspect_embedding_rebuild 返回"
            )
        self.__inner = inner
        self.__config = config
        self.__policy = policy

    @property
    def state(self) -> str:
        return str(_safe_payload(self.__inner)["state"])

    @property
    def revision(self) -> str:
        return str(_safe_payload(self.__inner)["revision"])

    def to_dict(self) -> dict[str, object]:
        return {
            "index": _safe_payload(self.__inner),
            "policy": self.__policy.to_dict(),
        }

    def __repr__(self) -> str:
        return f"EmbeddingRebuildInspection({self.to_dict()!r})"


class EmbeddingRebuildPlan:
    """Opaque, process-local foreground rebuild plan."""

    __slots__ = (
        "__inner",
        "__config",
        "__policy",
        "__public_plan_id",
        "__public_replay_key",
    )

    def __init__(
        self,
        inner: _CorePlan,
        config: Config,
        policy: EmbeddingRebuildPolicyInspection,
        *,
        _token: object | None = None,
    ) -> None:
        if _token is not _HANDLE_TOKEN:
            raise TypeError(
                "EmbeddingRebuildPlan 必须由 pkv_kernel.embedding.plan_embedding_rebuild 返回"
            )
        self.__inner = inner
        self.__config = config
        self.__policy = policy
        self.__public_plan_id = _public_plan_id(inner, policy)
        self.__public_replay_key = _public_replay_key(inner, policy)

    @property
    def plan_id(self) -> str:
        return self.__public_plan_id

    @property
    def force(self) -> bool:
        return bool(_safe_payload(self.__inner)["force"])

    @property
    def inspection(self) -> EmbeddingRebuildInspection:
        return EmbeddingRebuildInspection(
            self.__inner.inspection,
            self.__config,
            self.__policy,
            _token=_HANDLE_TOKEN,
        )

    def to_dict(self) -> dict[str, object]:
        payload = _safe_payload(self.__inner)
        payload["plan_id"] = self.__public_plan_id
        payload["policy"] = self.__policy.to_dict()
        return payload

    def __repr__(self) -> str:
        return f"EmbeddingRebuildPlan({self.to_dict()!r})"


class EmbeddingRebuildConfirmation:
    """Opaque explicit confirmation for a single public rebuild plan."""

    __slots__ = ("__inner", "__public_plan_id")

    def __init__(
        self,
        inner: _CoreConfirmation,
        public_plan_id: str,
        *,
        _token: object | None = None,
    ) -> None:
        if _token is not _HANDLE_TOKEN:
            raise TypeError(
                "EmbeddingRebuildConfirmation 必须由 pkv_kernel.embedding.confirm_embedding_rebuild 返回"
        )
        self.__inner = inner
        self.__public_plan_id = public_plan_id

    @property
    def plan_id(self) -> str:
        return self.__public_plan_id

    def to_dict(self) -> dict[str, object]:
        return {
            "plan_id": self.__public_plan_id,
            "approved": self.__inner.approved,
            "allow_network": self.__inner.allow_network,
        }

    def __repr__(self) -> str:
        return f"EmbeddingRebuildConfirmation({self.to_dict()!r})"


class EmbeddingRebuildExecution:
    """Safe public result of an activated Embedding generation."""

    __slots__ = ("__inner", "__ledger")

    def __init__(
        self,
        inner: _CoreExecution,
        ledger: EmbeddingRebuildLedger,
        *,
        _token: object | None = None,
    ) -> None:
        if _token is not _HANDLE_TOKEN:
            raise TypeError(
                "EmbeddingRebuildExecution 必须由 pkv_kernel.embedding.execute_embedding_rebuild 返回"
            )
        self.__inner = inner
        self.__ledger = ledger

    @property
    def operation_id(self) -> str:
        return self.__inner.operation_id

    @property
    def generation_id(self) -> str:
        return self.__inner.generation_id

    def to_dict(self) -> dict[str, object]:
        payload = _safe_payload(self.__inner)
        payload["ledger"] = _public_ledger_payload(
            self.__ledger.get_operation(self.__inner.operation_id)
        )
        return payload

    def __repr__(self) -> str:
        return f"EmbeddingRebuildExecution({self.to_dict()!r})"


def _estimated_embedding_tokens(inspection: _CoreInspection) -> int:
    """Return the explicit local, price-free reservation estimate for one capture."""

    captured = inspection._captured_source
    if captured is None:
        raise PKVRuntimeError(
            ErrorCode.REPAIR_REQUIRED,
            "Embedding rebuild 检查缺少可验证的源捕获。",
            stage="embedding_rebuild_ledger",
            recoverable=True,
        )
    characters = sum(
        len(record.content) + sum(len(chunk) for _, chunk in record.chunks)
        for record in captured.records
    )
    return math.ceil(characters / 4)


class _MeteredPreChunkedEmbeddingAdapter:
    """Capture only reported Provider usage around the approved Core adapter."""

    def __init__(self, provider_embedder: Any) -> None:
        self._adapter = PreChunkedEmbeddingAdapter(provider_embedder)
        self._client = getattr(provider_embedder, "client", None)
        self._provider_attempted = False
        self._provider_call_count = 0
        self._provider_usages: list[TokenUsage] = []
        self._provider_usage_complete = True

    @property
    def provider_attempted(self) -> bool:
        return self._provider_attempted

    @property
    def provider_usages(self) -> tuple[TokenUsage, ...]:
        return tuple(self._provider_usages)

    @property
    def provider_call_count(self) -> int:
        return self._provider_call_count

    @property
    def provider_usage_complete(self) -> bool:
        return self._provider_usage_complete

    def _capture_usage(self) -> None:
        try:
            usage = getattr(self._client, "last_usage", None)
        except Exception:
            usage = None
        try:
            complete = getattr(self._client, "last_usage_complete", None)
        except Exception:
            complete = None
        if isinstance(usage, TokenUsage) and usage.source == "provider_reported":
            self._provider_usages.append(usage)
        else:
            self._provider_usage_complete = False
        if complete is not True:
            self._provider_usage_complete = False

    def embed_document(self, text: str):
        self._provider_attempted = True
        self._provider_call_count += 1
        try:
            return self._adapter.embed_document(text)
        finally:
            self._capture_usage()

    def embed_stored_chunks(self, chunks: Iterable[str]):
        self._provider_attempted = True
        self._provider_call_count += 1
        try:
            return self._adapter.embed_stored_chunks(tuple(chunks))
        finally:
            self._capture_usage()


def inspect_embedding_rebuild(config: Config | None = None) -> EmbeddingRebuildInspection:
    """Inspect index/binding and independent policy without writes or Provider use."""

    resolved = _require_config(config)
    return EmbeddingRebuildInspection(
        _inspect_core(resolved, source=SQLiteEmbeddingSource()),
        resolved,
        inspect_embedding_rebuild_policy(resolved),
        _token=_HANDLE_TOKEN,
    )


def plan_embedding_rebuild(
    inspection: EmbeddingRebuildInspection,
    *,
    force: bool = False,
) -> EmbeddingRebuildPlan:
    """Create a public rebuild plan; planning never constructs a Provider."""

    if not isinstance(inspection, EmbeddingRebuildInspection):
        raise TypeError(
            "inspection 必须由 pkv_kernel.embedding.inspect_embedding_rebuild 返回"
        )
    if type(force) is not bool:
        raise TypeError("force 必须是 bool")
    return EmbeddingRebuildPlan(
        _plan_core(inspection._EmbeddingRebuildInspection__inner, force=force),
        inspection._EmbeddingRebuildInspection__config,
        inspection._EmbeddingRebuildInspection__policy,
        _token=_HANDLE_TOKEN,
    )


def confirm_embedding_rebuild(
    plan: EmbeddingRebuildPlan,
    *,
    approved: bool = True,
    allow_network: bool = False,
) -> EmbeddingRebuildConfirmation:
    """Create explicit one-shot write and network confirmation for a plan."""

    if not isinstance(plan, EmbeddingRebuildPlan):
        raise TypeError("plan 必须由 pkv_kernel.embedding.plan_embedding_rebuild 返回")
    if type(approved) is not bool or type(allow_network) is not bool:
        raise TypeError("approved 与 allow_network 必须是 bool")
    return EmbeddingRebuildConfirmation(
        _confirm_core(
            plan._EmbeddingRebuildPlan__inner,
            approved=approved,
            allow_network=allow_network,
        ),
        plan.plan_id,
        _token=_HANDLE_TOKEN,
    )


def execute_embedding_rebuild(
    plan: EmbeddingRebuildPlan,
    confirmation: EmbeddingRebuildConfirmation | None = None,
) -> EmbeddingRebuildExecution:
    """Execute a confirmed plan through the formal production Provider interface.

    There are deliberately no Provider, fake, path, storage, or lease injection
    parameters on this public surface.  The only construction point is a Core
    ``embedder_factory`` invoked after the manual policy ledger has reserved
    capacity and after the root lease has been released.
    """

    if not isinstance(plan, EmbeddingRebuildPlan):
        raise TypeError("plan 必须由 pkv_kernel.embedding.plan_embedding_rebuild 返回")
    if confirmation is not None and not isinstance(
        confirmation,
        EmbeddingRebuildConfirmation,
    ):
        raise TypeError(
            "confirmation 必须由 pkv_kernel.embedding.confirm_embedding_rebuild 返回"
        )
    if confirmation is not None and confirmation.plan_id != plan.plan_id:
        raise PKVRuntimeError(
            ErrorCode.RUNTIME_PLAN_STALE,
            "确认不属于当前 Embedding rebuild 计划。",
            stage="embedding_plan",
            recoverable=True,
        )
    config = plan._EmbeddingRebuildPlan__config
    from src.runtime.embedding_rebuild_v2 import ManualRebuildObserver

    observer = ManualRebuildObserver(
        config,
        public_plan_id=plan.plan_id,
        replay_key=plan._EmbeddingRebuildPlan__public_replay_key,
    )

    def create_metered_embedder() -> _MeteredPreChunkedEmbeddingAdapter:
        meter = _MeteredPreChunkedEmbeddingAdapter(create_embedder(config))
        observer.attach_meter(meter)
        return meter

    core_execution = _execute_core(
        plan._EmbeddingRebuildPlan__inner,
        (
            None
            if confirmation is None
            else confirmation._EmbeddingRebuildConfirmation__inner
        ),
        embedder_factory=create_metered_embedder,
        execution_observer=observer,
    )
    return EmbeddingRebuildExecution(
        core_execution,
        observer.ledger,
        _token=_HANDLE_TOKEN,
    )


# V2 display values are a closed boundary, separate from Core's private handles
# and from the preserved v1 DTOs.  An unexpected field is a contract failure,
# never an opportunity to serialize internal configuration or a stage path.
_V2_FIELDS = {
    "inspection": frozenset({
        "index", "data_root_identity_sha256", "runtime_snapshot_sha256", "policy",
        "budget_check_period", "ledger_schema_version", "ledger_upgrade",
    }),
    "plan": frozenset({
        "plan_id", "action", "force", "inspection", "estimated_embedding_tokens",
        "requires_confirmation", "requires_network",
    }),
    "confirmation": frozenset({"plan_id", "approved", "allow_network"}),
    "operation": frozenset({
        "operation_id", "effective_state", "provider_effect", "generation_id",
        "original_plan_id", "data_root_identity_sha256", "runtime_snapshot_sha256",
        "proof_valid", "proof_sha256", "reservation", "local_estimate", "provider_usage",
        "publication_committed", "ledger_completion_pending", "audit_completion_pending",
        "ledger_schema_version", "ledger_upgrade", "issues",
    }),
    "publication-plan": frozenset({
        "plan_id", "action", "operation_id", "generation_id", "operation_inspection",
        "ledger_upgrade", "requires_confirmation", "requires_network", "creates_reservation",
    }),
    "publication-confirmation": frozenset({"plan_id", "operation_id", "approved"}),
    "execution": frozenset({
        "operation_id", "generation_id", "previous_generation_id", "status", "provider_called",
        "publication_committed", "ledger_completion_pending", "audit_completion_pending", "issues",
    }),
}
_OPERATION_ID_V2 = re.compile(r"embedding-rebuild-[0-9a-f]{32}\Z")
_SHA256_V2 = re.compile(r"[0-9a-f]{64}\Z")
_GENERATION_ID_V2 = re.compile(r"g-[a-z0-9][a-z0-9-]{7,95}\Z")
_EFFECTIVE_STATES_V2 = frozenset({
    "processing", "awaiting_publish", "activation_intent", "completed", "failed",
    "budget_paused", "repair_required",
})


def _v2_invalid() -> None:
    raise ValueError("Core Embedding v2 returned an invalid public contract")


def _v2_object(value: object, fields: set[str] | frozenset[str]) -> dict[str, object]:
    if type(value) is not dict or set(value) != fields:
        _v2_invalid()
    return value


def _v2_bool(value: object) -> None:
    if type(value) is not bool:
        _v2_invalid()


def _v2_integer(value: object, *, nullable: bool = False, positive: bool = False) -> None:
    if value is None and nullable:
        return
    if type(value) is not int or value < (1 if positive else 0):
        _v2_invalid()


def _v2_text(value: object) -> None:
    if type(value) is not str or not value:
        _v2_invalid()


def _v2_digest(value: object, *, nullable: bool = False) -> None:
    if value is None and nullable:
        return
    if type(value) is not str or _SHA256_V2.fullmatch(value) is None:
        _v2_invalid()


def _v2_operation_id(value: object) -> None:
    if type(value) is not str or _OPERATION_ID_V2.fullmatch(value) is None:
        raise ValueError("operation_id 必须是有效的 Embedding operation 标识")


def _v2_generation(value: object, *, nullable: bool = False) -> None:
    if value is None and nullable:
        return
    if type(value) is not str or _GENERATION_ID_V2.fullmatch(value) is None:
        _v2_invalid()


def _v2_ledger_upgrade(value: object) -> None:
    if value is None:
        return
    item = _v2_object(value, {
        "from_schema", "to_schema", "source_sha256", "operation_count", "blocking_operation_count",
    })
    if type(item["from_schema"]) is not int or item["from_schema"] not in {1, 2}:
        _v2_invalid()
    if type(item["to_schema"]) is not int or item["to_schema"] != 3:
        _v2_invalid()
    _v2_digest(item["source_sha256"])
    _v2_integer(item["operation_count"])
    _v2_integer(item["blocking_operation_count"])
    if item["blocking_operation_count"] > item["operation_count"]:
        _v2_invalid()


def _v2_period(value: object) -> None:
    from datetime import date
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    item = _v2_object(value, {"timezone", "local_day", "local_month"})
    for field in item.values():
        _v2_text(field)
    try:
        ZoneInfo(item["timezone"])
        day = date.fromisoformat(item["local_day"])
    except (ValueError, ZoneInfoNotFoundError):
        _v2_invalid()
    if day.isoformat() != item["local_day"] or item["local_month"] != day.strftime("%Y-%m"):
        _v2_invalid()


def _v2_issues(value: object) -> None:
    if type(value) is not list:
        _v2_invalid()
    for item in value:
        if type(item) is not dict or set(item) not in (
            {"code", "message", "stage", "recoverable"},
            {"code", "message", "stage", "recoverable", "details"},
        ):
            _v2_invalid()
        for field in ("code", "message", "stage"):
            _v2_text(item[field])
        _v2_bool(item["recoverable"])
        # This pre-existing inspection finding is informational, not an error
        # enum member; it has no approved details extension.
        if item["code"] == "embedding_initial_generation_pending" and "details" not in item:
            continue
        try:
            code = ErrorCode(item["code"])
            PKVRuntimeError(
                code, item["message"], stage=item["stage"], recoverable=item["recoverable"],
                details=item.get("details"),
            )
        except (TypeError, ValueError):
            _v2_invalid()


def _v2_usage(value: object) -> None:
    if value is None:
        return
    item = _v2_object(value, {
        "uncached_input_tokens", "cached_input_tokens", "generated_tokens",
        "embedding_input_tokens", "source",
    })
    try:
        TokenUsage(**item)
    except (TypeError, ValueError):
        _v2_invalid()


def _v2_validate_payload(kind: str, value: object) -> dict[str, object]:
    item = _v2_object(value, _V2_FIELDS[kind] | {"schema"})
    if item["schema"] != f"pkv.embedding.{kind}.v2":
        _v2_invalid()
    for key in ("plan_id", "original_plan_id", "data_root_identity_sha256", "runtime_snapshot_sha256"):
        if key in item:
            # Schema-1 history never recorded a public plan ID.  Preserve that
            # absence rather than exposing its process-private Core plan hash.
            _v2_digest(item[key], nullable=key == "original_plan_id")
    for key in ("operation_id",):
        if key in item:
            _v2_operation_id(item[key])
    for key in ("force", "approved", "allow_network", "proof_valid", "provider_called",
                "publication_committed", "ledger_completion_pending", "audit_completion_pending"):
        if key in item:
            _v2_bool(item[key])
    for key in ("generation_id", "previous_generation_id"):
        if key in item:
            _v2_generation(item[key], nullable=kind == "operation" or key == "previous_generation_id")
    if "ledger_schema_version" in item:
        version = item["ledger_schema_version"]
        if version is not None and (type(version) is not int or version not in {1, 2, 3}):
            _v2_invalid()
    if "ledger_upgrade" in item:
        _v2_ledger_upgrade(item["ledger_upgrade"])
    if "issues" in item:
        _v2_issues(item["issues"])
    if kind == "inspection":
        index = _v2_object(item["index"], {
            "state", "source_digest", "source_count", "chunk_count", "active_generation_id",
            "previous_generation_id", "active_manifest_sha256", "contract", "issues",
        })
        if index["state"] not in {
            "ready", "initial_generation_pending", "rebuild_required", "processing", "retry_required",
            "budget_paused", "authorization_required", "repair_required",
        }:
            _v2_invalid()
        _v2_digest(index["source_digest"])
        _v2_integer(index["source_count"])
        _v2_integer(index["chunk_count"])
        _v2_generation(index["active_generation_id"], nullable=True)
        _v2_generation(index["previous_generation_id"], nullable=True)
        _v2_digest(index["active_manifest_sha256"], nullable=True)
        contract = _v2_object(index["contract"], {"provider", "model", "dimension", "endpoint_fingerprint"})
        _v2_text(contract["provider"])
        _v2_text(contract["model"])
        _v2_integer(contract["dimension"], positive=True)
        _v2_digest(contract["endpoint_fingerprint"])
        _v2_issues(index["issues"])
        policy = _v2_object(item["policy"], {
            "enabled", "authorization_mode", "timezone", "daily_embedding_token_limit",
            "monthly_embedding_token_limit", "fingerprint",
        })
        _v2_bool(policy["enabled"])
        _v2_text(policy["authorization_mode"])
        _v2_text(policy["timezone"])
        _v2_integer(policy["daily_embedding_token_limit"], nullable=True)
        _v2_integer(policy["monthly_embedding_token_limit"], nullable=True)
        _v2_digest(policy["fingerprint"])
        _v2_period(item["budget_check_period"])
        if policy["timezone"] != item["budget_check_period"]["timezone"]:
            _v2_invalid()
    elif kind == "plan":
        if item["action"] != "rebuild" or item["requires_confirmation"] is not True or item["requires_network"] is not True:
            _v2_invalid()
        _v2_validate_payload("inspection", item["inspection"])
        _v2_integer(item["estimated_embedding_tokens"])
    elif kind == "operation":
        if item["effective_state"] not in _EFFECTIVE_STATES_V2 or item["provider_effect"] not in {"not_started", "completed", "unknown"}:
            _v2_invalid()
        _v2_digest(item["proof_sha256"], nullable=not item["proof_valid"])
        if not item["proof_valid"] and item["proof_sha256"] is not None:
            _v2_invalid()
        reservation = item["reservation"]
        if reservation is not None:
            reservation = _v2_object(reservation, {
                "state", "timezone", "local_day", "local_month", "reserved_tokens", "settled_tokens",
            })
            if reservation["state"] not in {"reserved", "settled", "released"}:
                _v2_invalid()
            _v2_period({key: reservation[key] for key in ("timezone", "local_day", "local_month")})
            _v2_integer(reservation["reserved_tokens"])
            _v2_integer(reservation["settled_tokens"], nullable=True)
        _v2_usage(item["local_estimate"])
        _v2_usage(item["provider_usage"])
    elif kind == "publication-plan":
        if (
            item["action"] not in {"publish_only", "reconcile_completion"}
            or item["requires_confirmation"] is not True
            or item["requires_network"] is not False
            or item["creates_reservation"] is not False
        ):
            _v2_invalid()
        _v2_validate_payload("operation", item["operation_inspection"])
        operation = item["operation_inspection"]
        if any(item[key] != operation[key] for key in ("operation_id", "generation_id", "ledger_upgrade")):
            _v2_invalid()
    elif kind == "execution":
        if item["status"] != "ready" or item["publication_committed"] is not True:
            _v2_invalid()
    return copy.deepcopy(item)


class _EmbeddingV2Handle:
    """A frozen process-local capability with only detached public properties."""

    __slots__ = ("__inner", "__payload")
    _kind: str

    def __init__(self, inner: object = None, payload: object = None, *, _token: object = None) -> None:
        if _token is not _HANDLE_TOKEN:
            raise TypeError(f"{type(self).__name__} is factory-only; use pkv_kernel.embedding")
        object.__setattr__(self, "_EmbeddingV2Handle__inner", inner)
        object.__setattr__(self, "_EmbeddingV2Handle__payload", _v2_validate_payload(self._kind, payload))

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("Embedding v2 handles are immutable")

    def __getattr__(self, name: str) -> object:
        if name in _V2_FIELDS[self._kind] or name == "schema":
            return copy.deepcopy(self.__payload[name])
        raise AttributeError(name)

    def to_dict(self) -> dict[str, object]:
        return copy.deepcopy(self.__payload)

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.to_dict()!r})"

    def __reduce_ex__(self, protocol: int) -> object:
        raise TypeError("Embedding v2 handles cannot be serialized; use to_dict for display")


class EmbeddingRebuildInspectionV2(_EmbeddingV2Handle):
    """Read-only index, policy and explicit ledger-upgrade inspection."""
    __slots__ = ()
    _kind = "inspection"


class EmbeddingRebuildPlanV2(_EmbeddingV2Handle):
    """A source-, root-, snapshot- and period-bound network plan."""
    __slots__ = ()
    _kind = "plan"


class EmbeddingRebuildConfirmationV2(_EmbeddingV2Handle):
    """Explicit approval and network permission for one rebuild plan."""
    __slots__ = ()
    _kind = "confirmation"


class EmbeddingOperationInspectionV2(_EmbeddingV2Handle):
    """Read-only, proof-verified manual-operation facts for the current root."""
    __slots__ = ()
    _kind = "operation"


class EmbeddingPublicationPlanV2(_EmbeddingV2Handle):
    """A plan to publish sealed work or reconcile an already committed pointer."""
    __slots__ = ()
    _kind = "publication-plan"


class EmbeddingPublicationConfirmationV2(_EmbeddingV2Handle):
    """Explicit approval for one operation's publication plan."""
    __slots__ = ()
    _kind = "publication-confirmation"


class EmbeddingRebuildExecutionV2(_EmbeddingV2Handle):
    """Safe completion facts; ready always means the pointer was committed."""
    __slots__ = ()
    _kind = "execution"


def _v2_wrap(handle_type: type[_EmbeddingV2Handle], inner: object) -> _EmbeddingV2Handle:
    return handle_type(inner, inner.to_dict(), _token=_HANDLE_TOKEN)


def _v2_require_handle(value: object, handle_type: type[_EmbeddingV2Handle]) -> None:
    # Exact types keep subclasses from substituting arbitrary properties or
    # execution objects while pretending to be a façade-created capability.
    if type(value) is not handle_type:
        raise TypeError(f"Expected a factory-created {handle_type.__name__}")


def inspect_embedding_rebuild_v2(config: Config | None = None) -> EmbeddingRebuildInspectionV2:
    """Inspect the current root without writes, reservations or Provider use."""
    from src.runtime.embedding_rebuild_v2 import inspect_rebuild

    return _v2_wrap(EmbeddingRebuildInspectionV2, inspect_rebuild(_require_config(config)))


def plan_embedding_rebuild_v2(
    inspection: EmbeddingRebuildInspectionV2, *, force: bool = False,
) -> EmbeddingRebuildPlanV2:
    """Capture a reproducible network plan and any explicit ledger upgrade."""
    _v2_require_handle(inspection, EmbeddingRebuildInspectionV2)
    if type(force) is not bool:
        raise TypeError("force 必须是 bool")
    from src.runtime.embedding_rebuild_v2 import plan_rebuild

    return _v2_wrap(
        EmbeddingRebuildPlanV2,
        plan_rebuild(inspection._EmbeddingV2Handle__inner, force=force),
    )


def confirm_embedding_rebuild_v2(
    plan: EmbeddingRebuildPlanV2, *, approved: bool = True, allow_network: bool = False,
) -> EmbeddingRebuildConfirmationV2:
    """Record explicit permissions in a process-local, plan-bound handle."""
    _v2_require_handle(plan, EmbeddingRebuildPlanV2)
    if type(approved) is not bool or type(allow_network) is not bool:
        raise TypeError("approved 与 allow_network 必须是 bool")
    return EmbeddingRebuildConfirmationV2(
        None,
        {"schema": "pkv.embedding.confirmation.v2", "plan_id": plan.plan_id,
         "approved": approved, "allow_network": allow_network},
        _token=_HANDLE_TOKEN,
    )


def _v2_check_confirmation(
    plan: _EmbeddingV2Handle, confirmation: object,
    confirmation_type: type[_EmbeddingV2Handle],
) -> None:
    if confirmation is not None:
        _v2_require_handle(confirmation, confirmation_type)
        if confirmation.plan_id != plan.plan_id or (
            confirmation_type is EmbeddingPublicationConfirmationV2
            and confirmation.operation_id != plan.operation_id
        ):
            raise PKVRuntimeError(
                ErrorCode.RUNTIME_PLAN_STALE, "确认不属于当前 Embedding 计划。",
                stage="embedding_plan", recoverable=True,
            )
    if confirmation is None or not confirmation.approved:
        raise PKVRuntimeError(
            ErrorCode.CONFIRMATION_REQUIRED, "执行 Embedding 计划需要明确确认。",
            stage="embedding_plan", recoverable=True,
        )


def execute_embedding_rebuild_v2(
    plan: EmbeddingRebuildPlanV2,
    confirmation: EmbeddingRebuildConfirmationV2 | None = None,
) -> EmbeddingRebuildExecutionV2:
    """Execute only after explicit approval and network permission match the plan."""
    _v2_require_handle(plan, EmbeddingRebuildPlanV2)
    _v2_check_confirmation(plan, confirmation, EmbeddingRebuildConfirmationV2)
    if not confirmation.allow_network:
        raise PKVRuntimeError(
            ErrorCode.EMBEDDING_REBUILD_AUTHORIZATION_REQUIRED,
            "Embedding 重建需要明确的网络授权。", stage="embedding_plan", recoverable=True,
        )
    from src.runtime.embedding_rebuild_v2 import execute_rebuild

    return _v2_wrap(EmbeddingRebuildExecutionV2, execute_rebuild(plan._EmbeddingV2Handle__inner))


def inspect_embedding_operation_v2(
    operation_id: str, *, config: Config | None = None,
) -> EmbeddingOperationInspectionV2:
    """Verify a manual operation's durable facts in the selected root only."""
    _v2_operation_id(operation_id)
    from src.runtime.embedding_rebuild_v2 import inspect_operation

    return _v2_wrap(
        EmbeddingOperationInspectionV2,
        inspect_operation(operation_id, config=_require_config(config)),
    )


def plan_embedding_publication_v2(
    inspection: EmbeddingOperationInspectionV2,
) -> EmbeddingPublicationPlanV2:
    """Plan publication or completion reconciliation without Provider work."""
    _v2_require_handle(inspection, EmbeddingOperationInspectionV2)
    from src.runtime.embedding_rebuild_v2 import plan_publication

    return _v2_wrap(
        EmbeddingPublicationPlanV2,
        plan_publication(inspection._EmbeddingV2Handle__inner),
    )


def confirm_embedding_publication_v2(
    plan: EmbeddingPublicationPlanV2, *, approved: bool = True,
) -> EmbeddingPublicationConfirmationV2:
    """Approve the exact operation and publication plan; never authorize network."""
    _v2_require_handle(plan, EmbeddingPublicationPlanV2)
    if type(approved) is not bool:
        raise TypeError("approved 必须是 bool")
    return EmbeddingPublicationConfirmationV2(
        None,
        {"schema": "pkv.embedding.publication-confirmation.v2", "plan_id": plan.plan_id,
         "operation_id": plan.operation_id, "approved": approved},
        _token=_HANDLE_TOKEN,
    )


def execute_embedding_publication_v2(
    plan: EmbeddingPublicationPlanV2,
    confirmation: EmbeddingPublicationConfirmationV2 | None = None,
) -> EmbeddingRebuildExecutionV2:
    """Publish verified sealed work without creating a Provider or reservation."""
    _v2_require_handle(plan, EmbeddingPublicationPlanV2)
    _v2_check_confirmation(plan, confirmation, EmbeddingPublicationConfirmationV2)
    from src.runtime.embedding_rebuild_v2 import execute_publication

    execution = _v2_wrap(
        EmbeddingRebuildExecutionV2, execute_publication(plan._EmbeddingV2Handle__inner),
    )
    if execution.provider_called:
        _v2_invalid()
    return execution
