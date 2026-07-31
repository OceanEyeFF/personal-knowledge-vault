"""E2E tests for MCP prompt templates (knowledge_qa/search_and_summarize/idea_sharpen)."""

from __future__ import annotations

import json
from typing import Any, Dict

import pytest


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


@pytest.mark.asyncio
async def test_knowledge_qa_prompt(mcp_server):
    prompt_result = await mcp_server.get_prompt(
        "knowledge_qa",
        {"question": "AI 工作流是什么？"},
    )
    prompt_text = _extract_prompt_text(prompt_result)

    assert "**问题**" in prompt_text
    assert "请执行以下步骤" in prompt_text
    assert "AI 工作流是什么" in prompt_text
    assert "search_knowledge" in prompt_text
    assert "get_entry" in prompt_text


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

    assert "思想磨砺" in text
    assert "idea Sharpen" in text
    assert "核心价值" in text
    assert "关键观点" in text
    assert "search_knowledge" in text
    assert "get_related" in text
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
