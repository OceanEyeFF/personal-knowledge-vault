"""E2E tests for MCP archive tools."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable, Tuple

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


async def _call_tool(
    session,
    tool_name: str,
    payload: Dict[str, Any],
    timeout_s: float = 120.0,
):
    return await session.call_tool(
        tool_name,
        payload,
        timeout_s=timeout_s,
    )


def _assert_error_payload(payload: Dict[str, Any], expected_substr: str | None = None) -> None:
    assert payload.get("success") is False
    assert isinstance(payload.get("error"), str)
    assert payload["error"].strip()
    if expected_substr:
        assert expected_substr in payload["error"]


def _assert_archive_success(payload: Dict[str, Any]) -> int:
    required_fields = {
        "success",
        "knowledge_id",
        "title",
        "entry_locator",
        "tags",
    }
    assert required_fields <= set(payload)
    assert "file_path" not in payload
    assert payload["success"] is True
    assert isinstance(payload["title"], str) and payload["title"].strip()
    assert isinstance(payload["tags"], list)

    knowledge_id = payload["knowledge_id"]
    assert isinstance(knowledge_id, int) and not isinstance(knowledge_id, bool)
    assert knowledge_id > 0
    assert payload["entry_locator"] == f"pkv://entries/{knowledge_id}"
    return knowledge_id


def _assert_stored_markdown_is_isolated(
    entry: Dict[str, Any],
    vault_dir: Path,
) -> None:
    file_path = Path(entry["file_path"]).resolve()
    assert file_path.is_relative_to(vault_dir.resolve())
    assert file_path.is_file(), f"归档文件不存在: {file_path}"


def _assert_search_payload(data: Dict[str, Any]) -> None:
    assert isinstance(data, dict)
    assert set(data) == {"total", "strategy_used", "results"}
    assert isinstance(data["total"], int)
    assert not isinstance(data["total"], bool)
    assert data["strategy_used"] == "bm25"
    assert isinstance(data["results"], list)
    assert data["total"] == len(data["results"])



@pytest.mark.network
@pytest.mark.asyncio
async def test_archive_url_success(live_archive_mcp_server, live_archive_test_env):
    urls: Iterable[Tuple[str, str]] = [
        ("wechat", "https://mp.weixin.qq.com/s/ZET927baoFCj3In_11fKeA"),
        ("zhihu", "https://zhuanlan.zhihu.com/p/1989702804772758028"),
        ("generic", "https://api-docs.deepseek.com/zh-cn/news/news251201"),
    ]

    store = SQLiteStore(live_archive_test_env.db_path)

    for source_type, url in urls:
        before_count = store.count_entries(source_type=source_type)

        result = await _call_tool(
            live_archive_mcp_server,
            "archive_url",
            {"url": url},
            timeout_s=180.0,
        )
        data = _parse_tool_content(result)
        knowledge_id = _assert_archive_success(data)

        after_count = store.count_entries(source_type=source_type)
        assert after_count == before_count + 1

        entry = store.query_by_id(knowledge_id)
        assert entry is not None
        assert entry["source_type"] == source_type
        assert entry["source_url"] == url
        _assert_stored_markdown_is_isolated(entry, live_archive_test_env.vault_dir)


@pytest.mark.asyncio
async def test_archive_url_ssrf_protection(
    archive_mcp_server,
    archive_test_env,
):
    store = SQLiteStore(archive_test_env.db_path)
    before_count = store.count_entries()

    result = await _call_tool(
        archive_mcp_server,
        "archive_url",
        {"url": "http://127.0.0.1/health"},
        timeout_s=10.0,
    )
    data = _parse_tool_content(result)
    _assert_error_payload(data, expected_substr="禁止访问内网地址")

    after_count = store.count_entries()
    assert after_count == before_count


@pytest.mark.asyncio
async def test_archive_url_invalid_format(
    archive_mcp_server,
    archive_test_env,
):
    store = SQLiteStore(archive_test_env.db_path)
    before_count = store.count_entries()

    result = await _call_tool(
        archive_mcp_server,
        "archive_url",
        {"url": "not-a-url"},
        timeout_s=10.0,
    )
    data = _parse_tool_content(result)
    _assert_error_payload(data, expected_substr="URL scheme")

    after_count = store.count_entries()
    assert after_count == before_count


@pytest.mark.network
@pytest.mark.asyncio
async def test_archive_text_success(live_archive_mcp_server, live_archive_test_env):
    text = "# E2E 归档纯文本\n\n这是一次端到端归档测试，验证文本能被成功写入数据库。"

    store = SQLiteStore(live_archive_test_env.db_path)
    before_count = store.count_entries(source_type="text")

    result = await _call_tool(
        live_archive_mcp_server,
        "archive_text",
        {"text": text},
        timeout_s=120.0,
    )
    data = _parse_tool_content(result)
    knowledge_id = _assert_archive_success(data)

    after_count = store.count_entries(source_type="text")
    assert after_count == before_count + 1

    entry = store.query_by_id(knowledge_id)
    assert entry is not None
    assert entry["source_type"] == "text"
    assert "E2E 归档纯文本" in entry["title"]
    _assert_stored_markdown_is_isolated(entry, live_archive_test_env.vault_dir)


@pytest.mark.network
@pytest.mark.asyncio
async def test_archive_text_with_title(live_archive_mcp_server):
    text = "E2E 标题覆盖测试内容，包含标签词：归档、标题、标签。"
    title = "E2E 自定义标题"

    result = await _call_tool(
        live_archive_mcp_server,
        "archive_text",
        {"text": text, "title": title},
        timeout_s=120.0,
    )
    data = _parse_tool_content(result)
    _assert_archive_success(data)
    assert data["title"] == title
    assert isinstance(data.get("tags", []), list)
    assert data.get("tags")


@pytest.mark.asyncio
async def test_archive_text_length_limit(archive_mcp_server):
    too_long_text = "a" * 100001
    result = await _call_tool(
        archive_mcp_server,
        "archive_text",
        {"text": too_long_text},
        timeout_s=10.0,
    )
    data = _parse_tool_content(result)
    _assert_error_payload(data, expected_substr="超过限制")


@pytest.mark.network
@pytest.mark.asyncio
async def test_archive_then_search(live_archive_mcp_server):
    keyword = "归档搜索验证E2E"
    text = f"# {keyword}\n\n归档后立即搜索，确保结果可检索。"

    archive_result = await _call_tool(
        live_archive_mcp_server,
        "archive_text",
        {"text": text, "title": keyword},
        timeout_s=120.0,
    )
    archive_data = _parse_tool_content(archive_result)
    _assert_archive_success(archive_data)

    search_result = await _call_tool(
        live_archive_mcp_server,
        "search_knowledge",
        {"query": keyword, "strategy": "bm25", "top_k": 5},
        timeout_s=60.0,
    )
    search_data = _parse_tool_content(search_result)
    _assert_search_payload(search_data)

    titles = [item.get("title", "") for item in search_data["results"]]
    assert any(keyword in title for title in titles), f"搜索未命中归档条目: {titles}"
