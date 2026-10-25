"""Independent foreground Embedding rebuild policy and ledger contracts."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.runtime.ai_automation_policy import TokenUsage
from src.runtime.embedding_rebuild_ledger import (
    EmbeddingRebuildLedger,
    EmbeddingRebuildLedgerState,
)
from src.runtime.embedding_rebuild_policy import (
    EmbeddingRebuildPolicyState,
    inspect_embedding_rebuild_policy,
)
from src.runtime.errors import ErrorCode, PKVRuntimeError
from src.runtime.layout import RuntimeLayout
from src.runtime.write_lease import write_lease_scope
from src.utils.config import Config


PROJECT_ROOT = Path(__file__).parent.parent.parent
_PLAN_ID = "a" * 64
_PUBLIC_PLAN_ID = "c" * 64
_REPLAY_KEY = "d" * 64
_SOURCE_REVISION = "b" * 64


def _configured(
    tmp_path: Path,
    *,
    daily_total_tokens: int | None = 200,
    monthly_total_tokens: int | None = 400,
) -> Config:
    layout = RuntimeLayout.resolve(
        resources_root=PROJECT_ROOT,
        user_data_root=tmp_path / "data",
        profile_root=tmp_path / "profile",
        environment={},
    )
    layout.user_config_path.parent.mkdir(parents=True)
    layout.user_config_path.write_text(
        json.dumps(
            {
                "ai": {
                    "embedding": {
                        "api_key": "embedding-test-secret",
                        "base_url": "https://embedding.invalid/v1",
                        "model": "r4-fake-3",
                        "dim": 3,
                    },
                    "embedding_rebuild": {
                        "schema_version": 1,
                        "token_budget": {
                            "timezone": "UTC",
                            "daily_total_tokens": daily_total_tokens,
                            "monthly_total_tokens": monthly_total_tokens,
                        },
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    return Config(layout=layout)


def _begin(config: Config, *, estimated_tokens: int = 40):
    ledger = EmbeddingRebuildLedger(
        config.layout,
        clock=lambda: datetime(2026, 9, 4, tzinfo=timezone.utc),
    )
    policy = inspect_embedding_rebuild_policy(config)
    with write_lease_scope(config.layout):
        operation = ledger.begin(
            plan_id=_PLAN_ID,
            public_plan_id=_PUBLIC_PLAN_ID,
            replay_key=_REPLAY_KEY,
            source_revision=_SOURCE_REVISION,
            policy=policy,
            estimated_tokens=estimated_tokens,
        )
    return ledger, operation


def test_policy_requires_independent_hard_cap_without_enabling_automation(
    tmp_path: Path,
) -> None:
    config = _configured(tmp_path, daily_total_tokens=None, monthly_total_tokens=None)

    policy = inspect_embedding_rebuild_policy(config)

    assert policy.state is EmbeddingRebuildPolicyState.AUTHORIZATION_REQUIRED
    assert policy.token_quota is not None
    assert policy.token_quota.has_hard_cap is False
    assert policy.issues[0].code == ErrorCode.EMBEDDING_REBUILD_AUTHORIZATION_REQUIRED.value


def test_ledger_reserves_settles_provider_usage_and_persists_across_instances(
    tmp_path: Path,
) -> None:
    config = _configured(tmp_path)
    ledger, operation = _begin(config)

    assert operation.state is EmbeddingRebuildLedgerState.PROCESSING
    assert operation.reservation is not None
    assert operation.reservation.state == "reserved"
    assert operation.local_estimate is not None
    assert operation.local_estimate.embedding_input_tokens == 40

    with write_lease_scope(config.layout):
        ledger.mark_activation_intent(
            operation.operation_id,
            generation_id="g-" + "a" * 20,
        )
        completed = ledger.mark_completed(
            operation.operation_id,
            provider_usages=(
                TokenUsage(embedding_input_tokens=53, source="provider_reported"),
            ),
            provider_attempted=True,
        )

    reloaded = EmbeddingRebuildLedger(config.layout).get_operation(operation.operation_id)
    assert completed.state is EmbeddingRebuildLedgerState.COMPLETED
    assert completed.reservation is not None
    assert completed.reservation.state == "settled"
    assert completed.reservation.settled_tokens == 53
    assert completed.provider_usage is not None
    assert completed.provider_usage.embedding_input_tokens == 53
    assert reloaded == completed


def test_settlement_never_counts_the_local_reservation_twice(tmp_path: Path) -> None:
    config = _configured(tmp_path)
    ledger, operation = _begin(config, estimated_tokens=40)

    with write_lease_scope(config.layout):
        ledger.mark_activation_intent(
            operation.operation_id,
            generation_id="g-" + "b" * 20,
        )
        completed = ledger.mark_completed(
            operation.operation_id,
            provider_usages=(
                TokenUsage(embedding_input_tokens=17, source="provider_reported"),
            ),
            provider_attempted=True,
        )

    assert completed.reservation is not None
    assert completed.reservation.settled_tokens == 40


def test_failed_pre_provider_operation_releases_reservation_without_spending_budget(
    tmp_path: Path,
) -> None:
    config = _configured(tmp_path)
    ledger, operation = _begin(config)

    with write_lease_scope(config.layout):
        failed = ledger.mark_failed(
            operation.operation_id,
            error_code=ErrorCode.PROVIDER_UNAVAILABLE,
            provider_attempted=False,
        )

    assert failed.state is EmbeddingRebuildLedgerState.FAILED
    assert failed.reservation is not None
    assert failed.reservation.state == "released"
    assert failed.reservation.settled_tokens is None


def test_budget_pause_is_durable_before_provider_or_stage_work(tmp_path: Path) -> None:
    config = _configured(tmp_path, daily_total_tokens=0, monthly_total_tokens=100)
    ledger = EmbeddingRebuildLedger(config.layout)
    policy = inspect_embedding_rebuild_policy(config)

    with write_lease_scope(config.layout), pytest.raises(PKVRuntimeError) as paused:
        ledger.begin(
            plan_id=_PLAN_ID,
            public_plan_id=_PUBLIC_PLAN_ID,
            replay_key=_REPLAY_KEY,
            source_revision=_SOURCE_REVISION,
            policy=policy,
            estimated_tokens=1,
        )

    assert paused.value.code is ErrorCode.EMBEDDING_BUDGET_PAUSED
    operations = ledger.read_operations()
    assert len(operations) == 1
    assert operations[0].state is EmbeddingRebuildLedgerState.BUDGET_PAUSED
    assert operations[0].reservation is None


def test_mutation_requires_the_current_root_writer_lease(tmp_path: Path) -> None:
    config = _configured(tmp_path)
    ledger = EmbeddingRebuildLedger(config.layout)

    with pytest.raises(PKVRuntimeError) as missing_lease:
        ledger.begin(
            plan_id=_PLAN_ID,
            public_plan_id=_PUBLIC_PLAN_ID,
            replay_key=_REPLAY_KEY,
            source_revision=_SOURCE_REVISION,
            policy=inspect_embedding_rebuild_policy(config),
            estimated_tokens=1,
        )

    assert missing_lease.value.code is ErrorCode.WRITE_BUSY
    assert not config.layout.user_data_root.exists()


def test_same_replay_key_cannot_create_a_second_inflight_provider_operation(
    tmp_path: Path,
) -> None:
    config = _configured(tmp_path)
    ledger, first = _begin(config)

    with write_lease_scope(config.layout), pytest.raises(PKVRuntimeError) as busy:
        ledger.begin(
            plan_id="e" * 64,
            public_plan_id="f" * 64,
            replay_key=_REPLAY_KEY,
            source_revision=_SOURCE_REVISION,
            policy=inspect_embedding_rebuild_policy(config),
            estimated_tokens=40,
        )

    assert busy.value.code is ErrorCode.WRITE_BUSY
    operations = ledger.read_operations()
    assert operations == (first,)


def test_consumed_replay_key_cannot_be_reexecuted_after_completion(tmp_path: Path) -> None:
    config = _configured(tmp_path)
    ledger, operation = _begin(config)
    with write_lease_scope(config.layout):
        ledger.mark_activation_intent(
            operation.operation_id,
            generation_id="g-" + "e" * 20,
        )
        ledger.mark_completed(
            operation.operation_id,
            provider_attempted=True,
        )

    with write_lease_scope(config.layout), pytest.raises(PKVRuntimeError) as stale:
        ledger.begin(
            plan_id="f" * 64,
            public_plan_id="e" * 64,
            replay_key=_REPLAY_KEY,
            source_revision=_SOURCE_REVISION,
            policy=inspect_embedding_rebuild_policy(config),
            estimated_tokens=40,
        )

    assert stale.value.code is ErrorCode.RUNTIME_PLAN_STALE
