"""W3/OC-10a: exact Outcome reviews through real MCP stdio sessions."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from mcp import StdioServerParameters
from mcp.client.session import ClientSession
from mcp.client.stdio import stdio_client

from src.storage.coordinator import StorageCoordinator
from src.storage.markdown_store import Entry, MarkdownStore
from src.storage.sqlite_store import SQLiteStore
from tests.offline_runtime import prepare_offline_child_env


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SOURCE_BODY = "w3-mcp-private-source-body"
V2_TOOLS = {
    "get_outcome_v2": True,
    "get_outcome_research_brief_v2": True,
    "render_outcome_research_brief_v2": False,
    "approve_outcome_research_brief_v2": False,
    "return_outcome_research_brief_v2": False,
}


def _seed_source(data_root: Path) -> int:
    store = SQLiteStore(data_root / "db" / "knowledge_vault.db")
    store.initialize()
    archived = StorageCoordinator(
        MarkdownStore(data_root / "vault"), store, data_root / "runtime" / "operations"
    ).archive(
        Entry(title="W3 MCP source", source_type="text", content=SOURCE_BODY),
        chunks=[SOURCE_BODY],
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


def _payload(result: Any) -> dict[str, object]:
    assert not getattr(result, "isError", False), result
    content = getattr(result, "content", None)
    assert isinstance(content, list) and len(content) == 1
    text = getattr(content[0], "text", None)
    assert isinstance(text, str)
    payload = json.loads(text)
    assert isinstance(payload, dict)
    return payload


async def _v1_call(session: ClientSession, name: str, arguments: dict[str, object]) -> dict[str, object]:
    payload = _payload(await session.call_tool(name, arguments))
    assert payload["status"] == "success", payload
    assert isinstance(payload["result"], dict)
    return payload


async def _v2_call(
    session: ClientSession, name: str, arguments: dict[str, object], *, error: str | None = None
) -> dict[str, object]:
    payload = _payload(await session.call_tool(name, arguments))
    assert set(payload) == {"schema", "status", "terminal", "operation", "result", "issues"}
    assert payload["schema"] == "pkv.lifecycle-response.v2"
    assert payload["operation"] == name
    assert payload["status"] == payload["terminal"] == ("error" if error else "success")
    assert isinstance(payload["issues"], list)
    if error:
        assert payload["result"] is None
        assert payload["issues"][0]["code"] == error, payload
    else:
        assert payload["issues"] == []
    assert SOURCE_BODY not in json.dumps(payload)
    return payload


def _tree_fingerprint(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): (
            "directory" if path.is_dir() else hashlib.sha256(path.read_bytes()).hexdigest()
        )
        for path in sorted(root.rglob("*"))
    }


@pytest.mark.asyncio
async def test_outcome_mcp_v2_exact_review_replay_restart_and_v1_deprecation(tmp_path: Path) -> None:
    """OC-10a: a fresh server observes review receipts and the exact revision."""

    data_root = tmp_path / "w3-outcome-mcp"
    knowledge_id = _seed_source(data_root)
    params = _server_parameters(data_root)
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        tools = {tool.name: tool for tool in (await session.list_tools()).tools}
        assert set(V2_TOOLS).issubset(tools)
        for name, read_only in V2_TOOLS.items():
            assert tools[name].annotations.readOnlyHint is read_only
            assert tools[name].inputSchema["additionalProperties"] is False

        topic = (await _v1_call(session, "create_outcome_topic", {
            "title": "W3 MCP review", "question": "Does this exact revision retain its approval?",
        }))["result"]
        topic_id = topic["topic_id"]
        evidence = (await _v1_call(session, "attach_outcome_evidence", {
            "topic_id": topic_id, "locator": f"pkv://entries/{knowledge_id}",
        }))["result"]
        claim = (await _v1_call(session, "create_outcome_claim", {
            "topic_id": topic_id, "statement": "The source supports the first claim.", "state": "supported",
        }))["result"]
        await _v1_call(session, "link_outcome_claim_evidence", {
            "claim_id": claim["claim_id"], "evidence_id": evidence["evidence_id"], "polarity": "support",
        })
        bundle = (await _v2_call(session, "get_outcome_v2", {"topic_id": topic_id}))["result"]
        assert bundle["schema"] == "pkv.outcome.bundle.v2"
        assert bundle["brief"] is None
        assert (await _v2_call(session, "get_outcome_research_brief_v2", {"topic_id": topic_id}))["result"] is None

        first = (await _v2_call(session, "render_outcome_research_brief_v2", {"topic_id": topic_id}))["result"]
        assert first["schema"] == "pkv.outcome.brief.v2"
        assert first["binding_state"] == "verified"
        old_confirmation = first["review_confirmation"]
        assert isinstance(old_confirmation, dict)
        await _v1_call(session, "revise_outcome_claim", {
            "claim_id": claim["claim_id"], "statement": "The source supports the revised claim.",
        })
        second = (await _v2_call(session, "render_outcome_research_brief_v2", {"topic_id": topic_id}))["result"]
        assert second["revision"] > first["revision"]
        assert second["current_input_revision"] > first["current_input_revision"]
        assert second["outcome_id"] == first["outcome_id"]
        confirmation = second["review_confirmation"]
        assert isinstance(confirmation, dict)
        assert set(confirmation) == {
            "outcome_id", "expected_revision", "content_sha256", "expected_input_revision"
        }
        await _v2_call(session, "approve_outcome_research_brief_v2", old_confirmation, error="outcome_state_conflict")
        unchanged = (await _v2_call(session, "get_outcome_research_brief_v2", {
            "outcome_id": second["outcome_id"],
        }))["result"]
        assert unchanged["revision"] == second["revision"]
        assert unchanged["state"] == "awaiting_review"
        approved = (await _v2_call(session, "approve_outcome_research_brief_v2", confirmation))["result"]
        assert approved["schema"] == "pkv.outcome.review-result.v2"
        assert approved["idempotent"] is False

    # Restart between approval and replay: no in-memory receipt can satisfy it.
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        replayed = (await _v2_call(session, "approve_outcome_research_brief_v2", confirmation))["result"]
        assert replayed["idempotent"] is True
        assert replayed["review_receipt_id"] == approved["review_receipt_id"]
        assert replayed["brief"]["state"] == "approved"
        returned_args = {**confirmation, "reason_code": "needs_revision"}
        returned = (await _v2_call(session, "return_outcome_research_brief_v2", returned_args))["result"]
        returned_replay = (await _v2_call(session, "return_outcome_research_brief_v2", returned_args))["result"]
        assert returned["idempotent"] is False
        assert returned_replay["idempotent"] is True
        assert returned["review_receipt_id"] == returned_replay["review_receipt_id"]
        assert returned["review_receipt_id"] != approved["review_receipt_id"]
        await _v2_call(session, "approve_outcome_research_brief_v2", confirmation, error="outcome_state_conflict")
        await _v2_call(session, "return_outcome_research_brief_v2", {
            **confirmation, "reason_code": "other",
        }, error="outcome_state_conflict")

    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        final = await _v2_call(session, "get_outcome_v2", {"topic_id": topic_id})
        assert final["result"]["brief"]["state"] == "returned"
        assert final["result"]["brief"]["revision"] == second["revision"]
        assert str(data_root) not in json.dumps(final)
        await _v2_call(session, "render_outcome_research_brief_v2", {"topic_id": topic_id})
        for name, replacement, expected_state in (
            ("approve_outcome_research_brief", "approve_outcome_research_brief_v2", "approved"),
            ("return_outcome_research_brief", "return_outcome_research_brief_v2", "returned"),
        ):
            legacy = await _v1_call(session, name, {"topic_id": topic_id})
            assert legacy["result"]["state"] == expected_state
            assert legacy["deprecation"] == {"replacement": replacement, "removal_api_major": 2}


@pytest.mark.asyncio
async def test_outcome_mcp_v2_rejects_coercion_missing_and_extra_fields_without_writes(tmp_path: Path) -> None:
    """Frozen scalar/object shapes are checked before SDK execution."""

    data_root = tmp_path / "w3-outcome-mcp-invalid"
    _seed_source(data_root)
    params = _server_parameters(data_root)
    confirmation = {
        "outcome_id": "outcome-test", "expected_revision": 1,
        "content_sha256": "a" * 64, "expected_input_revision": 1,
    }
    invalid_calls = [
        ("get_outcome_v2", {"topic_id": 1}),
        ("get_outcome_v2", {"topic_id": None}),
        ("get_outcome_v2", {}),
        ("get_outcome_research_brief_v2", {}),
        ("get_outcome_research_brief_v2", {"topic_id": "topic-test", "outcome_id": "outcome-test"}),
        ("get_outcome_research_brief_v2", {"topic_id": True}),
        ("render_outcome_research_brief_v2", {"topic_id": []}),
        ("approve_outcome_research_brief_v2", {**confirmation, "expected_revision": True}),
        ("approve_outcome_research_brief_v2", {**confirmation, "expected_revision": "1"}),
        ("approve_outcome_research_brief_v2", {**confirmation, "expected_revision": 0}),
        ("approve_outcome_research_brief_v2", {**confirmation, "expected_revision": 2**63}),
        ("approve_outcome_research_brief_v2", {**confirmation, "expected_input_revision": 2**63}),
        ("approve_outcome_research_brief_v2", {**confirmation, "expected_input_revision": 1.0}),
        ("approve_outcome_research_brief_v2", {**confirmation, "content_sha256": "A" * 64}),
        ("return_outcome_research_brief_v2", confirmation),
        ("return_outcome_research_brief_v2", {**confirmation, "reason_code": True}),
        ("return_outcome_research_brief_v2", {**confirmation, "reason_code": "free text"}),
    ]
    for name in V2_TOOLS:
        shaped = {"topic_id": "topic-test"} if name in {
            "get_outcome_v2", "get_outcome_research_brief_v2", "render_outcome_research_brief_v2"
        } else dict(confirmation)
        if name == "return_outcome_research_brief_v2":
            shaped["reason_code"] = "other"
        invalid_calls.append((name, {**shaped, "unexpected": "private-extra-canary"}))

    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        before = _tree_fingerprint(data_root)
        # "null" is a legal opaque ID string, not JSON null: FastMCP must not
        # pre-decode optional string fields and turn this into a missing selector.
        literal_null = await _v2_call(
            session, "get_outcome_research_brief_v2", {"topic_id": "null"},
        )
        assert literal_null["result"] is None
        assert _tree_fingerprint(data_root) == before
        for name, arguments in invalid_calls:
            payload = await _v2_call(session, name, arguments, error="outcome_invalid")
            assert "private-extra-canary" not in json.dumps(payload)
            assert str(data_root) not in json.dumps(payload)
            assert _tree_fingerprint(data_root) == before, (name, arguments)
