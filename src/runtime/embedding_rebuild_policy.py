"""Pure authorization and budget policy for a public Embedding rebuild.

The R4 ``ai.automation`` policy authorizes an *internal* durable Q2 worker.
It must not be silently reused by a foreground maintenance action: a user who
confirms one rebuild must not thereby enable background derivation work.  This
module therefore reads a small independent policy block and performs no write,
Provider construction, or network operation.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping

from src.runtime.ai_automation_policy import TokenQuota
from src.runtime.errors import ErrorCode
from src.utils.config import endpoint_contract_sha256


EMBEDDING_REBUILD_POLICY_SCHEMA_VERSION = 1


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _mapping(value: object, *, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise ValueError(f"{label} 必须是映射")
    return value


def _optional_nonnegative_int(value: object, *, label: str) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value < 0:
        raise ValueError(f"{label} 必须是非负整数或 null")
    return value


class EmbeddingRebuildPolicyState(str, Enum):
    """The only safe outcomes before a foreground Provider is constructed."""

    READY = "ready"
    AUTHORIZATION_REQUIRED = "authorization_required"
    INVALID = "invalid"


@dataclass(frozen=True)
class EmbeddingRebuildPolicyIssue:
    code: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


@dataclass(frozen=True)
class EmbeddingRebuildPolicyInspection:
    """Secret-free policy result for exactly one immutable Config snapshot."""

    state: EmbeddingRebuildPolicyState
    policy_fingerprint: str | None
    token_quota: TokenQuota | None
    issues: tuple[EmbeddingRebuildPolicyIssue, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "state": self.state.value,
            "policy_fingerprint": self.policy_fingerprint,
            "token_quota": self.token_quota.to_dict() if self.token_quota else None,
            "issues": [issue.to_dict() for issue in self.issues],
        }


def _provider_contract(config: Any) -> dict[str, object]:
    try:
        provider = config.embd_provider
        endpoint = config.embd_base_url
        model = config.embd_model
        dimension = config.embedding_dim
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("当前 Config 无法解析 Embedding Provider contract") from exc
    if (
        not isinstance(provider, str)
        or not provider
        or not isinstance(endpoint, str)
        or not endpoint
        or not isinstance(model, str)
        or not model
        or type(dimension) is not int
        or dimension <= 0
    ):
        raise ValueError("Embedding Provider contract 无效")
    return {
        "provider": provider,
        "endpoint_sha256": endpoint_contract_sha256(endpoint),
        "model": model,
        "dimension": dimension,
    }


def inspect_embedding_rebuild_policy(config: Any) -> EmbeddingRebuildPolicyInspection:
    """Read the independent foreground rebuild budget without side effects.

    Explicit ``approved + allow_network`` confirmation is the authorization for
    this single request.  The policy block contributes only the token hard cap
    and its stable fingerprint; it never turns on ``ai.automation``.
    """

    try:
        getter = getattr(config, "get", None)
        if not callable(getter):
            raise ValueError("config 必须是显式 Config snapshot")
        raw_value = getter("ai.embedding_rebuild", None)
        if raw_value is None:
            return EmbeddingRebuildPolicyInspection(
                EmbeddingRebuildPolicyState.AUTHORIZATION_REQUIRED,
                None,
                None,
                (
                    EmbeddingRebuildPolicyIssue(
                        ErrorCode.EMBEDDING_REBUILD_AUTHORIZATION_REQUIRED.value,
                        "尚未设置手动 Embedding 重建的 token hard cap。",
                    ),
                ),
            )
        raw = _mapping(raw_value, label="ai.embedding_rebuild")
        if set(raw) != {"schema_version", "token_budget"}:
            raise ValueError("ai.embedding_rebuild 包含未知或缺失字段")
        if raw.get("schema_version") != EMBEDDING_REBUILD_POLICY_SCHEMA_VERSION:
            raise ValueError("ai.embedding_rebuild schema_version 不受支持")
        budget = _mapping(raw.get("token_budget"), label="ai.embedding_rebuild.token_budget")
        if set(budget) != {"timezone", "daily_total_tokens", "monthly_total_tokens"}:
            raise ValueError("ai.embedding_rebuild.token_budget 字段无效")
        quota = TokenQuota(
            timezone=budget.get("timezone"),
            daily_total_tokens=_optional_nonnegative_int(
                budget.get("daily_total_tokens"), label="daily_total_tokens"
            ),
            monthly_total_tokens=_optional_nonnegative_int(
                budget.get("monthly_total_tokens"), label="monthly_total_tokens"
            ),
        )
        fingerprint = _canonical_sha256(
            {
                "schema_version": EMBEDDING_REBUILD_POLICY_SCHEMA_VERSION,
                "provider_contract": _provider_contract(config),
                "token_budget": quota.to_dict(),
            }
        )
        if not quota.has_hard_cap:
            return EmbeddingRebuildPolicyInspection(
                EmbeddingRebuildPolicyState.AUTHORIZATION_REQUIRED,
                fingerprint,
                quota,
                (
                    EmbeddingRebuildPolicyIssue(
                        ErrorCode.EMBEDDING_REBUILD_AUTHORIZATION_REQUIRED.value,
                        "手动 Embedding 重建至少需要一个 token hard cap。",
                    ),
                ),
            )
        return EmbeddingRebuildPolicyInspection(
            EmbeddingRebuildPolicyState.READY,
            fingerprint,
            quota,
            (),
        )
    except (AttributeError, TypeError, ValueError):
        return EmbeddingRebuildPolicyInspection(
            EmbeddingRebuildPolicyState.INVALID,
            None,
            None,
            (
                EmbeddingRebuildPolicyIssue(
                    ErrorCode.EMBEDDING_REBUILD_AUTHORIZATION_REQUIRED.value,
                    "手动 Embedding 重建策略无效；不会创建 Provider。",
                ),
            ),
        )


__all__ = [
    "EMBEDDING_REBUILD_POLICY_SCHEMA_VERSION",
    "EmbeddingRebuildPolicyInspection",
    "EmbeddingRebuildPolicyIssue",
    "EmbeddingRebuildPolicyState",
    "inspect_embedding_rebuild_policy",
]
