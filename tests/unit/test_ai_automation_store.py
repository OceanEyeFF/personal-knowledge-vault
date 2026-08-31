"""R4 P1 durable AI lifecycle ledger contracts (isolated SQLite only)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from src.runtime.ai_automation_policy import TokenUsage
from src.runtime.bootstrap import bootstrap_runtime
from src.runtime.errors import ErrorCode, PKVRuntimeError
from src.runtime.layout import RuntimeLayout
from src.runtime.write_lease import write_lease_scope
from src.storage.ai_automation_store import (
    AIAutomationTaskState,
    AIAutomationTaskStore,
)
from src.utils.config import Config


PROJECT_ROOT = Path(__file__).parent.parent.parent
_SOURCE_A = "a" * 64
_SOURCE_B = "b" * 64
_POLICY_A = "c" * 64
_POLICY_B = "d" * 64


def _configured(tmp_path: Path) -> Config:
    layout = RuntimeLayout.resolve(
        resources_root=PROJECT_ROOT,
        user_data_root=tmp_path / "data",
        profile_root=tmp_path / "profile",
        environment={},
    )
    layout.user_config_path.parent.mkdir(parents=True, exist_ok=True)
    layout.user_config_path.write_text(
        json.dumps(
            {
                "ai": {
                    "llm": {
                        "api_key": "test-llm-secret",
                        "base_url": "https://llm.invalid/v1",
                    },
                    "embedding": {
                        "api_key": "test-embedding-secret",
                        "base_url": "https://embedding.invalid/v1",
                        "model": "r4-fake",
                        "dim": 3,
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    return Config(layout=layout)


def _ready_store(tmp_path: Path) -> tuple[Config, AIAutomationTaskStore]:
    config = _configured(tmp_path)
    # Fresh-schema initialization is confined to pytest's selected .data-test
    # root by the test wrapper; it never opens a user Vault or Provider.
    bootstrap_runtime(config)
    return config, AIAutomationTaskStore(config.layout)


def _count_tasks(config: Config) -> int:
    with sqlite3.connect(config.layout.db_path) as connection:
        return int(connection.execute("SELECT COUNT(*) FROM ai_automation_tasks").fetchone()[0])


def test_ledger_mutations_fail_closed_without_lease_and_reads_remain_side_effect_free(
    tmp_path: Path,
) -> None:
    config, store = _ready_store(tmp_path)

    assert store.get_task("not-created") is None
    assert _count_tasks(config) == 0
    with pytest.raises(PKVRuntimeError) as error:
        store.enqueue_rebuild(
            mutation_id="archive:1",
            source_digest=_SOURCE_A,
            policy_fingerprint=_POLICY_A,
        )

    assert error.value.code is ErrorCode.WRITE_BUSY
    assert _count_tasks(config) == 0


def test_ledger_claim_usage_settlement_preserves_unknown_usage_dimensions(
    tmp_path: Path,
) -> None:
    config, store = _ready_store(tmp_path)

    with write_lease_scope(config.layout):
        queued = store.enqueue_rebuild(
            mutation_id="archive:1",
            source_digest=_SOURCE_A,
            policy_fingerprint=_POLICY_A,
        )
        repeated = store.enqueue_rebuild(
            mutation_id="archive:1",
            source_digest=_SOURCE_A,
            policy_fingerprint=_POLICY_A,
        )
        assert repeated.task_id == queued.task_id
        with pytest.raises(PKVRuntimeError) as stale:
            store.enqueue_rebuild(
                mutation_id="archive:1",
                source_digest=_SOURCE_B,
                policy_fingerprint=_POLICY_A,
            )
        assert stale.value.code is ErrorCode.RUNTIME_PLAN_STALE

        claimed = store.claim_next()
        assert claimed is not None
        assert claimed.state is AIAutomationTaskState.PROCESSING
        assert claimed.claim_token is not None
        assert store.claim_next() is None

        reservation = store.reserve_tokens(
            claimed.task_id,
            claim_token=claimed.claim_token,
            timezone="UTC",
            local_day="2026-08-31",
            local_month="2026-08",
            reserved_tokens=50,
            daily_total_tokens=200,
            monthly_total_tokens=500,
        )
        assert reservation is not None
        store.record_usage(
            claimed.task_id,
            claim_token=claimed.claim_token,
            reservation_id=reservation.reservation_id,
            usage=TokenUsage(
                uncached_input_tokens=60,
                cached_input_tokens=None,
                generated_tokens=12,
                embedding_input_tokens=None,
            ),
        )
        settled = store.settle_reservation(
            reservation.reservation_id,
            claim_token=claimed.claim_token,
        )
        completed = store.mark_completed(
            claimed.task_id,
            claim_token=claimed.claim_token,
        )

    assert settled.settled_tokens == 72
    assert completed.state is AIAutomationTaskState.COMPLETED
    assert store.token_total(
        policy_fingerprint=_POLICY_A,
        timezone="UTC",
        local_day="2026-08-31",
    ) == 72
    with sqlite3.connect(config.layout.db_path) as connection:
        row = connection.execute(
            "SELECT cached_input_tokens, embedding_input_tokens FROM ai_token_usage"
        ).fetchone()
    assert row == (None, None)


def test_budget_pause_precedes_provider_work_and_authorized_resume_rebinds_policy(
    tmp_path: Path,
) -> None:
    config, store = _ready_store(tmp_path)

    with write_lease_scope(config.layout):
        queued = store.enqueue_rebuild(
            mutation_id="archive:budget",
            source_digest=_SOURCE_A,
            policy_fingerprint=_POLICY_A,
        )
        claimed = store.claim_next()
        assert claimed is not None and claimed.claim_token is not None
        # A zero hard cap is an explicit stop even if an imprecise local
        # estimator would have returned zero.  No Provider seam exists here.
        assert (
            store.reserve_tokens(
                claimed.task_id,
                claim_token=claimed.claim_token,
                timezone="UTC",
                local_day="2026-08-31",
                local_month="2026-08",
                reserved_tokens=0,
                daily_total_tokens=0,
                monthly_total_tokens=None,
            )
            is None
        )
        paused = store.get_task(queued.task_id)
        assert paused is not None
        assert paused.state is AIAutomationTaskState.BUDGET_PAUSED
        assert paused.last_error_code == ErrorCode.EMBEDDING_BUDGET_PAUSED.value

        resumed = store.resume_paused_task(
            queued.task_id,
            source_digest=_SOURCE_A,
            policy_fingerprint=_POLICY_B,
        )

    assert resumed.state is AIAutomationTaskState.PENDING
    assert resumed.policy_fingerprint == _POLICY_B
    assert _count_tasks(config) == 1


def test_stale_claim_cannot_attach_usage_to_a_prior_reservation(tmp_path: Path) -> None:
    config, store = _ready_store(tmp_path)

    with write_lease_scope(config.layout):
        queued = store.enqueue_rebuild(
            mutation_id="archive:retry",
            source_digest=_SOURCE_A,
            policy_fingerprint=_POLICY_A,
        )
        first = store.claim_next()
        assert first is not None and first.claim_token is not None
        reservation = store.reserve_tokens(
            queued.task_id,
            claim_token=first.claim_token,
            timezone="UTC",
            local_day="2026-08-31",
            local_month="2026-08",
            reserved_tokens=20,
            daily_total_tokens=200,
            monthly_total_tokens=500,
        )
        assert reservation is not None
        store.mark_retry(
            queued.task_id,
            claim_token=first.claim_token,
            error_code="provider_unavailable",
        )
        second = store.claim_next()
        assert second is not None and second.claim_token is not None

        with pytest.raises(PKVRuntimeError) as stale:
            store.record_usage(
                queued.task_id,
                claim_token=second.claim_token,
                reservation_id=reservation.reservation_id,
                usage=TokenUsage(embedding_input_tokens=1),
            )

    assert stale.value.code is ErrorCode.RUNTIME_PLAN_STALE


def test_superseded_claim_is_terminal_and_does_not_block_newer_mutation(
    tmp_path: Path,
) -> None:
    config, store = _ready_store(tmp_path)

    with write_lease_scope(config.layout):
        old_task = store.enqueue_rebuild(
            mutation_id="archive:old",
            source_digest=_SOURCE_A,
            policy_fingerprint=_POLICY_A,
        )
        claimed = store.claim_next()
        assert claimed is not None and claimed.task_id == old_task.task_id
        assert claimed.claim_token is not None
        new_task = store.enqueue_rebuild(
            mutation_id="archive:new",
            source_digest=_SOURCE_B,
            policy_fingerprint=_POLICY_A,
        )
        retired = store.mark_superseded(
            claimed.task_id,
            claim_token=claimed.claim_token,
        )
        next_task = store.claim_next()

    assert retired.state is AIAutomationTaskState.SUPERSEDED
    assert retired.last_error_code == ErrorCode.RUNTIME_PLAN_STALE.value
    assert next_task is not None and next_task.task_id == new_task.task_id
