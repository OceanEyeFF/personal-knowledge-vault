"""True MCP stdio process-boundary R4 source acceptance."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import stdio_client
import pytest

from tests.blackbox.r4_fullflow_support import (
    ProviderHarness,
    R4BlackboxRuntime,
    assert_no_public_leak,
    assert_r4_success_ledger,
    parse_mcp_tool_json,
)


ARCHIVE_TITLE = "R4 MCP deterministic semantic archive"
ARCHIVE_CONTENT = (
    "Copper lanterns retain semantic evidence through a complete MCP stdio "
    "server restart. This text belongs only to an isolated black-box root."
)
SEARCH_QUERY = "semantic evidence retained after server restart"


async def _release_provider_step(
    harness: ProviderHarness,
    runtime: R4BlackboxRuntime,
    step_id: str,
) -> None:
    event = await asyncio.wait_for(
        asyncio.to_thread(harness.await_request, step_id),
        timeout=35,
    )
    assert event["request_number"] >= 1
    assert event["item_count"] == 1
    assert isinstance(event["content_sha256"], str)
    assert len(event["content_sha256"]) == 64
    await asyncio.wait_for(
        asyncio.to_thread(runtime.prove_writer_lease_is_free),
        timeout=10,
    )
    harness.continue_request(step_id)
    await asyncio.wait_for(
        asyncio.to_thread(
            harness.await_event,
            "step_completed",
            step_id=step_id,
        ),
        timeout=35,
    )


async def _initialize(session: ClientSession) -> None:
    await asyncio.wait_for(session.initialize(), timeout=30)


@pytest.mark.slow
@pytest.mark.asyncio
async def test_mcp_stdio_archive_restart_vector_and_hybrid_fullflow(
    tmp_path: Path,
) -> None:
    """Archive through stdio, stop it, then retrieve through a fresh server."""

    with ProviderHarness.start(
        tmp_path / "provider",
        scenario_id="mcp-r4-success",
    ) as harness:
        runtime = R4BlackboxRuntime.create(
            tmp_path / "product",
            provider_base_url=harness.base_url,
        )

        first_stderr_path = runtime.layout.tmp_dir / "mcp-first.stderr"
        with first_stderr_path.open("w+", encoding="utf-8") as first_server_stderr:
            async with stdio_client(
                runtime.mcp_server_params(), errlog=first_server_stderr
            ) as (read, write):
                async with ClientSession(read, write) as first_session:
                    await _initialize(first_session)
                    archive_call = asyncio.create_task(
                        first_session.call_tool(
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
                        await _release_provider_step(harness, runtime, step_id)
                    archive_result = await asyncio.wait_for(archive_call, timeout=30)
            first_server_stderr.flush()
            first_server_stderr.seek(0)
            first_server_stderr_text = first_server_stderr.read()
        assert_no_public_leak(
            first_server_stderr_text,
            runtime,
            ARCHIVE_TITLE,
            ARCHIVE_CONTENT,
            SEARCH_QUERY,
        )

        archive_payload = parse_mcp_tool_json(archive_result)
        assert archive_payload["success"] is True
        assert archive_payload["terminal"] == "success"
        assert archive_payload["storage_status"] == "ready"
        assert archive_payload["core_committed"] is True
        assert archive_payload["do_not_retry"] is True
        assert archive_payload["warnings"] == []
        assert archive_payload["issues"] == []
        assert archive_payload["title"] == ARCHIVE_TITLE
        assert archive_payload["tags"] == ["r4", "blackbox", "semantic"]
        operation_id = archive_payload["operation_id"]
        assert isinstance(operation_id, str)
        assert len(operation_id) == 32
        assert all(character in "0123456789abcdef" for character in operation_id)
        assert type(archive_payload["knowledge_id"]) is int
        knowledge_id = archive_payload["knowledge_id"]
        assert knowledge_id > 0
        assert archive_payload["entry_locator"] == f"pkv://entries/{knowledge_id}"
        archive_json = json.dumps(archive_payload, ensure_ascii=False)
        assert_no_public_leak(archive_json, runtime, ARCHIVE_CONTENT)

        durable = assert_r4_success_ledger(
            runtime,
            operation_id=operation_id,
            knowledge_id=knowledge_id,
            expected_title=ARCHIVE_TITLE,
            expected_content=ARCHIVE_CONTENT,
        )

        second_stderr_path = runtime.layout.tmp_dir / "mcp-second.stderr"
        with second_stderr_path.open("w+", encoding="utf-8") as second_server_stderr:
            async with stdio_client(
                runtime.mcp_server_params(), errlog=second_server_stderr
            ) as (read, write):
                async with ClientSession(read, write) as second_session:
                    await _initialize(second_session)
                    entry_result = await asyncio.wait_for(
                        second_session.call_tool(
                            "get_entry",
                            {"knowledge_id": str(knowledge_id)},
                        ),
                        timeout=30,
                    )
                    entry = parse_mcp_tool_json(entry_result)
                    assert entry["status"] == "success"
                    assert entry["issues"] == []
                    assert entry["knowledge_id"] == knowledge_id
                    assert entry["title"] == ARCHIVE_TITLE
                    assert entry["content"] == ARCHIVE_CONTENT
                    assert entry["summary_100_words"] == '["r4","blackbox","semantic"]'
                    assert entry["tags"] == ["r4", "blackbox", "semantic"]
                    assert_no_public_leak(
                        json.dumps(entry, ensure_ascii=False),
                        runtime,
                    )

                    vector_call = asyncio.create_task(
                        second_session.call_tool(
                            "search_knowledge",
                            {
                                "query": SEARCH_QUERY,
                                "strategy": "vector",
                                "top_k": 5,
                            },
                        )
                    )
                    await _release_provider_step(harness, runtime, "embedding-query-1")
                    vector_result = await asyncio.wait_for(vector_call, timeout=30)
                    vector = parse_mcp_tool_json(vector_result)
                    assert vector["status"] == "success"
                    assert vector["strategy"] == "vector"
                    assert vector["issues"] == []
                    assert any(
                        result["knowledge_id"] == knowledge_id
                        for result in vector["results"]
                    )

                    hybrid_call = asyncio.create_task(
                        second_session.call_tool(
                            "search_knowledge",
                            {
                                "query": SEARCH_QUERY,
                                "strategy": "hybrid",
                                "top_k": 5,
                            },
                        )
                    )
                    await _release_provider_step(harness, runtime, "embedding-query-2")
                    hybrid_result = await asyncio.wait_for(hybrid_call, timeout=30)
                    hybrid = parse_mcp_tool_json(hybrid_result)
                    assert hybrid["status"] == "success"
                    assert hybrid["strategy"] == "hybrid"
                    assert hybrid["issues"] == []
                    assert any(
                        result["knowledge_id"] == knowledge_id
                        for result in hybrid["results"]
                    )
            second_server_stderr.flush()
            second_server_stderr.seek(0)
            second_server_stderr_text = second_server_stderr.read()

        assert_no_public_leak(
            second_server_stderr_text,
            runtime,
            ARCHIVE_TITLE,
            ARCHIVE_CONTENT,
            SEARCH_QUERY,
        )

        assert_no_public_leak(json.dumps(vector, ensure_ascii=False), runtime)
        assert_no_public_leak(json.dumps(hybrid, ensure_ascii=False), runtime)
        assert_r4_success_ledger(
            runtime,
            operation_id=operation_id,
            knowledge_id=knowledge_id,
            expected_title=ARCHIVE_TITLE,
            expected_content=ARCHIVE_CONTENT,
        ) == durable
        harness.assert_redacted_telemetry(
            ARCHIVE_TITLE,
            ARCHIVE_CONTENT,
            SEARCH_QUERY,
        )
