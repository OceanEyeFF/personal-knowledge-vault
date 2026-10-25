"""Durable, lease-protected accounting for foreground Embedding rebuilds.

The public rebuild action is deliberately independent from the internal R4 Q2
automation ledger.  A foreground confirmation authorizes one explicit plan;
it must neither create a background worker nor inherit the internal
``ai.automation`` policy.  This small JSON ledger records only secret-free
operation, reservation, and token facts.  It is mutated only while its caller
holds the data-root writer lease, so Provider construction and requests remain
outside that lease.

The file lives below runtime state rather than SQLite on purpose: it is a
feature-local maintenance ledger with an independently versioned schema.  It
does not alter the user knowledge schema or make a public maintenance action
look like a Q2 derivation task.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from src.runtime.ai_automation_policy import TokenQuota, TokenUsage
from src.runtime.embedding_rebuild_policy import (
    EmbeddingRebuildPolicyInspection,
    EmbeddingRebuildPolicyState,
)
from src.runtime.errors import ErrorCode, PKVRuntimeError
from src.runtime.writer_inventory import require_active_data_root_writer


EMBEDDING_REBUILD_LEDGER_SCHEMA_VERSION = 2
_STAGE = "embedding_rebuild_ledger"
_OPERATION_ID_PATTERN = re.compile(r"embedding-rebuild-[0-9a-f]{32}\Z")
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_GENERATION_ID_PATTERN = re.compile(r"g-[0-9a-f-]{8,80}\Z")
_ERROR_CODE_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,95}\Z")


class EmbeddingRebuildLedgerState(str, Enum):
    """Durable terminal and in-flight states for one explicit rebuild."""

    PROCESSING = "processing"
    ACTIVATION_INTENT = "activation_intent"
    COMPLETED = "completed"
    FAILED = "failed"
    BUDGET_PAUSED = "budget_paused"


@dataclass(frozen=True)
class EmbeddingRebuildReservation:
    """One conservative token reservation owned by a manual operation."""

    reservation_id: str
    timezone: str
    local_day: str
    local_month: str
    reserved_tokens: int
    settled_tokens: int | None
    state: str

    def to_dict(self) -> dict[str, object]:
        return {
            "reservation_id": self.reservation_id,
            "timezone": self.timezone,
            "local_day": self.local_day,
            "local_month": self.local_month,
            "reserved_tokens": self.reserved_tokens,
            "settled_tokens": self.settled_tokens,
            "state": self.state,
        }


@dataclass(frozen=True)
class EmbeddingRebuildLedgerOperation:
    """One secret-free operation record, used by the private lifecycle bridge."""

    operation_id: str
    plan_id: str
    public_plan_id: str
    replay_key: str
    source_revision: str
    policy_fingerprint: str
    state: EmbeddingRebuildLedgerState
    reservation: EmbeddingRebuildReservation | None
    local_estimate: TokenUsage | None
    provider_usage: TokenUsage | None
    generation_id: str | None
    last_error_code: str | None
    provider_attempted: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "operation_id": self.operation_id,
            "plan_id": self.plan_id,
            "public_plan_id": self.public_plan_id,
            "replay_key": self.replay_key,
            "source_revision": self.source_revision,
            "policy_fingerprint": self.policy_fingerprint,
            "state": self.state.value,
            "reservation": (
                self.reservation.to_dict() if self.reservation is not None else None
            ),
            "local_estimate": (
                self.local_estimate.to_dict() if self.local_estimate is not None else None
            ),
            "provider_usage": (
                self.provider_usage.to_dict() if self.provider_usage is not None else None
            ),
            "generation_id": self.generation_id,
            "last_error_code": self.last_error_code,
            "provider_attempted": self.provider_attempted,
        }


def _safe_error(code: ErrorCode, message: str) -> PKVRuntimeError:
    return PKVRuntimeError(code, message, stage=_STAGE, recoverable=True)


def _require_sha256(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _SHA256_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{label} 必须是 sha256")
    return value


def _require_operation_id(value: object) -> str:
    if not isinstance(value, str) or _OPERATION_ID_PATTERN.fullmatch(value) is None:
        raise ValueError("operation_id 无效")
    return value


def _require_nonnegative_int(value: object, *, label: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{label} 必须是非负整数")
    return value


def _require_period(value: object, *, pattern: re.Pattern[str], label: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise ValueError(f"{label} 无效")
    return value


_DAY_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
_MONTH_PATTERN = re.compile(r"\d{4}-\d{2}\Z")


def _usage_from_payload(value: object, *, label: str) -> TokenUsage | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} 必须是映射")
    required = {
        "uncached_input_tokens",
        "cached_input_tokens",
        "generated_tokens",
        "embedding_input_tokens",
        "source",
    }
    if set(value) != required:
        raise ValueError(f"{label} 字段无效")
    return TokenUsage(
        uncached_input_tokens=value["uncached_input_tokens"],
        cached_input_tokens=value["cached_input_tokens"],
        generated_tokens=value["generated_tokens"],
        embedding_input_tokens=value["embedding_input_tokens"],
        source=value["source"],
    )


def _reservation_from_payload(value: object) -> EmbeddingRebuildReservation | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError("reservation 必须是映射")
    required = {
        "reservation_id",
        "timezone",
        "local_day",
        "local_month",
        "reserved_tokens",
        "settled_tokens",
        "state",
    }
    if set(value) != required:
        raise ValueError("reservation 字段无效")
    reservation_id = value["reservation_id"]
    if not isinstance(reservation_id, str) or not re.fullmatch(r"[0-9a-f]{32}", reservation_id):
        raise ValueError("reservation_id 无效")
    timezone_name = value["timezone"]
    if not isinstance(timezone_name, str) or not timezone_name:
        raise ValueError("reservation timezone 无效")
    ZoneInfo(timezone_name)
    state = value["state"]
    if state not in {"reserved", "settled", "released"}:
        raise ValueError("reservation state 无效")
    settled = value["settled_tokens"]
    if settled is not None:
        settled = _require_nonnegative_int(settled, label="settled_tokens")
    if state == "settled" and settled is None:
        raise ValueError("settled reservation 缺少 settled_tokens")
    if state != "settled" and settled is not None:
        raise ValueError("非 settled reservation 不得有 settled_tokens")
    return EmbeddingRebuildReservation(
        reservation_id=reservation_id,
        timezone=timezone_name,
        local_day=_require_period(value["local_day"], pattern=_DAY_PATTERN, label="local_day"),
        local_month=_require_period(
            value["local_month"], pattern=_MONTH_PATTERN, label="local_month"
        ),
        reserved_tokens=_require_nonnegative_int(
            value["reserved_tokens"], label="reserved_tokens"
        ),
        settled_tokens=settled,
        state=state,
    )


def _operation_from_payload(value: object) -> EmbeddingRebuildLedgerOperation:
    if not isinstance(value, Mapping):
        raise ValueError("operation 必须是映射")
    legacy_required = {
        "operation_id",
        "plan_id",
        "source_revision",
        "policy_fingerprint",
        "state",
        "reservation",
        "local_estimate",
        "provider_usage",
        "generation_id",
        "last_error_code",
        "provider_attempted",
    }
    required = legacy_required | {"public_plan_id", "replay_key"}
    fields = set(value)
    if fields != legacy_required and fields != required:
        raise ValueError("operation 字段无效")
    # v1 was not yet a released public format.  Terminal historical rows are
    # safely upgraded on the next ledger write; a nonterminal v1 row is blocked
    # below because it has no cross-process-safe replay identity.
    legacy_identity = fields == legacy_required
    plan_id = _require_sha256(value["plan_id"], label="plan_id")
    state = EmbeddingRebuildLedgerState(value["state"])
    reservation = _reservation_from_payload(value["reservation"])
    local_estimate = _usage_from_payload(value["local_estimate"], label="local_estimate")
    provider_usage = _usage_from_payload(value["provider_usage"], label="provider_usage")
    if local_estimate is not None and local_estimate.source != "local_estimate":
        raise ValueError("local_estimate source 无效")
    if provider_usage is not None and provider_usage.source != "provider_reported":
        raise ValueError("provider_usage source 无效")
    generation_id = value["generation_id"]
    if generation_id is not None and (
        not isinstance(generation_id, str)
        or _GENERATION_ID_PATTERN.fullmatch(generation_id) is None
    ):
        raise ValueError("generation_id 无效")
    last_error_code = value["last_error_code"]
    if last_error_code is not None and (
        not isinstance(last_error_code, str)
        or _ERROR_CODE_PATTERN.fullmatch(last_error_code) is None
    ):
        raise ValueError("last_error_code 无效")
    provider_attempted = value["provider_attempted"]
    if type(provider_attempted) is not bool:
        raise ValueError("provider_attempted 必须是 bool")
    if state is EmbeddingRebuildLedgerState.BUDGET_PAUSED and reservation is not None:
        raise ValueError("budget_paused 不得保留 reservation")
    if state is EmbeddingRebuildLedgerState.COMPLETED:
        if reservation is None or reservation.state != "settled" or generation_id is None:
            raise ValueError("completed operation 不完整")
    if state is EmbeddingRebuildLedgerState.FAILED and reservation is not None:
        if provider_attempted and reservation.state != "settled":
            raise ValueError("Provider 已尝试的失败操作必须结算")
        if not provider_attempted and reservation.state != "released":
            raise ValueError("未调用 Provider 的失败操作必须释放 reservation")
    return EmbeddingRebuildLedgerOperation(
        operation_id=_require_operation_id(value["operation_id"]),
        plan_id=plan_id,
        public_plan_id=(
            plan_id
            if legacy_identity
            else _require_sha256(value["public_plan_id"], label="public_plan_id")
        ),
        replay_key=(
            plan_id
            if legacy_identity
            else _require_sha256(value["replay_key"], label="replay_key")
        ),
        source_revision=_require_sha256(value["source_revision"], label="source_revision"),
        policy_fingerprint=_require_sha256(
            value["policy_fingerprint"], label="policy_fingerprint"
        ),
        state=state,
        reservation=reservation,
        local_estimate=local_estimate,
        provider_usage=provider_usage,
        generation_id=generation_id,
        last_error_code=last_error_code,
        provider_attempted=provider_attempted,
    )


def _aggregate_provider_usage(usages: Iterable[TokenUsage]) -> TokenUsage | None:
    """Sum only reported fields, retaining ``None`` when every response omitted one."""

    values = tuple(usages)
    if not all(isinstance(usage, TokenUsage) for usage in values):
        raise TypeError("Provider usage 必须是 TokenUsage")
    if not values:
        return None

    def total(attribute: str) -> int | None:
        known = [getattr(usage, attribute) for usage in values]
        numeric = [value for value in known if value is not None]
        return sum(numeric) if numeric else None

    projected = TokenUsage(
        uncached_input_tokens=total("uncached_input_tokens"),
        cached_input_tokens=total("cached_input_tokens"),
        generated_tokens=total("generated_tokens"),
        embedding_input_tokens=total("embedding_input_tokens"),
        source="provider_reported",
    )
    return projected


class EmbeddingRebuildLedger:
    """Feature-local accounting port for confirmed public rebuild execution.

    The public Kernel bridge creates this object only after it has an explicit
    Config snapshot.  Reads are safe and side-effect free; all state changes
    require an active root writer lease owned by the current task.
    """

    def __init__(
        self,
        layout: Any,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._layout = layout
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.path = Path(layout.runtime_state_dir) / "embedding-rebuild-ledger.json"

    def _require_writer(self) -> None:
        require_active_data_root_writer(self._layout, owner="embedding_generation")

    @staticmethod
    def _now(clock: Callable[[], datetime]) -> datetime:
        value = clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise ValueError("Embedding rebuild ledger 时钟必须返回有时区 datetime")
        return value

    def _read_payload(self) -> dict[str, object]:
        try:
            target = self._layout.validate_user_file(
                self.path,
                label="Embedding rebuild ledger",
                allow_missing=True,
            )
            if not target.exists():
                return {"schema_version": EMBEDDING_REBUILD_LEDGER_SCHEMA_VERSION, "operations": {}}
            with self._layout.open_user_file(
                target,
                "rb",
                label="Embedding rebuild ledger",
            ) as source:
                raw = source.read()
            payload = json.loads(raw.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("ledger payload 无效")
            if set(payload) != {"schema_version", "operations"}:
                raise ValueError("ledger 顶层字段无效")
            if payload["schema_version"] not in {
                1,
                EMBEDDING_REBUILD_LEDGER_SCHEMA_VERSION,
            }:
                raise ValueError("ledger schema_version 不支持")
            operations = payload["operations"]
            if not isinstance(operations, dict) or not all(
                isinstance(key, str) for key in operations
            ):
                raise ValueError("ledger operations 无效")
            for operation_id, operation in operations.items():
                parsed = _operation_from_payload(operation)
                if parsed.operation_id != operation_id:
                    raise ValueError("ledger operation identity 不匹配")
            return payload
        except PKVRuntimeError:
            raise
        except (OSError, UnicodeError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise _safe_error(ErrorCode.REPAIR_REQUIRED, "Embedding rebuild 账本无法安全读取。") from exc

    def _publish_payload(self, payload: Mapping[str, object]) -> None:
        self._require_writer()
        try:
            safe_payload = {
                # A write is the explicit upgrade point for the private v1
                # ledger.  v2 adds only opaque replay identities; no user data
                # or Provider material is introduced during this rewrite.
                "schema_version": EMBEDDING_REBUILD_LEDGER_SCHEMA_VERSION,
                "operations": payload["operations"],
            }
            # The exact schema validation below rejects every unknown field,
            # including any credential-shaped key.  We cannot reuse the generic
            # runtime-snapshot detector here because legitimate token *counts*
            # intentionally contain ``token`` in their field names.
            for operation in dict(safe_payload["operations"]).values():
                _operation_from_payload(operation)
            encoded = (
                json.dumps(
                    safe_payload,
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            ).encode("utf-8")
            self._layout.ensure_user_directories()
            target = self._layout.writable_user_path(
                self.path,
                label="Embedding rebuild ledger",
            )
            self._layout.atomic_publish_user_file(
                target,
                label="Embedding rebuild ledger",
                data=encoded,
            )
            if self._read_payload() != safe_payload:
                raise ValueError("ledger 发布后校验失败")
        except PKVRuntimeError:
            raise
        except (OSError, TypeError, ValueError) as exc:
            raise _safe_error(ErrorCode.REPAIR_REQUIRED, "Embedding rebuild 账本无法持久化。") from exc

    @staticmethod
    def _operation_payload(operation: EmbeddingRebuildLedgerOperation) -> dict[str, object]:
        return operation.to_dict()

    def read_operations(self) -> tuple[EmbeddingRebuildLedgerOperation, ...]:
        """Return all durable records without creating a root or ledger file."""

        payload = self._read_payload()
        operations = payload["operations"]
        assert isinstance(operations, Mapping)
        return tuple(
            _operation_from_payload(operations[operation_id])
            for operation_id in sorted(operations)
        )

    def get_operation(self, operation_id: str) -> EmbeddingRebuildLedgerOperation | None:
        """Read exactly one operation by opaque operation identity."""

        operation_id = _require_operation_id(operation_id)
        payload = self._read_payload()
        operations = payload["operations"]
        assert isinstance(operations, Mapping)
        raw = operations.get(operation_id)
        return _operation_from_payload(raw) if raw is not None else None

    @staticmethod
    def _token_total(
        operations: Mapping[str, object],
        *,
        policy_fingerprint: str,
        timezone_name: str,
        period_key: str,
        period: str,
    ) -> int:
        total = 0
        for raw in operations.values():
            operation = _operation_from_payload(raw)
            reservation = operation.reservation
            if (
                operation.policy_fingerprint != policy_fingerprint
                or reservation is None
                or reservation.state not in {"reserved", "settled"}
                or reservation.timezone != timezone_name
                or getattr(reservation, period_key) != period
            ):
                continue
            total += (
                reservation.settled_tokens
                if reservation.state == "settled"
                else reservation.reserved_tokens
            )
        return total

    @staticmethod
    def _has_legacy_identity(raw: object) -> bool:
        """Return whether a raw v1 operation lacks a durable public replay key."""

        return isinstance(raw, Mapping) and "replay_key" not in raw

    @staticmethod
    def _assert_replay_not_consumed(
        operations: Mapping[str, object],
        *,
        replay_key: str,
    ) -> None:
        """Fence duplicate foreground Provider work before stage allocation.

        A public plan is recomputed in each CLI/MCP process, while its Core
        plan carries a process-private HMAC.  The stable replay key therefore
        lives in the durable ledger rather than relying on an in-memory
        confirmation object.  No public v1 resume/retry exists, so an already
        terminal action is consumed and an in-flight action is busy.
        """

        for raw in operations.values():
            operation = _operation_from_payload(raw)
            if (
                EmbeddingRebuildLedger._has_legacy_identity(raw)
                and operation.state
                in {
                    EmbeddingRebuildLedgerState.PROCESSING,
                    EmbeddingRebuildLedgerState.ACTIVATION_INTENT,
                }
            ):
                raise _safe_error(
                    ErrorCode.REPAIR_REQUIRED,
                    "存在无法安全关联的旧 Embedding rebuild operation；请先完成修复。",
                )
            if operation.replay_key != replay_key:
                continue
            if operation.state in {
                EmbeddingRebuildLedgerState.PROCESSING,
                EmbeddingRebuildLedgerState.ACTIVATION_INTENT,
            }:
                raise _safe_error(
                    ErrorCode.WRITE_BUSY,
                    "同一 Embedding rebuild 计划正在执行。",
                )
            raise _safe_error(
                ErrorCode.RUNTIME_PLAN_STALE,
                "该 Embedding rebuild 计划已经使用；请重新检查当前 generation。",
            )

    def begin(
        self,
        *,
        plan_id: str,
        public_plan_id: str,
        replay_key: str,
        source_revision: str,
        policy: EmbeddingRebuildPolicyInspection,
        estimated_tokens: int,
    ) -> EmbeddingRebuildLedgerOperation:
        """Reserve current policy capacity before a Provider can be constructed.

        A hard-cap failure is itself durably recorded as ``budget_paused`` but
        creates no reservation and raises before the Core can allocate a stage
        or invoke a Provider factory.
        """

        self._require_writer()
        plan_id = _require_sha256(plan_id, label="plan_id")
        public_plan_id = _require_sha256(public_plan_id, label="public_plan_id")
        replay_key = _require_sha256(replay_key, label="replay_key")
        source_revision = _require_sha256(source_revision, label="source_revision")
        estimated_tokens = _require_nonnegative_int(
            estimated_tokens,
            label="estimated_tokens",
        )
        if not isinstance(policy, EmbeddingRebuildPolicyInspection):
            raise TypeError("policy 必须由 inspect_embedding_rebuild_policy 返回")
        if (
            policy.state is not EmbeddingRebuildPolicyState.READY
            or policy.policy_fingerprint is None
            or policy.token_quota is None
            or not policy.token_quota.has_hard_cap
        ):
            raise _safe_error(
                ErrorCode.EMBEDDING_REBUILD_AUTHORIZATION_REQUIRED,
                "手动 Embedding 重建尚未通过独立策略授权。",
            )
        policy_fingerprint = _require_sha256(
            policy.policy_fingerprint,
            label="policy_fingerprint",
        )
        quota = policy.token_quota
        assert isinstance(quota, TokenQuota)
        now = self._now(self._clock)
        local_now = now.astimezone(ZoneInfo(quota.timezone))
        local_day = local_now.date().isoformat()
        local_month = local_now.strftime("%Y-%m")
        payload = self._read_payload()
        operations = payload["operations"]
        assert isinstance(operations, dict)
        self._assert_replay_not_consumed(operations, replay_key=replay_key)
        operation_id = f"embedding-rebuild-{uuid.uuid4().hex}"
        local_estimate = TokenUsage(
            embedding_input_tokens=estimated_tokens,
            source="local_estimate",
        )
        day_total = self._token_total(
            operations,
            policy_fingerprint=policy_fingerprint,
            timezone_name=quota.timezone,
            period_key="local_day",
            period=local_day,
        )
        month_total = self._token_total(
            operations,
            policy_fingerprint=policy_fingerprint,
            timezone_name=quota.timezone,
            period_key="local_month",
            period=local_month,
        )
        exceeds_cap = (
            quota.daily_total_tokens == 0
            or quota.monthly_total_tokens == 0
            or (
                quota.daily_total_tokens is not None
                and day_total + estimated_tokens > quota.daily_total_tokens
            )
            or (
                quota.monthly_total_tokens is not None
                and month_total + estimated_tokens > quota.monthly_total_tokens
            )
        )
        if exceeds_cap:
            paused = EmbeddingRebuildLedgerOperation(
                operation_id=operation_id,
                plan_id=plan_id,
                public_plan_id=public_plan_id,
                replay_key=replay_key,
                source_revision=source_revision,
                policy_fingerprint=policy_fingerprint,
                state=EmbeddingRebuildLedgerState.BUDGET_PAUSED,
                reservation=None,
                local_estimate=local_estimate,
                provider_usage=None,
                generation_id=None,
                last_error_code=ErrorCode.EMBEDDING_BUDGET_PAUSED.value,
                provider_attempted=False,
            )
            operations[operation_id] = self._operation_payload(paused)
            self._publish_payload(payload)
            raise _safe_error(
                ErrorCode.EMBEDDING_BUDGET_PAUSED,
                "手动 Embedding 重建已达到独立 token hard cap。",
            )
        reservation = EmbeddingRebuildReservation(
            reservation_id=uuid.uuid4().hex,
            timezone=quota.timezone,
            local_day=local_day,
            local_month=local_month,
            reserved_tokens=estimated_tokens,
            settled_tokens=None,
            state="reserved",
        )
        operation = EmbeddingRebuildLedgerOperation(
            operation_id=operation_id,
            plan_id=plan_id,
            public_plan_id=public_plan_id,
            replay_key=replay_key,
            source_revision=source_revision,
            policy_fingerprint=policy_fingerprint,
            state=EmbeddingRebuildLedgerState.PROCESSING,
            reservation=reservation,
            local_estimate=local_estimate,
            provider_usage=None,
            generation_id=None,
            last_error_code=None,
            provider_attempted=False,
        )
        operations[operation_id] = self._operation_payload(operation)
        self._publish_payload(payload)
        return operation

    def _replace(self, operation: EmbeddingRebuildLedgerOperation) -> None:
        self._require_writer()
        payload = self._read_payload()
        operations = payload["operations"]
        assert isinstance(operations, dict)
        existing = operations.get(operation.operation_id)
        if existing is None:
            raise _safe_error(ErrorCode.RUNTIME_PLAN_STALE, "Embedding rebuild operation 已不存在。")
        operations[operation.operation_id] = self._operation_payload(operation)
        self._publish_payload(payload)

    def mark_activation_intent(
        self,
        operation_id: str,
        *,
        generation_id: str,
    ) -> EmbeddingRebuildLedgerOperation:
        """Record the final generation before the irreversible pointer CAS."""

        self._require_writer()
        operation = self.get_operation(operation_id)
        if operation is None:
            raise _safe_error(ErrorCode.RUNTIME_PLAN_STALE, "Embedding rebuild operation 已不存在。")
        if not isinstance(generation_id, str) or _GENERATION_ID_PATTERN.fullmatch(generation_id) is None:
            raise ValueError("generation_id 无效")
        if operation.state is EmbeddingRebuildLedgerState.ACTIVATION_INTENT:
            if operation.generation_id != generation_id:
                raise _safe_error(ErrorCode.RUNTIME_PLAN_STALE, "Embedding rebuild activation intent 已变化。")
            return operation
        if operation.state is not EmbeddingRebuildLedgerState.PROCESSING:
            raise _safe_error(ErrorCode.RUNTIME_PLAN_STALE, "Embedding rebuild operation 不能进入 activation intent。")
        intent = EmbeddingRebuildLedgerOperation(
            **{
                **operation.__dict__,
                "state": EmbeddingRebuildLedgerState.ACTIVATION_INTENT,
                "generation_id": generation_id,
            }
        )
        self._replace(intent)
        return intent

    @staticmethod
    def _with_provider_usage(
        operation: EmbeddingRebuildLedgerOperation,
        usages: Iterable[TokenUsage],
        *,
        provider_attempted: bool,
    ) -> EmbeddingRebuildLedgerOperation:
        reported = _aggregate_provider_usage(usages)
        if operation.provider_usage is not None:
            if reported is not None and operation.provider_usage != reported:
                raise _safe_error(ErrorCode.RUNTIME_PLAN_STALE, "Embedding Provider usage 已变化。")
            reported = operation.provider_usage
        return EmbeddingRebuildLedgerOperation(
            **{
                **operation.__dict__,
                "provider_usage": reported,
                "provider_attempted": operation.provider_attempted or provider_attempted,
            }
        )

    @staticmethod
    def _settled_reservation(
        operation: EmbeddingRebuildLedgerOperation,
        *,
        provider_attempted: bool,
    ) -> EmbeddingRebuildReservation | None:
        reservation = operation.reservation
        if reservation is None:
            return None
        if reservation.state == "settled":
            return reservation
        if reservation.state == "released":
            return reservation
        if not provider_attempted:
            return EmbeddingRebuildReservation(
                **{**reservation.__dict__, "state": "released", "settled_tokens": None}
            )
        # The local estimate is the reservation basis, not an additional billable
        # usage component.  Once the Provider reports usage, settle
        # conservatively to the larger of that fact and the original reserve;
        # adding both would charge the same embedding work twice.
        known_tokens = (
            operation.provider_usage.known_total_tokens
            if operation.provider_usage is not None
            else 0
        )
        return EmbeddingRebuildReservation(
            **{
                **reservation.__dict__,
                "state": "settled",
                "settled_tokens": max(reservation.reserved_tokens, known_tokens),
            }
        )

    def mark_completed(
        self,
        operation_id: str,
        *,
        provider_usages: Iterable[TokenUsage] = (),
        provider_attempted: bool,
    ) -> EmbeddingRebuildLedgerOperation:
        """Settle facts after a successful pointer activation.

        The method is idempotent for the same operation; callers can therefore
        reconcile an interrupted final accounting write without re-running any
        Provider operation.
        """

        self._require_writer()
        operation = self.get_operation(operation_id)
        if operation is None:
            raise _safe_error(ErrorCode.RUNTIME_PLAN_STALE, "Embedding rebuild operation 已不存在。")
        if operation.state is EmbeddingRebuildLedgerState.COMPLETED:
            return operation
        if operation.state is not EmbeddingRebuildLedgerState.ACTIVATION_INTENT:
            raise _safe_error(ErrorCode.RUNTIME_PLAN_STALE, "Embedding rebuild operation 尚未完成 activation intent。")
        updated = self._with_provider_usage(
            operation,
            provider_usages,
            provider_attempted=provider_attempted,
        )
        settled = self._settled_reservation(updated, provider_attempted=True)
        if settled is None or settled.state != "settled" or updated.generation_id is None:
            raise _safe_error(ErrorCode.REPAIR_REQUIRED, "Embedding rebuild 完成账本缺少结算事实。")
        completed = EmbeddingRebuildLedgerOperation(
            **{
                **updated.__dict__,
                "state": EmbeddingRebuildLedgerState.COMPLETED,
                "reservation": settled,
                "last_error_code": None,
                "provider_attempted": updated.provider_attempted or provider_attempted,
            }
        )
        self._replace(completed)
        return completed

    def mark_failed(
        self,
        operation_id: str,
        *,
        error_code: ErrorCode | str,
        provider_usages: Iterable[TokenUsage] = (),
        provider_attempted: bool,
    ) -> EmbeddingRebuildLedgerOperation:
        """Settle or release a reservation after a failed pre-activation run."""

        self._require_writer()
        operation = self.get_operation(operation_id)
        if operation is None:
            raise _safe_error(ErrorCode.RUNTIME_PLAN_STALE, "Embedding rebuild operation 已不存在。")
        if operation.state is EmbeddingRebuildLedgerState.FAILED:
            return operation
        if operation.state in {
            EmbeddingRebuildLedgerState.COMPLETED,
            EmbeddingRebuildLedgerState.BUDGET_PAUSED,
        }:
            raise _safe_error(ErrorCode.RUNTIME_PLAN_STALE, "Embedding rebuild operation 已到达终态。")
        code = error_code.value if isinstance(error_code, ErrorCode) else error_code
        if not isinstance(code, str) or _ERROR_CODE_PATTERN.fullmatch(code) is None:
            raise ValueError("error_code 无效")
        updated = self._with_provider_usage(
            operation,
            provider_usages,
            provider_attempted=provider_attempted,
        )
        attempted = updated.provider_attempted
        failed = EmbeddingRebuildLedgerOperation(
            **{
                **updated.__dict__,
                "state": EmbeddingRebuildLedgerState.FAILED,
                "reservation": self._settled_reservation(
                    updated,
                    provider_attempted=attempted,
                ),
                "last_error_code": code,
            }
        )
        self._replace(failed)
        return failed


__all__ = [
    "EMBEDDING_REBUILD_LEDGER_SCHEMA_VERSION",
    "EmbeddingRebuildLedger",
    "EmbeddingRebuildLedgerOperation",
    "EmbeddingRebuildLedgerState",
    "EmbeddingRebuildReservation",
]
