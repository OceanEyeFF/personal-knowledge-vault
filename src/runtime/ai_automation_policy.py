"""Pure, explicit-config contract for internal AI automation.

This module intentionally owns no queue, Provider, clock-driven worker, or
data-root write.  It gives later R4 mutation owners one strict answer to three
questions before they can construct a Provider: is automation enabled, has the
current provider/model/token policy been approved, and what token hard caps
apply?  Price cards are optional; their absence never invents a monetary value
or prevents token-only accounting.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from src.runtime.errors import ErrorCode
from src.utils.config import endpoint_contract_sha256


AUTOMATION_POLICY_SCHEMA_VERSION = 1
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_CURRENCY_PATTERN = re.compile(r"[A-Z]{3}\Z")


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and _SHA256_PATTERN.fullmatch(value) is not None


def _optional_nonnegative_int(value: object, *, label: str) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value < 0:
        raise ValueError(f"{label} 必须是非负整数或 null")
    return value


@dataclass(frozen=True)
class TokenQuota:
    """One approved local-calendar quota; zero is an intentional hard stop."""

    timezone: str
    daily_total_tokens: int | None
    monthly_total_tokens: int | None

    def __post_init__(self) -> None:
        if not isinstance(self.timezone, str) or not self.timezone:
            raise ValueError("token 配额必须声明 IANA 时区")
        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("token 配额时区无效") from exc
        for value in (self.daily_total_tokens, self.monthly_total_tokens):
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError("token 配额必须是非负整数或 null")

    @property
    def has_hard_cap(self) -> bool:
        return self.daily_total_tokens is not None or self.monthly_total_tokens is not None

    def to_dict(self) -> dict[str, object]:
        return {
            "timezone": self.timezone,
            "daily_total_tokens": self.daily_total_tokens,
            "monthly_total_tokens": self.monthly_total_tokens,
        }


@dataclass(frozen=True)
class OptionalPricePolicy:
    """A declared optional price-card reference, never a calculated price."""

    card_id: str
    card_sha256: str
    currency: str
    daily_cap_micros: int | None
    monthly_cap_micros: int | None

    def __post_init__(self) -> None:
        if not isinstance(self.card_id, str) or not self.card_id:
            raise ValueError("price card id 无效")
        if not _is_sha256(self.card_sha256):
            raise ValueError("price card sha256 无效")
        if not isinstance(self.currency, str) or _CURRENCY_PATTERN.fullmatch(self.currency) is None:
            raise ValueError("price card 币种无效")
        for value in (self.daily_cap_micros, self.monthly_cap_micros):
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError("金额上限必须是非负整数或 null")

    def to_dict(self) -> dict[str, object]:
        return {
            "card_id": self.card_id,
            "card_sha256": self.card_sha256,
            "currency": self.currency,
            "daily_cap_micros": self.daily_cap_micros,
            "monthly_cap_micros": self.monthly_cap_micros,
        }


@dataclass(frozen=True)
class TokenUsage:
    """Usage facts without treating provider omissions as zero.

    ``None`` means the provider did not report that particular dimension.  A
    later usage ledger may record a local estimate/reservation separately, but
    cannot rewrite a missing cached/uncached/generated value to ``0``.
    """

    uncached_input_tokens: int | None = None
    cached_input_tokens: int | None = None
    generated_tokens: int | None = None
    embedding_input_tokens: int | None = None
    source: str = "provider_reported"

    def __post_init__(self) -> None:
        if self.source not in {
            "provider_reported",
            "local_estimate",
            "conservative_reservation",
        }:
            raise ValueError("usage source 无效")
        values = (
            self.uncached_input_tokens,
            self.cached_input_tokens,
            self.generated_tokens,
            self.embedding_input_tokens,
        )
        if all(value is None for value in values):
            raise ValueError("用量至少需要一个已知 token 字段")
        if any(value is not None and (type(value) is not int or value < 0) for value in values):
            raise ValueError("token 用量必须是非负整数或 null")

    @property
    def known_total_tokens(self) -> int:
        return sum(
            value
            for value in (
                self.uncached_input_tokens,
                self.cached_input_tokens,
                self.generated_tokens,
                self.embedding_input_tokens,
            )
            if value is not None
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "uncached_input_tokens": self.uncached_input_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "generated_tokens": self.generated_tokens,
            "embedding_input_tokens": self.embedding_input_tokens,
            "source": self.source,
        }


class AutomationPolicyState(str, Enum):
    """The safe result of inspecting one explicit Config snapshot."""

    DISABLED = "disabled"
    AUTHORIZATION_REQUIRED = "authorization_required"
    READY = "ready"
    INVALID = "invalid"


@dataclass(frozen=True)
class AutomationPolicyIssue:
    code: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


@dataclass(frozen=True)
class AutomationPolicyInspection:
    """Pure result for one Config; it never exposes credentials or raw endpoints."""

    state: AutomationPolicyState
    policy_fingerprint: str | None
    token_quota: TokenQuota | None
    retry_max_attempts: int | None
    price_policy: OptionalPricePolicy | None
    issues: tuple[AutomationPolicyIssue, ...]

    @property
    def is_authorized(self) -> bool:
        return self.state is AutomationPolicyState.READY

    def to_dict(self) -> dict[str, object]:
        return {
            "state": self.state.value,
            "policy_fingerprint": self.policy_fingerprint,
            "token_quota": self.token_quota.to_dict() if self.token_quota else None,
            "retry_max_attempts": self.retry_max_attempts,
            "price_policy": self.price_policy.to_dict() if self.price_policy else None,
            "issues": [issue.to_dict() for issue in self.issues],
        }


def _mapping(value: object, *, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} 必须是映射")
    if not all(isinstance(key, str) for key in value):
        raise ValueError(f"{label} 的键必须是字符串")
    return value


def _exact_keys(value: Mapping[str, Any], *, label: str, allowed: set[str]) -> None:
    unknown = set(value).difference(allowed)
    if unknown:
        raise ValueError(f"{label} 包含未知字段")


def _provider_contract(config: Any) -> dict[str, object]:
    try:
        llm_provider = config.llm_provider
        llm_base_url = config.llm_base_url
        llm_model = config.llm_model
        embedding_provider = config.embd_provider
        embedding_base_url = config.embd_base_url
        embedding_model = config.embd_model
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("当前 Config 无法解析 AI Provider contract") from exc
    fields = (
        llm_provider,
        llm_base_url,
        llm_model,
        embedding_provider,
        embedding_base_url,
        embedding_model,
    )
    if not all(isinstance(value, str) and value for value in fields):
        raise ValueError("AI Provider contract 无效")
    return {
        "llm": {
            "provider": llm_provider,
            "endpoint_sha256": endpoint_contract_sha256(llm_base_url),
            "model": llm_model,
        },
        "embedding": {
            "provider": embedding_provider,
            "endpoint_sha256": endpoint_contract_sha256(embedding_base_url),
            "model": embedding_model,
        },
    }


def _parse_price_policy(raw: object) -> OptionalPricePolicy | None:
    if raw is None:
        return None
    value = _mapping(raw, label="ai.automation.pricing")
    _exact_keys(
        value,
        label="ai.automation.pricing",
        allowed={
            "card_id",
            "card_sha256",
            "currency",
            "daily_cap_micros",
            "monthly_cap_micros",
        },
    )
    return OptionalPricePolicy(
        card_id=value.get("card_id"),
        card_sha256=value.get("card_sha256"),
        currency=value.get("currency"),
        daily_cap_micros=_optional_nonnegative_int(
            value.get("daily_cap_micros"), label="daily_cap_micros"
        ),
        monthly_cap_micros=_optional_nonnegative_int(
            value.get("monthly_cap_micros"), label="monthly_cap_micros"
        ),
    )


def inspect_ai_automation_policy(config: Any) -> AutomationPolicyInspection:
    """Inspect one explicit Config without writing, constructing a Provider, or probing a network.

    The caller receives the computed fingerprint even when approval is missing,
    allowing a future settings UI to present a one-time confirmation.  Only the
    matching hash may authorize automatic paid/network work.
    """

    try:
        getter = getattr(config, "get", None)
        if not callable(getter):
            raise ValueError("config 必须是显式 Config snapshot")
        raw = _mapping(getter("ai.automation", None), label="ai.automation")
        _exact_keys(
            raw,
            label="ai.automation",
            allowed={
                "schema_version",
                "enabled",
                "token_budget",
                "retry",
                "authorization",
                "pricing",
            },
        )
        if raw.get("schema_version") != AUTOMATION_POLICY_SCHEMA_VERSION:
            raise ValueError("ai.automation schema_version 不受支持")
        enabled = raw.get("enabled")
        if type(enabled) is not bool:
            raise ValueError("ai.automation.enabled 必须是 bool")

        budget = _mapping(raw.get("token_budget"), label="ai.automation.token_budget")
        _exact_keys(
            budget,
            label="ai.automation.token_budget",
            allowed={"timezone", "daily_total_tokens", "monthly_total_tokens"},
        )
        quota = TokenQuota(
            timezone=budget.get("timezone"),
            daily_total_tokens=_optional_nonnegative_int(
                budget.get("daily_total_tokens"), label="daily_total_tokens"
            ),
            monthly_total_tokens=_optional_nonnegative_int(
                budget.get("monthly_total_tokens"), label="monthly_total_tokens"
            ),
        )

        retry = _mapping(raw.get("retry"), label="ai.automation.retry")
        _exact_keys(retry, label="ai.automation.retry", allowed={"max_attempts"})
        retry_max_attempts = retry.get("max_attempts")
        if type(retry_max_attempts) is not int or not 0 <= retry_max_attempts <= 10:
            raise ValueError("retry.max_attempts 必须是 0..10 的整数")
        price_policy = _parse_price_policy(raw.get("pricing"))

        fingerprint = _canonical_sha256(
            {
                "schema_version": AUTOMATION_POLICY_SCHEMA_VERSION,
                "provider_contract": _provider_contract(config),
                "token_budget": quota.to_dict(),
                "retry_max_attempts": retry_max_attempts,
                "price_policy": price_policy.to_dict() if price_policy else None,
            }
        )
        if not enabled:
            return AutomationPolicyInspection(
                AutomationPolicyState.DISABLED,
                fingerprint,
                quota,
                retry_max_attempts,
                price_policy,
                (),
            )
        if not quota.has_hard_cap:
            raise ValueError("启用自动化至少需要一个 token hard cap")

        approval_raw = raw.get("authorization")
        approval = _mapping(approval_raw, label="ai.automation.authorization")
        _exact_keys(
            approval,
            label="ai.automation.authorization",
            allowed={"policy_sha256"},
        )
        approved_fingerprint = approval.get("policy_sha256")
        if approved_fingerprint != fingerprint:
            return AutomationPolicyInspection(
                AutomationPolicyState.AUTHORIZATION_REQUIRED,
                fingerprint,
                quota,
                retry_max_attempts,
                price_policy,
                (
                    AutomationPolicyIssue(
                        ErrorCode.EMBEDDING_AUTOMATION_AUTHORIZATION_REQUIRED.value,
                        "AI 自动化配置、模型、token 配额或可选计价策略尚未确认。",
                    ),
                ),
            )
        return AutomationPolicyInspection(
            AutomationPolicyState.READY,
            fingerprint,
            quota,
            retry_max_attempts,
            price_policy,
            (),
        )
    except (AttributeError, TypeError, ValueError) as exc:
        return AutomationPolicyInspection(
            AutomationPolicyState.INVALID,
            None,
            None,
            None,
            None,
            (
                AutomationPolicyIssue(
                    ErrorCode.EMBEDDING_AUTOMATION_AUTHORIZATION_REQUIRED.value,
                    "AI 自动化配置无效，已暂停自动任务。",
                ),
            ),
        )


__all__ = [
    "AUTOMATION_POLICY_SCHEMA_VERSION",
    "AutomationPolicyInspection",
    "AutomationPolicyIssue",
    "AutomationPolicyState",
    "OptionalPricePolicy",
    "TokenQuota",
    "TokenUsage",
    "inspect_ai_automation_policy",
]
