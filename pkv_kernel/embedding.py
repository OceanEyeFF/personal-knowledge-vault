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
        self._provider_usages: list[TokenUsage] = []

    @property
    def provider_attempted(self) -> bool:
        return self._provider_attempted

    @property
    def provider_usages(self) -> tuple[TokenUsage, ...]:
        return tuple(self._provider_usages)

    def _capture_usage(self) -> None:
        try:
            usage = getattr(self._client, "last_usage", None)
        except Exception:
            return
        if isinstance(usage, TokenUsage) and usage.source == "provider_reported":
            self._provider_usages.append(usage)

    def embed_document(self, text: str):
        self._provider_attempted = True
        try:
            return self._adapter.embed_document(text)
        finally:
            self._capture_usage()

    def embed_stored_chunks(self, chunks: Iterable[str]):
        self._provider_attempted = True
        try:
            return self._adapter.embed_stored_chunks(tuple(chunks))
        finally:
            self._capture_usage()


class _ManualRebuildObserver:
    """Private bridge from Core lease phases to the independent JSON ledger."""

    def __init__(
        self,
        config: Config,
        *,
        public_plan_id: str,
        replay_key: str,
    ) -> None:
        self._config = config
        self._public_plan_id = public_plan_id
        self._replay_key = replay_key
        self._ledger = EmbeddingRebuildLedger(config.layout)
        self._meter: _MeteredPreChunkedEmbeddingAdapter | None = None

    @property
    def ledger(self) -> EmbeddingRebuildLedger:
        return self._ledger

    def attach_meter(self, meter: _MeteredPreChunkedEmbeddingAdapter) -> None:
        if self._meter is not None:
            raise RuntimeError("manual Embedding rebuild Provider 已构造")
        self._meter = meter

    def reserve(self, plan: _CorePlan, inspection: _CoreInspection) -> str:
        if plan._config is not self._config:
            raise PKVRuntimeError(
                ErrorCode.RUNTIME_PLAN_STALE,
                "Embedding rebuild 计划不属于当前 Config snapshot。",
                stage="embedding_rebuild_ledger",
                recoverable=True,
            )
        policy = inspect_embedding_rebuild_policy(self._config)
        operation = self._ledger.begin(
            plan_id=plan.plan_id,
            public_plan_id=self._public_plan_id,
            replay_key=self._replay_key,
            source_revision=inspection._config_source_revision or "",
            policy=policy,
            estimated_tokens=_estimated_embedding_tokens(inspection),
        )
        return operation.operation_id

    def mark_activation_intent(self, operation_id: str, generation_id: str) -> None:
        self._ledger.mark_activation_intent(operation_id, generation_id=generation_id)

    def mark_completed(self, operation_id: str) -> None:
        meter = self._meter
        self._ledger.mark_completed(
            operation_id,
            provider_usages=() if meter is None else meter.provider_usages,
            provider_attempted=False if meter is None else meter.provider_attempted,
        )

    def mark_failed(self, operation_id: str, error: PKVRuntimeError) -> None:
        meter = self._meter
        self._ledger.mark_failed(
            operation_id,
            error_code=error.code,
            provider_usages=() if meter is None else meter.provider_usages,
            provider_attempted=False if meter is None else meter.provider_attempted,
        )


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
    observer = _ManualRebuildObserver(
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
