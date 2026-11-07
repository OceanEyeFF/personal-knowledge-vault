"""True MCP-stdio public Embedding rebuild process-boundary acceptance."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sqlite3
import stat

from mcp import ClientSession
from mcp.client.stdio import stdio_client
import pytest

from src.runtime.embedding_rebuild_ledger import (
    EmbeddingRebuildLedger,
    EmbeddingRebuildLedgerState,
)
from src.runtime.embedding_stage_proof import validate_publish_ready
from src.runtime.runtime_snapshot import RuntimeSnapshotStore
from src.runtime.write_lease import write_lease_scope
from src.storage.derivation_patch import DerivationPatchReference, DerivationPatchSpool
from src.storage.markdown_store import Entry, MarkdownStore
from src.storage.sqlite_store import SQLiteStore
from tests.blackbox.r4_fullflow_support import (
    ProviderHarness,
    R4BlackboxRuntime,
    archive_only_provider_script,
    assert_no_public_leak,
    assert_r4_success_ledger,
    manual_embedding_rebuild_provider_script,
    parse_mcp_tool_json,
)


ARCHIVE_TITLE = "R4 P1 public MCP rebuild"
ARCHIVE_CONTENT = (
    "Juniper relays retain a public semantic generation through MCP stdio "
    "restart after a foreground rebuild."
)
SEARCH_QUERY = "public semantic generation after restart"


def _committed_r4_facts(
    runtime: R4BlackboxRuntime,
) -> tuple[tuple[str, ...], dict[str, bytes], dict[str, object]]:
    """Retain all synthetic Q2 accounting/commit and source facts across maintenance."""

    with sqlite3.connect(runtime.layout.db_path.as_uri() + "?mode=ro", uri=True) as connection:
        database = tuple(connection.iterdump())
        references = connection.execute(
            "SELECT patch_ref, patch_sha256 FROM ai_derivation_tasks "
            "WHERE patch_ref IS NOT NULL"
        ).fetchall()
    # Reopen the original spool through its strict digest/identity validator;
    # the unchanged SQLite snapshot also preserves every task/commit binding.
    patches = {
        patch_id: DerivationPatchSpool(runtime.layout).read(
            DerivationPatchReference(patch_id, payload_sha256)
        ).to_payload()
        for patch_id, payload_sha256 in references
    }
    markdown = {
        path.relative_to(runtime.layout.vault_dir).as_posix(): path.read_bytes()
        for path in runtime.layout.vault_dir.rglob("*.md")
    }
    return database, markdown, patches


async def _initialize(session: ClientSession) -> None:
    await asyncio.wait_for(session.initialize(), timeout=30)


async def _release_step(
    harness: ProviderHarness,
    runtime: R4BlackboxRuntime,
    step_id: str,
    *,
    operation: asyncio.Task[object] | None = None,
) -> None:
    waiting_for_request = asyncio.create_task(
        asyncio.to_thread(harness.await_request, step_id)
    )
    if operation is not None:
        completed, _ = await asyncio.wait(
            (waiting_for_request, operation),
            return_when=asyncio.FIRST_COMPLETED,
        )
        if operation in completed:
            if not waiting_for_request.done():
                waiting_for_request.cancel()
            result = operation.result()
            payload = parse_mcp_tool_json(result)
            issues = payload.get("issues")
            raise AssertionError(
                "Embedding rebuild returned before its expected Provider barrier: "
                f"terminal={payload.get('terminal')!r} issues={issues!r}"
            )
    request = await asyncio.wait_for(waiting_for_request, timeout=35)
    assert request["request_number"] >= 1
    await asyncio.wait_for(
        asyncio.to_thread(runtime.prove_writer_lease_is_free),
        timeout=10,
    )
    harness.continue_request(step_id)
    await asyncio.wait_for(
        asyncio.to_thread(harness.await_event, "step_completed", step_id=step_id),
        timeout=35,
    )


@pytest.mark.slow
@pytest.mark.asyncio
async def test_mcp_stdio_public_rebuild_activates_and_searches_after_new_server(
    tmp_path: Path,
) -> None:
    """MCP uses only public tools, then a wholly fresh stdio server reads it."""

    scenario_id = "mcp-r4p1-public-rebuild"
    with ProviderHarness.start(
        tmp_path / "provider",
        scenario_id=scenario_id,
        script=manual_embedding_rebuild_provider_script(scenario_id),
    ) as harness:
        runtime = R4BlackboxRuntime.create(
            tmp_path / "product",
            provider_base_url=harness.base_url,
        )
        first_stderr_path = runtime.layout.tmp_dir / "mcp-rebuild-first.stderr"
        with first_stderr_path.open("w+", encoding="utf-8") as first_stderr:
            async with stdio_client(runtime.mcp_server_params(), errlog=first_stderr) as (
                read,
                write,
            ):
                async with ClientSession(read, write) as session:
                    await _initialize(session)
                    archive_call = asyncio.create_task(
                        session.call_tool(
                            "archive_text",
                            {"text": ARCHIVE_CONTENT, "title": ARCHIVE_TITLE},
                        )
                    )
                    for step_id in (
                        "chat-1",
                        "chat-2",
                        "embedding-archive-1",
                        "embedding-archive-2",
                    ):
                        await _release_step(harness, runtime, step_id)
                    archive = parse_mcp_tool_json(
                        await asyncio.wait_for(archive_call, timeout=30)
                    )
                    assert archive["terminal"] == "success"
                    assert archive["storage_status"] == "ready"
                    knowledge_id = archive["knowledge_id"]
                    operation_id = archive["operation_id"]
                    assert type(knowledge_id) is int and knowledge_id > 0
                    assert type(operation_id) is str and len(operation_id) == 32
                    # Q2 must clean its temporary stage. Manual rebuilds retain
                    # their sealed proof under the W4 recovery contract.
                    assert_r4_success_ledger(
                        runtime,
                        operation_id=operation_id,
                        knowledge_id=knowledge_id,
                        expected_title=ARCHIVE_TITLE,
                        expected_content=ARCHIVE_CONTENT,
                    )
                    original_r4_facts = _committed_r4_facts(runtime)

                    inspected = parse_mcp_tool_json(
                        await asyncio.wait_for(
                            session.call_tool("inspect_embedding_rebuild", {}),
                            timeout=30,
                        )
                    )
                    assert inspected["status"] == "success"
                    assert inspected["terminal"] == "success"
                    assert inspected["plan"] is None
                    assert inspected["inspection"]["index"]["state"] == "ready"

                    planned = parse_mcp_tool_json(
                        await asyncio.wait_for(
                            session.call_tool(
                                "plan_embedding_rebuild",
                                {"force": True},
                            ),
                            timeout=30,
                        )
                    )
                    assert planned["status"] == "success"
                    plan = planned["plan"]
                    assert type(plan) is dict and plan["force"] is True
                    plan_id = plan["plan_id"]
                    assert type(plan_id) is str and len(plan_id) == 64
                    prior_generation = plan["inspection"]["active_generation"]
                    assert type(prior_generation) is str

                    rebuild_call = asyncio.create_task(
                        session.call_tool(
                            "execute_embedding_rebuild",
                            {
                                "plan_id": plan_id,
                                "approved": True,
                                "allow_network": True,
                                "force": True,
                            },
                        )
                    )
                    await _release_step(
                        harness,
                        runtime,
                        "embedding-rebuild-document",
                        operation=rebuild_call,
                    )
                    await _release_step(
                        harness,
                        runtime,
                        "embedding-rebuild-chunks",
                        operation=rebuild_call,
                    )
                    rebuilt = parse_mcp_tool_json(
                        await asyncio.wait_for(rebuild_call, timeout=30)
                    )
            first_stderr.flush()
            first_stderr.seek(0)
            first_stderr_text = first_stderr.read()

        assert rebuilt["terminal"] == "success"
        execution = rebuilt["execution"]
        assert type(execution) is dict
        rebuild_operation_id = execution["operation_id"]
        assert type(rebuild_operation_id) is str
        assert rebuild_operation_id.startswith("embedding-rebuild-")
        assert execution["inspection"]["state"] == "ready"
        assert execution["inspection"]["active_generation"] == execution["generation_id"]
        assert execution["inspection"]["previous_generation"] == prior_generation
        assert execution["generation_id"] != prior_generation
        assert execution["ledger"]["state"] == "completed"
        assert execution["ledger"]["reservation"]["state"] == "settled"
        assert "reservation_id" not in execution["ledger"]["reservation"]
        assert (
            execution["ledger"]["reservation"]["settled_tokens"]
            == execution["ledger"]["reservation"]["reserved_tokens"]
        )
        assert execution["ledger"]["provider_usage"]["embedding_input_tokens"] == 34
        assert_no_public_leak(
            json.dumps(rebuilt, ensure_ascii=False),
            runtime,
            ARCHIVE_TITLE,
            ARCHIVE_CONTENT,
        )
        assert_no_public_leak(
            first_stderr_text,
            runtime,
            ARCHIVE_TITLE,
            ARCHIVE_CONTENT,
            SEARCH_QUERY,
        )

        durable = EmbeddingRebuildLedger(runtime.layout).get_operation(rebuild_operation_id)
        assert durable is not None
        assert durable.state is EmbeddingRebuildLedgerState.COMPLETED
        assert durable.generation_id == execution["generation_id"]
        assert durable.reservation is not None
        assert durable.reservation.state == "settled"
        pointer = RuntimeSnapshotStore(runtime.layout).read().payload["embedding_index"]
        assert pointer["active_generation"] == execution["generation_id"]
        assert pointer["previous_generation"] == prior_generation
        assert {prior_generation, execution["generation_id"]}.issubset(
            set(pointer["retained_generations"])
        )
        assert not any(
            (runtime.layout.vector_index_dir / name).exists()
            for name in (
                "doc_vectors.idx",
                "doc_vectors_metadata.json",
                "chunk_vectors.idx",
                "chunk_vectors_metadata.json",
            )
        )
        assert _committed_r4_facts(runtime) == original_r4_facts
        vector_roots = list(runtime.layout.vector_index_dir.iterdir())
        assert {path.name for path in vector_roots} == {"generations", "staging"}
        for root in vector_roots:
            root_stat = os.lstat(root)
            assert stat.S_ISDIR(root_stat.st_mode)
            assert not stat.S_ISLNK(root_stat.st_mode)
            assert not bool(
                getattr(root_stat, "st_file_attributes", 0)
                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            )
        staging_root = runtime.layout.vector_index_dir / "staging"
        assert durable.stage_key is not None
        assert {path.name for path in staging_root.iterdir()} == {durable.stage_key}
        sealed = validate_publish_ready(
            runtime.layout,
            stage_key=durable.stage_key,
            expected={
                **{name: getattr(durable, name) for name in (
                    "operation_id", "plan_id", "public_plan_id", "source_revision",
                    "data_root_identity_sha256", "runtime_snapshot_sha256", "contract_sha256",
                    "policy_fingerprint", "grant_sha256", "proof_sha256", "generation_id",
                )},
                "reservation_id": durable.reservation.reservation_id,
            },
        )
        assert sealed.generation_id == execution["generation_id"]
        assert sealed.proof_sha256 == durable.proof_sha256
        artifact_names = {
            "doc_vectors.idx", "doc_vectors_metadata.json", "chunk_vectors.idx",
            "chunk_vectors_metadata.json", "generation-manifest.json",
        }
        # VectorStore retains its four one-byte advisory-lock sidecars in the
        # private stage; only the five immutable artifacts enter generations.
        stage_locks = {f".{name}.lock" for name in artifact_names if name != "generation-manifest.json"}
        assert {path.name for path in sealed.stage_dir.iterdir()} == artifact_names | stage_locks | {
            ".generation-stage-grant.json", ".publish-ready.json",
        }
        assert all((sealed.stage_dir / name).read_bytes() == b"\0" for name in stage_locks)
        generation = runtime.layout.vector_index_dir / "generations" / sealed.generation_id
        assert {path.name for path in generation.iterdir()} == artifact_names
        assert all((sealed.stage_dir / name).read_bytes() == (generation / name).read_bytes()
                   for name in artifact_names)

        second_stderr_path = runtime.layout.tmp_dir / "mcp-rebuild-second.stderr"
        with second_stderr_path.open("w+", encoding="utf-8") as second_stderr:
            async with stdio_client(runtime.mcp_server_params(), errlog=second_stderr) as (
                read,
                write,
            ):
                async with ClientSession(read, write) as session:
                    await _initialize(session)
                    fresh_inspected = parse_mcp_tool_json(
                        await asyncio.wait_for(
                            session.call_tool("inspect_embedding_rebuild", {}),
                            timeout=30,
                        )
                    )
                    fresh_index = fresh_inspected["inspection"]["index"]
                    assert fresh_index["state"] == "ready"
                    assert fresh_index["active_generation"] == execution["generation_id"]
                    assert fresh_index["previous_generation"] == prior_generation
                    entry = parse_mcp_tool_json(
                        await asyncio.wait_for(
                            session.call_tool(
                                "get_entry",
                                {"knowledge_id": str(knowledge_id)},
                            ),
                            timeout=30,
                        )
                    )
                    assert entry["status"] == "success"
                    assert entry["content"] == ARCHIVE_CONTENT

                    for strategy, step_id in (
                        ("vector", "embedding-query-1"),
                        ("hybrid", "embedding-query-2"),
                    ):
                        search_call = asyncio.create_task(
                            session.call_tool(
                                "search_knowledge",
                                {
                                    "query": SEARCH_QUERY,
                                    "strategy": strategy,
                                    "top_k": 5,
                                },
                            )
                        )
                        await _release_step(harness, runtime, step_id)
                        search = parse_mcp_tool_json(
                            await asyncio.wait_for(search_call, timeout=30)
                        )
                        assert search["status"] == "success"
                        assert search["strategy"] == strategy
                        assert any(
                            result["knowledge_id"] == knowledge_id
                            for result in search["results"]
                        )
                        assert_no_public_leak(json.dumps(search, ensure_ascii=False), runtime)
            second_stderr.flush()
            second_stderr.seek(0)
            second_stderr_text = second_stderr.read()

        assert_no_public_leak(
            second_stderr_text,
            runtime,
            ARCHIVE_TITLE,
            ARCHIVE_CONTENT,
            SEARCH_QUERY,
        )
        harness.assert_redacted_telemetry(
            ARCHIVE_TITLE,
            ARCHIVE_CONTENT,
            SEARCH_QUERY,
        )


@pytest.mark.slow
@pytest.mark.asyncio
async def test_mcp_reloads_current_policy_before_executing_a_submitted_plan(
    tmp_path: Path,
) -> None:
    """A running stdio server fails closed after a test-owned policy edit."""

    scenario_id = "mcp-r4p1-config-drift"
    with ProviderHarness.start(
        tmp_path / "provider",
        scenario_id=scenario_id,
        script=archive_only_provider_script(scenario_id),
    ) as harness:
        runtime = R4BlackboxRuntime.create(
            tmp_path / "product",
            provider_base_url=harness.base_url,
        )
        with (runtime.layout.tmp_dir / "mcp-config-drift.stderr").open(
            "w+",
            encoding="utf-8",
        ) as stderr:
            async with stdio_client(runtime.mcp_server_params(), errlog=stderr) as (
                read,
                write,
            ):
                async with ClientSession(read, write) as session:
                    await _initialize(session)
                    archive_call = asyncio.create_task(
                        session.call_tool(
                            "archive_text",
                            {"text": ARCHIVE_CONTENT, "title": ARCHIVE_TITLE},
                        )
                    )
                    for step_id in (
                        "chat-1",
                        "chat-2",
                        "embedding-archive-1",
                        "embedding-archive-2",
                    ):
                        await _release_step(harness, runtime, step_id)
                    archive = parse_mcp_tool_json(
                        await asyncio.wait_for(archive_call, timeout=30)
                    )
                    assert archive["terminal"] == "success"

                    planned = parse_mcp_tool_json(
                        await asyncio.wait_for(
                            session.call_tool(
                                "plan_embedding_rebuild",
                                {"force": True},
                            ),
                            timeout=30,
                        )
                    )
                    plan_id = planned["plan"]["plan_id"]
                    assert type(plan_id) is str

                    config_payload = json.loads(
                        runtime.user_config_path.read_text(encoding="utf-8")
                    )
                    config_payload["ai"]["embedding_rebuild"]["token_budget"][
                        "daily_total_tokens"
                    ] = 0
                    runtime.user_config_path.write_text(
                        json.dumps(config_payload, ensure_ascii=False, sort_keys=True),
                        encoding="utf-8",
                    )

                    stale = parse_mcp_tool_json(
                        await asyncio.wait_for(
                            session.call_tool(
                                "execute_embedding_rebuild",
                                {
                                    "plan_id": plan_id,
                                    "approved": True,
                                    "allow_network": True,
                                    "force": True,
                                },
                            ),
                            timeout=30,
                        )
                    )

        assert stale["terminal"] == "error"
        assert stale["issues"][0]["code"] == "runtime_plan_stale"
        assert EmbeddingRebuildLedger(runtime.layout).read_operations() == ()
        harness.assert_redacted_telemetry(ARCHIVE_TITLE, ARCHIVE_CONTENT)


@pytest.mark.slow
@pytest.mark.asyncio
async def test_mcp_rebuild_confirmation_is_root_bound_across_fresh_servers(
    tmp_path: Path,
) -> None:
    """ER-01/02: independent stdio servers preserve only same-root confirmation."""

    scenario_id = "mcp-w2-rebuild-root-identity"
    script = manual_embedding_rebuild_provider_script(scenario_id)
    script["steps"] = script["steps"][4:6]
    with ProviderHarness.start(
        tmp_path / "provider",
        scenario_id=scenario_id,
        script=script,
    ) as harness:
        first = R4BlackboxRuntime.create(
            tmp_path / "first-root", provider_base_url=harness.base_url
        )
        second = R4BlackboxRuntime.create(
            tmp_path / "second-root", provider_base_url=harness.base_url
        )
        assert first.config.data_root_identity != second.config.data_root_identity
        for runtime in (first, second):
            with write_lease_scope(runtime.layout):
                markdown = MarkdownStore(runtime.layout.vault_dir)
                entry = Entry(
                    title=ARCHIVE_TITLE,
                    source_type="text",
                    content=ARCHIVE_CONTENT,
                )
                write_plan = markdown.plan_save(entry)
                markdown.save_planned(write_plan, entry)
                SQLiteStore(runtime.layout.db_path).insert_entry_with_chunks(
                    entry, write_plan.relative_path, [ARCHIVE_CONTENT]
                )

        outputs: list[tuple[dict[str, object], R4BlackboxRuntime]] = []
        with (first.layout.tmp_dir / "identity-plan.stderr").open(
            "w+", encoding="utf-8"
        ) as first_stderr:
            async with stdio_client(first.mcp_server_params(), errlog=first_stderr) as (
                read, write
            ):
                async with ClientSession(read, write) as session:
                    await _initialize(session)
                    planned = parse_mcp_tool_json(
                        await asyncio.wait_for(
                            session.call_tool("plan_embedding_rebuild", {}), timeout=30
                        )
                    )
                    assert planned["terminal"] == "success"
                    first_plan = planned["plan"]
                    plan_id = first_plan["plan_id"]
                    assert type(plan_id) is str and len(plan_id) == 64
                    outputs.append((planned, first))

        with (second.layout.tmp_dir / "identity-wrong-root.stderr").open(
            "w+", encoding="utf-8"
        ) as second_stderr:
            async with stdio_client(second.mcp_server_params(), errlog=second_stderr) as (
                read, write
            ):
                async with ClientSession(read, write) as session:
                    await _initialize(session)
                    other_planned = parse_mcp_tool_json(
                        await asyncio.wait_for(
                            session.call_tool("plan_embedding_rebuild", {}), timeout=30
                        )
                    )
                    assert other_planned["terminal"] == "success"
                    second_plan = other_planned["plan"]
                    for field in (
                        "contract", "source", "active_generation", "active_manifest_sha256"
                    ):
                        assert second_plan["inspection"][field] == first_plan["inspection"][field]
                    assert second_plan["plan_id"] != plan_id
                    second_snapshot = RuntimeSnapshotStore(second.layout).read()
                    wrong_root = parse_mcp_tool_json(
                        await asyncio.wait_for(
                            session.call_tool(
                                "execute_embedding_rebuild",
                                {"plan_id": plan_id, "approved": True, "allow_network": True},
                            ),
                            timeout=30,
                        )
                    )
                    assert wrong_root["terminal"] == "error"
                    assert wrong_root["issues"][0]["code"] == "runtime_plan_stale"
                    assert RuntimeSnapshotStore(second.layout).read() == second_snapshot
                    assert EmbeddingRebuildLedger(second.layout).read_operations() == ()
                    assert not (second.layout.vector_index_dir / "staging").exists()
                    assert not (second.layout.vector_index_dir / "generations").exists()
                    outputs.extend(((other_planned, second), (wrong_root, second)))

        with (first.layout.tmp_dir / "identity-fresh-server.stderr").open(
            "w+", encoding="utf-8"
        ) as fresh_stderr:
            async with stdio_client(first.mcp_server_params(), errlog=fresh_stderr) as (
                read, write
            ):
                async with ClientSession(read, write) as session:
                    await _initialize(session)
                    fresh_planned = parse_mcp_tool_json(
                        await asyncio.wait_for(
                            session.call_tool("plan_embedding_rebuild", {}), timeout=30
                        )
                    )
                    assert fresh_planned["terminal"] == "success"
                    assert fresh_planned["plan"]["plan_id"] == plan_id
                    rebuild_call = asyncio.create_task(
                        session.call_tool(
                            "execute_embedding_rebuild",
                            {"plan_id": plan_id, "approved": True, "allow_network": True},
                        )
                    )
                    await _release_step(
                        harness, first, "embedding-rebuild-document", operation=rebuild_call
                    )
                    await _release_step(
                        harness, first, "embedding-rebuild-chunks", operation=rebuild_call
                    )
                    rebuilt = parse_mcp_tool_json(
                        await asyncio.wait_for(rebuild_call, timeout=30)
                    )
                    assert rebuilt["terminal"] == "success"
                    assert rebuilt["execution"]["inspection"]["state"] == "ready"
                    assert rebuilt["execution"]["ledger"]["state"] == "completed"
                    outputs.extend(((fresh_planned, first), (rebuilt, first)))

        assert len(EmbeddingRebuildLedger(first.layout).read_operations()) == 1
        for payload, runtime in outputs:
            assert_no_public_leak(
                json.dumps(payload, ensure_ascii=False), runtime, ARCHIVE_TITLE, ARCHIVE_CONTENT
            )
        harness.assert_redacted_telemetry(ARCHIVE_TITLE, ARCHIVE_CONTENT)
