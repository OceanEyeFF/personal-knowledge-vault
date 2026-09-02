"""R4 Q2: fenced Provider work happens only after Q0/Q1′ and reservation."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import numpy as np
import pytest

import src.application.r4_derivation_lifecycle as r4_derivation_module
from src.application import KnowledgeApplication
from src.application.r4_derivation_lifecycle import R4DerivationLifecycle
from src.application.r4_lifecycle import R4ContentLifecycle
from src.runtime.ai_automation_policy import (
    AutomationPolicyState,
    OptionalPricePolicy,
    TokenUsage,
    inspect_ai_automation_policy,
)
from src.runtime.bootstrap import bootstrap_runtime
from src.runtime.runtime_snapshot import RuntimeSnapshotStore
from src.runtime.write_lease import write_lease_scope
from src.runtime.embedding_lifecycle import (
    EmbeddingIndexState,
    SQLiteEmbeddingSource,
    inspect_embedding_index,
)
from src.runtime.errors import ErrorCode, PKVRuntimeError
from src.runtime.price_cards import PriceCard, resolve_price_card
from src.storage.content_lifecycle import ContentLifecycleStore, PreparedDocument
from src.storage.markdown_store import Entry
from src.utils.config import Config
from src.runtime.layout import RuntimeLayout


PROJECT_ROOT = Path(__file__).parent.parent.parent


def _configured(
    tmp_path: Path,
    *,
    daily_total_tokens: int,
    retry_max_attempts: int = 2,
    pricing: dict[str, object] | None = None,
) -> Config:
    layout = RuntimeLayout.resolve(
        resources_root=PROJECT_ROOT,
        user_data_root=tmp_path / "data",
        profile_root=tmp_path / "profile",
        environment={},
    )
    layout.user_config_path.parent.mkdir(parents=True, exist_ok=True)
    automation: dict[str, object] = {
        "schema_version": 1,
        "enabled": True,
        "authorization": {"policy_sha256": None},
        "token_budget": {
            "timezone": "UTC",
            "daily_total_tokens": daily_total_tokens,
            "monthly_total_tokens": daily_total_tokens * 10,
        },
        "retry": {"max_attempts": retry_max_attempts},
    }
    if pricing is not None:
        automation["pricing"] = pricing
    payload = {
        "ai": {
            "llm": {
                "api_key": "r4-q2-test-llm-secret",
                "base_url": "https://llm.invalid/v1",
                "model": "r4-q2-test-llm",
            },
            "embedding": {
                "api_key": "r4-q2-test-embedding-secret",
                "base_url": "https://embedding.invalid/v1",
                "model": "r4-q2-test-embedding",
                "dim": 3,
            },
            "automation": automation,
        }
    }
    layout.user_config_path.write_text(json.dumps(payload), encoding="utf-8")
    pending = Config(layout=layout)
    policy = inspect_ai_automation_policy(pending)
    assert policy.state is AutomationPolicyState.AUTHORIZATION_REQUIRED
    assert policy.policy_fingerprint is not None
    payload["ai"]["automation"]["authorization"] = {
        "policy_sha256": policy.policy_fingerprint
    }
    layout.user_config_path.write_text(json.dumps(payload), encoding="utf-8")
    config = Config(layout=layout)
    assert inspect_ai_automation_policy(config).state is AutomationPolicyState.READY
    bootstrap_runtime(config)
    _seed_r2_snapshot(config)
    return config


def _seed_r2_snapshot(config: Config) -> None:
    assert config.embedding_dim is not None
    payload = {
        "schema_version": 1,
        "database": {"schema_version": "1.2.6"},
        "embedding": {
            "provider": config.embd_provider,
            "fingerprint": config.embedding_index_fingerprint(config.embedding_dim),
        },
    }
    store = RuntimeSnapshotStore(config.layout)
    with write_lease_scope(config.layout):
        store.publish(store.read(), payload)


def _reload_with_authorized_budget(config: Config, daily_total_tokens: int) -> Config:
    payload = json.loads(config.layout.user_config_path.read_text(encoding="utf-8"))
    automation = payload["ai"]["automation"]
    automation["token_budget"]["daily_total_tokens"] = daily_total_tokens
    automation["token_budget"]["monthly_total_tokens"] = daily_total_tokens * 10
    automation["authorization"] = {"policy_sha256": None}
    config.layout.user_config_path.write_text(json.dumps(payload), encoding="utf-8")
    pending = Config(layout=config.layout)
    policy = inspect_ai_automation_policy(pending)
    assert policy.state is AutomationPolicyState.AUTHORIZATION_REQUIRED
    assert policy.policy_fingerprint is not None
    automation["authorization"] = {"policy_sha256": policy.policy_fingerprint}
    config.layout.user_config_path.write_text(json.dumps(payload), encoding="utf-8")
    reloaded = Config(layout=config.layout)
    assert inspect_ai_automation_policy(reloaded).state is AutomationPolicyState.READY
    bootstrap_runtime(reloaded)
    return reloaded


class _FakeLLM:
    def __init__(self) -> None:
        self.last_usage: TokenUsage | None = None
        self.calls: list[str] = []

    def summarize(self, content: str) -> str:
        self.calls.append("summary")
        self.last_usage = TokenUsage(
            uncached_input_tokens=max(1, len(content) // 4),
            generated_tokens=4,
        )
        return "Q2 generated summary."

    def extract_tags(self, content: str) -> list[str]:
        self.calls.append("tags")
        self.last_usage = TokenUsage(
            uncached_input_tokens=max(1, len(content) // 4),
            generated_tokens=2,
        )
        return ["q2", "lifecycle", "test"]


class _FakeEmbeddingClient:
    def __init__(self) -> None:
        self.last_usage: TokenUsage | None = None

    def embed_batch_numpy(self, texts: list[str]) -> np.ndarray:
        self.last_usage = TokenUsage(
            embedding_input_tokens=sum(max(1, len(text) // 4) for text in texts)
        )
        return np.asarray(
            [[float(max(1, len(text))), 2.0, 3.0] for text in texts],
            dtype=np.float32,
        )


class _FakeEmbedder:
    def __init__(self) -> None:
        self.client = _FakeEmbeddingClient()

    def embed_document(self, text: str) -> np.ndarray:
        self.client.last_usage = TokenUsage(
            embedding_input_tokens=max(1, len(text) // 4)
        )
        return np.asarray([float(max(1, len(text))), 2.0, 3.0], dtype=np.float32)


def test_price_cards_do_not_estimate_partial_usage_or_accept_unknown_card(tmp_path: Path) -> None:
    config = _configured(tmp_path, daily_total_tokens=20_000)
    card = PriceCard(
        card_id="fixture",
        currency="USD",
        llm_uncached_input_micros_per_million=1_000_000,
        llm_cached_input_micros_per_million=2_000_000,
        llm_generated_micros_per_million=3_000_000,
        embedding_input_micros_per_million=4_000_000,
        sha256="a" * 64,
    )

    assert card.amount_for_usage(
        "summary",
        TokenUsage(uncached_input_tokens=10, generated_tokens=None),
    ) is None
    assert card.amount_for_usage(
        "summary",
        TokenUsage(uncached_input_tokens=10, generated_tokens=5),
    ) is None
    with pytest.raises(PKVRuntimeError) as captured:
        resolve_price_card(
            config,
            OptionalPricePolicy(
                card_id="not-in-bundled-resource",
                card_sha256="a" * 64,
                currency="USD",
                daily_cap_micros=100,
                monthly_cap_micros=100,
            ),
        )
    assert captured.value.code is ErrorCode.EMBEDDING_AUTOMATION_AUTHORIZATION_REQUIRED


def test_q2_writes_patch_only_through_q1_and_records_actual_usage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _configured(tmp_path, daily_total_tokens=20_000)
    app = KnowledgeApplication(config)
    fake_llm = _FakeLLM()
    monkeypatch.setattr(app, "_create_deepseek_client", lambda: fake_llm)
    monkeypatch.setattr(app, "_create_embedder", _FakeEmbedder)

    body = "R4 Q2 provider-backed body"
    result = asyncio.run(app.archive_text(body))

    assert result.success is True
    assert result.terminal == "success", (
        result.data,
        result.issues,
        result.warnings,
        fake_llm.calls,
    )
    assert result.data["ai_automation"] == {
        "status": "ready",
        "task_state": "completed",
    }
    assert fake_llm.calls == ["summary", "tags"]
    with sqlite3.connect(config.db_path) as connection:
        task = connection.execute(
            "SELECT state, patch_ref, patch_applied FROM ai_derivation_tasks"
        ).fetchone()
        assert task is not None
        assert task[0] == "completed" and task[1] and task[2] == 1
        patch_commit = connection.execute(
            "SELECT COUNT(*) FROM r4_content_operation_commits"
        ).fetchone()[0]
        reservation = connection.execute(
            """
            SELECT reserved_tokens, settled_tokens, reserved_micros,
                   settled_micros, currency, state
            FROM ai_derivation_reservations
            """
        ).fetchone()
        usage = connection.execute(
            """
            SELECT stage, uncached_input_tokens, cached_input_tokens,
                   generated_tokens, embedding_input_tokens,
                   amount_micros, currency
            FROM ai_derivation_usage
            WHERE source = 'provider_reported'
            ORDER BY stage
            """
        ).fetchall()
        row = connection.execute(
            "SELECT summary_100_words, tags FROM knowledge_items"
        ).fetchone()
    assert patch_commit == 1
    assert reservation is not None
    assert reservation[0] == reservation[1]
    assert reservation[2:] == (None, None, None, "settled")
    expected_input = max(1, len(body) // 4)
    assert usage[0] == ("embedding", None, None, None, expected_input * 2, None, None)
    assert usage[1] == ("summary", expected_input, None, 4, None, None, None)
    assert usage[2] == ("tags", expected_input, None, 2, None, None, None)
    assert row == ("Q2 generated summary.", "q2,lifecycle,test")


def test_q2_budget_pause_precedes_provider_then_resumes_after_authorized_reload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _configured(tmp_path, daily_total_tokens=0)
    app = KnowledgeApplication(config)
    monkeypatch.setattr(
        app,
        "_create_deepseek_client",
        lambda: (_ for _ in ()).throw(AssertionError("budget must precede LLM")),
    )
    monkeypatch.setattr(
        app,
        "_create_embedder",
        lambda: (_ for _ in ()).throw(AssertionError("budget must precede embedder")),
    )

    result = asyncio.run(app.archive_text("R4 Q2 budget body"))

    assert result.success is True
    assert result.terminal == "degraded"
    assert result.data["ai_automation"] == {
        "status": "budget_paused",
        "task_state": "budget_paused",
    }
    with sqlite3.connect(config.db_path) as connection:
        state = connection.execute("SELECT state FROM ai_derivation_tasks").fetchone()
        reservations = connection.execute(
            "SELECT COUNT(*) FROM ai_derivation_reservations"
        ).fetchone()
        patch_count = connection.execute(
            "SELECT COUNT(*) FROM r4_content_operation_commits"
        ).fetchone()
    assert state == ("budget_paused",)
    assert reservations == (0,)
    assert patch_count == (0,)

    operation_id = result.data["operation_id"]
    assert isinstance(operation_id, str)
    reloaded_config = _reload_with_authorized_budget(config, 20_000)
    reloaded_app = KnowledgeApplication(reloaded_config)
    fake_llm = _FakeLLM()
    reloaded_q1 = R4ContentLifecycle(reloaded_app)
    reloaded_q2 = R4DerivationLifecycle(
        reloaded_app,
        q1_lifecycle=reloaded_q1,
        llm_factory=lambda: fake_llm,
        embedder_factory=_FakeEmbedder,
    )

    resumed = asyncio.run(reloaded_q2.drain_for_operation(operation_id))

    assert resumed.task is not None
    assert resumed.task.state.value == "completed"
    assert fake_llm.calls == ["summary", "tags"]
    with sqlite3.connect(reloaded_config.db_path) as connection:
        assert connection.execute(
            "SELECT state FROM ai_derivation_tasks WHERE operation_id = ?",
            (operation_id,),
        ).fetchone() == ("completed",)
        assert connection.execute(
            "SELECT COUNT(*) FROM ai_derivation_usage WHERE source = 'provider_reported'"
        ).fetchone() == (3,)
        assert connection.execute(
            "SELECT COUNT(*) FROM ai_derivation_reservations WHERE state != 'settled'"
        ).fetchone() == (0,)


def test_q2_retry_ceiling_precedes_every_provider_factory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _configured(
        tmp_path,
        daily_total_tokens=20_000,
        retry_max_attempts=0,
    )
    app = KnowledgeApplication(config)
    monkeypatch.setattr(
        app,
        "_create_deepseek_client",
        lambda: (_ for _ in ()).throw(AssertionError("retry ceiling must precede LLM")),
    )
    monkeypatch.setattr(
        app,
        "_create_embedder",
        lambda: (_ for _ in ()).throw(AssertionError("retry ceiling must precede embedder")),
    )

    result = asyncio.run(app.archive_text("R4 Q2 retry ceiling"))

    assert result.terminal == "degraded"
    assert result.data["ai_automation"] == {
        "status": "retry_required",
        "task_state": "retry_required",
    }
    with sqlite3.connect(config.db_path) as connection:
        task = connection.execute(
            "SELECT state, attempt_count, last_error_code FROM ai_derivation_tasks"
        ).fetchone()
        reservations = connection.execute(
            "SELECT COUNT(*) FROM ai_derivation_reservations"
        ).fetchone()
    assert task == ("retry_required", 0, "retry_exhausted")
    assert reservations == (0,)


def test_q2_not_before_precedes_every_provider_factory(tmp_path: Path) -> None:
    config = _configured(tmp_path, daily_total_tokens=20_000)
    app = KnowledgeApplication(config)
    q1 = R4ContentLifecycle(app)

    async def submit() -> object:
        with app._write_lease_scope():
            return await q1.submit_and_drain(
                PreparedDocument.for_archive(
                    Entry(title="not before", source_type="text", content="body")
                )
            )

    archived = asyncio.run(submit())
    with sqlite3.connect(config.db_path) as connection:
        connection.execute(
            """
            UPDATE ai_derivation_tasks
            SET not_before = datetime('now', '+1 hour')
            WHERE operation_id = ?
            """,
            (archived.task.operation_id,),
        )
        connection.commit()

    q2 = R4DerivationLifecycle(
        app,
        q1_lifecycle=q1,
        llm_factory=lambda: (_ for _ in ()).throw(
            AssertionError("not_before must precede LLM")
        ),
        embedder_factory=lambda: (_ for _ in ()).throw(
            AssertionError("not_before must precede embedder")
        ),
    )
    result = asyncio.run(q2.drain_for_operation(archived.task.operation_id))

    assert result.task is not None
    assert result.task.state.value == "pending"


def test_q2_expired_claim_is_recovered_with_a_new_fence(tmp_path: Path) -> None:
    config = _configured(tmp_path, daily_total_tokens=20_000)
    app = KnowledgeApplication(config)
    q1 = R4ContentLifecycle(app)
    fake_llm = _FakeLLM()

    async def submit() -> object:
        with app._write_lease_scope():
            return await q1.submit_and_drain(
                PreparedDocument.for_archive(
                    Entry(title="expired Q2", source_type="text", content="body")
                )
            )

    asyncio.run(submit())
    policy = inspect_ai_automation_policy(config)
    assert policy.retry_max_attempts is not None
    q2 = R4DerivationLifecycle(
        app,
        q1_lifecycle=q1,
        llm_factory=lambda: fake_llm,
        embedder_factory=_FakeEmbedder,
    )
    with app._write_lease_scope():
        claimed = q2.store.claim_next_derivation(
            max_attempts=policy.retry_max_attempts,
            lease_seconds=120,
        )
    assert claimed is not None
    with sqlite3.connect(config.db_path) as connection:
        connection.execute(
            """
            UPDATE ai_derivation_tasks
            SET claimed_until = datetime('now', '-1 second')
            WHERE task_id = ?
            """,
            (claimed.task_id,),
        )
        connection.commit()

    reloaded_config = Config(layout=config.layout)
    bootstrap_runtime(reloaded_config)
    reloaded_app = KnowledgeApplication(reloaded_config)
    recovered_q2 = R4DerivationLifecycle(
        reloaded_app,
        q1_lifecycle=R4ContentLifecycle(reloaded_app),
        llm_factory=lambda: fake_llm,
        embedder_factory=_FakeEmbedder,
    )
    results = asyncio.run(recovered_q2.recover_and_drain())

    recovered = next(
        result.task
        for result in results
        if result.task is not None and result.task.task_id == claimed.task_id
    )
    assert recovered.state.value == "completed"
    assert recovered.owner_fence == claimed.owner_fence + 1
    assert recovered.attempt_count == claimed.attempt_count + 1


def test_q2_source_drift_supersedes_before_every_provider_factory(tmp_path: Path) -> None:
    config = _configured(tmp_path, daily_total_tokens=20_000)
    app = KnowledgeApplication(config)
    q1 = R4ContentLifecycle(app)

    async def submit(title: str, content: str) -> object:
        with app._write_lease_scope():
            return await q1.submit_and_drain(
                PreparedDocument.for_archive(
                    Entry(title=title, source_type="text", content=content)
                )
            )

    first = asyncio.run(submit("stale source A", "first body"))
    asyncio.run(submit("stale source B", "second body"))
    q2 = R4DerivationLifecycle(
        app,
        q1_lifecycle=q1,
        llm_factory=lambda: (_ for _ in ()).throw(
            AssertionError("source drift must precede LLM")
        ),
        embedder_factory=lambda: (_ for _ in ()).throw(
            AssertionError("source drift must precede embedder")
        ),
    )

    result = asyncio.run(q2.drain_for_operation(first.task.operation_id))

    assert result.task is not None
    assert result.task.state.value == "superseded"


def test_q2_price_cap_precedes_every_provider_factory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    card_sha = "a" * 64
    config = _configured(
        tmp_path,
        daily_total_tokens=20_000,
        pricing={
            "card_id": "fixture",
            "card_sha256": card_sha,
            "currency": "USD",
            "daily_cap_micros": 0,
            "monthly_cap_micros": 0,
        },
    )
    app = KnowledgeApplication(config)
    monkeypatch.setattr(
        "src.application.r4_derivation_lifecycle.resolve_price_card",
        lambda *_: PriceCard(
            card_id="fixture",
            currency="USD",
            llm_uncached_input_micros_per_million=1_000_000,
            llm_cached_input_micros_per_million=0,
            llm_generated_micros_per_million=1_000_000,
            embedding_input_micros_per_million=1_000_000,
            sha256=card_sha,
        ),
    )
    monkeypatch.setattr(
        app,
        "_create_deepseek_client",
        lambda: (_ for _ in ()).throw(AssertionError("price cap must precede LLM")),
    )
    monkeypatch.setattr(
        app,
        "_create_embedder",
        lambda: (_ for _ in ()).throw(AssertionError("price cap must precede embedder")),
    )

    result = asyncio.run(app.archive_text("R4 Q2 price cap"))

    assert result.terminal == "degraded"
    assert result.data["ai_automation"] == {
        "status": "budget_paused",
        "task_state": "budget_paused",
    }
    with sqlite3.connect(config.db_path) as connection:
        reservation_count = connection.execute(
            "SELECT COUNT(*) FROM ai_derivation_reservations"
        ).fetchone()
    assert reservation_count == (0,)


def test_q2_reuses_recorded_patch_without_recalling_llm_or_settling_unstarted_embedding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Q1′ patch retry retains one LLM charge and retries only generation."""

    config = _configured(tmp_path, daily_total_tokens=20_000)
    app = KnowledgeApplication(config)
    fake_llm = _FakeLLM()
    monkeypatch.setattr(app, "_create_deepseek_client", lambda: fake_llm)
    monkeypatch.setattr(app, "_create_embedder", _FakeEmbedder)

    original_complete_patch = ContentLifecycleStore.complete_patch_task
    failed_once = False

    def fail_after_first_core_commit(
        self: ContentLifecycleStore,
        task_id: str,
    ):
        nonlocal failed_once
        if not failed_once:
            failed_once = True
            raise RuntimeError("fixture crash after patch core commit")
        return original_complete_patch(self, task_id)

    monkeypatch.setattr(
        ContentLifecycleStore,
        "complete_patch_task",
        fail_after_first_core_commit,
    )

    first = asyncio.run(app.archive_text("R4 Q2 recorded patch retry"))

    assert first.terminal == "degraded"
    assert first.data["ai_automation"]["task_state"] == "retry_required"
    assert fake_llm.calls == ["summary", "tags"]
    with sqlite3.connect(config.db_path) as connection:
        operation_id = connection.execute(
            "SELECT operation_id FROM ai_derivation_tasks"
        ).fetchone()[0]
        connection.execute(
            "UPDATE ai_derivation_tasks SET not_before = CURRENT_TIMESTAMP"
        )
        connection.commit()

    retried = asyncio.run(R4DerivationLifecycle(app).drain_for_operation(operation_id))

    assert retried.task is not None
    assert retried.task.state.value == "completed"
    assert fake_llm.calls == ["summary", "tags"]
    with sqlite3.connect(config.db_path) as connection:
        reservations = connection.execute(
            """
            SELECT r.reserved_tokens, r.settled_tokens, r.state,
                   GROUP_CONCAT(u.stage)
            FROM ai_derivation_reservations AS r
            LEFT JOIN ai_derivation_usage AS u
              ON u.reservation_id = r.reservation_id
             AND u.source = 'provider_reported'
            GROUP BY r.reservation_id
            """
        ).fetchall()
        local_estimates = connection.execute(
            """
            SELECT stage, COUNT(*) FROM ai_derivation_usage
            WHERE source = 'local_estimate'
            GROUP BY stage
            """
        ).fetchall()
    assert len(reservations) == 2
    reservations_by_stages = {
        frozenset((stages or "").split(",")): (reserved, settled, state)
        for reserved, settled, state, stages in reservations
    }
    llm_reservation = reservations_by_stages[frozenset({"summary", "tags"})]
    embedding_reservation = reservations_by_stages[frozenset({"embedding"})]
    assert llm_reservation[2] == "settled"
    assert llm_reservation[1] < llm_reservation[0]
    assert embedding_reservation[2] == "settled"
    assert embedding_reservation[1] == embedding_reservation[0]
    assert dict(local_estimates)["summary"] == 1
    assert dict(local_estimates)["tags"] == 1
    assert retried.task.patch_applied is True
    assert retried.task.source_digest == SQLiteEmbeddingSource().capture(config).summary.digest


@pytest.mark.parametrize("fault_phase", ["pre_cas", "post_cas"])
def test_q2_generation_fault_recovers_without_repeating_completed_stages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault_phase: str,
) -> None:
    config = _configured(tmp_path, daily_total_tokens=40_000)
    app = KnowledgeApplication(config)
    fake_llm = _FakeLLM()
    embedder_factories = 0

    def embedder_factory() -> _FakeEmbedder:
        nonlocal embedder_factories
        embedder_factories += 1
        return _FakeEmbedder()

    monkeypatch.setattr(app, "_create_deepseek_client", lambda: fake_llm)
    monkeypatch.setattr(app, "_create_embedder", embedder_factory)
    original_execute = r4_derivation_module.execute_embedding_rebuild
    fired = False

    def fault_once(*args, **kwargs):
        nonlocal fired
        if fired:
            return original_execute(*args, **kwargs)
        fired = True
        if fault_phase == "post_cas":
            original_execute(*args, **kwargs)
        raise PKVRuntimeError(
            ErrorCode.STORAGE_VECTOR_FAILED,
            f"fixture generation {fault_phase} failure",
            stage="fixture_generation",
            recoverable=True,
        )

    monkeypatch.setattr(
        r4_derivation_module,
        "execute_embedding_rebuild",
        fault_once,
    )

    first = asyncio.run(app.archive_text(f"R4 Q2 generation {fault_phase}"))

    assert first.terminal == "degraded"
    assert first.data["ai_automation"]["task_state"] == "retry_required"
    assert fake_llm.calls == ["summary", "tags"]
    state_after_fault = inspect_embedding_index(
        config,
        source=SQLiteEmbeddingSource(),
    ).state
    if fault_phase == "pre_cas":
        assert state_after_fault is not EmbeddingIndexState.READY
    else:
        assert state_after_fault is EmbeddingIndexState.READY
    with sqlite3.connect(config.db_path) as connection:
        operation_id = connection.execute(
            "SELECT operation_id FROM ai_derivation_tasks"
        ).fetchone()[0]
        connection.execute(
            "UPDATE ai_derivation_tasks SET not_before = CURRENT_TIMESTAMP"
        )
        connection.commit()

    recovered = asyncio.run(
        R4DerivationLifecycle(app).drain_for_operation(operation_id)
    )

    assert recovered.task is not None
    assert recovered.task.state.value == "completed"
    assert fake_llm.calls == ["summary", "tags"]
    assert embedder_factories == (2 if fault_phase == "pre_cas" else 1)
    assert inspect_embedding_index(
        config,
        source=SQLiteEmbeddingSource(),
    ).state is EmbeddingIndexState.READY
    with sqlite3.connect(config.db_path) as connection:
        reservation_states = connection.execute(
            "SELECT state, COUNT(*) FROM ai_derivation_reservations GROUP BY state"
        ).fetchall()
        provider_usage = dict(
            connection.execute(
                """
                SELECT stage, COUNT(*) FROM ai_derivation_usage
                WHERE source = 'provider_reported' GROUP BY stage
                """
            ).fetchall()
        )
    assert dict(reservation_states) == (
        {"settled": 2}
        if fault_phase == "pre_cas"
        else {"released": 1, "settled": 1}
    )
    assert provider_usage["summary"] == 1
    assert provider_usage["tags"] == 1
    assert provider_usage["embedding"] == (2 if fault_phase == "pre_cas" else 1)


def test_late_provider_result_only_settles_its_old_reservation_after_supersede(
    tmp_path: Path,
) -> None:
    """A fenced late worker cannot republish a binding or revive deleted work."""

    config = _configured(tmp_path, daily_total_tokens=20_000)
    app = KnowledgeApplication(config)
    q1 = R4ContentLifecycle(app)
    async def submit() -> object:
        with app._write_lease_scope():
            return await q1.submit_and_drain(
                PreparedDocument.for_archive(
                    Entry(title="late Q2", source_type="text", content="body")
                )
            )

    archived = asyncio.run(
        submit()
    )
    task = q1.store.get_derivation_task(archived.task.operation_id)
    assert task is not None and task.target_knowledge_id is not None
    q2: R4DerivationLifecycle

    class _SupersedingLLM(_FakeLLM):
        def summarize(self, content: str) -> str:
            summary = super().summarize(content)
            with app._write_lease_scope():
                q2.store.supersede_derivations_for_target(
                    task.target_knowledge_id or 0,
                    excluding_operation_id="f" * 32,
                )
            return summary

    fake_llm = _SupersedingLLM()
    q2 = R4DerivationLifecycle(
        app,
        q1_lifecycle=q1,
        llm_factory=lambda: fake_llm,
        embedder_factory=lambda: (_ for _ in ()).throw(
            AssertionError("superseded worker must not construct embedder")
        ),
    )

    result = asyncio.run(q2.drain_for_operation(archived.task.operation_id))

    assert result.task is not None
    assert result.task.state.value == "superseded"
    assert fake_llm.calls == ["summary", "tags"]
    assert inspect_embedding_index(
        config,
        source=SQLiteEmbeddingSource(),
    ).state is not EmbeddingIndexState.READY
    with sqlite3.connect(config.db_path) as connection:
        reservation = connection.execute(
            "SELECT state, settled_tokens FROM ai_derivation_reservations"
        ).fetchone()
        patch_state = connection.execute(
            "SELECT patch_ref, patch_applied FROM ai_derivation_tasks"
        ).fetchone()
        patch_commits = connection.execute(
            "SELECT COUNT(*) FROM r4_content_operation_commits"
        ).fetchone()[0]
        stored = connection.execute(
            "SELECT summary_100_words, tags FROM knowledge_items"
        ).fetchone()
    assert reservation is not None
    assert reservation[0] == "settled" and reservation[1] is not None
    assert patch_state == (None, 0)
    assert patch_commits == 0
    assert stored == ("", "")
