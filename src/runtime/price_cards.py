"""Validated optional price-card resources for R4 Q2 cost controls.

The bundled resource is intentionally empty by default: token-only automation
continues to work, and no guessed currency amount is ever displayed.  A user
policy can opt into a reviewed card only by naming both its id and canonical
digest; any card/rate/model-policy change therefore requires re-confirmation.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml

from src.runtime.ai_automation_policy import OptionalPricePolicy, TokenUsage
from src.runtime.errors import ErrorCode, PKVRuntimeError
from src.runtime.layout import open_user_file_nofollow


_RESOURCE_NAME = "price-cards.yaml"
_SCHEMA_VERSION = 1


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _nonnegative_rate(value: object, *, label: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{label} 必须是非负整数 micro-currency / million tokens")
    return value


@dataclass(frozen=True)
class PriceCard:
    card_id: str
    currency: str
    llm_uncached_input_micros_per_million: int
    llm_cached_input_micros_per_million: int
    llm_generated_micros_per_million: int
    embedding_input_micros_per_million: int
    sha256: str

    def amount_for_usage(self, stage: str, usage: TokenUsage) -> int | None:
        """Return only an exact, fully reported amount; otherwise leave it NULL."""

        if stage == "embedding":
            if usage.embedding_input_tokens is None:
                return None
            return self._micros(
                usage.embedding_input_tokens,
                self.embedding_input_micros_per_million,
            )
        if stage not in {"summary", "tags"}:
            raise ValueError("price usage stage 无效")
        if usage.uncached_input_tokens is None or usage.generated_tokens is None:
            return None
        if (
            self.llm_cached_input_micros_per_million > 0
            and usage.cached_input_tokens is None
        ):
            return None
        return (
            self._micros(
                usage.uncached_input_tokens,
                self.llm_uncached_input_micros_per_million,
            )
            + self._micros(
                usage.cached_input_tokens or 0,
                self.llm_cached_input_micros_per_million,
            )
            + self._micros(
                usage.generated_tokens,
                self.llm_generated_micros_per_million,
            )
        )

    def conservative_embedding_amount(self, tokens: int) -> int:
        if type(tokens) is not int or tokens < 0:
            raise ValueError("tokens 必须是非负整数")
        return self._micros(tokens, self.embedding_input_micros_per_million)

    def conservative_llm_amount(
        self,
        *,
        uncached_input_tokens: int,
        generated_tokens: int,
    ) -> int:
        """Ceiling-priced LLM reserve using no assumed cache discount."""

        if (
            type(uncached_input_tokens) is not int
            or uncached_input_tokens < 0
            or type(generated_tokens) is not int
            or generated_tokens < 0
        ):
            raise ValueError("LLM reservation token 数必须是非负整数")
        return self._micros(
            uncached_input_tokens,
            self.llm_uncached_input_micros_per_million,
        ) + self._micros(
            generated_tokens,
            self.llm_generated_micros_per_million,
        )

    @staticmethod
    def _micros(tokens: int, rate_per_million: int) -> int:
        # Ceiling is deliberately conservative for pre-Provider reservation.
        return (tokens * rate_per_million + 999_999) // 1_000_000


def _resource_path(config: Any) -> Path:
    return config.layout.validate_bundled_path(
        Path(config.layout.resources_root) / "config" / _RESOURCE_NAME,
        label="R4 price card resource",
    )


def _read_cards(config: Any) -> dict[str, PriceCard]:
    path = _resource_path(config)
    try:
        with open_user_file_nofollow(
            path,
            "r",
            label="R4 price card resource",
            encoding="utf-8",
        ) as handle:
            raw = yaml.safe_load(handle) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise PKVRuntimeError(
            ErrorCode.EMBEDDING_AUTOMATION_AUTHORIZATION_REQUIRED,
            "可选价格卡资源不可验证；自动化已暂停。",
            stage="r4_price_card",
            recoverable=True,
        ) from exc
    if not isinstance(raw, Mapping) or raw.get("schema_version") != _SCHEMA_VERSION:
        raise PKVRuntimeError(
            ErrorCode.EMBEDDING_AUTOMATION_AUTHORIZATION_REQUIRED,
            "可选价格卡资源 schema 无效；自动化已暂停。",
            stage="r4_price_card",
            recoverable=True,
        )
    cards_raw = raw.get("cards")
    if not isinstance(cards_raw, list):
        raise PKVRuntimeError(
            ErrorCode.EMBEDDING_AUTOMATION_AUTHORIZATION_REQUIRED,
            "可选价格卡资源 cards 字段无效；自动化已暂停。",
            stage="r4_price_card",
            recoverable=True,
        )
    cards: dict[str, PriceCard] = {}
    for item in cards_raw:
        try:
            if not isinstance(item, Mapping) or set(item) != {
                "card_id",
                "currency",
                "llm_uncached_input_micros_per_million",
                "llm_cached_input_micros_per_million",
                "llm_generated_micros_per_million",
                "embedding_input_micros_per_million",
            }:
                raise ValueError("字段不完整")
            card_id = item.get("card_id")
            currency = item.get("currency")
            if not isinstance(card_id, str) or not card_id:
                raise ValueError("card_id")
            if not isinstance(currency, str) or len(currency) != 3 or not currency.isupper():
                raise ValueError("currency")
            canonical = {
                "card_id": card_id,
                "currency": currency,
                "llm_uncached_input_micros_per_million": _nonnegative_rate(
                    item.get("llm_uncached_input_micros_per_million"),
                    label="llm_uncached_input_micros_per_million",
                ),
                "llm_cached_input_micros_per_million": _nonnegative_rate(
                    item.get("llm_cached_input_micros_per_million"),
                    label="llm_cached_input_micros_per_million",
                ),
                "llm_generated_micros_per_million": _nonnegative_rate(
                    item.get("llm_generated_micros_per_million"),
                    label="llm_generated_micros_per_million",
                ),
                "embedding_input_micros_per_million": _nonnegative_rate(
                    item.get("embedding_input_micros_per_million"),
                    label="embedding_input_micros_per_million",
                ),
            }
            if card_id in cards:
                raise ValueError("重复 card_id")
            cards[card_id] = PriceCard(**canonical, sha256=_canonical_sha256(canonical))
        except (TypeError, ValueError) as exc:
            raise PKVRuntimeError(
                ErrorCode.EMBEDDING_AUTOMATION_AUTHORIZATION_REQUIRED,
                "可选价格卡条目无效；自动化已暂停。",
                stage="r4_price_card",
                recoverable=True,
            ) from exc
    return cards


def resolve_price_card(config: Any, policy: OptionalPricePolicy | None) -> PriceCard | None:
    if policy is None:
        return None
    card = _read_cards(config).get(policy.card_id)
    if card is None or card.currency != policy.currency or card.sha256 != policy.card_sha256:
        raise PKVRuntimeError(
            ErrorCode.EMBEDDING_AUTOMATION_AUTHORIZATION_REQUIRED,
            "价格卡已变化、缺失或未确认；自动化已暂停。",
            stage="r4_price_card",
            recoverable=True,
        )
    return card


__all__ = ["PriceCard", "resolve_price_card"]
