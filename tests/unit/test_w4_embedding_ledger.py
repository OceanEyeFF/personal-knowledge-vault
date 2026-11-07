"""W4 ledger-3 migration, replay and conservative accounting contracts.

Every fixture is synthetic and every mutation runs inside the existing writer
lease. No Provider or user Vault is needed to establish these invariants.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.runtime.ai_automation_policy import TokenQuota, TokenUsage
from src.runtime.embedding_rebuild_ledger import EmbeddingRebuildLedger, current_period
from src.runtime.embedding_rebuild_policy import inspect_embedding_rebuild_policy
from src.runtime.errors import ErrorCode, PKVRuntimeError
from src.runtime.layout import RuntimeLayout
from src.runtime.write_lease import write_lease_scope
from src.utils.config import Config


PROJECT_ROOT = Path(__file__).parents[2]
NOW = datetime(2026, 10, 25, tzinfo=timezone.utc)
OPERATION_ID = "embedding-rebuild-" + "a" * 32
STAGE_KEY = ".stage-g-" + "b" * 16 + "-" + "c" * 16
GENERATION_ID = "g-" + "b" * 16


@pytest.fixture
def configured(tmp_path: Path):
    layout = RuntimeLayout.resolve(resources_root=PROJECT_ROOT,
                                   user_data_root=tmp_path / "data",
                                   profile_root=tmp_path / "profile", environment={})
    layout.user_config_path.parent.mkdir(parents=True)
    layout.user_config_path.write_text(json.dumps({"ai": {
        "embedding": {"api_key": "test-only", "base_url": "https://embedding.invalid/v1", "model": "fake", "dim": 3},
        "embedding_rebuild": {"schema_version": 1, "token_budget": {
            "timezone": "UTC", "daily_total_tokens": 200, "monthly_total_tokens": 400,
        }},
    }}), encoding="utf-8")
    config = Config(layout=layout)
    ledger = EmbeddingRebuildLedger(layout, clock=lambda: NOW)
    return config, ledger, inspect_embedding_rebuild_policy(config)


def _begin(ledger, policy, *, replay_key="d" * 64, estimate=40, period=None):
    return ledger.begin_v3(
        plan_id="a" * 64, public_plan_id="b" * 64, replay_key=replay_key,
        source_revision="c" * 64, policy=policy, estimated_tokens=estimate,
        data_root_identity_sha256="e" * 64, runtime_snapshot_sha256="f" * 64,
        contract_sha256="1" * 64,
        budget_check_period=period or {"timezone": "UTC", "local_day": "2026-10-25", "local_month": "2026-10"},
    )


def _legacy_row(version, state):
    reservation = {"reservation_id": "f" * 32, "timezone": "UTC", "local_day": "2026-10-24",
                   "local_month": "2026-10", "reserved_tokens": 40, "settled_tokens": None, "state": "reserved"}
    row = {
        "operation_id": OPERATION_ID, "plan_id": "a" * 64,
        "source_revision": "b" * 64, "policy_fingerprint": "c" * 64,
        "state": state, "reservation": reservation,
        "local_estimate": TokenUsage(embedding_input_tokens=40, source="local_estimate").to_dict(),
        "provider_usage": None, "generation_id": None, "last_error_code": None,
        "provider_attempted": False,
    }
    if version == 2:
        row.update(public_plan_id="d" * 64, replay_key="e" * 64)
    if state == "budget_paused":
        row.update(reservation=None, last_error_code="embedding_budget_paused")
    elif state == "failed_released":
        row.update(state="failed", last_error_code="provider_unavailable")
        reservation["state"] = "released"
    elif state in {"completed", "failed_paid"}:
        reservation.update(state="settled", settled_tokens=73)
        row.update(provider_attempted=True, provider_usage=TokenUsage(embedding_input_tokens=73).to_dict())
        if state == "completed":
            row["generation_id"] = GENERATION_ID
        else:
            row.update(state="failed", last_error_code="provider_unavailable")
    elif state == "activation_intent":
        row["generation_id"] = GENERATION_ID
    return row


def _write_legacy(ledger, version, state="completed"):
    row = _legacy_row(version, state)
    ledger.path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps({"schema_version": version, "operations": {OPERATION_ID: row}}).encode()
    ledger.path.write_bytes(raw)
    return raw, row


def _seal(ledger, operation, *, usages=(), completeness="unknown"):
    ledger.bind_stage(operation.operation_id, stage_key=STAGE_KEY, grant_sha256="2" * 64)
    ledger.mark_provider_unknown(operation.operation_id)
    return ledger.adopt_proof(operation.operation_id, proof_sha256="3" * 64,
                             generation_id=GENERATION_ID, provider_usages=usages,
                             usage_completeness=completeness)


def test_empty_inspection_never_creates_root_or_ledger(configured):
    config, ledger, _ = configured
    inspected = ledger.inspect()
    assert inspected.schema_version is None
    assert inspected.operations == () and inspected.ledger_upgrade is None
    assert not config.layout.user_data_root.exists()


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("state,expected,effect,blocked", [
    ("budget_paused", "budget_paused", "not_started", 0),
    ("failed_released", "failed", "not_started", 0),
    ("completed", "completed", "unknown", 0),
    ("failed_paid", "failed", "unknown", 0),
    ("processing", "repair_required", "unknown", 1),
    ("activation_intent", "repair_required", "unknown", 1),
])
def test_legacy_readonly_classification_preserves_original_facts(configured, version, state, expected, effect, blocked):
    _, ledger, _ = configured
    raw, original = _write_legacy(ledger, version, state)
    inspected = ledger.inspect()
    assert ledger.path.read_bytes() == raw
    assert inspected.schema_version == version
    assert inspected.ledger_upgrade == {
        "from_schema": version, "to_schema": 3, "source_sha256": hashlib.sha256(raw).hexdigest(),
        "operation_count": 1, "blocking_operation_count": blocked,
    }
    operation = inspected.operations[0]
    assert operation.state.value == expected and operation.provider_effect == effect
    assert operation.public_plan_id == (None if version == 1 else original["public_plan_id"])
    assert operation.record_origin == f"legacy_v{version}"
    assert operation.proof_sha256 is None and operation.stage_key is None
    assert (operation.reservation.to_dict() if operation.reservation else None) == original["reservation"]
    assert (operation.provider_usage.to_dict() if operation.provider_usage else None) == original["provider_usage"]
    assert operation.generation_id == original["generation_id"]


@pytest.mark.parametrize("version", [1, 2])
def test_explicit_upgrade_is_atomic_and_old_confirmation_becomes_stale(configured, version):
    config, ledger, _ = configured
    _, original = _write_legacy(ledger, version)
    proposed = ledger.inspect()
    with write_lease_scope(config.layout):
        upgraded = ledger.upgrade(proposed.ledger_upgrade)
    assert upgraded.schema_version == 3 and upgraded.ledger_upgrade is None
    assert upgraded.operations == proposed.operations
    stored = json.loads(ledger.path.read_text())
    assert "provider_attempted" not in stored["operations"][OPERATION_ID]
    assert stored["operations"][OPERATION_ID]["reservation"] == original["reservation"]
    with write_lease_scope(config.layout), pytest.raises(PKVRuntimeError) as stale:
        ledger.upgrade(proposed.ledger_upgrade)
    assert stale.value.code is ErrorCode.RUNTIME_PLAN_STALE


@pytest.mark.parametrize("version", [1, 2])
def test_legacy_mutation_requires_explicit_upgrade_even_for_terminal_rows(configured, version):
    config, ledger, policy = configured
    original, _ = _write_legacy(ledger, version)
    with write_lease_scope(config.layout), pytest.raises(PKVRuntimeError) as denied:
        _begin(ledger, policy)
    assert denied.value.code is ErrorCode.EMBEDDING_LEDGER_UPGRADE_REQUIRED
    assert ledger.path.read_bytes() == original
    with write_lease_scope(config.layout), pytest.raises(PKVRuntimeError) as denied:
        ledger.mark_completed(OPERATION_ID)
    assert denied.value.code is ErrorCode.EMBEDDING_LEDGER_UPGRADE_REQUIRED


def test_upgrade_cas_binds_raw_bytes_not_just_equivalent_json(configured):
    config, ledger, _ = configured
    raw, _ = _write_legacy(ledger, 2)
    proposal = ledger.inspect().ledger_upgrade
    changed = raw + b"\n"
    ledger.path.write_bytes(changed)
    with write_lease_scope(config.layout), pytest.raises(PKVRuntimeError) as stale:
        ledger.upgrade(proposal)
    assert stale.value.code is ErrorCode.RUNTIME_PLAN_STALE
    assert ledger.path.read_bytes() == changed


def test_confirmed_upgrade_reports_stale_if_current_bytes_are_now_invalid(configured):
    config, ledger, _ = configured
    _write_legacy(ledger, 2)
    proposal = ledger.inspect().ledger_upgrade
    ledger.path.write_bytes(b"interrupted external update")
    with write_lease_scope(config.layout), pytest.raises(PKVRuntimeError) as stale:
        ledger.upgrade(proposal)
    assert stale.value.code is ErrorCode.RUNTIME_PLAN_STALE
    assert ledger.path.read_bytes() == b"interrupted external update"


def test_upgrade_rechecks_original_bytes_at_atomic_replace_boundary(configured, monkeypatch):
    config, ledger, _ = configured
    raw, _ = _write_legacy(ledger, 2)
    proposal = ledger.inspect().ledger_upgrade
    changed = raw + b"\n"
    original = RuntimeLayout.atomic_publish_user_file

    def replace_during_preparation(layout, target, **kwargs):
        ledger.path.write_bytes(changed)
        return original(layout, target, **kwargs)

    monkeypatch.setattr(RuntimeLayout, "atomic_publish_user_file", replace_during_preparation)
    with write_lease_scope(config.layout), pytest.raises(PKVRuntimeError) as stale:
        ledger.upgrade(proposal)
    assert stale.value.code is ErrorCode.RUNTIME_PLAN_STALE
    assert ledger.path.read_bytes() == changed


@pytest.mark.parametrize("raw", [
    b'{"schema_version":2,"schema_version":2,"operations":{}}',
    b'{"schema_version":true,"operations":{}}',
    b'{"schema_version":4,"operations":{}}',
    b'{"schema_version":2,"operations":{},"extra":0}',
    b'{"schema_version":2,"operations":[]}',
    b'not-json',
])
def test_corrupt_future_duplicate_and_unknown_fields_never_become_empty(configured, raw):
    _, ledger, _ = configured
    ledger.path.parent.mkdir(parents=True)
    ledger.path.write_bytes(raw)
    with pytest.raises(PKVRuntimeError) as corrupt:
        ledger.inspect()
    assert corrupt.value.code is ErrorCode.REPAIR_REQUIRED
    assert ledger.path.read_bytes() == raw


def test_bad_second_legacy_record_refuses_whole_upgrade(configured):
    _, ledger, _ = configured
    _write_legacy(ledger, 2)
    payload = json.loads(ledger.path.read_text())
    other = _legacy_row(2, "completed")
    other.update(operation_id="embedding-rebuild-" + "b" * 32, provider_attempted="false")
    payload["operations"][other["operation_id"]] = other
    raw = json.dumps(payload).encode()
    ledger.path.write_bytes(raw)
    with pytest.raises(PKVRuntimeError):
        ledger.inspect()
    assert ledger.path.read_bytes() == raw


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("state", ["completed", "failed_paid"])
def test_legacy_underreported_settlement_is_not_a_paid_terminal_exception(configured, version, state):
    _, ledger, _ = configured
    _write_legacy(ledger, version, state)
    payload = json.loads(ledger.path.read_text())
    payload["operations"][OPERATION_ID]["reservation"]["settled_tokens"] = 0
    raw = json.dumps(payload).encode()
    ledger.path.write_bytes(raw)
    with pytest.raises(PKVRuntimeError) as bad:
        ledger.inspect()
    assert bad.value.code is ErrorCode.REPAIR_REQUIRED
    assert ledger.path.read_bytes() == raw


@pytest.mark.parametrize("after_replace", [False, True])
def test_upgrade_replace_interruption_retains_complete_old_or_new_document(configured, monkeypatch, after_replace):
    config, ledger, _ = configured
    raw, _ = _write_legacy(ledger, 2)
    proposal = ledger.inspect().ledger_upgrade
    original = RuntimeLayout.atomic_publish_user_file

    def interrupt(layout, target, **kwargs):
        if after_replace:
            original(layout, target, **kwargs)
        raise OSError("synthetic publication interruption")

    monkeypatch.setattr(RuntimeLayout, "atomic_publish_user_file", interrupt)
    with write_lease_scope(config.layout), pytest.raises(PKVRuntimeError):
        ledger.upgrade(proposal)
    observed = ledger.inspect()
    assert observed.schema_version == (3 if after_replace else 2)
    assert len(observed.operations) == 1
    if not after_replace:
        assert ledger.path.read_bytes() == raw


@pytest.mark.parametrize("state,blocked", [("completed", False), ("failed_paid", False), ("processing", True), ("activation_intent", True)])
def test_legacy_paid_terminal_does_not_fence_root_but_unresolved_effect_does(configured, state, blocked):
    config, ledger, policy = configured
    _write_legacy(ledger, 2, state)
    with write_lease_scope(config.layout):
        ledger.upgrade(ledger.inspect().ledger_upgrade)
        if blocked:
            with pytest.raises(PKVRuntimeError) as rejected:
                _begin(ledger, policy)
            assert rejected.value.code is ErrorCode.REPAIR_REQUIRED
        else:
            operation = _begin(ledger, policy)
            assert operation.record_origin == "native_v3"


def test_successful_upgrade_survives_later_budget_refusal(configured):
    config, ledger, policy = configured
    _write_legacy(ledger, 2)
    proposal = ledger.inspect().ledger_upgrade
    with write_lease_scope(config.layout):
        ledger.upgrade(proposal)
        with pytest.raises(PKVRuntimeError) as paused:
            _begin(ledger, policy, estimate=201)
    assert paused.value.code is ErrorCode.EMBEDDING_BUDGET_PAUSED
    assert ledger.inspect().schema_version == 3
    assert len(ledger.read_operations()) == 2


def test_native_stage_effect_proof_and_original_period_survive_restart(configured):
    config, ledger, policy = configured
    with write_lease_scope(config.layout):
        operation = _begin(ledger, policy)
        sealed = _seal(ledger, operation, usages=(TokenUsage(embedding_input_tokens=250),), completeness="partial")
    raw = json.loads(ledger.path.read_text())["operations"][operation.operation_id]
    assert "provider_attempted" not in raw
    assert raw["provider_effect"] == "completed" and raw["state"] == "awaiting_publish"
    assert raw["provider_usage"]["cached_input_tokens"] is None
    reloaded = EmbeddingRebuildLedger(config.layout, clock=lambda: datetime(2027, 1, 1, tzinfo=timezone.utc))
    with write_lease_scope(config.layout):
        reloaded.mark_activation_intent(operation.operation_id, generation_id=GENERATION_ID)
        completed = reloaded.mark_completed(operation.operation_id)
        bytes_after_completion = ledger.path.read_bytes()
        assert reloaded.mark_completed(operation.operation_id) == completed
    assert ledger.path.read_bytes() == bytes_after_completion
    assert completed.provider_usage == sealed.provider_usage
    assert completed.reservation.reservation_id == operation.reservation.reservation_id
    assert completed.reservation.local_day == "2026-10-25"
    assert completed.reservation.local_month == "2026-10"
    assert completed.reservation.settled_tokens == 250
    assert completed.settlement_basis == "conservative_reservation"
    assert completed.usage_completeness == "partial"
    with write_lease_scope(config.layout), pytest.raises(PKVRuntimeError) as over_cap:
        _begin(ledger, policy, replay_key="9" * 64)
    assert over_cap.value.code is ErrorCode.EMBEDDING_BUDGET_PAUSED


def test_reported_overrun_counts_before_final_settlement_after_restart(configured):
    config, ledger, policy = configured
    with write_lease_scope(config.layout):
        operation = _begin(ledger, policy)
        sealed = _seal(ledger, operation,
                       usages=(TokenUsage(embedding_input_tokens=250), TokenUsage(generated_tokens=3)),
                       completeness="partial")
    assert sealed.reservation.state == "reserved" and sealed.reservation.reserved_tokens == 40
    assert sealed.provider_usage.known_total_tokens == 253
    assert sealed.provider_usage.cached_input_tokens is None
    reloaded = EmbeddingRebuildLedger(config.layout, clock=lambda: NOW)
    with write_lease_scope(config.layout):
        with pytest.raises(PKVRuntimeError) as unfinished:
            _begin(reloaded, policy, replay_key="9" * 64, estimate=1)
        assert unfinished.value.code is ErrorCode.REPAIR_REQUIRED
        reloaded.mark_failed(operation.operation_id, error_code=ErrorCode.RUNTIME_PLAN_STALE, provider_attempted=True)
        with pytest.raises(PKVRuntimeError) as over_cap:
            _begin(reloaded, policy, replay_key="9" * 64, estimate=1)
        assert over_cap.value.code is ErrorCode.EMBEDDING_BUDGET_PAUSED


@pytest.mark.parametrize("completeness,usage,expected,basis", [
    ("unknown", (), 40, "conservative_reservation"),
    ("complete", (TokenUsage(embedding_input_tokens=17),), 40, "conservative_reservation"),
    ("complete", (TokenUsage(embedding_input_tokens=53),), 53, "provider_reported"),
])
def test_settlement_never_fabricates_usage_or_double_counts_reservation(configured, completeness, usage, expected, basis):
    config, ledger, policy = configured
    with write_lease_scope(config.layout):
        operation = _begin(ledger, policy)
        _seal(ledger, operation, usages=usage, completeness=completeness)
        ledger.mark_activation_intent(operation.operation_id, generation_id=GENERATION_ID)
        completed = ledger.mark_completed(operation.operation_id)
    assert completed.reservation.settled_tokens == expected
    assert completed.settlement_basis == basis
    assert (completed.provider_usage is None) == (completeness == "unknown")
    if completed.provider_usage:
        assert completed.provider_usage.cached_input_tokens is None


@pytest.mark.parametrize("live_proof", [False, True])
def test_unknown_effect_requires_live_no_start_proof_to_release(configured, live_proof):
    config, ledger, policy = configured
    with write_lease_scope(config.layout):
        operation = _begin(ledger, policy)
        ledger.bind_stage(operation.operation_id, stage_key=STAGE_KEY, grant_sha256="2" * 64)
        ledger.mark_provider_unknown(operation.operation_id)
    reloaded = EmbeddingRebuildLedger(config.layout, clock=lambda: NOW)
    with write_lease_scope(config.layout):
        failed = reloaded.mark_failed(operation.operation_id, error_code=ErrorCode.PROVIDER_UNAVAILABLE,
                                      provider_attempted=False, provider_not_started=live_proof)
        assert failed.reservation.state == ("released" if live_proof else "settled")
        if live_proof:
            retry = _begin(reloaded, policy)
            assert retry.operation_id != operation.operation_id
        else:
            assert failed.provider_effect == "unknown"
            with pytest.raises(PKVRuntimeError) as consumed:
                _begin(reloaded, policy)
            assert consumed.value.code is ErrorCode.RUNTIME_PLAN_STALE


@pytest.mark.parametrize("sealed", [False, True])
def test_same_material_remains_consumed_across_month_boundary(configured, sealed):
    config, ledger, policy = configured
    with write_lease_scope(config.layout):
        operation = _begin(ledger, policy)
        if sealed:
            _seal(ledger, operation)
    later = EmbeddingRebuildLedger(config.layout, clock=lambda: datetime(2026, 11, 1, tzinfo=timezone.utc))
    with write_lease_scope(config.layout), pytest.raises(PKVRuntimeError) as busy:
        _begin(later, policy, period={"timezone": "UTC", "local_day": "2026-11-01", "local_month": "2026-11"})
    assert busy.value.code is ErrorCode.WRITE_BUSY
    assert len(ledger.read_operations()) == 1


def test_budget_rejection_does_not_consume_replay_eligibility(configured):
    config, ledger, policy = configured
    with write_lease_scope(config.layout):
        with pytest.raises(PKVRuntimeError) as paused:
            _begin(ledger, policy, estimate=201)
        accepted = _begin(ledger, policy, estimate=40)
    assert paused.value.code is ErrorCode.EMBEDDING_BUDGET_PAUSED
    assert accepted.reservation.state == "reserved"
    assert len(ledger.read_operations()) == 2


@pytest.mark.parametrize("phase", ["unknown", "awaiting_publish", "activation_intent"])
def test_unsettled_effect_blocks_different_material_after_restart(configured, phase):
    config, ledger, policy = configured
    with write_lease_scope(config.layout):
        operation = _begin(ledger, policy)
        if phase == "unknown":
            ledger.bind_stage(operation.operation_id, stage_key=STAGE_KEY, grant_sha256="2" * 64)
            ledger.mark_provider_unknown(operation.operation_id)
        else:
            _seal(ledger, operation)
            if phase == "activation_intent":
                ledger.mark_activation_intent(operation.operation_id, generation_id=GENERATION_ID)
    raw = ledger.path.read_bytes()
    reloaded = EmbeddingRebuildLedger(config.layout, clock=lambda: NOW)
    assert reloaded.inspect().blocking_operation_count == 1
    with write_lease_scope(config.layout):
        with pytest.raises(PKVRuntimeError) as blocked:
            _begin(reloaded, policy, replay_key="9" * 64)
        assert blocked.value.code is ErrorCode.REPAIR_REQUIRED
        with pytest.raises(PKVRuntimeError) as busy:
            _begin(reloaded, policy)
        assert busy.value.code is ErrorCode.WRITE_BUSY
    assert ledger.path.read_bytes() == raw


def test_monthly_cap_remains_after_day_rollover_and_resets_only_next_month(configured):
    config, ledger, policy = configured
    with write_lease_scope(config.layout):
        operation = _begin(ledger, policy)
        _seal(ledger, operation, usages=(TokenUsage(embedding_input_tokens=390),), completeness="complete")
        ledger.mark_activation_intent(operation.operation_id, generation_id=GENERATION_ID)
        ledger.mark_completed(operation.operation_id)
    next_day = EmbeddingRebuildLedger(config.layout, clock=lambda: datetime(2026, 10, 26, tzinfo=timezone.utc))
    with write_lease_scope(config.layout), pytest.raises(PKVRuntimeError) as paused:
        _begin(next_day, policy, replay_key="9" * 64,
               period={"timezone": "UTC", "local_day": "2026-10-26", "local_month": "2026-10"})
    assert paused.value.code is ErrorCode.EMBEDDING_BUDGET_PAUSED
    next_month = EmbeddingRebuildLedger(config.layout, clock=lambda: datetime(2026, 11, 1, tzinfo=timezone.utc))
    with write_lease_scope(config.layout):
        accepted = _begin(next_month, policy, replay_key="9" * 64,
                          period={"timezone": "UTC", "local_day": "2026-11-01", "local_month": "2026-11"})
    assert accepted.reservation.state == "reserved"


@pytest.mark.parametrize("change,estimate", [("cap_up", 60), ("cap_down", 1), ("model", 60), ("endpoint", 60)])
def test_policy_changes_do_not_erase_tokens_already_spent_in_current_period(configured, change, estimate):
    config, ledger, policy = configured
    with write_lease_scope(config.layout):
        operation = _begin(ledger, policy)
        _seal(ledger, operation, usages=(TokenUsage(embedding_input_tokens=150),), completeness="complete")
        ledger.mark_activation_intent(operation.operation_id, generation_id=GENERATION_ID)
        ledger.mark_completed(operation.operation_id)
    settings = json.loads(config.layout.user_config_path.read_text())
    if change.startswith("cap_"):
        settings["ai"]["embedding_rebuild"]["token_budget"]["daily_total_tokens"] = 201 if change == "cap_up" else 100
    elif change == "model":
        settings["ai"]["embedding"]["model"] = "new-fake"
    else:
        settings["ai"]["embedding"]["base_url"] = "https://other-embedding.invalid/v1"
    config.layout.user_config_path.write_text(json.dumps(settings))
    new_policy = inspect_embedding_rebuild_policy(Config(layout=config.layout))
    assert new_policy.policy_fingerprint != policy.policy_fingerprint
    with write_lease_scope(config.layout), pytest.raises(PKVRuntimeError) as paused:
        _begin(ledger, new_policy, replay_key="9" * 64, estimate=estimate)
    assert paused.value.code is ErrorCode.EMBEDDING_BUDGET_PAUSED


@pytest.mark.parametrize("cap", ["daily", "monthly"])
def test_timezone_change_conservatively_counts_overlapping_original_period(configured, cap):
    config, _, policy = configured
    ledger = EmbeddingRebuildLedger(config.layout, clock=lambda: datetime(2026, 10, 31, 20, tzinfo=timezone.utc))
    with write_lease_scope(config.layout):
        operation = _begin(ledger, policy, period={"timezone": "UTC", "local_day": "2026-10-31", "local_month": "2026-10"})
        _seal(ledger, operation, usages=(TokenUsage(embedding_input_tokens=190),), completeness="complete")
        ledger.mark_activation_intent(operation.operation_id, generation_id=GENERATION_ID)
        ledger.mark_completed(operation.operation_id)
    new_policy = replace(policy, policy_fingerprint="9" * 64,
                         token_quota=TokenQuota(timezone="Asia/Shanghai",
                                                daily_total_tokens=200 if cap == "daily" else None,
                                                monthly_total_tokens=200 if cap == "monthly" else None))
    next_day = EmbeddingRebuildLedger(config.layout, clock=lambda: datetime(2026, 11, 1, tzinfo=timezone.utc))
    with write_lease_scope(config.layout), pytest.raises(PKVRuntimeError) as paused:
        _begin(next_day, new_policy, replay_key="8" * 64, estimate=20,
               period={"timezone": "Asia/Shanghai", "local_day": "2026-11-01", "local_month": "2026-11"})
    assert paused.value.code is ErrorCode.EMBEDDING_BUDGET_PAUSED
    settled = ledger.get_operation(operation.operation_id).reservation
    assert settled.timezone == "UTC" and settled.local_month == "2026-10"
    assert settled.settled_tokens == 190


def test_terminal_accounting_cannot_accept_new_usage_facts(configured):
    config, ledger, policy = configured
    with write_lease_scope(config.layout):
        operation = _begin(ledger, policy)
        _seal(ledger, operation)
        ledger.mark_activation_intent(operation.operation_id, generation_id=GENERATION_ID)
        ledger.mark_completed(operation.operation_id)
        raw = ledger.path.read_bytes()
        with pytest.raises(PKVRuntimeError) as stale:
            ledger.mark_completed(operation.operation_id, provider_usages=(TokenUsage(embedding_input_tokens=55),))
    assert stale.value.code is ErrorCode.RUNTIME_PLAN_STALE
    assert ledger.path.read_bytes() == raw


def test_stale_budget_period_is_rejected_before_any_operation(configured):
    config, ledger, policy = configured
    with write_lease_scope(config.layout), pytest.raises(PKVRuntimeError) as stale:
        _begin(ledger, policy, period={"timezone": "UTC", "local_day": "2026-10-24", "local_month": "2026-10"})
    assert stale.value.code is ErrorCode.RUNTIME_PLAN_STALE
    assert not ledger.path.exists()


def test_budget_inspection_and_default_ledger_share_one_clock(configured, monkeypatch):
    config, _, policy = configured
    import src.runtime.embedding_rebuild_ledger as module
    monkeypatch.setattr(module, "current_utc", lambda: datetime(2026, 11, 1, tzinfo=timezone.utc))
    ledger = EmbeddingRebuildLedger(config.layout)
    period = current_period(policy)
    with write_lease_scope(config.layout):
        operation = _begin(ledger, policy, period=period)
    assert operation.reservation.local_day == period["local_day"] == "2026-11-01"


def test_mutations_without_lease_cannot_upgrade_bind_or_change_shared_ledger(configured):
    config, ledger, policy = configured
    raw, _ = _write_legacy(ledger, 2)
    with pytest.raises(PKVRuntimeError) as busy:
        ledger.upgrade(ledger.inspect().ledger_upgrade)
    assert busy.value.code is ErrorCode.WRITE_BUSY and ledger.path.read_bytes() == raw
    with write_lease_scope(config.layout):
        ledger.upgrade(ledger.inspect().ledger_upgrade)
        operation = _begin(ledger, policy)
    raw = ledger.path.read_bytes()
    with pytest.raises(PKVRuntimeError) as busy:
        ledger.bind_stage(operation.operation_id, stage_key=STAGE_KEY, grant_sha256="2" * 64)
    assert busy.value.code is ErrorCode.WRITE_BUSY and ledger.path.read_bytes() == raw


@pytest.mark.parametrize("field,value", [
    ("provider_attempted", False), ("provider_effect", "finished"),
    ("data_root_identity_sha256", None), ("stage_key", "../outside"),
    ("usage_completeness", "complete"), ("settlement_basis", "provider_reported"),
    ("budget_check_period", {"timezone": "UTC", "local_day": "2026-02-31", "local_month": "2026-02"}),
])
def test_v3_strict_fields_and_cross_field_constraints(configured, field, value):
    config, ledger, policy = configured
    with write_lease_scope(config.layout):
        operation = _begin(ledger, policy)
    payload = json.loads(ledger.path.read_text())
    payload["operations"][operation.operation_id][field] = value
    ledger.path.write_text(json.dumps(payload))
    with pytest.raises(PKVRuntimeError) as corrupt:
        ledger.inspect()
    assert corrupt.value.code is ErrorCode.REPAIR_REQUIRED


@pytest.mark.parametrize("state", ["completed", "activation_intent"])
def test_native_publication_cannot_use_legacy_unknown_effect_exception(configured, state):
    config, ledger, policy = configured
    with write_lease_scope(config.layout):
        operation = _begin(ledger, policy)
        ledger.bind_stage(operation.operation_id, stage_key=STAGE_KEY, grant_sha256="2" * 64)
        ledger.mark_provider_unknown(operation.operation_id)
    payload = json.loads(ledger.path.read_text())
    row = payload["operations"][operation.operation_id]
    row.update(state=state, generation_id=GENERATION_ID)
    if state == "completed":
        row["reservation"].update(state="settled", settled_tokens=40)
        row["settlement_basis"] = "conservative_reservation"
    raw = json.dumps(payload).encode()
    ledger.path.write_bytes(raw)
    with pytest.raises(PKVRuntimeError) as corrupt:
        ledger.inspect()
    assert corrupt.value.code is ErrorCode.REPAIR_REQUIRED
    assert ledger.path.read_bytes() == raw
