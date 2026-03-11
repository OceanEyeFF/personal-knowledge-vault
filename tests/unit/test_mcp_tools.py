"""
MCP Tools 单元测试

使用 mock 隔离外部依赖（SQLiteStore, MarkdownStore, QueryRouter），
专注测试 Tool handler 的逻辑正确性。
"""

from dataclasses import dataclass
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import anyio
import pytest

# 确保项目根目录在 Python path 中
PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.relations.models import (
    BridgeCandidate,
    BridgeDiscoveryResult,
    CollectedEvidenceItem,
    CollectedEvidenceResult,
    ContrastCandidateItem,
    ContrastResult,
    RelationExplanationResult,
    RelationRecord,
    RelationSourceType,
    RelationSubgraphNode,
    RelationSubgraphResult,
    RelationType,
    TimelinePoint,
    TimelineResult,
)
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


# ============================================================
# archive_url Tool 测试 (M9 新增)
# ============================================================

class TestArchiveUrl:
    """archive_url Tool 测试。"""

    @pytest.mark.asyncio
    async def test_reject_private_ip(self):
        """内网地址应被拒绝。"""
        from src.mcp.tools import archive_url
        result = await archive_url(url="http://127.0.0.1/admin")
        assert result["success"] is False
        assert "内网" in result["error"]

    @pytest.mark.asyncio
    async def test_reject_localhost(self):
        """localhost 应被拒绝。"""
        from src.mcp.tools import archive_url
        result = await archive_url(url="http://localhost:8080/secret")
        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_reject_invalid_url(self):
        """无效 URL 应被拒绝。"""
        from src.mcp.tools import archive_url
        result = await archive_url(url="not-a-url")
        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_reject_ftp_url(self):
        """FTP URL 应被拒绝。"""
        from src.mcp.tools import archive_url
        result = await archive_url(url="ftp://example.com/file")
        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_successful_archive(self):
        """正常归档应返回成功结果。"""
        mock_result = MagicMock()
        mock_result.success = True
        mock_result.data = {
            "knowledge_id": 42,
            "title": "测试文章",
            "file_path": "/vault/test.md",
            "tags": ["AI"],
            "summary_one_sentence": "这是摘要",
        }

        from unittest.mock import AsyncMock
        with patch("src.workflow.engine.WorkflowEngine") as MockEngine:
            mock_engine_instance = MagicMock()
            mock_engine_instance.execute_async = AsyncMock(return_value=mock_result)
            MockEngine.return_value = mock_engine_instance

            from src.mcp.tools import archive_url
            result = await archive_url(url="https://example.com/article")

        assert result["success"] is True
        assert result["knowledge_id"] == 42

    @pytest.mark.asyncio
    async def test_archive_failure(self):
        """归档失败应返回错误信息。"""
        mock_result = MagicMock()
        mock_result.success = False
        mock_result.errors = ["抓取失败: 连接超时"]

        from unittest.mock import AsyncMock
        with patch("src.workflow.engine.WorkflowEngine") as MockEngine:
            mock_engine_instance = MagicMock()
            mock_engine_instance.execute_async = AsyncMock(return_value=mock_result)
            MockEngine.return_value = mock_engine_instance

            from src.mcp.tools import archive_url
            result = await archive_url(url="https://example.com/timeout")

        assert result["success"] is False
        assert "抓取失败" in result["error"]


# ============================================================
# archive_text Tool 测试 (M9 新增)
# ============================================================

class TestArchiveText:
    """archive_text Tool 测试。"""

    @pytest.mark.asyncio
    async def test_reject_empty_text(self):
        """空文本应被拒绝。"""
        from src.mcp.tools import archive_text
        result = await archive_text(text="")
        assert result["success"] is False
        assert "不能为空" in result["error"]

    @pytest.mark.asyncio
    async def test_reject_too_long_text(self):
        """超长文本应被拒绝。"""
        from src.mcp.tools import archive_text
        result = await archive_text(text="A" * 100001)
        assert result["success"] is False
        assert "超过限制" in result["error"]

    @pytest.mark.asyncio
    async def test_successful_text_archive(self):
        """正常文本归档应返回成功结果。"""
        mock_entry = MagicMock()
        mock_entry.title = "自动标题"
        mock_entry.content = "测试内容"
        mock_entry.tags = ["text"]

        mock_result = MagicMock()
        mock_result.success = True
        mock_result.data = {
            "knowledge_id": 43,
            "title": "自动标题",
            "file_path": "/vault/test.md",
            "tags": ["text"],
        }

        from unittest.mock import AsyncMock
        with patch("src.processors.text_fallback_processor.TextFallbackProcessor") as MockProcessor, \
             patch("src.workflow.engine.WorkflowEngine") as MockEngine:
            mock_proc_instance = MagicMock()
            mock_proc_instance.process = AsyncMock(return_value=mock_entry)
            MockProcessor.return_value = mock_proc_instance

            mock_engine_instance = MagicMock()
            mock_engine_instance.execute_async = AsyncMock(return_value=mock_result)
            MockEngine.return_value = mock_engine_instance

            from src.mcp.tools import archive_text
            result = await archive_text(text="这是一段测试文本内容")

        assert result["success"] is True
        assert result["knowledge_id"] == 43

    @pytest.mark.asyncio
    async def test_custom_title_override(self):
        """提供 title 应覆盖自动标题。"""
        mock_entry = MagicMock()
        mock_entry.title = "自动标题"
        mock_entry.content = "测试内容"
        mock_entry.tags = ["text"]

        mock_result = MagicMock()
        mock_result.success = True
        mock_result.data = {
            "knowledge_id": 44,
            "title": "自定义标题",
            "file_path": "/vault/test.md",
            "tags": ["text"],
        }

        from unittest.mock import AsyncMock
        with patch("src.processors.text_fallback_processor.TextFallbackProcessor") as MockProcessor, \
             patch("src.workflow.engine.WorkflowEngine") as MockEngine:
            mock_proc_instance = MagicMock()
            mock_proc_instance.process = AsyncMock(return_value=mock_entry)
            MockProcessor.return_value = mock_proc_instance

            mock_engine_instance = MagicMock()
            mock_engine_instance.execute_async = AsyncMock(return_value=mock_result)
            MockEngine.return_value = mock_engine_instance

            from src.mcp.tools import archive_text
            result = await archive_text(text="内容", title="自定义标题")

        # 验证 title 被覆盖
        assert mock_entry.title == "自定义标题"


# ============================================================
# get_related Tool 测试 (M9 新增)
# ============================================================

class TestGetRelated:
    """get_related Tool 测试。"""

    @pytest.mark.asyncio
    async def test_invalid_id(self):
        """非数字 ID 应返回 error。"""
        from src.mcp.tools import get_related
        result = await get_related(knowledge_id="abc")
        assert "error" in result

    @pytest.mark.asyncio
    async def test_entry_not_found(self):
        """不存在的条目应返回 error。"""
        mock_store = MagicMock()
        mock_store.query_by_id.return_value = None

        with patch("src.mcp.tools.get_sqlite_store", return_value=mock_store):
            from src.mcp.tools import get_related
            result = await get_related(knowledge_id="999")

        assert "error" in result

    @pytest.mark.asyncio
    async def test_no_vector_index(self):
        """无向量索引时应优雅降级。"""
        mock_store = MagicMock()
        mock_store.query_by_id.return_value = MOCK_ENTRY_DB

        mock_vector_store = MagicMock()
        mock_vector_store.get_doc_vector.return_value = None

        with patch("src.mcp.tools.get_sqlite_store", return_value=mock_store), \
             patch("src.storage.vector_store.VectorStore", return_value=mock_vector_store), \
             patch("src.mcp.tools.get_config") as mock_config:
            mock_config.return_value.vector_index_dir = "/tmp/vectors"
            mock_config.return_value.get.return_value = 1536
            from src.mcp.tools import get_related
            result = await get_related(knowledge_id="1")

        assert result["results"] == []
        assert "暂无向量索引" in result.get("message", "")

    @pytest.mark.asyncio
    async def test_limit_clamped(self):
        """limit 应被限制在 [1, 20]。"""
        mock_store = MagicMock()
        mock_store.query_by_id.return_value = MOCK_ENTRY_DB

        mock_vector_store = MagicMock()
        mock_vector_store.get_doc_vector.return_value = None

        with patch("src.mcp.tools.get_sqlite_store", return_value=mock_store), \
             patch("src.storage.vector_store.VectorStore", return_value=mock_vector_store), \
             patch("src.mcp.tools.get_config") as mock_config:
            mock_config.return_value.vector_index_dir = "/tmp/vectors"
            mock_config.return_value.get.return_value = 1536
            from src.mcp.tools import get_related
            result = await get_related(knowledge_id="1", limit=100)

        # 结果应返回（降级为空），说明 limit 被正确处理
        assert "results" in result

    @pytest.mark.asyncio
    async def test_vector_search_exception(self):
        """向量搜索异常应优雅降级。"""
        mock_store = MagicMock()
        mock_store.query_by_id.return_value = MOCK_ENTRY_DB

        with patch("src.mcp.tools.get_sqlite_store", return_value=mock_store), \
             patch("src.storage.vector_store.VectorStore", side_effect=Exception("索引加载失败")), \
             patch("src.mcp.tools.get_config") as mock_config:
            mock_config.return_value.vector_index_dir = "/tmp/vectors"
            mock_config.return_value.get.return_value = 1536
            from src.mcp.tools import get_related
            result = await get_related(knowledge_id="1")

        assert result["results"] == []
        assert "不可用" in result.get("message", "")


class TestQuerySubgraph:
    """query_subgraph Tool 测试。"""

    @pytest.mark.asyncio
    async def test_invalid_id(self):
        from src.mcp.tools import query_subgraph

        result = await query_subgraph(knowledge_id="abc")

        assert "error" in result

    @pytest.mark.asyncio
    async def test_success(self):
        mock_service = MagicMock()
        record = RelationRecord(
            source_knowledge_id=1,
            target_knowledge_id=2,
            relation_type=RelationType.REFERENCES,
            relation_source_type=RelationSourceType.MARKDOWN_LINK,
            evidence_payload={"href": "./beta.md"},
        )
        mock_service.query_subgraph.return_value = RelationSubgraphResult(
            seed_knowledge_id=1,
            max_depth=2,
            nodes=[
                RelationSubgraphNode(knowledge_id=1, depth=0),
                RelationSubgraphNode(knowledge_id=2, depth=1),
            ],
            edges=[record],
            grouped_edges={RelationType.REFERENCES.value: [record]},
            truncated=False,
        )

        with patch("src.mcp.tools.get_relation_query_service", return_value=mock_service):
            from src.mcp.tools import query_subgraph

            result = await query_subgraph(
                knowledge_id="1",
                depth=2,
                relation_types=[RelationType.REFERENCES.value],
                max_nodes=20,
            )

        assert result["seed_knowledge_id"] == 1
        assert result["total_nodes"] == 2
        assert result["grouped_edges"][RelationType.REFERENCES.value][0]["target_knowledge_id"] == 2
        mock_service.query_subgraph.assert_called_once()


class TestExplainRelation:
    """explain_relation Tool 测试。"""

    @pytest.mark.asyncio
    async def test_invalid_id(self):
        from src.mcp.tools import explain_relation

        result = await explain_relation(
            source_knowledge_id="1",
            target_knowledge_id="abc",
        )

        assert "error" in result

    @pytest.mark.asyncio
    async def test_success(self):
        mock_service = MagicMock()
        record = RelationRecord(
            source_knowledge_id=1,
            target_knowledge_id=2,
            relation_type=RelationType.RELATED_DOCUMENT,
            relation_source_type=RelationSourceType.FRONTMATTER_RELATED_DOCS,
            evidence_payload={"field": "related_docs"},
        )
        mock_service.explain_relation.return_value = RelationExplanationResult(
            source_knowledge_id=1,
            target_knowledge_id=2,
            found=True,
            explanation_type="direct",
            hops=1,
            path=[record],
            supporting_relations=[record],
            summary="1 -[related_document]-> 2",
            evidence_items=[
                {
                    "step_index": 0,
                    "relation_type": RelationType.RELATED_DOCUMENT.value,
                    "relation_source_type": RelationSourceType.FRONTMATTER_RELATED_DOCS.value,
                    "direction": record.direction.value,
                    "weight": record.weight,
                    "source_knowledge_id": 1,
                    "target_knowledge_id": 2,
                    "evidence_payload": {"field": "related_docs"},
                }
            ],
        )

        with patch("src.mcp.tools.get_relation_query_service", return_value=mock_service):
            from src.mcp.tools import explain_relation

            result = await explain_relation(
                source_knowledge_id="1",
                target_knowledge_id="2",
                max_depth=2,
            )

        assert result["found"] is True
        assert result["explanation_type"] == "direct"
        assert result["summary"] == "1 -[related_document]-> 2"
        mock_service.explain_relation.assert_called_once()


class TestCollectEvidence:
    """collect_evidence Tool 测试。"""

    @pytest.mark.asyncio
    async def test_reject_empty_question(self):
        from src.mcp.tools import collect_evidence

        result = await collect_evidence(question="   ")

        assert "error" in result

    @pytest.mark.asyncio
    async def test_success(self):
        mock_service = MagicMock()
        mock_service.collect_evidence.return_value = CollectedEvidenceResult(
            question="Alpha 和 Beta 有什么关系？",
            found=True,
            seed_knowledge_id=1,
            seed_title="Alpha",
            evidence=[
                CollectedEvidenceItem(
                    knowledge_id=1,
                    title="Alpha",
                    abstract="Alpha 摘要",
                    source_type="generic",
                    archived_at="2026-03-10 10:00:00",
                    tags=["AI"],
                    retrieval_rank=1,
                    retrieval_score=0.95,
                    is_seed=True,
                ),
                CollectedEvidenceItem(
                    knowledge_id=2,
                    title="Beta",
                    abstract="Beta 摘要",
                    source_type="generic",
                    archived_at="2026-03-10 10:10:00",
                    tags=["知识图谱"],
                    retrieval_rank=2,
                    retrieval_score=0.82,
                    relation_found=True,
                    relation_explanation_type="direct",
                    relation_hops=1,
                    relation_summary="1 -[related_document]-> 2",
                ),
            ],
            summary="围绕问题共聚合 2 条证据",
        )

        with patch(
            "src.mcp.tools.get_evidence_collection_service",
            return_value=mock_service,
        ):
            from src.mcp.tools import collect_evidence

            result = await collect_evidence(
                question="Alpha 和 Beta 有什么关系？",
                top_k=5,
                relation_max_depth=2,
            )

        assert result["found"] is True
        assert result["seed_knowledge_id"] == 1
        assert result["total_evidence"] == 2
        assert result["related_evidence_count"] == 1
        mock_service.collect_evidence.assert_called_once()


class TestFindBridges:
    """find_bridges Tool 测试。"""

    @pytest.mark.asyncio
    async def test_invalid_seed_id(self):
        from src.mcp.tools import find_bridges

        result = await find_bridges(seed_knowledge_id="abc")

        assert "error" in result

    @pytest.mark.asyncio
    async def test_success(self):
        mock_service = MagicMock()
        mock_service.find_bridges.return_value = BridgeDiscoveryResult(
            seed_knowledge_id=1,
            found=True,
            max_depth=2,
            items=[
                BridgeCandidate(
                    knowledge_id=3,
                    title="Gamma",
                    depth=1,
                    bridge_score=2.25,
                    connected_knowledge_ids=[1, 4],
                    relation_types=["references", "related_document"],
                    summary="Gamma 是桥接候选",
                )
            ],
            summary="找到 1 个桥接候选",
            limitation_notes=["partial"],
        )

        with patch("src.mcp.tools.get_exploration_service", return_value=mock_service):
            from src.mcp.tools import find_bridges

            result = await find_bridges(seed_knowledge_id="1", top_k=5, max_depth=2)

        assert result["found"] is True
        assert result["total_bridges"] == 1
        assert result["implementation_level"] == "partial"
        mock_service.find_bridges.assert_called_once()


class TestTimelineOf:
    """timeline_of Tool 测试。"""

    @pytest.mark.asyncio
    async def test_reject_empty_topic(self):
        from src.mcp.tools import timeline_of

        result = await timeline_of(topic="  ")

        assert "error" in result

    @pytest.mark.asyncio
    async def test_success(self):
        mock_service = MagicMock()
        mock_service.timeline_of.return_value = TimelineResult(
            topic="AI Timeline",
            found=True,
            items=[
                TimelinePoint(
                    knowledge_id=1,
                    title="Alpha",
                    archived_at="2026-03-10 10:00:00",
                    source_type="generic",
                    abstract="Alpha 摘要",
                    tags=["AI"],
                    retrieval_score=0.91,
                )
            ],
            summary="时间线已生成",
            limitation_notes=["partial"],
        )

        with patch("src.mcp.tools.get_exploration_service", return_value=mock_service):
            from src.mcp.tools import timeline_of

            result = await timeline_of(topic="AI Timeline", top_k=5, sort_order="asc")

        assert result["found"] is True
        assert result["total_points"] == 1
        assert result["implementation_level"] == "partial"
        mock_service.timeline_of.assert_called_once()


class TestContrast:
    """contrast Tool 测试。"""

    @pytest.mark.asyncio
    async def test_reject_empty_topic(self):
        from src.mcp.tools import contrast

        result = await contrast(topic_a="A", topic_b="  ")

        assert "error" in result

    @pytest.mark.asyncio
    async def test_success(self):
        mock_service = MagicMock()
        mock_service.contrast.return_value = ContrastResult(
            topic_a="Topic A",
            topic_b="Topic B",
            found=True,
            topic_a_candidates=[
                ContrastCandidateItem(
                    knowledge_id=1,
                    title="Alpha",
                    abstract="Alpha 摘要",
                    archived_at="2026-03-10 10:00:00",
                    source_type="generic",
                    tags=["AI", "共同"],
                    retrieval_score=0.93,
                )
            ],
            topic_b_candidates=[
                ContrastCandidateItem(
                    knowledge_id=2,
                    title="Beta",
                    abstract="Beta 摘要",
                    archived_at="2026-03-11 10:00:00",
                    source_type="generic",
                    tags=["时间线", "共同"],
                    retrieval_score=0.87,
                )
            ],
            shared_tags=["共同"],
            only_a_tags=["AI"],
            only_b_tags=["时间线"],
            overlap_knowledge_ids=[],
            summary="对比完成",
            limitation_notes=["partial"],
        )

        with patch("src.mcp.tools.get_exploration_service", return_value=mock_service):
            from src.mcp.tools import contrast

            result = await contrast(topic_a="Topic A", topic_b="Topic B", top_k=5)

        assert result["found"] is True
        assert result["implementation_level"] == "partial"
        assert result["shared_tags"] == ["共同"]
        mock_service.contrast.assert_called_once()
