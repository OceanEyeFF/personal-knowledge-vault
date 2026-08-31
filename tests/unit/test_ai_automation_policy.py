"""R4 P0 token-first automation policy contracts (offline and zero-write)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.runtime.ai_automation_policy import (
    AutomationPolicyState,
    TokenUsage,
    inspect_ai_automation_policy,
)
from src.runtime.layout import RuntimeLayout
from src.utils.config import Config


PROJECT_ROOT = Path(__file__).parent.parent.parent


def _configured(
    tmp_path: Path,
    automation: dict[str, object] | None = None,
) -> tuple[RuntimeLayout, Config]:
    """Build an explicit fake-only Config without creating its data root."""

    layout = RuntimeLayout.resolve(
        resources_root=PROJECT_ROOT,
        user_data_root=tmp_path / "data",
        profile_root=tmp_path / "profile",
        environment={},
    )
    payload: dict[str, object] = {
        "ai": {
            "llm": {
                "api_key": "llm-test-secret",
                "base_url": "https://llm.invalid/v1",
                "model": "llm-fake-1",
            },
            "embedding": {
                "api_key": "embedding-test-secret",
                "base_url": "https://embedding.invalid/v1",
                "model": "embedding-fake-1",
                "dim": 3,
            },
        }
    }
    if automation is not None:
        payload["ai"] = {**payload["ai"], "automation": automation}
    layout.user_config_path.parent.mkdir(parents=True, exist_ok=True)
    layout.user_config_path.write_text(json.dumps(payload), encoding="utf-8")
    return layout, Config(layout=layout)


def _enabled_policy(*, approval: str | None = None) -> dict[str, object]:
    return {
        "schema_version": 1,
        "enabled": True,
        "authorization": {"policy_sha256": approval},
        "token_budget": {
            "timezone": "Asia/Shanghai",
            "daily_total_tokens": 1000,
            "monthly_total_tokens": 10_000,
        },
        "retry": {"max_attempts": 2},
    }


def test_disabled_policy_is_zero_write_and_constructs_no_provider(tmp_path: Path) -> None:
    layout, config = _configured(tmp_path)

    inspection = inspect_ai_automation_policy(config)

    assert inspection.state is AutomationPolicyState.DISABLED
    assert inspection.token_quota is not None
    assert inspection.token_quota.has_hard_cap is False
    assert inspection.price_policy is None
    assert inspection.policy_fingerprint is not None
    assert not layout.user_data_root.exists()


def test_enabled_policy_needs_one_time_matching_approval_not_a_price_card(
    tmp_path: Path,
) -> None:
    _, unapproved = _configured(tmp_path, _enabled_policy())

    pending = inspect_ai_automation_policy(unapproved)

    assert pending.state is AutomationPolicyState.AUTHORIZATION_REQUIRED
    assert pending.policy_fingerprint is not None
    assert pending.price_policy is None

    _, approved = _configured(
        tmp_path,
        _enabled_policy(approval=pending.policy_fingerprint),
    )
    ready = inspect_ai_automation_policy(approved)

    assert ready.state is AutomationPolicyState.READY
    assert ready.is_authorized is True
    assert ready.price_policy is None
    assert ready.token_quota is not None
    assert ready.token_quota.timezone == "Asia/Shanghai"


def test_enabled_policy_without_token_cap_fails_closed(tmp_path: Path) -> None:
    automation = _enabled_policy()
    automation["token_budget"] = {
        "timezone": "UTC",
        "daily_total_tokens": None,
        "monthly_total_tokens": None,
    }
    _, config = _configured(tmp_path, automation)

    inspection = inspect_ai_automation_policy(config)

    assert inspection.state is AutomationPolicyState.INVALID
    assert inspection.policy_fingerprint is None


def test_token_usage_preserves_unreported_fields_as_unknown() -> None:
    usage = TokenUsage(
        uncached_input_tokens=12,
        cached_input_tokens=None,
        generated_tokens=7,
        embedding_input_tokens=None,
    )

    assert usage.known_total_tokens == 19
    assert usage.to_dict()["cached_input_tokens"] is None
    assert usage.to_dict()["embedding_input_tokens"] is None

    with pytest.raises(ValueError, match="至少需要一个"):
        TokenUsage()
    with pytest.raises(ValueError, match="非负整数"):
        TokenUsage(generated_tokens=-1)
