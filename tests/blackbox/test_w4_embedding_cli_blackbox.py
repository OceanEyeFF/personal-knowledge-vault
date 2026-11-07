"""W4 ER-04/05/07/08 through independent, actual public CLI processes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.blackbox.r4_fullflow_support import (
    ProviderHarness,
    assert_no_public_leak,
    finish_process,
    stop_process,
)
from tests.harness.w4_embedding_lifecycle import (
    CONTENT,
    NEXT_DAY,
    TITLE,
    assert_publication_durable,
    assert_sealed_proof,
    competing_writer,
    configure_budget,
    create_runtime,
    envelope,
    issue_code,
    ledger_operations,
    pending_operation,
    provider_script,
    shared_bytes,
    write_clock,
)


def _call(runtime, *arguments: str, success: bool = True) -> dict:
    result = runtime.run_cli("embedding", *arguments)
    assert result.returncode == (0 if success else 1), result.stderr
    raw = result.stdout if success else result.stderr
    payload = json.loads(raw)
    assert_no_public_leak(raw, runtime, TITLE, CONTENT)
    return payload


def _plan(runtime, *, force: bool = False) -> dict:
    return envelope(
        _call(runtime, "rebuild-v2", *(("--force",) if force else ())),
        "plan_embedding_rebuild_v2",
    )


def _start(runtime, plan: dict):
    return runtime.start_cli(
        "embedding", "rebuild-v2", *(("--force",) if plan["force"] else ()),
        "--apply", "--confirm", plan["plan_id"], "--allow-network",
    )


def _release(harness, runtime, step: str) -> None:
    harness.await_request(step)
    runtime.prove_writer_lease_is_free()
    harness.continue_request(step)
    harness.await_event("step_completed", step_id=step)


def _successful_rebuild(harness, runtime, plan: dict, number: int) -> dict:
    process = _start(runtime, plan)
    try:
        _release(harness, runtime, f"rebuild-{number}-document")
        _release(harness, runtime, f"rebuild-{number}-chunks")
        stdout, stderr = finish_process(process)
    except BaseException:
        stop_process(process)
        raise
    assert process.returncode == 0, stderr
    execution = envelope(json.loads(stdout), "execute_embedding_rebuild_v2")
    assert execution["provider_called"] is True
    assert execution["publication_committed"] is True
    return execution


@pytest.mark.slow
def test_cli_sealed_pending_restarts_across_day_and_month_without_provider(tmp_path: Path) -> None:
    """ER-04/05/08: durable seal survives D contention and process death."""

    scenario = "w4-cli-publish-only"
    with ProviderHarness.start(tmp_path / "provider", scenario_id=scenario, script=provider_script(scenario)) as harness:
        runtime = create_runtime(tmp_path / "product", provider_base_url=harness.base_url)
        planned = _plan(runtime)
        assert planned["inspection"]["budget_check_period"]["local_day"] == "2026-09-30"
        process = _start(runtime, planned)
        try:
            _release(harness, runtime, "rebuild-1-document")
            harness.await_request("rebuild-1-chunks")
            with competing_writer(runtime):
                harness.continue_request("rebuild-1-chunks")
                harness.await_event("step_completed", step_id="rebuild-1-chunks")
                stdout, stderr = finish_process(process)
                assert process.returncode == 1
                rejected = json.loads(stderr)
                envelope(rejected, "execute_embedding_rebuild_v2", success=False)
                operation_id = pending_operation(rejected)
                # Read under the competing writer. This must use the durable
                # seal, not mutate the still-processing shared ledger.
                pending = envelope(_call(runtime, "operation-v2", operation_id), "inspect_embedding_operation_v2")
                assert pending["effective_state"] == "awaiting_publish"
                assert pending["proof_valid"] is True
                assert pending["provider_effect"] == "completed"
                assert pending["publication_committed"] is False
        except BaseException:
            stop_process(process)
            raise
        assert process.poll() is not None  # Original host is gone before adoption.
        saved_stage = assert_sealed_proof(runtime, operation_id, pending)
        before = ledger_operations(runtime)
        assert len(before) == 1
        reservation_before = before[operation_id]["reservation"].copy()
        assert reservation_before["local_day"] == "2026-09-30"
        assert reservation_before["local_month"] == "2026-09"
        proof = pending["proof_sha256"]
        old_publish = envelope(_call(runtime, "publish-v2", operation_id), "plan_embedding_publication_v2")

        write_clock(runtime, NEXT_DAY)
        inspected = envelope(_call(runtime, "operation-v2", operation_id), "inspect_embedding_operation_v2")
        assert inspected["proof_sha256"] == proof
        assert inspected["reservation"] == pending["reservation"]
        durable_before_reads = shared_bytes(runtime)
        publication = envelope(_call(runtime, "publish-v2", operation_id), "plan_embedding_publication_v2")
        assert publication["plan_id"] == old_publish["plan_id"]
        assert publication["action"] == "publish_only"
        assert publication["requires_network"] is False
        assert publication["creates_reservation"] is False
        assert shared_bytes(runtime) == durable_before_reads

        # Force and a new budget period cannot authorize a duplicate Provider
        # attempt. Either planning or execution may surface the active fence.
        force = runtime.run_cli("embedding", "rebuild-v2", "--force")
        if force.returncode == 0:
            force_plan = envelope(json.loads(force.stdout), "plan_embedding_rebuild_v2")
            blocked = _call(runtime, "rebuild-v2", "--force", "--apply", "--confirm", force_plan["plan_id"], "--allow-network", success=False)
        else:
            assert force.returncode == 1
            blocked = json.loads(force.stderr)
        assert pending_operation(blocked) == operation_id
        assert ledger_operations(runtime) == before

        missing = _call(runtime, "publish-v2", operation_id, "--apply", success=False)
        assert issue_code(missing) == "confirmation_required"
        assert ledger_operations(runtime) == before
        execution = envelope(_call(runtime, "publish-v2", operation_id, "--apply", "--confirm", publication["plan_id"]), "execute_embedding_publication_v2")
        assert execution["operation_id"] == operation_id
        assert execution["status"] == "ready"
        assert execution["provider_called"] is False
        assert execution["publication_committed"] is True
        assert execution["ledger_completion_pending"] is False
        assert execution["audit_completion_pending"] is False
        completed = envelope(_call(runtime, "operation-v2", operation_id), "inspect_embedding_operation_v2")
        assert completed["effective_state"] == "completed"
        assert completed["proof_sha256"] == proof
        after = ledger_operations(runtime)
        assert len(after) == 1
        reservation_after = after[operation_id]["reservation"]
        for key in ("reservation_id", "local_day", "local_month", "reserved_tokens"):
            assert reservation_after[key] == reservation_before[key]
        assert reservation_after["state"] == "settled"
        assert reservation_after["settled_tokens"] == max(reservation_before["reserved_tokens"], 34)
        completed_bytes = shared_bytes(runtime)
        terminal = _call(runtime, "publish-v2", operation_id, success=False)
        assert issue_code(terminal) == "embedding_operation_completed"
        assert shared_bytes(runtime) == completed_bytes
        assert_publication_durable(runtime, operation_id, saved_stage)
        # Harness has exactly two scripted calls; an extra one also fails its
        # final strict request_count assertion when the context exits.
        harness.assert_redacted_telemetry(TITLE, CONTENT)


@pytest.mark.slow
@pytest.mark.parametrize("budget_kind", ["day", "month"])
def test_cli_budget_period_recovery_requires_new_public_confirmation(tmp_path: Path, budget_kind: str) -> None:
    """ER-07: public quota rejection is recoverable, with period-bound consent."""

    scenario = f"w4-cli-budget-{budget_kind}"
    with ProviderHarness.start(tmp_path / "provider", scenario_id=scenario, script=provider_script(scenario, rebuilds=2)) as harness:
        runtime = create_runtime(tmp_path / "product", provider_base_url=harness.base_url, instant="2026-09-29T12:00:00Z")
        charge = max(_plan(runtime)["estimated_embedding_tokens"], 34)
        configure_budget(runtime, daily=charge if budget_kind == "day" else charge * 10, monthly=charge * 10 if budget_kind == "day" else charge)
        _successful_rebuild(harness, runtime, _plan(runtime), 1)
        rejected_plan = _plan(runtime, force=True)
        rejected = _call(runtime, "rebuild-v2", "--force", "--apply", "--confirm", rejected_plan["plan_id"], "--allow-network", success=False)
        assert issue_code(rejected) == "embedding_budget_paused"
        paused = [value for value in ledger_operations(runtime).values() if value["state"] == "budget_paused"]
        assert paused and all(value["reservation"] is None and value["provider_effect"] == "not_started" for value in paused)

        write_clock(runtime, "2026-09-30T12:00:00Z")
        stale = _call(runtime, "rebuild-v2", "--force", "--apply", "--confirm", rejected_plan["plan_id"], "--allow-network", success=False)
        assert issue_code(stale) == "runtime_plan_stale"
        next_plan = _plan(runtime, force=True)
        assert next_plan["plan_id"] != rejected_plan["plan_id"]
        if budget_kind == "month":
            same_month = _call(runtime, "rebuild-v2", "--force", "--apply", "--confirm", next_plan["plan_id"], "--allow-network", success=False)
            assert issue_code(same_month) == "embedding_budget_paused"
            write_clock(runtime, NEXT_DAY)
            stale_month = _call(runtime, "rebuild-v2", "--force", "--apply", "--confirm", next_plan["plan_id"], "--allow-network", success=False)
            assert issue_code(stale_month) == "runtime_plan_stale"
            next_plan = _plan(runtime, force=True)
        denied = _call(runtime, "rebuild-v2", "--force", "--apply", "--confirm", next_plan["plan_id"], success=False)
        assert issue_code(denied) == "embedding_rebuild_authorization_required"
        result = _successful_rebuild(harness, runtime, next_plan, 2)
        operations = ledger_operations(runtime)
        completed = [value for value in operations.values() if value["state"] == "completed"]
        assert len(completed) == 2
        final_reservation = operations[result["operation_id"]]["reservation"]
        assert final_reservation["local_day"] == ("2026-09-30" if budget_kind == "day" else "2026-10-01")
        assert final_reservation["local_month"] == ("2026-09" if budget_kind == "day" else "2026-10")
        harness.assert_redacted_telemetry(TITLE, CONTENT)
