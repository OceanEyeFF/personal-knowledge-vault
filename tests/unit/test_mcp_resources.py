"""
MCP Resources 单元测试

使用 mock 隔离外部依赖（SQLiteStore, MarkdownStore），
专注测试 Resource handler 的逻辑正确性。
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch
from dataclasses import dataclass

import pytest

# 确保项目根目录在 Python path 中
PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


# Mock 数据

MOCK_ENTRY_DB = {
    "knowledge_id": 1,
    "title": "测试微信文章",
    "summary_one_sentence": "这是一句话摘要",
    "tags": "AI,NLP",
    "keywords": "人工智能,自然语言",
    "source_type": "wechat",
    "source_url": "https://mp.weixin.qq.com/s/test",
    "archived_at": "2026-02-18",
    "word_count": 500,
    "file_path": "/vault/wechat/2026/02/20260218-test.md",
}


@dataclass
class MockEntry:
    """Mock Entry 对象。"""
    content: str = "# 测试文章\n\n这是全文内容"
    title: str = "测试微信文章"


class TestGetEntryContent:
    """pkv://entries/{knowledge_id} Resource 测试。"""

    @pytest.mark.asyncio
    async def test_found_entry(self):
        """正常条目应返回 Markdown 全文。"""
        mock_store = MagicMock()
        mock_store.query_by_id.return_value = MOCK_ENTRY_DB
        mock_md_store = MagicMock()
        mock_md_store.load.return_value = MockEntry()

        with patch("src.mcp.resources.get_sqlite_store", return_value=mock_store), \
             patch("src.mcp.resources.get_markdown_store", return_value=mock_md_store):
            from src.mcp.resources import get_entry_content
            result = await get_entry_content(knowledge_id="1")

        assert "全文内容" in result

    @pytest.mark.asyncio
    async def test_not_found(self):
        """未找到条目应返回友好提示。"""
        mock_store = MagicMock()
        mock_store.query_by_id.return_value = None

        with patch("src.mcp.resources.get_sqlite_store", return_value=mock_store):
            from src.mcp.resources import get_entry_content
            result = await get_entry_content(knowledge_id="999")

        assert "未找到条目" in result

    @pytest.mark.asyncio
    async def test_invalid_id(self):
        """非数字 ID 应返回错误信息。"""
        from src.mcp.resources import get_entry_content
        result = await get_entry_content(knowledge_id="abc")
        assert "错误" in result or "无效" in result

    @pytest.mark.asyncio
    async def test_file_not_found(self):
        """Markdown 文件不存在应优雅降级。"""
        mock_store = MagicMock()
        mock_store.query_by_id.return_value = MOCK_ENTRY_DB
        mock_md_store = MagicMock()
        mock_md_store.load.side_effect = FileNotFoundError()

        with patch("src.mcp.resources.get_sqlite_store", return_value=mock_store), \
             patch("src.mcp.resources.get_markdown_store", return_value=mock_md_store):
            from src.mcp.resources import get_entry_content
            result = await get_entry_content(knowledge_id="1")

        assert "不存在" in result

    @pytest.mark.asyncio
    async def test_no_file_path(self):
        """file_path 为空应返回路径缺失提示。"""
        entry_no_path = {**MOCK_ENTRY_DB, "file_path": ""}
        mock_store = MagicMock()
        mock_store.query_by_id.return_value = entry_no_path

        with patch("src.mcp.resources.get_sqlite_store", return_value=mock_store):
            from src.mcp.resources import get_entry_content
            result = await get_entry_content(knowledge_id="1")

        assert "缺失" in result


class TestGetEntryMetadata:
    """pkv://entries/{knowledge_id}/metadata Resource 测试。"""

    @pytest.mark.asyncio
    async def test_found_entry(self):
        """正常条目应返回 JSON 格式的元数据。"""
        mock_store = MagicMock()
        mock_store.query_by_id.return_value = MOCK_ENTRY_DB

        with patch("src.mcp.resources.get_sqlite_store", return_value=mock_store):
            from src.mcp.resources import get_entry_metadata
            result = await get_entry_metadata(knowledge_id="1")

        data = json.loads(result)
        assert data["knowledge_id"] == 1
        assert data["title"] == "测试微信文章"
        assert "file_path" not in data
        # tags 应被转换为列表
        assert data["tags"] == ["AI", "NLP"]
        assert data["keywords"] == ["人工智能", "自然语言"]

    @pytest.mark.asyncio
    async def test_not_found(self):
        """未找到条目应返回 JSON 错误。"""
        mock_store = MagicMock()
        mock_store.query_by_id.return_value = None

        with patch("src.mcp.resources.get_sqlite_store", return_value=mock_store):
            from src.mcp.resources import get_entry_metadata
            result = await get_entry_metadata(knowledge_id="999")

        data = json.loads(result)
        assert "error" in data


class TestCitationResources:
    """精确 citation Resource handler 测试。"""

    @pytest.mark.asyncio
    async def test_chunk_resource_returns_canonical_locator(self):
        mock_store = MagicMock()
        mock_store.get_chunk_by_id.return_value = {
            "chunk_id": 101,
            "knowledge_id": 1,
            "chunk_index": 0,
            "chunk_text": "精确片段",
        }

        with patch("src.mcp.resources.get_sqlite_store", return_value=mock_store):
            from src.mcp.resources import get_entry_chunk
            result = await get_entry_chunk(knowledge_id="1", chunk_id="101")

        data = json.loads(result)
        assert data["chunk_text"] == "精确片段"
        assert data["citation_locator"] == "pkv://entries/1/chunks/101"

    @pytest.mark.asyncio
    async def test_metadata_field_resource_rejects_non_timeline_field(self):
        mock_store = MagicMock()
        mock_store.query_by_id.return_value = MOCK_ENTRY_DB

        with patch("src.mcp.resources.get_sqlite_store", return_value=mock_store):
            from src.mcp.resources import get_entry_metadata_field
            with pytest.raises(ValueError, match="不支持"):
                await get_entry_metadata_field(
                    knowledge_id="1",
                    field_name="title",
                )

    @pytest.mark.asyncio
    async def test_metadata_field_requires_persisted_legacy_value(self):
        mock_store = MagicMock()
        mock_store.query_by_id.return_value = {
            **MOCK_ENTRY_DB,
            "file_path": "missing-legacy.md",
        }

        with patch("src.mcp.resources.get_sqlite_store", return_value=mock_store):
            from src.mcp.resources import get_entry_metadata_field
            with pytest.raises(ValueError, match="不存在元数据字段"):
                await get_entry_metadata_field(
                    knowledge_id="1",
                    field_name="published_time",
                )

    @pytest.mark.asyncio
    async def test_invalid_id(self):
        """非数字 ID 应返回 JSON 错误。"""
        from src.mcp.resources import get_entry_metadata
        result = await get_entry_metadata(knowledge_id="abc")
        data = json.loads(result)
        assert "error" in data


class TestGetTagsResource:
    """pkv://tags Resource 测试。"""

    @pytest.mark.asyncio
    async def test_tags_resource(self):
        """应返回 JSON 格式的标签列表。"""
        mock_store = MagicMock()
        mock_store.get_all_tags_with_count.return_value = [
            {"name": "AI", "count": 10},
            {"name": "NLP", "count": 5},
        ]

        with patch("src.mcp.resources.get_sqlite_store", return_value=mock_store):
            from src.mcp.resources import get_tags_resource
            result = await get_tags_resource()

        data = json.loads(result)
        assert data["total_tags"] == 2
        assert data["tags"][0]["name"] == "AI"

    @pytest.mark.asyncio
    async def test_empty_tags(self):
        """空标签应返回空列表。"""
        mock_store = MagicMock()
        mock_store.get_all_tags_with_count.return_value = []

        with patch("src.mcp.resources.get_sqlite_store", return_value=mock_store):
            from src.mcp.resources import get_tags_resource
            result = await get_tags_resource()

        data = json.loads(result)
        assert data["total_tags"] == 0


class TestGetStatsResource:
    """pkv://stats Resource 测试。"""

    @pytest.mark.asyncio
    async def test_stats_resource(self):
        """应返回 JSON 格式的统计数据。"""
        mock_stats = {
            "total_entries": 100,
            "total_tags": 20,
            "source_types": {"wechat": 50, "zhihu": 30},
        }
        mock_store = MagicMock()
        mock_store.get_statistics.return_value = mock_stats

        with patch("src.mcp.resources.get_sqlite_store", return_value=mock_store):
            from src.mcp.resources import get_stats_resource
            result = await get_stats_resource()

        data = json.loads(result)
        assert data["total_entries"] == 100
        assert data["total_tags"] == 20
