"""MCP W2 SSRF wiring must reject before any archive storage side effect."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.processors.safe_fetch import SafeFetcher
from src.processors.wechat_processor import WechatProcessor
from src.runtime.errors import ErrorCode
from src.utils.config import get_config
from src.workflow.engine import WorkflowEngine
from src.workflow.steps import StoreStep


def _tree_snapshot(root: Path) -> dict[str, str]:
    if not root.exists():
        return {}
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _database_snapshot(database_path: Path) -> dict[str, str]:
    candidates = (
        database_path,
        database_path.with_name(f"{database_path.name}-wal"),
        database_path.with_name(f"{database_path.name}-shm"),
    )
    return {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in candidates
        if path.exists()
    }


@pytest.mark.asyncio
async def test_archive_url_yaml_ssrf_dns_denial_has_zero_storage_writes(caplog):
    """Exercise real YAML + FetchStep + SafeFetcher, not a mocked runtime error."""

    config = get_config()
    canary = "W2-SSRF-ZERO-WRITE-CANARY"
    target_url = f"https://mp.weixin.qq.com/s/article?token={canary}"
    resolver = MagicMock(return_value=("127.0.0.1",))
    transport = MagicMock()
    processor = WechatProcessor(
        safe_fetcher=SafeFetcher(
            resolver=resolver,
            transport=transport,
        )
    )
    store_execute = AsyncMock(
        side_effect=AssertionError("store step must not execute after SSRF denial")
    )

    vault_before = _tree_snapshot(config.vault_dir)
    vectors_before = _tree_snapshot(config.vector_index_dir)
    database_before = _database_snapshot(config.db_path)
    wechat_tmp_before = {
        path.name for path in config.tmp_dir.glob("wechat_*") if path.is_file()
    }

    with (
        patch("src.workflow.steps.get_processor", return_value=processor),
        patch.object(StoreStep, "execute", new=store_execute),
    ):
        result = await WorkflowEngine(reload_config=True).execute_async(
            "archive-url",
            {
                "url": target_url,
                "skip_review": True,
                "skip_sharpen": True,
            },
        )

    matching_issues = [
        issue
        for issue in result.issues
        if issue.get("code") == ErrorCode.SSRF_TARGET_FORBIDDEN.value
    ]
    assert result.success is False
    assert result.terminal == "error"
    assert matching_issues
    assert matching_issues[0]["stage"] == "network_policy"
    assert matching_issues[0]["recoverable"] is False
    assert result.data.get("core_committed") is not True
    assert "knowledge_id" not in result.data
    store_execute.assert_not_awaited()
    transport.request.assert_not_called()
    # Stable security denials are terminal and must not consume the YAML retry
    # budget as if they were transient transport failures.
    resolver.assert_called_once_with("mp.weixin.qq.com", 443)

    assert _tree_snapshot(config.vault_dir) == vault_before
    assert _tree_snapshot(config.vector_index_dir) == vectors_before
    assert _database_snapshot(config.db_path) == database_before
    assert {
        path.name for path in config.tmp_dir.glob("wechat_*") if path.is_file()
    } == wechat_tmp_before
    # Workflow state intentionally retains its caller input; diagnostics and
    # logs must not copy that credential-bearing URL into public error text.
    assert canary not in repr(result.issues)
    assert canary not in repr(result.errors)
    assert canary not in repr(result.logs)
    assert canary not in caplog.text


@pytest.mark.parametrize("boundary", ["direct", "fastmcp"])
@pytest.mark.asyncio
async def test_archive_url_tool_dns_denial_has_stable_code_and_zero_writes(
    caplog,
    boundary,
):
    """Cross the real MCP Tool, YAML workflow and SafeFetcher policy boundary."""

    from src.mcp.tools import archive_url

    config = get_config()
    canary = "W2-MCP-SSRF-ZERO-WRITE-CANARY"
    target_url = f"https://mp.weixin.qq.com/s/article?token={canary}"
    resolver = MagicMock(return_value=("127.0.0.1",))
    transport = MagicMock()
    processor = WechatProcessor(
        safe_fetcher=SafeFetcher(
            resolver=resolver,
            transport=transport,
        )
    )
    store_execute = AsyncMock(
        side_effect=AssertionError("store step must not execute after SSRF denial")
    )

    vault_before = _tree_snapshot(config.vault_dir)
    vectors_before = _tree_snapshot(config.vector_index_dir)
    database_before = _database_snapshot(config.db_path)
    wechat_tmp_before = {
        path.name for path in config.tmp_dir.glob("wechat_*") if path.is_file()
    }

    with (
        patch("src.workflow.steps.get_processor", return_value=processor),
        patch.object(StoreStep, "execute", new=store_execute),
    ):
        if boundary == "direct":
            result = await archive_url(target_url)
        else:
            from src.mcp.server import mcp

            raw_result = await mcp.call_tool("archive_url", {"url": target_url})
            if isinstance(raw_result, dict):
                result = raw_result
            else:
                assert isinstance(raw_result, (list, tuple))
                assert len(raw_result) == 1
                result = json.loads(raw_result[0].text)

    matching_issues = [
        issue
        for issue in result["issues"]
        if issue.get("code") == ErrorCode.SSRF_TARGET_FORBIDDEN.value
    ]
    assert result["success"] is False
    assert result["terminal"] == "error"
    assert matching_issues
    assert matching_issues[0]["stage"] == "network_policy"
    assert matching_issues[0]["recoverable"] is False
    assert result.get("core_committed") is not True
    assert "knowledge_id" not in result
    store_execute.assert_not_awaited()
    transport.request.assert_not_called()
    resolver.assert_called_once_with("mp.weixin.qq.com", 443)

    assert _tree_snapshot(config.vault_dir) == vault_before
    assert _tree_snapshot(config.vector_index_dir) == vectors_before
    assert _database_snapshot(config.db_path) == database_before
    assert {
        path.name for path in config.tmp_dir.glob("wechat_*") if path.is_file()
    } == wechat_tmp_before
    assert canary not in repr(result)
    assert canary not in caplog.text
