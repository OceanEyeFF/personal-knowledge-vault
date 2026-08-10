"""Public MCP sinks must share the citation URL credential sanitizer."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from src.mcp import resources, server
from src.mcp.tools import collect_evidence, get_entry, query_subgraph, timeline_of
from src.relations.citations import build_entry_locator
from src.relations.models import (
    CollectedEvidenceItem,
    CollectedEvidenceResult,
    RelationRecord,
    RelationSourceType,
    RelationSubgraphNode,
    RelationSubgraphResult,
    RelationType,
    TimelinePoint,
    TimelineResult,
)


SECRET_URL = (
    "https://url-user:URL-PASSWORD@example.com/private"
    ";To%4Ben=MATRIX-SECRET?safe=visible&%61PI%5FKey=QUERY-SECRET"
    "#cookie=FRAGMENT-SECRET"
)
PUBLIC_URL = (
    "https://example.com/private;To%4Ben=redacted"
    "?safe=visible&%61PI%5FKey=redacted"
)
URL_SECRETS = (
    "url-user",
    "URL-PASSWORD",
    "MATRIX-SECRET",
    "QUERY-SECRET",
    "FRAGMENT-SECRET",
)


def _assert_public_payload(payload: object) -> None:
    rendered = repr(payload)
    for secret in URL_SECRETS:
        assert secret not in rendered


def _parse_fastmcp_tool_result(result):
    if isinstance(result, dict):
        return result
    assert isinstance(result, (list, tuple)) and len(result) == 1
    return json.loads(result[0].text)


@pytest.mark.asyncio
async def test_get_entry_sanitizes_public_source_url() -> None:
    store = MagicMock()
    store.query_by_id.return_value = {
        "knowledge_id": 7,
        "title": "fixture",
        "summary_one_sentence": None,
        "summary_100_words": None,
        "tags": None,
        "keywords": None,
        "source_type": "generic",
        "source_url": SECRET_URL,
        "archived_at": "2026-08-07",
        "word_count": 1,
        "file_path": "",
    }

    with patch("src.mcp.tools.get_sqlite_store", return_value=store):
        result = await get_entry(knowledge_id="7")

    assert result["source_url"] == PUBLIC_URL
    _assert_public_payload(result)


def test_json_resource_sanitizes_public_source_url() -> None:
    rendered = resources._json_resource(  # type: ignore[attr-defined]
        {"knowledge_id": 7, "source_url": SECRET_URL}
    )
    result = json.loads(rendered)

    assert result["source_url"] == PUBLIC_URL
    _assert_public_payload(result)


def test_json_resource_drops_encoded_fragment_credential() -> None:
    secret = "RESOURCE-ENCODED-FRAGMENT-SECRET"
    rendered = resources._json_resource(  # type: ignore[attr-defined]
        {
            "knowledge_id": 7,
            "source_url": (
                "https://example.com/a?safe=visible%2523"
                f"To%254Ben%253D{secret}"
            ),
        }
    )
    result = json.loads(rendered)

    assert result["source_url"] == "https://example.com/a"
    assert secret not in rendered


def test_json_resource_rejects_malformed_bracket_url_without_raising() -> None:
    secret = "RESOURCE-BRACKET-SECRET"
    rendered = resources._json_resource(  # type: ignore[attr-defined]
        {
            "knowledge_id": 7,
            "source_url": f"https://[invalid/path?token={secret}",
            "citation_source": f"https://[invalid/path?token={secret}",
        }
    )
    result = json.loads(rendered)

    assert result["source_url"] == ""
    assert result["citation_source"] == "pkv://entries/7"
    assert secret not in rendered


def test_json_resource_drops_nested_encoded_url_userinfo() -> None:
    rendered = resources._json_resource(  # type: ignore[attr-defined]
        {
            "knowledge_id": 7,
            "source_url": (
                "https://outer.example/r?next=https%253A%252F%252F"
                "NESTED-USER%253ANESTED-PASSWORD%2540inner.example%252Fa"
            ),
        }
    )
    result = json.loads(rendered)

    assert result["source_url"] == "https://outer.example/r"
    assert "NESTED-USER" not in rendered
    assert "NESTED-PASSWORD" not in rendered


@pytest.mark.asyncio
async def test_relation_tool_sanitizes_nested_source_urls() -> None:
    service = MagicMock()
    record = RelationRecord(
        source_knowledge_id=7,
        target_knowledge_id=8,
        relation_type=RelationType.REFERENCES,
        relation_source_type=RelationSourceType.MANUAL,
        evidence_payload={
            "source_url": SECRET_URL,
            "source": SECRET_URL,
            "security_fixture": {
                "raw_target": r"C:\private\relation.md",
                "source_url": SECRET_URL,
            },
        },
    )
    domain_result = RelationSubgraphResult(
        seed_knowledge_id=7,
        max_depth=2,
        nodes=[
            RelationSubgraphNode(knowledge_id=7, depth=0),
            RelationSubgraphNode(knowledge_id=8, depth=1),
        ],
        edges=[record],
        grouped_edges={RelationType.REFERENCES.value: [record]},
    )
    service.query_subgraph.return_value = domain_result

    with patch("src.mcp.tools.get_relation_query_service", return_value=service):
        direct = await query_subgraph(knowledge_id="7")
        fastmcp = _parse_fastmcp_tool_result(
            await server.mcp.call_tool("query_subgraph", {"knowledge_id": "7"})
        )

    for result in (direct, fastmcp):
        evidence = result["edges"][0]["evidence_payload"]
        assert evidence["source_url"] == PUBLIC_URL
        assert evidence["source"] == PUBLIC_URL
        assert evidence["security_fixture"] == {
            "raw_target": "[redacted-local-reference]",
            "source_url": PUBLIC_URL,
        }
        _assert_public_payload(result)


@pytest.mark.asyncio
async def test_relation_tool_rejects_credential_url_in_raw_target_dual_channel() -> None:
    service = MagicMock()
    record = RelationRecord(
        source_knowledge_id=7,
        target_knowledge_id=8,
        relation_type=RelationType.REFERENCES,
        relation_source_type=RelationSourceType.MANUAL,
        evidence_payload={"raw_target": SECRET_URL},
    )
    service.query_subgraph.return_value = RelationSubgraphResult(
        seed_knowledge_id=7,
        max_depth=2,
        nodes=[
            RelationSubgraphNode(knowledge_id=7, depth=0),
            RelationSubgraphNode(knowledge_id=8, depth=1),
        ],
        edges=[record],
        grouped_edges={RelationType.REFERENCES.value: [record]},
    )

    with patch("src.mcp.tools.get_relation_query_service", return_value=service):
        direct = await query_subgraph(knowledge_id="7")
        fastmcp = _parse_fastmcp_tool_result(
            await server.mcp.call_tool("query_subgraph", {"knowledge_id": "7"})
        )

    for result in (direct, fastmcp):
        assert result["status"] == "error"
        assert result["issues"][0]["code"] == "retrieval_backend_failed"
        _assert_public_payload(result)


@pytest.mark.asyncio
async def test_evidence_tool_sanitizes_citation_source_urls() -> None:
    service = MagicMock()
    domain_result = CollectedEvidenceResult(
        question="fixture",
        found=True,
        seed_knowledge_id=7,
        evidence=[
            CollectedEvidenceItem(
                knowledge_id=7,
                title="fixture",
                source_url=SECRET_URL,
                citation_source=SECRET_URL,
                is_seed=True,
                citation_locator=build_entry_locator(7),
            )
        ],
        chunk_retrieval_status="not_requested",
    )
    service.collect_evidence.return_value = domain_result

    with patch("src.mcp.tools.get_evidence_collection_service", return_value=service):
        result = await collect_evidence(question="fixture")

    item = result["evidence"][0]
    assert item["source_url"] == PUBLIC_URL
    assert item["citation_source"] == PUBLIC_URL
    _assert_public_payload(result)


@pytest.mark.asyncio
async def test_evidence_tool_fails_closed_for_malformed_url_like_source() -> None:
    malformed = (
        "ht!tps://url-user:URL-PASSWORD@example.com/a?token=QUERY-SECRET"
    )
    service = MagicMock()
    domain_result = CollectedEvidenceResult(
        question="fixture",
        found=True,
        seed_knowledge_id=7,
        evidence=[
            CollectedEvidenceItem(
                knowledge_id=7,
                title="fixture",
                citation_source=malformed,
                is_seed=True,
                citation_locator=build_entry_locator(7),
            )
        ],
        chunk_retrieval_status="not_requested",
    )
    service.collect_evidence.return_value = domain_result

    with patch("src.mcp.tools.get_evidence_collection_service", return_value=service):
        result = await collect_evidence(question="fixture")

    assert result["evidence"][0]["citation_source"] == "pkv://entries/7"
    assert "URL-PASSWORD" not in repr(result)
    assert "QUERY-SECRET" not in repr(result)


@pytest.mark.asyncio
async def test_evidence_tool_rejects_non_string_citation_source_shape() -> None:
    service = MagicMock()
    domain_result = CollectedEvidenceResult(
        question="fixture",
        found=True,
        seed_knowledge_id=7,
        evidence=[
            CollectedEvidenceItem(
                knowledge_id=7,
                title="fixture",
                citation_source=[  # type: ignore[arg-type]
                    "https://url-user:URL-PASSWORD@example.com/a?token=QUERY-SECRET"
                ],
                is_seed=True,
                citation_locator=build_entry_locator(7),
            )
        ],
        chunk_retrieval_status="not_requested",
    )
    service.collect_evidence.return_value = domain_result

    with patch("src.mcp.tools.get_evidence_collection_service", return_value=service):
        result = await collect_evidence(question="fixture")

    assert result["evidence"][0]["citation_source"] == "pkv://entries/7"
    assert "URL-PASSWORD" not in repr(result)
    assert "QUERY-SECRET" not in repr(result)


@pytest.mark.asyncio
async def test_exploration_tool_sanitizes_source_urls() -> None:
    service = MagicMock()
    domain_result = TimelineResult(
        topic="fixture",
        found=True,
        items=[
            TimelinePoint(
                knowledge_id=7,
                title="fixture",
                source_url=SECRET_URL,
                source=SECRET_URL,
                citation_locator=build_entry_locator(7),
                retrieval_score=0.5,
            )
        ],
        inferred_time_field="unavailable",
        time_source_priority=["event_time", "published_at", "archived_at"],
        evidence_sources=[
            "query_results",
            "entry_metadata",
            "structured_time_fields",
        ],
        limitation_notes=["partial"],
    )
    service.timeline_of.return_value = domain_result

    with patch("src.mcp.tools.get_exploration_service", return_value=service):
        result = await timeline_of(topic="fixture")

    item = result["items"][0]
    assert item["source_url"] == PUBLIC_URL
    assert item["source"] == PUBLIC_URL
    _assert_public_payload(result)
