"""E2E tests for MCP search tool."""

from __future__ import annotations

import asyncio
import json
import os
import statistics
import time
from datetime import datetime
from typing import Any, Dict, List

import pytest
from dotenv import load_dotenv

from src.storage.sqlite_store import SQLiteStore

load_dotenv()


def _parse_tool_content(result) -> Dict[str, Any]:
    if hasattr(result, "content") and result.content:
        first = result.content[0]
        text = getattr(first, "text", None)
        if text:
            return json.loads(text)
    raise ValueError(f"无法解析 call_tool 结果: {result}")


def _assert_search_payload(data: Dict[str, Any], min_total: int = 1) -> None:
    assert isinstance(data, dict)
    assert "total" in data
    assert "strategy_used" in data
    assert "results" in data
    assert isinstance(data["results"], list)
    assert data["total"] >= min_total

    for item in data["results"]:
        assert isinstance(item, dict)
        for key in ["knowledge_id", "title", "abstract", "score", "tags", "source_type", "archived_at"]:
            assert key in item
        assert isinstance(item["tags"], list)
        assert isinstance(item["score"], (int, float))


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _has_openai_key() -> bool:
    return bool(os.getenv("OPENAI_API_KEY"))


async def _call_search(session, payload: Dict[str, Any], timeout_s: float = 60.0):
    return await asyncio.wait_for(
        session.call_tool("search_knowledge", payload),
        timeout=timeout_s,
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
    result = await _call_search(
        mcp_server,
        {"query": "不存在的关键词xyz123", "strategy": "bm25", "top_k": 5},
    )
    data = _parse_tool_content(result)
    assert data["total"] == 0
    assert data["results"] == []


@pytest.mark.asyncio
async def test_search_strategy_selection(mcp_server, sample_knowledge_db):
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
    assert auto_data["results"][0]["title"] == bm25_data["results"][0]["title"]

    if not _has_openai_key() or not sample_knowledge_db.get("vector_enabled"):
        return

    long_query = "如何设计一个可扩展的 AI 工作流 系统 并进行效果评估"
    auto_long = await _call_search(
        mcp_server,
        {"query": long_query, "strategy": "auto", "top_k": 5},
    )
    hybrid = await _call_search(
        mcp_server,
        {"query": long_query, "strategy": "hybrid", "top_k": 5},
    )
    vector = await _call_search(
        mcp_server,
        {"query": "语义搜索 评测", "strategy": "vector", "top_k": 5},
    )

    auto_long_data = _parse_tool_content(auto_long)
    hybrid_data = _parse_tool_content(hybrid)
    vector_data = _parse_tool_content(vector)

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


def test_search_performance(sample_knowledge_db):
    from src.retrieval.bm25_retriever import BM25Retriever

    retriever = BM25Retriever(sample_knowledge_db["db_path"])

    # Warm-up (jieba + SQLite cache)
    retriever.search("AI", limit=5)

    durations: List[float] = []
    for _ in range(5):
        start = time.perf_counter()
        retriever.search("AI", limit=5)
        durations.append(time.perf_counter() - start)

    avg_ms = statistics.mean(durations) * 1000
    assert avg_ms < 100, f"平均响应时间过慢: {avg_ms:.2f}ms"
