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

import hashlib
import json
import re
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from src.runtime.ai_automation_policy import TokenQuota, TokenUsage
from src.runtime.embedding_rebuild_policy import (
    EmbeddingRebuildPolicyInspection,
    EmbeddingRebuildPolicyState,
)
from src.runtime.errors import ErrorCode, PKVRuntimeError
from src.runtime.writer_inventory import require_active_data_root_writer


EMBEDDING_REBUILD_LEDGER_SCHEMA_VERSION = 3
_STAGE = "embedding_rebuild_ledger"
_OPERATION_ID_PATTERN = re.compile(r"embedding-rebuild-[0-9a-f]{32}\Z")
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_GENERATION_ID_PATTERN = re.compile(r"g-[0-9a-f-]{8,80}\Z")
_ERROR_CODE_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,95}\Z")
_STAGE_KEY_PATTERN = re.compile(r"\.stage-g-[0-9a-f]{16}-[0-9a-f]{16}\Z")


class EmbeddingRebuildLedgerState(str, Enum):
    """Durable terminal and in-flight states for one explicit rebuild."""

    PROCESSING = "processing"
    AWAITING_PUBLISH = "awaiting_publish"
    ACTIVATION_INTENT = "activation_intent"
    COMPLETED = "completed"
    FAILED = "failed"
    BUDGET_PAUSED = "budget_paused"
    REPAIR_REQUIRED = "repair_required"


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
    public_plan_id: str | None
    replay_key: str
    source_revision: str
    policy_fingerprint: str
    state: EmbeddingRebuildLedgerState
    reservation: EmbeddingRebuildReservation | None
    local_estimate: TokenUsage | None
    provider_usage: TokenUsage | None
    generation_id: str | None
    last_error_code: str | None
    record_origin: str
    identity_schema: str
    data_root_identity_sha256: str | None
    runtime_snapshot_sha256: str | None
    contract_sha256: str | None
    budget_check_period: dict[str, str] | None
    provider_effect: str
    stage_key: str | None = None
    grant_sha256: str | None = None
    proof_sha256: str | None = None
    usage_completeness: str = "unknown"
    settlement_basis: str | None = None

    @property
    def provider_attempted(self) -> bool:
        """Compatibility projection only; never persisted in schema 3."""

        return self.provider_effect != "not_started"

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
            "record_origin": self.record_origin,
            "identity_schema": self.identity_schema,
            "data_root_identity_sha256": self.data_root_identity_sha256,
            "runtime_snapshot_sha256": self.runtime_snapshot_sha256,
            "contract_sha256": self.contract_sha256,
            "budget_check_period": (
                dict(self.budget_check_period) if self.budget_check_period else None
            ),
            "provider_effect": self.provider_effect,
            "stage_key": self.stage_key,
            "grant_sha256": self.grant_sha256,
            "proof_sha256": self.proof_sha256,
            "usage_completeness": self.usage_completeness,
            "settlement_basis": self.settlement_basis,
        }


@dataclass(frozen=True)
class EmbeddingRebuildLedgerInspection:
    """A read-only classification, including a raw-byte-bound upgrade proposal."""

    schema_version: int | None
    operations: tuple[EmbeddingRebuildLedgerOperation, ...]
    ledger_upgrade: dict[str, object] | None

    @property
    def blocking_operation_count(self) -> int:
        return sum(_blocks_root(operation) for operation in self.operations)


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


def current_utc() -> datetime:
    """The budget clock; lease and stage expiration use their own clocks."""

    return datetime.now(timezone.utc)


def _period_from_payload(value: object) -> dict[str, str]:
    if not isinstance(value, Mapping) or set(value) != {"timezone", "local_day", "local_month"}:
        raise ValueError("budget_check_period 字段无效")
    timezone_name = value["timezone"]
    if not isinstance(timezone_name, str) or not timezone_name:
        raise ValueError("budget_check_period timezone 无效")
    ZoneInfo(timezone_name)
    day = _require_period(value["local_day"], pattern=_DAY_PATTERN, label="local_day")
    month = _require_period(value["local_month"], pattern=_MONTH_PATTERN, label="local_month")
    date.fromisoformat(day)
    if day[:7] != month:
        raise ValueError("budget_check_period 月份不一致")
    return {"timezone": timezone_name, "local_day": day, "local_month": month}


def current_period(policy: EmbeddingRebuildPolicyInspection) -> dict[str, str]:
    if policy.token_quota is None:
        raise _safe_error(ErrorCode.EMBEDDING_REBUILD_AUTHORIZATION_REQUIRED, "预算时区尚未授权。")
    now = EmbeddingRebuildLedger._now(current_utc).astimezone(ZoneInfo(policy.token_quota.timezone))
    return {"timezone": policy.token_quota.timezone, "local_day": now.date().isoformat(), "local_month": now.strftime("%Y-%m")}


def _period_interval(*, period: str, period_key: str, timezone_name: str) -> tuple[datetime, datetime]:
    """UTC interval for conservative accounting across policy timezone changes."""

    start_date = date.fromisoformat(period if period_key == "local_day" else period + "-01")
    if period_key == "local_day":
        end_date = start_date + timedelta(days=1)
    else:
        end_date = date(start_date.year + (start_date.month == 12), start_date.month % 12 + 1, 1)
    zone = ZoneInfo(timezone_name)
    return (
        datetime(start_date.year, start_date.month, start_date.day, tzinfo=zone).astimezone(timezone.utc),
        datetime(end_date.year, end_date.month, end_date.day, tzinfo=zone).astimezone(timezone.utc),
    )


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
    period = _period_from_payload({key: value[key] for key in ("timezone", "local_day", "local_month")})
    return EmbeddingRebuildReservation(
        reservation_id=reservation_id,
        **period,
        reserved_tokens=_require_nonnegative_int(
            value["reserved_tokens"], label="reserved_tokens"
        ),
        settled_tokens=settled,
        state=state,
    )


_V1_FIELDS = {
    "operation_id", "plan_id", "source_revision", "policy_fingerprint", "state",
    "reservation", "local_estimate", "provider_usage", "generation_id",
    "last_error_code", "provider_attempted",
}
_V2_FIELDS = _V1_FIELDS | {"public_plan_id", "replay_key"}
_V3_FIELDS = (_V2_FIELDS - {"provider_attempted"}) | {
    "record_origin", "identity_schema", "data_root_identity_sha256",
    "runtime_snapshot_sha256", "contract_sha256", "budget_check_period",
    "provider_effect", "stage_key", "grant_sha256", "proof_sha256",
    "usage_completeness", "settlement_basis",
}


def _blocks_root(operation: EmbeddingRebuildLedgerOperation) -> bool:
    # Historical paid terminal facts have no proof; that alone is not a fence.
    return (
        operation.state is EmbeddingRebuildLedgerState.REPAIR_REQUIRED
        or (operation.provider_effect == "unknown"
            and (operation.reservation is None or operation.reservation.state != "settled"))
        or (operation.state in {EmbeddingRebuildLedgerState.AWAITING_PUBLISH, EmbeddingRebuildLedgerState.ACTIVATION_INTENT}
            and (operation.reservation is None or operation.reservation.state != "settled"))
    )


def _operation_from_payload(value: object, *, schema_version: int = 3) -> EmbeddingRebuildLedgerOperation:
    if not isinstance(value, Mapping):
        raise ValueError("operation 必须是映射")
    if set(value) != {1: _V1_FIELDS, 2: _V2_FIELDS, 3: _V3_FIELDS}[schema_version]:
        raise ValueError("operation 字段无效")
    plan_id = _require_sha256(value["plan_id"], label="plan_id")
    state = EmbeddingRebuildLedgerState(value["state"])
    reservation = _reservation_from_payload(value["reservation"])
    local_estimate = _usage_from_payload(value["local_estimate"], label="local_estimate")
    provider_usage = _usage_from_payload(value["provider_usage"], label="provider_usage")
    if local_estimate is not None and local_estimate.source != "local_estimate":
        raise ValueError("local_estimate source 无效")
    if provider_usage is not None and provider_usage.source != "provider_reported":
        raise ValueError("provider_usage source 无效")
    if reservation is not None and reservation.state == "settled" and provider_usage is not None and reservation.settled_tokens < provider_usage.known_total_tokens:
        raise ValueError("settled reservation 低于已报告 Provider 费用")
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
    legacy = schema_version in {1, 2}
    if legacy:
        if state in {EmbeddingRebuildLedgerState.AWAITING_PUBLISH, EmbeddingRebuildLedgerState.REPAIR_REQUIRED}:
            raise ValueError("legacy state 无效")
        attempted = value["provider_attempted"]
        if type(attempted) is not bool:
            raise ValueError("provider_attempted 必须是 bool")
        effect = "unknown"
        if state is EmbeddingRebuildLedgerState.BUDGET_PAUSED:
            if reservation is not None or attempted or provider_usage is not None or generation_id is not None:
                raise ValueError("budget_paused 包含外部影响")
            effect = "not_started"
        elif state is EmbeddingRebuildLedgerState.FAILED:
            if attempted and reservation is not None and reservation.state == "settled":
                pass
            elif not attempted and reservation is not None and reservation.state == "released":
                if provider_usage is not None and provider_usage.known_total_tokens != 0:
                    raise ValueError("未开始操作含有 Provider 费用")
                effect = "not_started"
                # A reported zero is preserved as a legacy fact rather than invented.
            else:
                state = EmbeddingRebuildLedgerState.REPAIR_REQUIRED
        elif state in {EmbeddingRebuildLedgerState.PROCESSING, EmbeddingRebuildLedgerState.ACTIVATION_INTENT}:
            state = EmbeddingRebuildLedgerState.REPAIR_REQUIRED
        extra = {
            "record_origin": f"legacy_v{schema_version}",
            "identity_schema": "pkv.embedding.rebuild-plan.v1" if schema_version == 1 else "pkv.embedding.transition.v1",
            "data_root_identity_sha256": None, "runtime_snapshot_sha256": None,
            "contract_sha256": None,
            "budget_check_period": ({key: getattr(reservation, key) for key in ("timezone", "local_day", "local_month")} if reservation else None),
            "provider_effect": effect,
            "usage_completeness": "partial" if provider_usage is not None else "unknown",
            "settlement_basis": "conservative_reservation" if reservation and reservation.state == "settled" else None,
        }
    else:
        extra = {key: value[key] for key in _V3_FIELDS - (_V2_FIELDS - {"provider_attempted"})}
        if extra["record_origin"] not in {"native_v3", "legacy_v1", "legacy_v2"}:
            raise ValueError("record_origin 无效")
        native = extra["record_origin"] == "native_v3"
        expected_identity = {"native_v3": "pkv.embedding.transition.v2", "legacy_v1": "pkv.embedding.rebuild-plan.v1", "legacy_v2": "pkv.embedding.transition.v1"}[extra["record_origin"]]
        if extra["identity_schema"] != expected_identity:
            raise ValueError("identity_schema 无效")
        if extra["record_origin"] == "legacy_v1" and value["public_plan_id"] is not None:
            raise ValueError("legacy v1 不存在可验证的 public_plan_id")
        for key in ("data_root_identity_sha256", "runtime_snapshot_sha256", "contract_sha256"):
            if native or extra[key] is not None:
                _require_sha256(extra[key], label=key)
        period = extra["budget_check_period"]
        if native or period is not None:
            extra["budget_check_period"] = _period_from_payload(period)
        if reservation is not None and extra["budget_check_period"] != {key: getattr(reservation, key) for key in ("timezone", "local_day", "local_month")}:
            raise ValueError("reservation 与 budget_check_period 不一致")
        effect = extra["provider_effect"]
        if effect not in {"not_started", "unknown", "completed"}:
            raise ValueError("provider_effect 无效")
        stage_key = extra["stage_key"]
        if stage_key is not None and (not isinstance(stage_key, str) or not _STAGE_KEY_PATTERN.fullmatch(stage_key)):
            raise ValueError("stage_key 无效")
        for key in ("grant_sha256", "proof_sha256"):
            if extra[key] is not None:
                _require_sha256(extra[key], label=key)
        if (stage_key is None) != (extra["grant_sha256"] is None):
            raise ValueError("stage/grant 绑定不完整")
        if extra["proof_sha256"] is not None and (stage_key is None or effect != "completed" or generation_id is None):
            raise ValueError("proof 绑定不完整")
        if native and effect == "completed" and extra["proof_sha256"] is None:
            raise ValueError("completed Provider effect 缺少 proof")
        if native and state in {EmbeddingRebuildLedgerState.COMPLETED, EmbeddingRebuildLedgerState.ACTIVATION_INTENT, EmbeddingRebuildLedgerState.AWAITING_PUBLISH} and (effect != "completed" or extra["proof_sha256"] is None):
            raise ValueError("native 发布状态缺少完整 Provider proof")
        if native and state is EmbeddingRebuildLedgerState.PROCESSING and (extra["proof_sha256"] is not None or generation_id is not None or effect == "completed"):
            raise ValueError("processing operation 含有已封存结果")
        if state is EmbeddingRebuildLedgerState.BUDGET_PAUSED and stage_key is not None:
            raise ValueError("budget_paused 不得分配 stage")
        if not native and any(extra[key] is not None for key in ("data_root_identity_sha256", "runtime_snapshot_sha256", "contract_sha256", "stage_key", "grant_sha256", "proof_sha256")):
            raise ValueError("legacy operation 不得补造新身份或 proof")
        completeness = extra["usage_completeness"]
        if completeness not in {"complete", "partial", "unknown"} or ((completeness == "unknown") != (provider_usage is None)):
            raise ValueError("usage_completeness 不一致")
        if extra["settlement_basis"] not in {None, "provider_reported", "conservative_reservation"}:
            raise ValueError("settlement_basis 无效")
        if (extra["settlement_basis"] is not None) != (reservation is not None and reservation.state == "settled"):
            raise ValueError("settlement_basis 与 reservation 不一致")
        if extra["settlement_basis"] == "provider_reported" and (provider_usage is None or completeness != "complete" or reservation is None or reservation.settled_tokens != provider_usage.known_total_tokens):
            raise ValueError("provider_reported settlement 缺少完整费用事实")
        if effect == "not_started" and (extra["proof_sha256"] is not None or (provider_usage is not None and (native or provider_usage.known_total_tokens != 0)) or (reservation and reservation.state == "settled")):
            raise ValueError("not_started 包含外部影响")
        if state is EmbeddingRebuildLedgerState.AWAITING_PUBLISH and extra["proof_sha256"] is None:
            raise ValueError("awaiting_publish 缺少 proof")
        if native and state in {EmbeddingRebuildLedgerState.PROCESSING, EmbeddingRebuildLedgerState.AWAITING_PUBLISH, EmbeddingRebuildLedgerState.ACTIVATION_INTENT} and (reservation is None or reservation.state != "reserved"):
            raise ValueError("进行中的 operation 缺少 reservation")
        if state is EmbeddingRebuildLedgerState.FAILED and (reservation is None or reservation.state != ("released" if effect == "not_started" else "settled")):
            raise ValueError("failed operation 缺少结算或释放事实")
        if native and reservation and reservation.state == "settled" and reservation.settled_tokens < max(reservation.reserved_tokens, provider_usage.known_total_tokens if provider_usage else 0):
            raise ValueError("settlement 低于已知费用")
    if state is EmbeddingRebuildLedgerState.BUDGET_PAUSED and (reservation is not None or effect != "not_started" or provider_usage is not None or generation_id is not None):
        raise ValueError("budget_paused 包含外部影响")
    if state is EmbeddingRebuildLedgerState.COMPLETED:
        if reservation is None or reservation.state != "settled" or generation_id is None:
            raise ValueError("completed operation 不完整")
    return EmbeddingRebuildLedgerOperation(
        operation_id=_require_operation_id(value["operation_id"]),
        plan_id=plan_id,
        public_plan_id=(
            None
            if schema_version == 1 or (schema_version == 3 and extra["record_origin"] == "legacy_v1" and value["public_plan_id"] is None)
            else _require_sha256(value["public_plan_id"], label="public_plan_id")
        ),
        replay_key=(
            plan_id
            if schema_version == 1
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
        **extra,
    )


def _aggregate_provider_usage(usages: Iterable[TokenUsage]) -> TokenUsage | None:
    """Sum only reported fields, retaining ``None`` when every response omitted one."""

    values = tuple(usages)
    if not all(isinstance(usage, TokenUsage) and usage.source == "provider_reported" for usage in values):
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
        self._clock = clock or (lambda: current_utc())
        self.path = Path(layout.runtime_state_dir) / "embedding-rebuild-ledger.json"

    def _require_writer(self) -> None:
        require_active_data_root_writer(self._layout, owner="embedding_generation")

    @staticmethod
    def _now(clock: Callable[[], datetime]) -> datetime:
        value = clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise ValueError("Embedding rebuild ledger 时钟必须返回有时区 datetime")
        return value

    def _read_document(self, *, expected_source_sha256: str | None = None) -> tuple[int | None, bytes | None, dict[str, object]]:
        try:
            target = self._layout.validate_user_file(
                self.path,
                label="Embedding rebuild ledger",
                allow_missing=True,
            )
            if not target.exists():
                if expected_source_sha256 is not None:
                    raise _safe_error(ErrorCode.RUNTIME_PLAN_STALE, "Embedding rebuild 账本升级源已不存在。")
                return None, None, {"schema_version": 3, "operations": {}}
            with self._layout.open_user_file(
                target,
                "rb",
                label="Embedding rebuild ledger",
            ) as source:
                raw = source.read()
            if expected_source_sha256 is not None and hashlib.sha256(raw).hexdigest() != expected_source_sha256:
                raise _safe_error(ErrorCode.RUNTIME_PLAN_STALE, "Embedding rebuild 账本升级源字节已变化。")
            def closed_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
                result: dict[str, object] = {}
                for key, item in pairs:
                    if key in result:
                        raise ValueError("ledger JSON 重复字段")
                    result[key] = item
                return result

            payload = json.loads(raw.decode("utf-8"), object_pairs_hook=closed_object)
            if not isinstance(payload, dict):
                raise ValueError("ledger payload 无效")
            if set(payload) != {"schema_version", "operations"}:
                raise ValueError("ledger 顶层字段无效")
            version = payload["schema_version"]
            if type(version) is not int or version not in {1, 2, 3}:
                raise ValueError("ledger schema_version 不支持")
            operations = payload["operations"]
            if not isinstance(operations, dict) or not all(
                isinstance(key, str) for key in operations
            ):
                raise ValueError("ledger operations 无效")
            for operation_id, operation in operations.items():
                parsed = _operation_from_payload(operation, schema_version=version)
                if parsed.operation_id != operation_id:
                    raise ValueError("ledger operation identity 不匹配")
            return version, raw, payload
        except PKVRuntimeError:
            raise
        except (OSError, UnicodeError, TypeError, ValueError, KeyError, ZoneInfoNotFoundError) as exc:
            raise _safe_error(ErrorCode.REPAIR_REQUIRED, "Embedding rebuild 账本无法安全读取。") from exc

    def inspect(self) -> EmbeddingRebuildLedgerInspection:
        """Strictly validate every row without creating directories or taking a lease."""

        return self._inspect()

    def _inspect(self, *, expected_source_sha256: str | None = None) -> EmbeddingRebuildLedgerInspection:
        version, raw, payload = self._read_document(expected_source_sha256=expected_source_sha256)
        operations = tuple(
            _operation_from_payload(row, schema_version=version or 3)
            for _, row in sorted(payload["operations"].items())
        )
        upgrade = None
        if version in {1, 2}:
            assert raw is not None
            upgrade = {
                "from_schema": version, "to_schema": 3,
                "source_sha256": hashlib.sha256(raw).hexdigest(),
                "operation_count": len(operations),
                "blocking_operation_count": sum(_blocks_root(row) for row in operations),
            }
        return EmbeddingRebuildLedgerInspection(version, operations, upgrade)

    def _read_payload(self) -> dict[str, object]:
        inspection = self.inspect()
        if inspection.ledger_upgrade is not None:
            raise _safe_error(ErrorCode.EMBEDDING_LEDGER_UPGRADE_REQUIRED, "旧 Embedding rebuild 账本须经 v2 计划显式确认升级。")
        return {"schema_version": 3, "operations": {operation.operation_id: operation.to_dict() for operation in inspection.operations}}

    def upgrade(self, expected_upgrade: Mapping[str, object]) -> EmbeddingRebuildLedgerInspection:
        """Atomically convert all validated legacy facts after a raw-byte CAS."""

        self._require_writer()
        fields = {"from_schema", "to_schema", "source_sha256", "operation_count", "blocking_operation_count"}
        if not isinstance(expected_upgrade, Mapping) or set(expected_upgrade) != fields:
            raise ValueError("ledger_upgrade 字段无效")
        if type(expected_upgrade["from_schema"]) is not int or expected_upgrade["from_schema"] not in {1, 2} or type(expected_upgrade["to_schema"]) is not int or expected_upgrade["to_schema"] != 3:
            raise ValueError("ledger_upgrade schema 无效")
        _require_sha256(expected_upgrade["source_sha256"], label="source_sha256")
        for key in ("operation_count", "blocking_operation_count"):
            _require_nonnegative_int(expected_upgrade[key], label=key)
        inspection = self._inspect(expected_source_sha256=expected_upgrade["source_sha256"])
        if inspection.ledger_upgrade != dict(expected_upgrade):
            raise _safe_error(ErrorCode.RUNTIME_PLAN_STALE, "Embedding rebuild 账本升级材料已变化。")
        self._publish_payload({"schema_version": 3, "operations": {row.operation_id: row.to_dict() for row in inspection.operations}}, expected_upgrade=expected_upgrade)
        return self.inspect()

    def _publish_payload(self, payload: Mapping[str, object], *, expected_upgrade: Mapping[str, object] | None = None) -> None:
        self._require_writer()
        try:
            safe_payload = {
                "schema_version": EMBEDDING_REBUILD_LEDGER_SCHEMA_VERSION,
                "operations": payload["operations"],
            }
            # The exact schema validation below rejects every unknown field,
            # including any credential-shaped key.  We cannot reuse the generic
            # runtime-snapshot detector here because legitimate token *counts*
            # intentionally contain ``token`` in their field names.
            for operation_id, operation in dict(safe_payload["operations"]).items():
                if _operation_from_payload(operation).operation_id != operation_id:
                    raise ValueError("ledger operation identity 不匹配")
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
            def compare_upgrade_source() -> None:
                if expected_upgrade is not None and self._inspect(expected_source_sha256=expected_upgrade["source_sha256"]).ledger_upgrade != dict(expected_upgrade):
                    raise _safe_error(ErrorCode.RUNTIME_PLAN_STALE, "Embedding rebuild 账本在升级发布前发生变化。")

            self._layout.atomic_publish_user_file(
                target,
                label="Embedding rebuild ledger",
                data=encoded,
                pre_replace=compare_upgrade_source,
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

        return self.inspect().operations

    def get_operation(self, operation_id: str) -> EmbeddingRebuildLedgerOperation | None:
        """Read exactly one operation by opaque operation identity."""

        operation_id = _require_operation_id(operation_id)
        return next((row for row in self.read_operations() if row.operation_id == operation_id), None)

    @staticmethod
    def _token_total(
        operations: Mapping[str, object],
        *,
        timezone_name: str,
        period_key: str,
        period: str,
    ) -> int:
        total = 0
        current_start, current_end = _period_interval(period=period, period_key=period_key, timezone_name=timezone_name)
        for raw in operations.values():
            operation = _operation_from_payload(raw)
            reservation = operation.reservation
            if (
                reservation is None
                or reservation.state not in {"reserved", "settled"}
            ):
                continue
            try:
                original_start, original_end = _period_interval(
                    period=getattr(reservation, period_key), period_key=period_key,
                    timezone_name=reservation.timezone,
                )
            except (ValueError, OverflowError) as exc:
                raise _safe_error(ErrorCode.REPAIR_REQUIRED, "旧预算期间无法安全归入当前 hard cap。") from exc
            if original_start >= current_end or current_start >= original_end:
                continue
            # A changed policy/model/endpoint does not erase incurred tokens.
            # With only a historical day/month, overlap is charged in full;
            # inventing exact usage timestamps or proportional splits is unsafe.
            total += (
                reservation.settled_tokens
                if reservation.state == "settled"
                else max(reservation.reserved_tokens, operation.provider_usage.known_total_tokens if operation.provider_usage else 0)
            )
        return total

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
            if _blocks_root(operation) and operation.replay_key != replay_key:
                raise _safe_error(
                    ErrorCode.REPAIR_REQUIRED,
                    "存在无法安全关联的旧 Embedding rebuild operation；请先完成修复。",
                )
            if operation.replay_key != replay_key:
                continue
            if operation.state is EmbeddingRebuildLedgerState.REPAIR_REQUIRED:
                raise _safe_error(ErrorCode.REPAIR_REQUIRED, "Embedding rebuild 的外部影响尚未核对。")
            if operation.provider_effect == "not_started" and (
                operation.state is EmbeddingRebuildLedgerState.BUDGET_PAUSED
                or (operation.state is EmbeddingRebuildLedgerState.FAILED
                    and operation.reservation is not None
                    and operation.reservation.state == "released")
            ):
                continue
            if operation.state in {
                EmbeddingRebuildLedgerState.PROCESSING,
                EmbeddingRebuildLedgerState.AWAITING_PUBLISH,
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
        """Compatibility for private callers with the original v1 identity.

        The public v1/v2 façades use ``begin_v3`` and supply current material.
        A private old-identity caller cannot invent missing root/snapshot hashes;
        its identity remains explicitly legacy inside the schema-3 container.
        """

        return self._begin(
            plan_id=plan_id, public_plan_id=public_plan_id, replay_key=replay_key,
            source_revision=source_revision, policy=policy, estimated_tokens=estimated_tokens,
            identity={"record_origin": "legacy_v2", "identity_schema": "pkv.embedding.transition.v1",
                      "data_root_identity_sha256": None, "runtime_snapshot_sha256": None,
                      "contract_sha256": None},
            budget_check_period=None,
        )

    def begin_v3(
        self, *, plan_id: str, public_plan_id: str, replay_key: str,
        source_revision: str, policy: EmbeddingRebuildPolicyInspection,
        estimated_tokens: int, data_root_identity_sha256: str,
        runtime_snapshot_sha256: str, contract_sha256: str,
        budget_check_period: Mapping[str, str],
    ) -> EmbeddingRebuildLedgerOperation:
        identity = {
            "record_origin": "native_v3", "identity_schema": "pkv.embedding.transition.v2",
            "data_root_identity_sha256": _require_sha256(data_root_identity_sha256, label="data_root_identity_sha256"),
            "runtime_snapshot_sha256": _require_sha256(runtime_snapshot_sha256, label="runtime_snapshot_sha256"),
            "contract_sha256": _require_sha256(contract_sha256, label="contract_sha256"),
        }
        return self._begin(
            plan_id=plan_id, public_plan_id=public_plan_id, replay_key=replay_key,
            source_revision=source_revision, policy=policy, estimated_tokens=estimated_tokens,
            identity=identity, budget_check_period=_period_from_payload(budget_check_period),
        )

    def _begin(
        self, *, plan_id: str, public_plan_id: str, replay_key: str,
        source_revision: str, policy: EmbeddingRebuildPolicyInspection,
        estimated_tokens: int, identity: dict[str, object],
        budget_check_period: dict[str, str] | None,
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
        period = {"timezone": quota.timezone, "local_day": local_day, "local_month": local_month}
        if budget_check_period is not None and budget_check_period != period:
            raise _safe_error(ErrorCode.RUNTIME_PLAN_STALE, "Embedding rebuild 预算期间已变化。")
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
            timezone_name=quota.timezone,
            period_key="local_day",
            period=local_day,
        )
        month_total = self._token_total(
            operations,
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
                **identity,
                budget_check_period=period,
                provider_effect="not_started",
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
            **identity,
            budget_check_period=period,
            provider_effect="not_started",
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

    def _mutable_operation(self, operation_id: str) -> EmbeddingRebuildLedgerOperation:
        self._require_writer()
        payload = self._read_payload()  # Never implicitly upgrade a legacy file.
        raw = payload["operations"].get(_require_operation_id(operation_id))
        if raw is None:
            raise _safe_error(ErrorCode.RUNTIME_PLAN_STALE, "Embedding rebuild operation 已不存在。")
        return _operation_from_payload(raw)

    def bind_stage(self, operation_id: str, *, stage_key: str, grant_sha256: str) -> EmbeddingRebuildLedgerOperation:
        operation = self._mutable_operation(operation_id)
        if not isinstance(stage_key, str) or not _STAGE_KEY_PATTERN.fullmatch(stage_key):
            raise ValueError("stage_key 无效")
        _require_sha256(grant_sha256, label="grant_sha256")
        if operation.stage_key is not None:
            if operation.stage_key == stage_key and operation.grant_sha256 == grant_sha256:
                return operation
            raise _safe_error(ErrorCode.RUNTIME_PLAN_STALE, "Embedding rebuild stage 已绑定。")
        if operation.state is not EmbeddingRebuildLedgerState.PROCESSING or operation.provider_effect != "not_started":
            raise _safe_error(ErrorCode.RUNTIME_PLAN_STALE, "Embedding rebuild operation 不能分配 stage。")
        updated = replace(operation, stage_key=stage_key, grant_sha256=grant_sha256)
        self._replace(updated)
        return updated

    def mark_provider_unknown(self, operation_id: str) -> EmbeddingRebuildLedgerOperation:
        """Persist uncertainty in A, before the Provider is allowed to start."""

        operation = self._mutable_operation(operation_id)
        if operation.state is not EmbeddingRebuildLedgerState.PROCESSING:
            raise _safe_error(ErrorCode.RUNTIME_PLAN_STALE, "Embedding rebuild operation 不能开始 Provider 工作。")
        if operation.record_origin == "native_v3" and operation.stage_key is None:
            raise _safe_error(ErrorCode.REPAIR_REQUIRED, "Embedding rebuild stage 授权尚未绑定。")
        if operation.provider_effect == "unknown":
            return operation
        if operation.provider_effect != "not_started":
            raise _safe_error(ErrorCode.RUNTIME_PLAN_STALE, "Embedding Provider effect 已变化。")
        updated = replace(operation, provider_effect="unknown")
        self._replace(updated)
        return updated

    def adopt_proof(
        self, operation_id: str, *, proof_sha256: str, generation_id: str,
        provider_usages: Iterable[TokenUsage] = (), usage_completeness: str = "unknown",
    ) -> EmbeddingRebuildLedgerOperation:
        """Persist already validated C facts in D; this method performs no I/O in C."""

        operation = self._mutable_operation(operation_id)
        _require_sha256(proof_sha256, label="proof_sha256")
        if not isinstance(generation_id, str) or not _GENERATION_ID_PATTERN.fullmatch(generation_id):
            raise ValueError("generation_id 无效")
        if operation.record_origin != "native_v3" or operation.stage_key is None or operation.grant_sha256 is None:
            raise _safe_error(ErrorCode.REPAIR_REQUIRED, "旧 Embedding operation 不能补造 proof。")
        if operation.proof_sha256 is not None:
            if operation.proof_sha256 != proof_sha256 or operation.generation_id != generation_id:
                raise _safe_error(ErrorCode.RUNTIME_PLAN_STALE, "Embedding rebuild proof 已变化。")
            updated = self._with_provider_usage(operation, provider_usages, provider_attempted=True, usage_completeness=usage_completeness)
            if updated != operation:
                raise _safe_error(ErrorCode.RUNTIME_PLAN_STALE, "Embedding rebuild proof 用量已变化。")
            return operation
        if operation.state is not EmbeddingRebuildLedgerState.PROCESSING or operation.provider_effect != "unknown":
            raise _safe_error(ErrorCode.RUNTIME_PLAN_STALE, "Embedding rebuild operation 不能采用 proof。")
        updated = self._with_provider_usage(operation, provider_usages, provider_attempted=True, usage_completeness=usage_completeness)
        updated = replace(updated, proof_sha256=proof_sha256, generation_id=generation_id,
                          provider_effect="completed", state=EmbeddingRebuildLedgerState.AWAITING_PUBLISH)
        self._replace(updated)
        return updated

    def mark_activation_intent(
        self,
        operation_id: str,
        *,
        generation_id: str,
    ) -> EmbeddingRebuildLedgerOperation:
        """Record the final generation before the irreversible pointer CAS."""

        operation = self._mutable_operation(operation_id)
        if not isinstance(generation_id, str) or _GENERATION_ID_PATTERN.fullmatch(generation_id) is None:
            raise ValueError("generation_id 无效")
        if operation.state is EmbeddingRebuildLedgerState.ACTIVATION_INTENT:
            if operation.generation_id != generation_id:
                raise _safe_error(ErrorCode.RUNTIME_PLAN_STALE, "Embedding rebuild activation intent 已变化。")
            return operation
        if operation.state not in {EmbeddingRebuildLedgerState.PROCESSING, EmbeddingRebuildLedgerState.AWAITING_PUBLISH}:
            raise _safe_error(ErrorCode.RUNTIME_PLAN_STALE, "Embedding rebuild operation 不能进入 activation intent。")
        if operation.record_origin == "native_v3" and (operation.proof_sha256 is None or operation.generation_id != generation_id):
            raise _safe_error(ErrorCode.REPAIR_REQUIRED, "Embedding rebuild activation intent 缺少已验证 proof。")
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
        usage_completeness: str | None = None,
    ) -> EmbeddingRebuildLedgerOperation:
        reported = _aggregate_provider_usage(usages)
        if operation.provider_usage is not None:
            if reported is not None and operation.provider_usage != reported:
                raise _safe_error(ErrorCode.RUNTIME_PLAN_STALE, "Embedding Provider usage 已变化。")
            reported = operation.provider_usage
        completeness = usage_completeness or (operation.usage_completeness if operation.provider_usage is not None else ("partial" if reported is not None else "unknown"))
        if completeness not in {"complete", "partial", "unknown"} or ((completeness == "unknown") != (reported is None)):
            raise ValueError("usage_completeness 与 Provider usage 不一致")
        return EmbeddingRebuildLedgerOperation(
            **{
                **operation.__dict__,
                "provider_usage": reported,
                "provider_effect": "unknown" if operation.provider_effect == "not_started" and provider_attempted else operation.provider_effect,
                "usage_completeness": completeness,
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
        provider_attempted: bool = True,
    ) -> EmbeddingRebuildLedgerOperation:
        """Settle facts after a successful pointer activation.

        The method is idempotent for the same operation; callers can therefore
        reconcile an interrupted final accounting write without re-running any
        Provider operation.
        """

        operation = self._mutable_operation(operation_id)
        if operation.state is EmbeddingRebuildLedgerState.COMPLETED:
            if self._with_provider_usage(operation, provider_usages, provider_attempted=provider_attempted) != operation:
                raise _safe_error(ErrorCode.RUNTIME_PLAN_STALE, "Embedding rebuild 完成费用事实已冻结。")
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
                "provider_effect": "completed" if updated.record_origin == "native_v3" else "unknown",
                "settlement_basis": self._settlement_basis(updated, settled),
            }
        )
        self._replace(completed)
        return completed

    @staticmethod
    def _settlement_basis(operation: EmbeddingRebuildLedgerOperation, reservation: EmbeddingRebuildReservation | None) -> str | None:
        if reservation is None or reservation.state != "settled":
            return None
        if operation.provider_usage is not None and operation.usage_completeness == "complete" and reservation.settled_tokens == operation.provider_usage.known_total_tokens:
            return "provider_reported"
        return "conservative_reservation"

    def mark_failed(
        self,
        operation_id: str,
        *,
        error_code: ErrorCode | str,
        provider_usages: Iterable[TokenUsage] = (),
        provider_attempted: bool,
        provider_not_started: bool = False,
    ) -> EmbeddingRebuildLedgerOperation:
        """Settle or release a reservation after a failed pre-activation run."""

        operation = self._mutable_operation(operation_id)
        if operation.state is EmbeddingRebuildLedgerState.FAILED:
            if self._with_provider_usage(operation, provider_usages, provider_attempted=provider_attempted) != operation:
                raise _safe_error(ErrorCode.RUNTIME_PLAN_STALE, "Embedding rebuild 失败费用事实已冻结。")
            return operation
        if operation.state in {
            EmbeddingRebuildLedgerState.COMPLETED,
            EmbeddingRebuildLedgerState.BUDGET_PAUSED,
            EmbeddingRebuildLedgerState.REPAIR_REQUIRED,
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
        if provider_not_started:
            if provider_attempted or updated.provider_usage is not None or operation.proof_sha256 is not None or operation.state is not EmbeddingRebuildLedgerState.PROCESSING:
                raise _safe_error(ErrorCode.REPAIR_REQUIRED, "Provider 未开始证明与已发生事实冲突。")
            updated = replace(updated, provider_effect="not_started")
        attempted = updated.provider_attempted
        settled = self._settled_reservation(updated, provider_attempted=attempted)
        failed = EmbeddingRebuildLedgerOperation(
            **{
                **updated.__dict__,
                "state": EmbeddingRebuildLedgerState.FAILED,
                "reservation": settled,
                "settlement_basis": self._settlement_basis(updated, settled),
                "last_error_code": code,
            }
        )
        self._replace(failed)
        return failed


__all__ = [
    "EMBEDDING_REBUILD_LEDGER_SCHEMA_VERSION",
    "EmbeddingRebuildLedger",
    "EmbeddingRebuildLedgerOperation",
    "EmbeddingRebuildLedgerInspection",
    "EmbeddingRebuildLedgerState",
    "EmbeddingRebuildReservation",
    "current_period",
    "current_utc",
]
