"""E2E tests for MCP search tool."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict

import pytest

from src.storage.sqlite_store import SQLiteStore


def _parse_tool_content(result) -> Dict[str, Any]:
    if getattr(result, "isError", False):
        raise ValueError(f"call_tool 返回 MCP error: {result}")
    content = getattr(result, "content", None)
    if not isinstance(content, list) or len(content) != 1:
        raise ValueError(f"call_tool 必须返回单一 TextContent: {result}")
    text = getattr(content[0], "text", None)
    if not isinstance(text, str) or not text:
        raise ValueError(f"call_tool TextContent 缺少 JSON 文本: {result}")
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ValueError(f"call_tool JSON 必须为 object: {type(payload).__name__}")
    return payload


def _assert_search_payload(data: Dict[str, Any], min_total: int = 1) -> None:
    assert isinstance(data, dict)
    assert set(data) == {"status", "strategy", "total", "results", "issues"}
    assert data["status"] in {"success", "no_hits", "invalid", "error", "degraded"}
    assert isinstance(data["strategy"], str)
    assert isinstance(data["total"], int)
    assert not isinstance(data["total"], bool)
    assert isinstance(data["results"], list)
    assert isinstance(data["issues"], list)
    assert data["total"] == len(data["results"])
    assert data["total"] >= min_total
    if min_total > 0:
        assert data["status"] == "success"
        assert data["issues"] == []

    for item in data["results"]:
        assert isinstance(item, dict)
        assert set(item) == {
            "knowledge_id",
            "title",
            "abstract",
            "score",
            "tags",
            "source_type",
            "archived_at",
        }
        assert isinstance(item["tags"], list)
        assert isinstance(item["score"], (int, float))
        assert not isinstance(item["score"], bool)


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value)


async def _call_search(
    session,
    payload: Dict[str, Any],
    timeout_s: float = 60.0,
):
    return await session.call_tool(
        "search_knowledge",
        payload,
        timeout_s=timeout_s,
    )


@pytest.mark.asyncio
async def test_search_by_keyword(mcp_server, sample_knowledge_db):
    result = await _call_search(
        mcp_server,
        {"query": "知识图谱", "strategy": "bm25", "top_k": 5},
    )
    data = _parse_tool_content(result)
    _assert_search_payload(data, min_total=1)

    titles = [r["title"] for r in data["results"]]
    assert any("知识图谱" in title for title in titles), f"未命中知识图谱条目: {titles}"

    store = SQLiteStore(sample_knowledge_db["db_path"])
    with store.get_connection() as conn:
        fts_count = conn.execute("SELECT COUNT(*) FROM knowledge_items_fts").fetchone()[0]
    assert fts_count >= 20, "FTS5 索引应包含全部测试条目"


@pytest.mark.asyncio
async def test_search_with_tag_filter(mcp_server, sample_knowledge_db):
    result = await _call_search(
        mcp_server,
        {"query": "AI", "tag": "AI", "top_k": 10},
    )
    data = _parse_tool_content(result)
    _assert_search_payload(data, min_total=1)

    for item in data["results"]:
        assert "AI" in item["tags"], f"标签过滤失效: {item}"


@pytest.mark.asyncio
async def test_search_empty_result(mcp_server):
    # A multi-token query intentionally relaxes to OR after a strict miss.
    # Use one unique token so this assertion tests the no-results contract.
    result = await _call_search(
        mcp_server,
        {"query": "pkvnomatchxyz123", "strategy": "bm25", "top_k": 5},
    )
    data = _parse_tool_content(result)
    assert data["total"] == 0
    assert data["results"] == []
    assert data["status"] == "no_hits"
    assert data["issues"] == []


@pytest.mark.asyncio
async def test_search_strategy_selection(mcp_server):
    auto_short = await _call_search(
        mcp_server,
        {"query": "AI 工作流", "strategy": "auto", "top_k": 5},
    )
    bm25 = await _call_search(
        mcp_server,
        {"query": "AI 工作流", "strategy": "bm25", "top_k": 5},
    )

    auto_data = _parse_tool_content(auto_short)
    bm25_data = _parse_tool_content(bm25)
    _assert_search_payload(auto_data, min_total=1)
    _assert_search_payload(bm25_data, min_total=1)
    assert auto_data["strategy"] == "bm25"
    assert bm25_data["strategy"] == "bm25"
    assert auto_data["results"][0]["title"] == bm25_data["results"][0]["title"]


@pytest.mark.network
@pytest.mark.asyncio
async def test_search_live_vector_strategies(live_mcp_server):
    long_query = "如何设计一个可扩展的 AI 工作流 系统 并进行效果评估"
    auto_long = await _call_search(
        live_mcp_server,
        {"query": long_query, "strategy": "auto", "top_k": 5},
    )
    hybrid = await _call_search(
        live_mcp_server,
        {"query": long_query, "strategy": "hybrid", "top_k": 5},
    )
    vector = await _call_search(
        live_mcp_server,
        {"query": "语义搜索 评测", "strategy": "vector", "top_k": 5},
    )

    auto_long_data = _parse_tool_content(auto_long)
    hybrid_data = _parse_tool_content(hybrid)
    vector_data = _parse_tool_content(vector)

    _assert_search_payload(auto_long_data, min_total=1)
    _assert_search_payload(hybrid_data, min_total=1)
    _assert_search_payload(vector_data, min_total=1)
    assert auto_long_data["results"][0]["title"] == hybrid_data["results"][0]["title"]


@pytest.mark.asyncio
async def test_search_ranking(mcp_server, sample_knowledge_db):
    result = await _call_search(
        mcp_server,
        {"query": "AI 工作流", "strategy": "bm25", "top_k": 5},
    )
    data = _parse_tool_content(result)
    _assert_search_payload(data, min_total=1)

    titles = [r["title"] for r in data["results"]]
    assert titles[0] == "AI 工作流深度实践", f"相关性排序异常: {titles}"

    scores = [r["score"] for r in data["results"]]
    assert scores == sorted(scores, reverse=True), f"分数未按相关性降序: {scores}"

    times = [_parse_time(r["archived_at"]) for r in data["results"] if r["archived_at"]]
    assert times, "archived_at 字段缺失"
    assert _parse_time(data["results"][0]["archived_at"]) == max(times)


def test_search_repeated_calls_are_deterministic(sample_knowledge_db):
    from src.retrieval.bm25_retriever import BM25Retriever

    retriever = BM25Retriever(sample_knowledge_db["db_path"])

    snapshots = [
        [
            (item.knowledge_id, item.title, item.score)
            for item in retriever.search("AI", limit=5).results
        ]
        for _ in range(3)
    ]

    assert snapshots[0]
    assert snapshots[1:] == [snapshots[0], snapshots[0]]


@pytest.mark.asyncio
async def test_search_invalid_query_is_distinguishable(mcp_server):
    result = await _call_search(
        mcp_server,
        {"query": "", "strategy": "bm25", "top_k": 5},
    )
    data = _parse_tool_content(result)

    assert set(data) == {"status", "strategy", "total", "results", "issues"}
    assert data["status"] == "invalid"
    assert data["strategy"] == "bm25"
    assert data["total"] == 0
    assert data["results"] == []
    assert data["issues"][0]["code"] == "retrieval_invalid_query"


@pytest.mark.asyncio
async def test_search_vector_without_provider_is_error_not_no_hits(mcp_server):
    result = await _call_search(
        mcp_server,
        {"query": "语义搜索", "strategy": "vector", "top_k": 5},
    )
    data = _parse_tool_content(result)

    assert set(data) == {"status", "strategy", "total", "results", "issues"}
    assert data["status"] == "error"
    assert data["strategy"] == "vector"
    assert data["total"] == 0
    assert data["results"] == []
    assert data["issues"][0]["code"] in {
        "provider_config_invalid",
        "retrieval_index_unavailable",
    }
