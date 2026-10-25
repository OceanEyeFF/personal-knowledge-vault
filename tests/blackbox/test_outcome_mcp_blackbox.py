"""P1-B real MCP stdio blackbox across fresh server processes."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
from mcp.client.session import ClientSession
from mcp.client.stdio import stdio_client

from mcp import StdioServerParameters
from src.storage.coordinator import StorageCoordinator
from src.storage.markdown_store import Entry, MarkdownStore
from src.storage.sqlite_store import SQLiteStore
from tests.offline_runtime import prepare_offline_child_env

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _seed_source(data_root: Path) -> int:
    db_path = data_root / "db" / "knowledge_vault.db"
    sqlite = SQLiteStore(db_path)
    sqlite.initialize()
    archived = StorageCoordinator(
        MarkdownStore(data_root / "vault"),
        sqlite,
        data_root / "runtime" / "operations",
    ).archive(
        Entry(
            title="MCP outcome source",
            source_type="text",
            content="mcp-source-body-must-not-leak",
            tags=["outcome"],
            keywords=["traceability"],
        ),
        chunks=["mcp-source-body-must-not-leak"],
    )
    assert archived.knowledge_id is not None
    return archived.knowledge_id


def _server_parameters(data_root: Path) -> StdioServerParameters:
    env = prepare_offline_child_env(
        project_root=PROJECT_ROOT,
        runtime_overrides={
            "DATA_DIR": data_root,
            "DB_PATH": data_root / "db" / "knowledge_vault.db",
            "VAULT_DIR": data_root / "vault",
            "VECTOR_DIR": data_root / "vectors",
            "LOG_DIR": data_root / "logs",
            "TMP_DIR": data_root / "tmp",
            "LOG_LEVEL": "WARNING",
        },
    )
    env["PKV_TEST_SYNTHETIC_RUNTIME_READY"] = "1"
    return StdioServerParameters(
        command=sys.executable,
        args=[str(PROJECT_ROOT / "tests" / "offline_entrypoint.py"), "mcp"],
        env=env,
        cwd=str(PROJECT_ROOT),
    )


def _source_markdown_path(data_root: Path, knowledge_id: int) -> Path:
    row = SQLiteStore(data_root / "db" / "knowledge_vault.db").query_by_id(knowledge_id)
    assert isinstance(row, dict)
    relative_path = row["file_path"]
    assert isinstance(relative_path, str)
    path = data_root / "vault" / relative_path
    assert path.is_file()
    return path


def _payload(result: Any) -> dict[str, object]:
    assert not getattr(result, "isError", False)
    content = getattr(result, "content", None)
    assert isinstance(content, list) and len(content) == 1
    text = getattr(content[0], "text", None)
    assert isinstance(text, str)
    parsed = json.loads(text)
    assert isinstance(parsed, dict)
    assert parsed["status"] == "success", parsed
    return parsed


def _failure_payload(result: Any) -> dict[str, object]:
    assert not getattr(result, "isError", False)
    content = getattr(result, "content", None)
    assert isinstance(content, list) and len(content) == 1
    text = getattr(content[0], "text", None)
    assert isinstance(text, str)
    parsed = json.loads(text)
    assert isinstance(parsed, dict)
    assert parsed["status"] == "error", parsed
    return parsed


@pytest.mark.asyncio
async def test_outcome_mcp_stdio_roundtrip_survives_server_restart(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "outcome-mcp"
    knowledge_id = _seed_source(data_root)
    params = _server_parameters(data_root)

    async with (
        stdio_client(params) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        tools = await session.list_tools()
        tool_map = {tool.name: tool for tool in tools.tools}
        assert {
            "create_outcome_topic",
            "get_outcome",
            "suggest_outcome_evidence",
            "attach_outcome_evidence",
            "detach_outcome_evidence",
            "create_outcome_claim",
            "revise_outcome_claim",
            "link_outcome_claim_evidence",
            "unlink_outcome_claim_evidence",
            "render_outcome_research_brief",
            "approve_outcome_research_brief",
            "return_outcome_research_brief",
        }.issubset(tool_map)
        assert tool_map["get_outcome"].annotations.readOnlyHint is True
        assert tool_map["suggest_outcome_evidence"].annotations.readOnlyHint is True

        topic_payload = _payload(
            await session.call_tool(
                "create_outcome_topic",
                {
                    "title": "MCP public outcome",
                    "question": "Can the selected evidence support this claim?",
                },
            )
        )
        topic = topic_payload["result"]
        assert isinstance(topic, dict)
        topic_id = topic["topic_id"]
        assert isinstance(topic_id, str)

        secret_locator = "https://reader:token-do-not-echo@example.invalid/source"
        rejected = _failure_payload(
            await session.call_tool(
                "attach_outcome_evidence",
                {"topic_id": topic_id, "locator": secret_locator},
            )
        )
        assert rejected["issues"][0]["code"] == "outcome_invalid"
        assert "token-do-not-echo" not in json.dumps(rejected, ensure_ascii=False)
        assert secret_locator not in json.dumps(rejected, ensure_ascii=False)

        evidence_payload = _payload(
            await session.call_tool(
                "attach_outcome_evidence",
                {
                    "topic_id": topic_id,
                    "locator": f"pkv://entries/{knowledge_id}",
                },
            )
        )
        evidence = evidence_payload["result"]
        assert isinstance(evidence, dict)
        evidence_id = evidence["evidence_id"]
        assert isinstance(evidence_id, str)

        credential_claim = "https://reader:token-do-not-echo@example.invalid/claim"
        rejected_claim = _failure_payload(
            await session.call_tool(
                "create_outcome_claim",
                {
                    "topic_id": topic_id,
                    "statement": credential_claim,
                },
            )
        )
        assert rejected_claim["issues"][0]["code"] == "outcome_invalid"
        assert "token-do-not-echo" not in json.dumps(
            rejected_claim,
            ensure_ascii=False,
        )
        assert credential_claim not in json.dumps(rejected_claim, ensure_ascii=False)

        claim_payload = _payload(
            await session.call_tool(
                "create_outcome_claim",
                {
                    "topic_id": topic_id,
                    "statement": "The selected source supports the scoped claim.",
                    "state": "supported",
                },
            )
        )
        claim = claim_payload["result"]
        assert isinstance(claim, dict)
        claim_id = claim["claim_id"]
        assert isinstance(claim_id, str)

        _payload(
            await session.call_tool(
                "link_outcome_claim_evidence",
                {
                    "claim_id": claim_id,
                    "evidence_id": evidence_id,
                    "polarity": "support",
                },
            )
        )
        brief_payload = _payload(
            await session.call_tool(
                "render_outcome_research_brief",
                {"topic_id": topic_id},
            )
        )
        brief = brief_payload["result"]
        assert isinstance(brief, dict)
        assert brief["state"] == "awaiting_review"
        assert "mcp-source-body-must-not-leak" not in json.dumps(
            brief_payload,
            ensure_ascii=False,
        )
        _payload(
            await session.call_tool(
                "approve_outcome_research_brief",
                {"topic_id": topic_id},
            )
        )

    # The first real stdio process is closed.  A fresh server process must read
    # only durable SQLite/Markdown state through the public MCP tool.
    async with (
        stdio_client(params) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        observed_payload = _payload(
            await session.call_tool("get_outcome", {"topic_id": topic_id})
        )

    observed = observed_payload["result"]
    assert isinstance(observed, dict)
    assert observed["brief"]["effective_state"] == "approved"
    assert observed["brief"]["projection_state"] == "current"
    assert observed["evidence"][0]["locator"] == f"pkv://entries/{knowledge_id}"
    assert str(data_root) not in json.dumps(observed_payload, ensure_ascii=False)

    source_path = _source_markdown_path(data_root, knowledge_id)
    original = source_path.read_text(encoding="utf-8")
    # Model a test-owned user/Git Markdown edit after the approved server has
    # exited.  The third server must discover the durable source mismatch only
    # through the published MCP stdio boundary.
    source_path.write_text(
        original.replace(
            "mcp-source-body-must-not-leak",
            "mcp-markdown-source-changed-after-capture",
        ),
        encoding="utf-8",
    )

    async with (
        stdio_client(params) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        drifted_payload = _payload(
            await session.call_tool("get_outcome", {"topic_id": topic_id})
        )
        rejected = _failure_payload(
            await session.call_tool(
                "render_outcome_research_brief",
                {"topic_id": topic_id},
            )
        )

    drifted = drifted_payload["result"]
    assert isinstance(drifted, dict)
    assert drifted["evidence"][0]["recorded_state"] == "stale"
    assert drifted["claims"][0]["state"] == "insufficient"
    assert drifted["brief"]["projection_state"] == "current"
    assert drifted["brief"]["effective_state"] == "needs_review"
    assert rejected["issues"][0]["code"] == "outcome_evidence_stale"
    assert "mcp-markdown-source-changed-after-capture" not in json.dumps(
        drifted_payload,
        ensure_ascii=False,
    )

    # A deleted projection is checked by a fourth real stdio process.  It may
    # report the precise recoverable absence, while an unsafe/unreadable path
    # remains a public error rather than this successful state.
    relative_path = brief["markdown_relative_path"]
    assert isinstance(relative_path, str)
    (data_root / "outcomes" / relative_path).unlink()
    async with (
        stdio_client(params) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        missing_payload = _payload(
            await session.call_tool("get_outcome", {"topic_id": topic_id})
        )

    missing = missing_payload["result"]
    assert isinstance(missing, dict)
    assert missing["brief"]["projection_state"] == "unavailable"
    assert missing["brief"]["effective_state"] == "needs_review"
