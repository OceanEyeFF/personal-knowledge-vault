"""
MCP Tools 单元测试

使用 mock 隔离外部依赖（SQLiteStore, MarkdownStore, QueryRouter），
专注测试 Tool handler 的逻辑正确性。
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch
from dataclasses import dataclass

import pytest
import anyio

# 确保项目根目录在 Python path 中
PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.mcp.utils import parse_tags_string, serialize_search_result, clamp_param
from src.retrieval.result import SearchResult


# ============================================================
# utils.py 测试
# ============================================================

class TestParseTagsString:
    """parse_tags_string 测试。"""

    def test_normal_string(self):
        assert parse_tags_string("AI,NLP,知识管理") == ["AI", "NLP", "知识管理"]

    def test_empty_string(self):
        assert parse_tags_string("") == []

    def test_none(self):
        assert parse_tags_string(None) == []

    def test_already_list(self):
        assert parse_tags_string(["AI", "NLP"]) == ["AI", "NLP"]

    def test_whitespace_trimming(self):
        assert parse_tags_string("AI , NLP , ML") == ["AI", "NLP", "ML"]

    def test_trailing_comma(self):
        assert parse_tags_string("AI,NLP,") == ["AI", "NLP"]

    def test_single_tag(self):
        assert parse_tags_string("AI") == ["AI"]


class TestSerializeSearchResult:
    """serialize_search_result 测试。"""

    def test_normal_result(self):
        result = SearchResult(
            knowledge_id=1,
            title="测试文章",
            score=0.95,
            highlight="这是摘要",
            metadata={
                "tags": "AI,NLP",
                "source_type": "wechat",
                "archived_at": "2026-02-18",
            },
        )
        serialized = serialize_search_result(result)
        assert serialized["knowledge_id"] == 1
        assert serialized["title"] == "测试文章"
        assert serialized["abstract"] == "这是摘要"  # highlight → abstract
        assert serialized["score"] == 0.95
        assert serialized["tags"] == ["AI", "NLP"]  # 逗号字符串 → 列表
        assert serialized["source_type"] == "wechat"

    def test_empty_metadata(self):
        result = SearchResult(
            knowledge_id=2,
            title="空元数据",
            score=0.5,
            highlight="",
            metadata={},
        )
        serialized = serialize_search_result(result)
        assert serialized["tags"] == []
        assert serialized["source_type"] == ""


class TestClampParam:
    """clamp_param 测试。"""

    def test_within_range(self):
        assert clamp_param(5, 1, 50) == 5

    def test_below_min(self):
        assert clamp_param(0, 1, 50) == 1

    def test_above_max(self):
        assert clamp_param(100, 1, 50) == 50

    def test_at_boundary(self):
        assert clamp_param(1, 1, 50) == 1
        assert clamp_param(50, 1, 50) == 50


# ============================================================
# Tool handler 测试
# ============================================================

# Mock 数据

MOCK_ENTRY_DB = {
    "knowledge_id": 1,
    "title": "测试微信文章",
    "summary_one_sentence": "这是一句话摘要",
    "summary_100_words": "这是百字摘要",
    "tags": "AI,NLP",
    "keywords": "人工智能,自然语言",
    "source_type": "wechat",
    "source_url": "https://mp.weixin.qq.com/s/test",
    "archived_at": "2026-02-18",
    "word_count": 500,
    "file_path": "/vault/wechat/2026/02/20260218-test.md",
}

MOCK_SEARCH_RESULTS = [
    SearchResult(
        knowledge_id=1,
        title="AI 文章",
        score=0.9,
        highlight="人工智能概述",
        metadata={"tags": "AI,ML", "source_type": "wechat", "archived_at": "2026-02-18"},
    ),
    SearchResult(
        knowledge_id=2,
        title="NLP 指南",
        score=0.7,
        highlight="自然语言处理入门",
        metadata={"tags": "NLP,AI", "source_type": "zhihu", "archived_at": "2026-02-17"},
    ),
]


@dataclass
class MockEntry:
    """Mock Entry 对象，模拟 MarkdownStore.load() 返回。"""
    content: str = "# 测试文章\n\n这是全文内容"
    title: str = "测试微信文章"


class TestSearchKnowledge:
    """search_knowledge Tool 测试。"""

    @pytest.mark.asyncio
    async def test_auto_strategy(self):
        """auto 策略应调用 QueryRouter.search()。"""
        mock_router = MagicMock()
        mock_router.search.return_value = MOCK_SEARCH_RESULTS

        with patch("src.mcp.tools.get_query_router", return_value=mock_router):
            from src.mcp.tools import search_knowledge
            result = await search_knowledge(query="AI", strategy="auto", top_k=5)

        assert result["total"] == 2
        assert result["strategy_used"] == "auto"
        assert len(result["results"]) == 2
        assert result["results"][0]["title"] == "AI 文章"
        assert result["results"][0]["abstract"] == "人工智能概述"
        assert result["results"][0]["tags"] == ["AI", "ML"]
        mock_router.search.assert_called_once_with("AI", limit=5)

    @pytest.mark.asyncio
    async def test_bm25_strategy(self):
        """bm25 策略应直接实例化 BM25Retriever。"""
        mock_retriever = MagicMock()
        mock_retriever.search.return_value = MOCK_SEARCH_RESULTS[:1]

        # BM25Retriever 是在 _impl 内部延迟导入的，需要 mock 原始模块
        with patch("src.retrieval.bm25_retriever.BM25Retriever", return_value=mock_retriever):
            from src.mcp.tools import search_knowledge
            result = await search_knowledge(query="AI", strategy="bm25", top_k=3)

        assert result["total"] == 1
        assert result["strategy_used"] == "bm25"

    @pytest.mark.asyncio
    async def test_invalid_strategy(self):
        """无效策略应返回错误。"""
        from src.mcp.tools import search_knowledge
        result = await search_knowledge(query="AI", strategy="invalid")
        assert "error" in result

    @pytest.mark.asyncio
    async def test_top_k_clamped(self):
        """top_k 应被限制在 [1, 50] 范围内。"""
        mock_router = MagicMock()
        mock_router.search.return_value = []

        with patch("src.mcp.tools.get_query_router", return_value=mock_router):
            from src.mcp.tools import search_knowledge
            await search_knowledge(query="AI", top_k=200)

        mock_router.search.assert_called_once_with("AI", limit=50)

    @pytest.mark.asyncio
    async def test_source_type_filter(self):
        """source_type 过滤应正确工作。"""
        mock_router = MagicMock()
        mock_router.search.return_value = MOCK_SEARCH_RESULTS

        with patch("src.mcp.tools.get_query_router", return_value=mock_router):
            from src.mcp.tools import search_knowledge
            result = await search_knowledge(query="AI", source_type="wechat")

        assert result["total"] == 1
        assert result["results"][0]["source_type"] == "wechat"

    @pytest.mark.asyncio
    async def test_tag_filter(self):
        """tag 过滤应正确工作。"""
        mock_router = MagicMock()
        mock_router.search.return_value = MOCK_SEARCH_RESULTS

        with patch("src.mcp.tools.get_query_router", return_value=mock_router):
            from src.mcp.tools import search_knowledge
            result = await search_knowledge(query="AI", tag="ML")

        assert result["total"] == 1
        assert "ML" in result["results"][0]["tags"]


class TestGetEntry:
    """get_entry Tool 测试。"""

    @pytest.mark.asyncio
    async def test_found_entry(self):
        """正常查找条目应返回完整信息。"""
        mock_store = MagicMock()
        mock_store.query_by_id.return_value = MOCK_ENTRY_DB
        mock_md_store = MagicMock()
        mock_md_store.load.return_value = MockEntry()

        with patch("src.mcp.tools.get_sqlite_store", return_value=mock_store), \
             patch("src.mcp.tools.get_markdown_store", return_value=mock_md_store):
            from src.mcp.tools import get_entry
            result = await get_entry(knowledge_id="1")

        assert result["knowledge_id"] == 1
        assert result["title"] == "测试微信文章"
        assert result["abstract"] == "这是一句话摘要"
        assert result["tags"] == ["AI", "NLP"]
        assert result["keywords"] == ["人工智能", "自然语言"]
        assert "全文内容" in result["content"]

    @pytest.mark.asyncio
    async def test_not_found(self):
        """未找到条目应返回 error。"""
        mock_store = MagicMock()
        mock_store.query_by_id.return_value = None

        with patch("src.mcp.tools.get_sqlite_store", return_value=mock_store):
            from src.mcp.tools import get_entry
            result = await get_entry(knowledge_id="999")

        assert "error" in result

    @pytest.mark.asyncio
    async def test_invalid_id(self):
        """非数字 ID 应返回 error。"""
        from src.mcp.tools import get_entry
        result = await get_entry(knowledge_id="abc")
        assert "error" in result

    @pytest.mark.asyncio
    async def test_markdown_not_found(self):
        """Markdown 文件不存在应优雅降级。"""
        mock_store = MagicMock()
        mock_store.query_by_id.return_value = MOCK_ENTRY_DB
        mock_md_store = MagicMock()
        mock_md_store.load.side_effect = FileNotFoundError("文件不存在")

        with patch("src.mcp.tools.get_sqlite_store", return_value=mock_store), \
             patch("src.mcp.tools.get_markdown_store", return_value=mock_md_store):
            from src.mcp.tools import get_entry
            result = await get_entry(knowledge_id="1")

        assert result["title"] == "测试微信文章"
        assert "不存在" in result["content"]


class TestListTags:
    """list_tags Tool 测试。"""

    @pytest.mark.asyncio
    async def test_list_tags(self):
        """应返回标签列表及计数。"""
        mock_store = MagicMock()
        mock_store.get_all_tags_with_count.return_value = [
            {"name": "AI", "count": 10},
            {"name": "NLP", "count": 5},
        ]

        with patch("src.mcp.tools.get_sqlite_store", return_value=mock_store):
            from src.mcp.tools import list_tags
            result = await list_tags()

        assert result["total_tags"] == 2
        assert result["tags"][0]["name"] == "AI"
        assert result["tags"][0]["count"] == 10

    @pytest.mark.asyncio
    async def test_empty_tags(self):
        """空知识库应返回空列表。"""
        mock_store = MagicMock()
        mock_store.get_all_tags_with_count.return_value = []

        with patch("src.mcp.tools.get_sqlite_store", return_value=mock_store):
            from src.mcp.tools import list_tags
            result = await list_tags()

        assert result["total_tags"] == 0
        assert result["tags"] == []


class TestListEntries:
    """list_entries Tool 测试。"""

    @pytest.mark.asyncio
    async def test_default_pagination(self):
        """默认分页应返回正确结构。"""
        mock_store = MagicMock()
        mock_store.list_entries.return_value = [MOCK_ENTRY_DB]
        mock_store.count_entries.return_value = 1

        with patch("src.mcp.tools.get_sqlite_store", return_value=mock_store):
            from src.mcp.tools import list_entries
            result = await list_entries()

        assert result["total"] == 1
        assert result["page"] == 1
        assert result["per_page"] == 20
        assert len(result["entries"]) == 1
        assert result["entries"][0]["tags"] == ["AI", "NLP"]

    @pytest.mark.asyncio
    async def test_per_page_clamped(self):
        """per_page 应被限制在 [1, 100] 范围内。"""
        mock_store = MagicMock()
        mock_store.list_entries.return_value = []
        mock_store.count_entries.return_value = 0

        with patch("src.mcp.tools.get_sqlite_store", return_value=mock_store):
            from src.mcp.tools import list_entries
            await list_entries(per_page=200)

        # 验证 limit 被限制为 100
        call_args = mock_store.list_entries.call_args
        assert call_args.kwargs.get("limit") == 100 or call_args[1].get("limit") == 100

    @pytest.mark.asyncio
    async def test_invalid_sort_by(self):
        """无效排序字段应返回 error。"""
        mock_store = MagicMock()
        mock_store.list_entries.side_effect = ValueError("无效的排序字段: invalid_field")

        with patch("src.mcp.tools.get_sqlite_store", return_value=mock_store):
            from src.mcp.tools import list_entries
            result = await list_entries(sort_by="invalid_field")

        assert "error" in result

    @pytest.mark.asyncio
    async def test_total_pages_calculation(self):
        """总页数应正确计算。"""
        mock_store = MagicMock()
        mock_store.list_entries.return_value = [MOCK_ENTRY_DB] * 10
        mock_store.count_entries.return_value = 25

        with patch("src.mcp.tools.get_sqlite_store", return_value=mock_store):
            from src.mcp.tools import list_entries
            result = await list_entries(per_page=10)

        assert result["total_pages"] == 3  # ceil(25/10) = 3


class TestGetStats:
    """get_stats Tool 测试。"""

    @pytest.mark.asyncio
    async def test_get_stats(self):
        """应返回统计信息。"""
        mock_stats = {
            "total_entries": 100,
            "source_types": [("wechat", 50), ("zhihu", 30)],
            "total_tags": 20,
        }
        mock_store = MagicMock()
        mock_store.get_statistics.return_value = mock_stats

        with patch("src.mcp.tools.get_sqlite_store", return_value=mock_store):
            from src.mcp.tools import get_stats
            result = await get_stats()

        assert result["total_entries"] == 100
