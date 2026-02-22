"""E2E tests for MCP prompt templates (knowledge_qa/search_and_summarize/idea_sharpen)."""

from __future__ import annotations

import json
from typing import Any, Dict, List

import pytest


def _parse_tool_content(result) -> Dict[str, Any]:
    if hasattr(result, "content") and result.content:
        first = result.content[0]
        text = getattr(first, "text", None)
        if text:
            return json.loads(text)
    raise ValueError(f"无法解析 call_tool 结果: {result}")


def _extract_prompt_text(prompt_result: Any) -> str:
    if hasattr(prompt_result, "messages") and prompt_result.messages:
        msg = prompt_result.messages[0]
        content = getattr(msg, "content", None)
        if isinstance(content, str) and content:
            return content
        text = getattr(content, "text", None)
        if text:
            return text
        return str(msg)
    return str(prompt_result)


def _inject_context(prompt_text: str, context_text: str) -> str:
    return f"{prompt_text}\n\n[检索上下文]\n{context_text}"


def _build_context_from_results(results: List[Dict[str, Any]], limit: int = 3) -> str:
    lines = []
    for item in results[:limit]:
        title = item.get("title", "").strip()
        source_type = item.get("source_type", "").strip()
        abstract = item.get("abstract", "").strip()
        lines.append(f"- {title} ({source_type}) | {abstract}")
    return "\n".join(lines)


@pytest.mark.asyncio
async def test_knowledge_qa_prompt(mcp_server):
    search_result = await mcp_server.call_tool(
        "search_knowledge",
        {"query": "AI 工作流", "strategy": "bm25", "top_k": 3},
    )
    data = _parse_tool_content(search_result)
    assert data["results"], "需要至少 1 条搜索结果用于上下文注入"

    context_text = _build_context_from_results(data["results"])
    assert "AI 工作流" in context_text
    assert "(" in context_text and ")" in context_text

    prompt_result = await mcp_server.get_prompt(
        "knowledge_qa",
        {"question": "AI 工作流是什么？"},
    )
    prompt_text = _extract_prompt_text(prompt_result)
    assert "**问题**" in prompt_text
    assert "请执行以下步骤" in prompt_text
    full_prompt = _inject_context(prompt_text, context_text)

    assert "AI 工作流是什么" in full_prompt
    assert "search_knowledge" in full_prompt
    assert "get_entry" in full_prompt
    assert "[检索上下文]" in full_prompt
    for line in context_text.splitlines():
        assert line in full_prompt


@pytest.mark.asyncio
async def test_search_and_summarize_prompt(mcp_server):
    prompt_result = await mcp_server.get_prompt(
        "search_and_summarize",
        {"query": "AI 工作流", "context": "准备写一份方案"},
    )
    text = _extract_prompt_text(prompt_result)

    assert "AI 工作流" in text
    assert "准备写一份方案" in text
    assert "背景信息" in text
    assert "请执行以下步骤" in text
    assert "search_knowledge" in text
    assert "1." in text and "2." in text
    assert "{query}" not in text and "{context}" not in text and "{results}" not in text


@pytest.mark.asyncio
async def test_search_and_summarize_prompt_results_injection(mcp_server):
    search_result = await mcp_server.call_tool(
        "search_knowledge",
        {"query": "向量检索", "strategy": "bm25", "top_k": 2},
    )
    data = _parse_tool_content(search_result)
    assert data["results"], "需要检索结果用于 {results} 注入验证"

    results_block = "\n".join(
        f"- {item['title']} | {item['abstract']}" for item in data["results"]
    )
    context_template = "已检索结果:\n{results}"
    context = context_template.format(results=results_block)

    prompt_result = await mcp_server.get_prompt(
        "search_and_summarize",
        {"query": "向量检索", "context": context},
    )
    text = _extract_prompt_text(prompt_result)

    assert "向量检索" in text
    assert "已检索结果" in text
    for line in results_block.splitlines():
        assert line in text


@pytest.mark.asyncio
async def test_idea_sharpen_prompt(mcp_server):
    prompt_result = await mcp_server.get_prompt(
        "idea_sharpen",
        {"content": "这是一段关于知识管理的内容", "entry_id": "42"},
    )
    text = _extract_prompt_text(prompt_result)

    assert "思想磨砺" in text or "idea Sharpen" in text
    assert "核心价值" in text
    assert "关键观点" in text
    assert "search_knowledge" in text or "get_related" in text
    assert "42" in text
    assert "get_entry" in text


@pytest.mark.asyncio
async def test_prompt_variable_conflict_handling(mcp_server):
    context = "包含占位符 {query} {results} {context}，应保留原样。"
    prompt_result = await mcp_server.get_prompt(
        "search_and_summarize",
        {"query": "AI 工程化", "context": context},
    )
    text = _extract_prompt_text(prompt_result)

    assert "{query}" in text
    assert "{results}" in text
    assert "{context}" in text


@pytest.mark.asyncio
async def test_idea_sharpen_prompt_long_text_truncation(mcp_server):
    long_content = "A" * 2100 + "TAIL_SHOULD_NOT_APPEAR"
    prompt_result = await mcp_server.get_prompt(
        "idea_sharpen",
        {"content": long_content},
    )
    text = _extract_prompt_text(prompt_result)

    assert "A" * 200 in text
    assert "TAIL_SHOULD_NOT_APPEAR" not in text
