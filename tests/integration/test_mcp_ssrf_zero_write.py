"""MCP W2 SSRF wiring must reject before any archive storage side effect."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.application import configure_application, reset_application
from src.processors.safe_fetch import SafeFetcher
from src.processors.wechat_processor import WechatProcessor
from src.runtime.bootstrap import bootstrap_runtime
from src.runtime.errors import ErrorCode
from src.runtime.layout import RuntimeLayout
from src.utils.config import Config, get_config
from src.workflow.engine import WorkflowEngine
from src.workflow.steps import StoreStep


PROJECT_ROOT = Path(__file__).parent.parent.parent
_PROTECTED_TABLES = (
    "knowledge_items",
    "content_chunks",
    "content_mutation_tasks",
    "content_ai_handoffs",
    "ai_derivation_tasks",
    "ai_derivation_reservations",
    "ai_derivation_usage",
    "storage_operation_commits",
    "r4_content_operation_commits",
)


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


def _isolated_config(tmp_path: Path) -> Config:
    layout = RuntimeLayout.resolve(
        resources_root=PROJECT_ROOT,
        user_data_root=tmp_path / "data",
        profile_root=tmp_path / "profile",
        environment={},
    )
    layout.user_config_path.parent.mkdir(parents=True, exist_ok=True)
    layout.user_config_path.write_text("{}\n", encoding="utf-8")
    config = Config(layout=layout)
    bootstrap_runtime(config)
    return config


def _table_counts(database_path: Path) -> dict[str, int]:
    with sqlite3.connect(database_path) as connection:
        return {
            table: connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            for table in _PROTECTED_TABLES
        }


def _contains_bytes(root: Path, needle: bytes) -> bool:
    if not root.exists():
        return False
    for path in root.rglob("*"):
        if not path.is_file() or path.name == "write.lease":
            continue
        try:
            if needle in path.read_bytes():
                return True
        except OSError:
            continue
    return False


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
async def test_archive_url_tool_dns_denial_has_stable_code_and_only_terminal_q0_write(
    caplog,
    boundary,
    tmp_path: Path,
):
    """Cross the MCP/Q0/SafeFetcher boundary without content-side effects.

    R4 intentionally persists its short Q0 admission before DNS resolution, so
    this test permits one terminal ingress ledger row.  It still proves that a
    DNS-based SSRF denial creates no Markdown, content row/chunk, generation,
    legacy StoreStep, or outbound transport activity.
    """

    from src.mcp.tools import archive_url

    config = _isolated_config(tmp_path)
    # The product R4 route must admit before it invokes SafeFetcher.  Establish
    # the isolated runtime first, then snapshot the state to which the request
    # itself is allowed to add only a private terminal ingress record.
    configure_application(config)
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
    protected_before = _table_counts(config.db_path)
    with sqlite3.connect(config.db_path) as connection:
        content_before = connection.execute(
            "SELECT COUNT(*) FROM knowledge_items"
        ).fetchone()[0]
        chunks_before = connection.execute(
            "SELECT COUNT(*) FROM content_chunks"
        ).fetchone()[0]
        ingress_before = connection.execute(
            "SELECT COUNT(*) FROM ingress_tasks"
        ).fetchone()[0]
        ingress_task_ids_before = {
            row[0]
            for row in connection.execute(
                "SELECT task_id FROM ingress_tasks"
            ).fetchall()
        }
    wechat_tmp_before = {
        path.name for path in config.tmp_dir.glob("wechat_*") if path.is_file()
    }

    try:
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
    finally:
        reset_application()

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
    # Q0 admission/rejection is the sole expected database delta.  Its private
    # spool body is discarded on this terminal security denial.
    assert _database_snapshot(config.db_path) != database_before
    with sqlite3.connect(config.db_path) as connection:
        content_after = connection.execute(
            "SELECT COUNT(*) FROM knowledge_items"
        ).fetchone()[0]
        chunks_after = connection.execute(
            "SELECT COUNT(*) FROM content_chunks"
        ).fetchone()[0]
        ingress_rows = connection.execute(
            "SELECT task_id, state, last_error_code, request_ref FROM ingress_tasks"
        ).fetchall()
        new_ingress_rows = [
            row for row in ingress_rows if row[0] not in ingress_task_ids_before
        ]
        ingress_count = connection.execute(
            "SELECT COUNT(*) FROM ingress_tasks"
        ).fetchone()[0]
    assert content_after == content_before
    assert chunks_after == chunks_before
    assert _table_counts(config.db_path) == protected_before
    assert ingress_count == ingress_before + 1
    assert len(new_ingress_rows) == 1
    _, ingress_state, ingress_error, request_ref = new_ingress_rows[0]
    assert (ingress_state, ingress_error) == (
        "rejected",
        ErrorCode.SSRF_TARGET_FORBIDDEN.value,
    )
    assert isinstance(request_ref, str) and len(request_ref) == 32
    assert not (
        config.layout.runtime_state_dir / "r4" / "ingress" / f"{request_ref}.json"
    ).exists()
    assert {
        path.name for path in config.tmp_dir.glob("wechat_*") if path.is_file()
    } == wechat_tmp_before
    assert canary not in repr(result)
    assert canary not in caplog.text
    assert not _contains_bytes(config.layout.runtime_state_dir / "r4", canary.encode())
    assert not _contains_bytes(config.db_path.parent, canary.encode())
