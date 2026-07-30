"""
MCP 集成测试

测试 MCP Server 的端到端工作流程：
- Tool handler 与真实 SQLiteStore 交互
- Resource handler 与真实存储交互
- 验证完整的数据流转

注意：使用临时数据库，不影响生产数据。
"""

import json
import sys
from pathlib import Path

import pytest

# 确保项目根目录在 Python path 中
PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.storage.sqlite_store import SQLiteStore  # noqa: E402
from src.storage.markdown_store import Entry, MarkdownStore  # noqa: E402


def assert_stats_payload(payload: dict) -> None:
    """Assert the canonical public statistics schema."""

    assert set(payload) == {"total_entries", "by_source_type", "top_tags"}
    assert isinstance(payload["total_entries"], int)
    assert not isinstance(payload["total_entries"], bool)
    assert payload["total_entries"] >= 0
    assert isinstance(payload["by_source_type"], list)
    assert all(
        isinstance(item, list)
        and len(item) == 2
        and isinstance(item[0], str)
        and isinstance(item[1], int)
        and not isinstance(item[1], bool)
        for item in payload["by_source_type"]
    )
    assert isinstance(payload["top_tags"], list)
    assert all(
        set(item) == {"name", "count"}
        and isinstance(item["name"], str)
        and isinstance(item["count"], int)
        and not isinstance(item["count"], bool)
        for item in payload["top_tags"]
    )


@pytest.fixture
def test_db(tmp_path: Path) -> SQLiteStore:
    """创建临时测试数据库。"""
    db_path = tmp_path / "test.db"
    store = SQLiteStore(db_path)
    store.initialize()
    return store


@pytest.fixture
def test_vault(tmp_path: Path) -> Path:
    """创建临时 Markdown vault 目录。"""
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    return vault_dir


@pytest.fixture
def populated_db(test_db: SQLiteStore, test_vault: Path) -> tuple:
    """填充测试数据的数据库和 vault。

    Returns:
        (SQLiteStore, vault_dir, list_of_entry_ids)
    """
    entries = [
        Entry(
            title="微信文章：AI 工程化",
            source_type="wechat",
            source_url="https://mp.weixin.qq.com/s/article1",
            tags=["AI", "工程化"],
            keywords=["人工智能", "MLOps"],
            abstract="AI 工程化实践总结",
            summary_one_sentence="AI 工程化的最佳实践指南",
            summary_100_words="本文总结了 AI 工程化的关键实践...",
            content="# AI 工程化\n\n这是关于 AI 工程化的详细内容。",
            word_count=200,
        ),
        Entry(
            title="知乎回答：NLP 入门",
            source_type="zhihu",
            source_url="https://www.zhihu.com/answer/456",
            tags=["NLP", "入门"],
            keywords=["自然语言处理"],
            abstract="NLP 入门路线图",
            summary_one_sentence="NLP 学习路线推荐",
            content="# NLP 入门\n\n自然语言处理入门指南。",
            word_count=150,
        ),
        Entry(
            title="通用网页：Python 教程",
            source_type="generic",
            source_url="https://example.com/python",
            tags=["Python", "教程"],
            keywords=["编程"],
            abstract="Python 基础教程",
            summary_one_sentence="Python 编程入门教程",
            content="# Python 教程\n\nPython 从入门到实践。",
            word_count=300,
        ),
    ]

    entry_ids = []
    for entry in entries:
        # 创建 Markdown 文件
        md_dir = test_vault / entry.source_type
        md_dir.mkdir(parents=True, exist_ok=True)
        md_path = md_dir / f"{entry.title[:10]}.md"
        md_path.write_text(
            f"---\ntitle: {entry.title}\nsource_type: {entry.source_type}\n"
            f"tags: [{', '.join(entry.tags)}]\n---\n{entry.content}",
            encoding="utf-8",
        )
        # 插入数据库
        kid = test_db.insert_entry(entry, str(md_path))
        entry_ids.append(kid)

    return test_db, test_vault, entry_ids


class TestToolsIntegration:
    """Tool handler 集成测试（使用真实 SQLiteStore）。"""

    @pytest.mark.asyncio
    async def test_list_entries_integration(self, populated_db):
        """list_entries 应返回填充的测试数据。"""
        from unittest.mock import patch
        store, vault_dir, entry_ids = populated_db

        with patch("src.mcp.tools.get_sqlite_store", return_value=store):
            from src.mcp.tools import list_entries
            result = await list_entries(page=1, per_page=10)

        assert result["total"] == 3
        assert len(result["entries"]) == 3
        # 验证 tags 被正确转换为列表
        for entry in result["entries"]:
            assert isinstance(entry["tags"], list)

    @pytest.mark.asyncio
    async def test_list_entries_with_source_filter(self, populated_db):
        """list_entries 按 source_type 过滤应正确工作。"""
        from unittest.mock import patch
        store, vault_dir, entry_ids = populated_db

        with patch("src.mcp.tools.get_sqlite_store", return_value=store):
            from src.mcp.tools import list_entries
            result = await list_entries(source_type="wechat")

        assert result["total"] == 1
        assert result["entries"][0]["source_type"] == "wechat"

    @pytest.mark.asyncio
    async def test_list_entries_with_tag_filter(self, populated_db):
        """list_entries 按 tag 过滤应正确工作。"""
        from unittest.mock import patch
        store, vault_dir, entry_ids = populated_db

        with patch("src.mcp.tools.get_sqlite_store", return_value=store):
            from src.mcp.tools import list_entries
            result = await list_entries(tag="AI")

        assert result["total"] == 1
        assert "AI" in result["entries"][0]["tags"]

    @pytest.mark.asyncio
    async def test_get_entry_integration(self, populated_db):
        """get_entry 应返回完整条目（含 Markdown 全文）。"""
        from unittest.mock import patch
        store, vault_dir, entry_ids = populated_db
        md_store = MarkdownStore(vault_dir)

        with patch("src.mcp.tools.get_sqlite_store", return_value=store), \
             patch("src.mcp.tools.get_markdown_store", return_value=md_store):
            from src.mcp.tools import get_entry
            result = await get_entry(knowledge_id=str(entry_ids[0]))

        assert result["title"] == "微信文章：AI 工程化"
        assert result["tags"] == ["AI", "工程化"]
        assert result["source_type"] == "wechat"
        assert "AI 工程化" in result["content"]

    @pytest.mark.asyncio
    async def test_list_tags_integration(self, populated_db):
        """list_tags 应返回所有标签及计数。"""
        from unittest.mock import patch
        store, vault_dir, entry_ids = populated_db

        with patch("src.mcp.tools.get_sqlite_store", return_value=store):
            from src.mcp.tools import list_tags
            result = await list_tags()

        assert result["total_tags"] > 0
        tag_names = [t["name"] for t in result["tags"]]
        assert "AI" in tag_names
        assert "NLP" in tag_names

    @pytest.mark.asyncio
    async def test_get_stats_integration(self, populated_db):
        """get_stats 应返回正确的统计数据。"""
        from unittest.mock import patch
        store, vault_dir, entry_ids = populated_db

        with patch("src.mcp.tools.get_sqlite_store", return_value=store):
            from src.mcp.tools import get_stats
            result = await get_stats()

        assert_stats_payload(result)


class TestResourcesIntegration:
    """Resource handler 集成测试。"""

    @pytest.mark.asyncio
    async def test_entry_content_resource(self, populated_db):
        """pkv://entries/{id} 应返回 Markdown 全文。"""
        from unittest.mock import patch
        store, vault_dir, entry_ids = populated_db
        md_store = MarkdownStore(vault_dir)

        with patch("src.mcp.resources.get_sqlite_store", return_value=store), \
             patch("src.mcp.resources.get_markdown_store", return_value=md_store):
            from src.mcp.resources import get_entry_content
            result = await get_entry_content(knowledge_id=str(entry_ids[0]))

        assert "AI 工程化" in result

    @pytest.mark.asyncio
    async def test_entry_metadata_resource(self, populated_db):
        """pkv://entries/{id}/metadata 应返回有效 JSON。"""
        from unittest.mock import patch
        store, vault_dir, entry_ids = populated_db
        md_store = MarkdownStore(vault_dir)

        with patch("src.mcp.resources.get_sqlite_store", return_value=store), \
             patch("src.mcp.resources.get_markdown_store", return_value=md_store):
            from src.mcp.resources import get_entry_metadata
            result = await get_entry_metadata(knowledge_id=str(entry_ids[0]))

        data = json.loads(result)
        assert data["title"] == "微信文章：AI 工程化"
        assert isinstance(data["tags"], list)  # 逗号字符串已转为列表

    @pytest.mark.asyncio
    async def test_tags_resource(self, populated_db):
        """pkv://tags 应返回 JSON 格式的标签列表。"""
        from unittest.mock import patch
        store, vault_dir, entry_ids = populated_db

        with patch("src.mcp.resources.get_sqlite_store", return_value=store):
            from src.mcp.resources import get_tags_resource
            result = await get_tags_resource()

        data = json.loads(result)
        assert data["total_tags"] > 0

    @pytest.mark.asyncio
    async def test_stats_resource(self, populated_db):
        """pkv://stats 应返回 JSON 格式的统计数据。"""
        from unittest.mock import patch
        store, vault_dir, entry_ids = populated_db

        with patch("src.mcp.resources.get_sqlite_store", return_value=store):
            from src.mcp.resources import get_stats_resource
            result = await get_stats_resource()

        data = json.loads(result)
        assert_stats_payload(data)
