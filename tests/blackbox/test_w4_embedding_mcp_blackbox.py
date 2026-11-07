"""W4 ER-04/05/07/08 via actual MCP JSON-RPC over independent stdio hosts."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import stdio_client
import pytest

from tests.blackbox.r4_fullflow_support import ProviderHarness, assert_no_public_leak, parse_mcp_tool_json
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


@asynccontextmanager
async def _server(runtime, label: str):
    with (runtime.layout.tmp_dir / f"w4-{label}.stderr").open("w+", encoding="utf-8") as error_log:
        async with stdio_client(runtime.mcp_server_params(), errlog=error_log) as (read, write):
            async with ClientSession(read, write) as session:
                await asyncio.wait_for(session.initialize(), timeout=30)
                yield session
        error_log.flush()
        error_log.seek(0)
        assert_no_public_leak(error_log.read(), runtime, TITLE, CONTENT)


async def _raw(session, name: str, arguments: dict | None = None) -> dict:
    result = await asyncio.wait_for(session.call_tool(name, arguments or {}), timeout=30)
    payload = parse_mcp_tool_json(result)
    structured = getattr(result, "structuredContent", None)
    assert structured == payload, "MCP text and structuredContent must carry the identical envelope"
    return payload


async def _call(session, name: str, arguments: dict | None = None, *, success: bool = True):
    payload = await _raw(session, name, arguments)
    envelope(payload, name, success=success)
    return payload["result"] if success else payload


async def _plan(session, *, force: bool = False) -> dict:
    return await _call(session, "plan_embedding_rebuild_v2", {"force": force})


async def _assert_raw_scalar_rejection(session, runtime) -> None:
    """JSON scalars must reach the closed contract before FastMCP coercion."""

    operation_id = "embedding-rebuild-" + "a" * 32
    plan_id = "b" * 64
    invalid = [
        ("inspect_embedding_rebuild_v2", {"force": False}),
        ("plan_embedding_rebuild_v2", {"force": 1}),
        ("plan_embedding_rebuild_v2", {"force": "false"}),
        ("execute_embedding_rebuild_v2", {"plan_id": plan_id, "approved": 1}),
        ("execute_embedding_rebuild_v2", {"plan_id": plan_id, "approved": True, "allow_network": "true"}),
        ("execute_embedding_rebuild_v2", {"plan_id": plan_id.upper()}),
        ("inspect_embedding_operation_v2", {"operation_id": 1}),
        ("plan_embedding_publication_v2", {"operation_id": operation_id, "force": True}),
        ("execute_embedding_publication_v2", {"operation_id": operation_id, "plan_id": plan_id, "allow_network": True}),
        ("execute_embedding_publication_v2", {"operation_id": operation_id, "plan_id": plan_id, "approved": "true"}),
    ]
    before = shared_bytes(runtime)
    for name, arguments in invalid:
        rejected = await _call(session, name, arguments, success=False)
        assert issue_code(rejected) == "runtime_plan_stale"
    assert shared_bytes(runtime) == before
    assert not (runtime.layout.vector_index_dir / "staging").exists()


def _execute_arguments(plan: dict, *, allow_network: bool = True) -> dict:
    return {"plan_id": plan["plan_id"], "approved": True, "allow_network": allow_network, "force": plan["force"]}


async def _request(harness, step: str, operation: asyncio.Task) -> None:
    waiting = asyncio.create_task(asyncio.to_thread(harness.await_request, step))
    done, _ = await asyncio.wait((waiting, operation), return_when=asyncio.FIRST_COMPLETED)
    if operation in done:
        waiting.cancel()
        raise AssertionError(f"MCP operation ended before Provider barrier: {operation.result()!r}")
    await asyncio.wait_for(waiting, timeout=35)


async def _release(harness, runtime, step: str, operation: asyncio.Task) -> None:
    await _request(harness, step, operation)
    await asyncio.to_thread(runtime.prove_writer_lease_is_free)
    harness.continue_request(step)
    await asyncio.to_thread(harness.await_event, "step_completed", step_id=step)


async def _successful_rebuild(session, harness, runtime, plan: dict, number: int) -> dict:
    operation = asyncio.create_task(_call(session, "execute_embedding_rebuild_v2", _execute_arguments(plan)))
    try:
        await _release(harness, runtime, f"rebuild-{number}-document", operation)
        await _release(harness, runtime, f"rebuild-{number}-chunks", operation)
        result = await operation
    finally:
        if not operation.done():
            operation.cancel()
    assert result["provider_called"] is True
    assert result["publication_committed"] is True
    return result


@pytest.mark.slow
@pytest.mark.asyncio
async def test_mcp_sealed_pending_restarts_across_day_and_month_without_provider(tmp_path: Path) -> None:
    """ER-04/05/08: the second host adopts proof and the original reservation."""

    scenario = "w4-mcp-publish-only"
    with ProviderHarness.start(tmp_path / "provider", scenario_id=scenario, script=provider_script(scenario)) as harness:
        runtime = create_runtime(tmp_path / "product", provider_base_url=harness.base_url)
        async with _server(runtime, "original") as session:
            await _assert_raw_scalar_rejection(session, runtime)
            plan = await _plan(session)
            assert plan["inspection"]["budget_check_period"]["local_day"] == "2026-09-30"
            operation = asyncio.create_task(_call(session, "execute_embedding_rebuild_v2", _execute_arguments(plan), success=False))
            try:
                await _release(harness, runtime, "rebuild-1-document", operation)
                await _request(harness, "rebuild-1-chunks", operation)
                with competing_writer(runtime):
                    harness.continue_request("rebuild-1-chunks")
                    await asyncio.to_thread(harness.await_event, "step_completed", step_id="rebuild-1-chunks")
                    rejected = await operation
                    operation_id = pending_operation(rejected)
                    pending = await _call(session, "inspect_embedding_operation_v2", {"operation_id": operation_id})
                    assert pending["effective_state"] == "awaiting_publish"
                    assert pending["provider_effect"] == "completed"
                    assert pending["proof_valid"] is True
                    assert pending["publication_committed"] is False
            finally:
                if not operation.done():
                    operation.cancel()
            old_publish = await _call(session, "plan_embedding_publication_v2", {"operation_id": operation_id})
        # stdio context exited: no first-host memory/capability can serve this.
        saved_stage = assert_sealed_proof(runtime, operation_id, pending)
        before = ledger_operations(runtime)
        assert len(before) == 1
        reservation_before = before[operation_id]["reservation"].copy()
        assert reservation_before["local_day"] == "2026-09-30"
        assert reservation_before["local_month"] == "2026-09"
        write_clock(runtime, NEXT_DAY)
        async with _server(runtime, "fresh-publication") as session:
            observed = await _call(session, "inspect_embedding_operation_v2", {"operation_id": operation_id})
            assert observed["proof_sha256"] == pending["proof_sha256"]
            assert observed["reservation"] == pending["reservation"]
            read_bytes = shared_bytes(runtime)
            publication = await _call(session, "plan_embedding_publication_v2", {"operation_id": operation_id})
            assert publication["plan_id"] == old_publish["plan_id"]
            assert publication["requires_network"] is False
            assert publication["creates_reservation"] is False
            assert publication["action"] == "publish_only"
            assert shared_bytes(runtime) == read_bytes
            forced = await _raw(session, "plan_embedding_rebuild_v2", {"force": True})
            if forced["status"] == "success":
                blocked = await _call(session, "execute_embedding_rebuild_v2", _execute_arguments(forced["result"]), success=False)
            else:
                blocked = forced
            assert pending_operation(blocked) == operation_id
            assert ledger_operations(runtime) == before
            missing = await _call(session, "execute_embedding_publication_v2", {"operation_id": operation_id, "plan_id": publication["plan_id"]}, success=False)
            assert issue_code(missing) == "confirmation_required"
            assert ledger_operations(runtime) == before
            result = await _call(session, "execute_embedding_publication_v2", {"operation_id": operation_id, "plan_id": publication["plan_id"], "approved": True})
            assert result["status"] == "ready"
            assert result["provider_called"] is False
            assert result["publication_committed"] is True
            assert result["ledger_completion_pending"] is False
            assert result["audit_completion_pending"] is False
        async with _server(runtime, "fresh-completed") as session:
            completed = await _call(session, "inspect_embedding_operation_v2", {"operation_id": operation_id})
            assert completed["effective_state"] == "completed"
            assert completed["proof_sha256"] == pending["proof_sha256"]
            completed_bytes = shared_bytes(runtime)
            terminal = await _call(session, "plan_embedding_publication_v2", {"operation_id": operation_id}, success=False)
            assert issue_code(terminal) == "embedding_operation_completed"
            assert shared_bytes(runtime) == completed_bytes
        after = ledger_operations(runtime)
        assert len(after) == 1
        reservation_after = after[operation_id]["reservation"]
        for key in ("reservation_id", "local_day", "local_month", "reserved_tokens"):
            assert reservation_after[key] == reservation_before[key]
        assert reservation_after["state"] == "settled"
        assert reservation_after["settled_tokens"] == max(reservation_before["reserved_tokens"], 34)
        assert_publication_durable(runtime, operation_id, saved_stage)
        assert_no_public_leak(json.dumps(result), runtime, TITLE, CONTENT)
        harness.assert_redacted_telemetry(TITLE, CONTENT)


@pytest.mark.slow
@pytest.mark.asyncio
@pytest.mark.parametrize("budget_kind", ["day", "month"])
async def test_mcp_budget_period_recovery_requires_new_public_confirmation(tmp_path: Path, budget_kind: str) -> None:
    """ER-07: quota recovery and explicit renewed consent use real MCP calls."""

    scenario = f"w4-mcp-budget-{budget_kind}"
    with ProviderHarness.start(tmp_path / "provider", scenario_id=scenario, script=provider_script(scenario, rebuilds=2)) as harness:
        runtime = create_runtime(tmp_path / "product", provider_base_url=harness.base_url, instant="2026-09-29T12:00:00Z")
        async with _server(runtime, "estimate") as session:
            charge = max((await _plan(session))["estimated_embedding_tokens"], 34)
        configure_budget(runtime, daily=charge if budget_kind == "day" else charge * 10, monthly=charge * 10 if budget_kind == "day" else charge)
        async with _server(runtime, "exhausted-period") as session:
            await _successful_rebuild(session, harness, runtime, await _plan(session), 1)
            rejected_plan = await _plan(session, force=True)
            rejected = await _call(session, "execute_embedding_rebuild_v2", _execute_arguments(rejected_plan), success=False)
            assert issue_code(rejected) == "embedding_budget_paused"
        paused = [value for value in ledger_operations(runtime).values() if value["state"] == "budget_paused"]
        assert paused and all(value["reservation"] is None and value["provider_effect"] == "not_started" for value in paused)
        write_clock(runtime, "2026-09-30T12:00:00Z")
        async with _server(runtime, "next-day") as session:
            stale = await _call(session, "execute_embedding_rebuild_v2", _execute_arguments(rejected_plan), success=False)
            assert issue_code(stale) == "runtime_plan_stale"
            next_plan = await _plan(session, force=True)
            assert next_plan["plan_id"] != rejected_plan["plan_id"]
            if budget_kind == "month":
                same_month = await _call(session, "execute_embedding_rebuild_v2", _execute_arguments(next_plan), success=False)
                assert issue_code(same_month) == "embedding_budget_paused"
            else:
                missing = await _call(session, "execute_embedding_rebuild_v2", _execute_arguments(next_plan, allow_network=False), success=False)
                assert issue_code(missing) == "embedding_rebuild_authorization_required"
                result = await _successful_rebuild(session, harness, runtime, next_plan, 2)
        if budget_kind == "month":
            write_clock(runtime, NEXT_DAY)
            async with _server(runtime, "next-month") as session:
                stale_month = await _call(session, "execute_embedding_rebuild_v2", _execute_arguments(next_plan), success=False)
                assert issue_code(stale_month) == "runtime_plan_stale"
                next_plan = await _plan(session, force=True)
                missing = await _call(session, "execute_embedding_rebuild_v2", _execute_arguments(next_plan, allow_network=False), success=False)
                assert issue_code(missing) == "embedding_rebuild_authorization_required"
                result = await _successful_rebuild(session, harness, runtime, next_plan, 2)
        operations = ledger_operations(runtime)
        assert len([value for value in operations.values() if value["state"] == "completed"]) == 2
        reservation = operations[result["operation_id"]]["reservation"]
        assert reservation["local_day"] == ("2026-09-30" if budget_kind == "day" else "2026-10-01")
        assert reservation["local_month"] == ("2026-09" if budget_kind == "day" else "2026-10")
        harness.assert_redacted_telemetry(TITLE, CONTENT)
