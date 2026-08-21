"""
MCP Tools 单元测试

使用 mock 隔离外部依赖（KnowledgeApplication、SQLiteStore、MarkdownStore），
专注测试 Tool handler 的逻辑正确性。
"""

from dataclasses import dataclass
import logging
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# 确保项目根目录在 Python path 中
PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.relations.models import (  # noqa: E402
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
from src.relations.citations import (  # noqa: E402
    build_chunk_locator,
    build_entry_locator,
    build_entry_metadata_locator,
)
from src.relations.exploration_service import ExplorationService  # noqa: E402
from src.mcp.utils import (  # noqa: E402
    parse_tags_string,
    serialize_search_result,
    clamp_param,
)
from src.retrieval.result import (  # noqa: E402
    RetrievalIssue,
    SearchResponse,
    SearchResult,
)
from src.runtime.errors import ErrorCode, PKVRuntimeError  # noqa: E402
from src.storage.markdown_store import Entry, MarkdownStore  # noqa: E402
from src.workflow.models import WorkflowResult  # noqa: E402


class _DiagnosticAccessBombResult:
    """Workflow-like result whose diagnostics must remain unread on contract failure."""

    def __init__(self, *, terminal, data):
        self.success = terminal != "error"
        self.terminal = terminal
        self.data = data
        self.diagnostic_accesses = []

    @property
    def errors(self):
        self.diagnostic_accesses.append("errors")
        raise AssertionError("must not read workflow errors")

    @property
    def warnings(self):
        self.diagnostic_accesses.append("warnings")
        raise AssertionError("must not read workflow warnings")

    @property
    def issues(self):
        self.diagnostic_accesses.append("issues")
        raise AssertionError("must not read workflow issues")


BRIDGE_EVIDENCE_SOURCES = [
    "relation_subgraph",
    "graph_bridge_signal",
    "entry_tags",
    "entry_title_summary",
]
TIMELINE_EVIDENCE_SOURCES = [
    "query_results",
    "entry_metadata",
    "structured_time_fields",
]
CONTRAST_EVIDENCE_SOURCES = [
    "query_results",
    "relation_graph",
    "entry_tags",
    "entry_summary",
]


def _empty_contrast_dimensions(
    candidates_a=None,
    candidates_b=None,
):
    candidates_a = list(candidates_a or [])
    candidates_b = list(candidates_b or [])
    tags_a = {tag for item in candidates_a for tag in item.tags}
    tags_b = {tag for item in candidates_b for tag in item.tags}
    shared_tags = sorted(tags_a & tags_b)
    only_a_tags = sorted(tags_a - tags_b)
    only_b_tags = sorted(tags_b - tags_a)
    overlap_ids = sorted(
        {item.knowledge_id for item in candidates_a}
        & {item.knowledge_id for item in candidates_b}
    )
    relation_summary = {
        "connected_candidate_pairs_count": 0,
        "topic_a_connected_candidate_count": 0,
        "topic_b_connected_candidate_count": 0,
        "shared_relation_types": [],
        "max_relation_hops": 0,
    }
    return {
        "shared_tags_count": len(shared_tags),
        "topic_a_only_tags_count": len(only_a_tags),
        "topic_b_only_tags_count": len(only_b_tags),
        "overlap_knowledge_count": len(overlap_ids),
        "candidate_count": {
            "topic_a": len(candidates_a),
            "topic_b": len(candidates_b),
        },
        "relation_graph_signal": relation_summary,
        "provenance": ExplorationService._build_contrast_provenance(
            candidates_a=candidates_a,
            candidates_b=candidates_b,
            shared_tags=shared_tags,
            only_a_tags=only_a_tags,
            only_b_tags=only_b_tags,
            overlap_knowledge_ids=overlap_ids,
            relation_pairs=[],
        ),
    }


def _valid_bridge_result():
    seed_edge = RelationRecord(
        relation_id=7,
        source_knowledge_id=1,
        target_knowledge_id=3,
        relation_type=RelationType.REFERENCES,
        relation_source_type=RelationSourceType.MARKDOWN_LINK,
        evidence_payload={"raw_target": "gamma.md"},
    )
    frontier_edge = RelationRecord(
        relation_id=8,
        source_knowledge_id=3,
        target_knowledge_id=4,
        relation_type=RelationType.RELATED_DOCUMENT,
        relation_source_type=RelationSourceType.FRONTMATTER_RELATED_DOCS,
        evidence_payload={"field": "related_docs"},
    )
    explanation = MagicMock(found=True, path=[seed_edge])
    subgraph_edges = [seed_edge, frontier_edge]
    semantic_inputs = {
        "fields_used": [
            "title",
            "summary_one_sentence",
            "summary_100_words",
            "tags",
        ],
        "candidate": {
            "knowledge_id": 3,
            "citation_locator": build_entry_locator(3),
            "metadata_locator": build_entry_metadata_locator(3),
            "token_count": 0,
        },
        "comparisons": [],
        "anchor_score": 0.0,
        "support_score": 0.0,
        "coverage_score": 0.0,
        "semantic_score": 0.0,
    }
    node_depths = {1: 0, 3: 1, 4: 2}
    adjacency = {1: {3}, 3: {1, 4}, 4: {3}}
    supporting_subgraph = ExplorationService._build_bridge_supporting_subgraph(
        seed_knowledge_id=1,
        candidate_knowledge_id=3,
        neighbors={1, 4},
        adjacency=adjacency,
        node_depth_map=node_depths,
        subgraph_edges=subgraph_edges,
        max_depth=2,
        semantic_score_inputs=semantic_inputs,
    )
    evidence_path = ExplorationService._build_bridge_evidence_path(
        seed_knowledge_id=1,
        candidate_knowledge_id=3,
        explanation=explanation,
        subgraph_edges=subgraph_edges,
    )
    return BridgeDiscoveryResult(
        seed_knowledge_id=1,
        found=True,
        max_depth=2,
        items=[
            BridgeCandidate(
                knowledge_id=3,
                title="Gamma",
                depth=1,
                bridge_score=0.66,
                structural_bridge_score=0.65,
                graph_bridge_score=1.0,
                semantic_bridge_score=0.0,
                connected_knowledge_ids=[1, 4],
                relation_types=["references", "related_document"],
                evidence_path=evidence_path,
                supporting_subgraph=supporting_subgraph,
                summary="Gamma 是桥接候选",
            )
        ],
        summary="找到 1 个桥接候选",
        evidence_sources=BRIDGE_EVIDENCE_SOURCES,
        limitation_notes=["partial"],
        subgraph_max_nodes=100,
        subgraph_max_edges=300,
        subgraph_node_count=3,
        subgraph_edge_count=2,
    )


def _timeline_degraded_result(*, found):
    items = (
        [
            TimelinePoint(
                knowledge_id=1,
                title="AI",
                source=build_entry_locator(1),
                citation_locator=build_entry_locator(1),
                retrieval_score=0.5,
            )
        ]
        if found
        else []
    )
    return TimelineResult(
        topic="AI",
        found=found,
        inferred_time_field="unavailable",
        time_source_priority=["event_time", "published_at", "archived_at"],
        items=items,
        evidence_sources=TIMELINE_EVIDENCE_SOURCES,
        limitation_notes=[
            "timeline_retrieval_degraded[provider_unavailable]：部分检索能力不可用"
        ],
    )


def _contrast_degraded_result(*, side, found):
    candidate = ContrastCandidateItem(
        knowledge_id=1,
        title="A",
        source=build_entry_locator(1),
        citation_locator=build_entry_locator(1),
        retrieval_score=0.5,
    )
    candidates_a = [candidate] if found else []
    return ContrastResult(
        topic_a="A",
        topic_b="B",
        found=found,
        topic_a_candidates=candidates_a,
        comparison_dimensions=_empty_contrast_dimensions(candidates_a, []),
        evidence_sources=CONTRAST_EVIDENCE_SOURCES,
        limitation_notes=[
            f"contrast_topic_{side}_retrieval_degraded[provider_unavailable]："
            "部分检索能力不可用"
        ],
    )


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

    @staticmethod
    def _completed(results=MOCK_SEARCH_RESULTS, *, strategy="bm25"):
        return SearchResponse.completed(results, strategy=strategy)

    @pytest.mark.asyncio
    async def test_auto_strategy(self):
        """auto 策略应委托共享应用服务。"""
        mock_application = MagicMock()
        mock_application.search.return_value = self._completed(strategy="bm25")

        with patch("src.mcp.tools.get_application", return_value=mock_application):
            from src.mcp.tools import search_knowledge
            result = await search_knowledge(query="AI", strategy="auto", top_k=5)

        assert set(result) == {"status", "strategy", "total", "results", "issues"}
        assert result["status"] == "success"
        assert result["strategy"] == "bm25"
        assert result["total"] == 2
        assert result["issues"] == []
        assert len(result["results"]) == 2
        assert result["results"][0]["title"] == "AI 文章"
        assert result["results"][0]["abstract"] == "人工智能概述"
        assert result["results"][0]["tags"] == ["AI", "ML"]
        mock_application.search.assert_called_once_with(
            "AI", "auto", 5, auto_token_threshold=5
        )

    @pytest.mark.asyncio
    async def test_bm25_strategy(self):
        """bm25 策略应委托共享应用服务。"""
        mock_application = MagicMock()
        mock_application.search.return_value = self._completed(
            MOCK_SEARCH_RESULTS[:1],
            strategy="bm25",
        )

        with patch("src.mcp.tools.get_application", return_value=mock_application):
            from src.mcp.tools import search_knowledge
            result = await search_knowledge(query="AI", strategy="bm25", top_k=3)

        assert result["total"] == 1
        assert result["status"] == "success"
        assert result["strategy"] == "bm25"
        mock_application.search.assert_called_once_with(
            "AI", "bm25", 3, auto_token_threshold=5
        )

    @pytest.mark.asyncio
    async def test_invalid_strategy(self):
        """无效策略应返回错误。"""
        from src.mcp.tools import search_knowledge
        result = await search_knowledge(query="AI", strategy="invalid")
        assert set(result) == {"status", "strategy", "total", "results", "issues"}
        assert result["status"] == "invalid"
        assert result["total"] == 0
        assert result["results"] == []
        assert result["issues"][0]["code"] == ErrorCode.RETRIEVAL_INVALID_QUERY.value

    @pytest.mark.asyncio
    async def test_invalid_strategy_is_redacted_before_logging_or_backend(self, caplog):
        secret = "api_key_CANARY"
        malicious_strategy = f"hybrid\r\n{secret}"

        with caplog.at_level(logging.INFO, logger="pkv.mcp"), patch(
            "src.mcp.tools.get_application"
        ) as mock_application:
            from src.mcp.tools import search_knowledge

            result = await search_knowledge(
                query="AI",
                strategy=malicious_strategy,
            )

        assert result["status"] == "invalid"
        assert result["strategy"] == "unknown"
        assert result["issues"][0]["stage"] == "strategy_validation"
        assert secret not in repr(result)
        assert secret not in caplog.text
        assert malicious_strategy not in caplog.text
        mock_application.assert_not_called()

        class StrategySubclass(str):
            pass

        subclass_result = await search_knowledge(
            query="AI",
            strategy=StrategySubclass("auto"),
        )
        assert subclass_result["status"] == "invalid"
        assert subclass_result["strategy"] == "unknown"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("strategy", ["auto", "bm25", "vector", "hybrid"])
    @pytest.mark.parametrize("query", ["", "   ", None, 123])
    async def test_invalid_query_never_constructs_backend(self, strategy, query):
        with patch("src.mcp.tools.get_application") as mock_application:
            from src.mcp.tools import search_knowledge

            result = await search_knowledge(query=query, strategy=strategy)

        assert result["status"] == "invalid"
        assert result["issues"][0]["stage"] == "query_validation"
        mock_application.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("strategy", ["auto", "bm25", "vector", "hybrid"])
    @pytest.mark.parametrize(
        ("source_type", "tag"),
        [(True, None), (None, {"secret": "pkv-filter-canary"}), ("", None)],
    )
    async def test_invalid_filters_never_construct_backend(
        self,
        strategy,
        source_type,
        tag,
        caplog,
    ):
        with patch("src.mcp.tools.get_application") as mock_application:
            from src.mcp.tools import search_knowledge

            result = await search_knowledge(
                query="有效查询",
                strategy=strategy,
                source_type=source_type,
                tag=tag,
            )

        assert result["status"] == "invalid"
        assert result["issues"][0]["stage"] == "filter_validation"
        assert "pkv-filter-canary" not in repr(result)
        assert "pkv-filter-canary" not in caplog.text
        mock_application.assert_not_called()

    @pytest.mark.asyncio
    async def test_top_k_clamped(self):
        """top_k 应被限制在 [1, 50] 范围内。"""
        mock_application = MagicMock()
        mock_application.search.return_value = SearchResponse.completed((), strategy="bm25")

        with patch("src.mcp.tools.get_application", return_value=mock_application):
            from src.mcp.tools import search_knowledge
            await search_knowledge(query="AI", top_k=200)

        mock_application.search.assert_called_once_with(
            "AI", "auto", 50, auto_token_threshold=5
        )

    @pytest.mark.asyncio
    async def test_source_type_filter(self):
        """source_type 过滤应正确工作。"""
        mock_router = MagicMock()
        mock_router.search.return_value = self._completed(strategy="bm25")

        with patch("src.mcp.tools.get_application", return_value=mock_router):
            from src.mcp.tools import search_knowledge
            result = await search_knowledge(query="AI", source_type="wechat")

        assert result["total"] == 1
        assert result["results"][0]["source_type"] == "wechat"

    @pytest.mark.asyncio
    async def test_tag_filter(self):
        """tag 过滤应正确工作。"""
        mock_router = MagicMock()
        mock_router.search.return_value = self._completed(strategy="bm25")

        with patch("src.mcp.tools.get_application", return_value=mock_router):
            from src.mcp.tools import search_knowledge
            result = await search_knowledge(query="AI", tag="ML")

        assert result["total"] == 1
        assert "ML" in result["results"][0]["tags"]

    @pytest.mark.asyncio
    async def test_list_metadata_tags_are_core_valid_and_public(self):
        item = SearchResult(
            knowledge_id=7,
            title="列表标签",
            score=0.8,
            highlight="",
            metadata={"tags": ["AI", "知识图谱"]},
        )
        mock_router = MagicMock()
        mock_router.search.return_value = SearchResponse.completed(
            (item,),
            strategy="bm25",
        )

        with patch("src.mcp.tools.get_application", return_value=mock_router):
            from src.mcp.tools import search_knowledge

            result = await search_knowledge(query="AI", tag="知识图谱")

        assert result["status"] == "success"
        assert result["results"][0]["tags"] == ["AI", "知识图谱"]

    @pytest.mark.asyncio
    async def test_filtering_all_success_results_becomes_no_hits(self):
        mock_router = MagicMock()
        mock_router.search.return_value = self._completed(strategy="bm25")

        with patch("src.mcp.tools.get_application", return_value=mock_router):
            from src.mcp.tools import search_knowledge

            result = await search_knowledge(query="AI", source_type="pdf")

        assert result == {
            "status": "no_hits",
            "strategy": "bm25",
            "total": 0,
            "results": [],
            "issues": [],
        }

    @pytest.mark.asyncio
    async def test_error_is_not_disguised_as_empty_results(self):
        mock_router = MagicMock()
        mock_router.search.return_value = SearchResponse.failed_response(
            RetrievalIssue(
                code=ErrorCode.RETRIEVAL_BACKEND_FAILED,
                message="BM25 检索后端不可用",
                stage="bm25_search",
                recoverable=True,
            ),
            strategy="bm25",
        )

        with patch("src.mcp.tools.get_application", return_value=mock_router):
            from src.mcp.tools import search_knowledge

            result = await search_knowledge(query="AI")

        assert result["status"] == "error"
        assert result["total"] == 0
        assert result["results"] == []
        assert result["issues"] == [
            {
                "code": ErrorCode.RETRIEVAL_BACKEND_FAILED.value,
                "message": "检索后端不可用",
                "stage": "bm25_search",
                "recoverable": True,
            }
        ]

    @pytest.mark.asyncio
    async def test_degraded_exposes_limitation_issue(self):
        mock_router = MagicMock()
        mock_router.search.return_value = SearchResponse.degraded_response(
            MOCK_SEARCH_RESULTS[:1],
            (
                RetrievalIssue(
                    code=ErrorCode.RETRIEVAL_INDEX_UNAVAILABLE,
                    message="向量索引不可用，已退化为 BM25",
                    stage="hybrid_vector",
                    recoverable=True,
                ),
            ),
            strategy="hybrid",
        )

        with patch("src.mcp.tools.get_application", return_value=mock_router):
            from src.mcp.tools import search_knowledge

            result = await search_knowledge(query="较长的 AI 工作流查询")

        assert result["status"] == "degraded"
        assert result["strategy"] == "hybrid"
        assert result["total"] == 1
        assert result["issues"][0]["code"] == ErrorCode.RETRIEVAL_INDEX_UNAVAILABLE.value
        assert result["issues"][0]["message"] == "检索索引不可用"

    @pytest.mark.asyncio
    async def test_legacy_list_return_is_explicit_backend_error(self):
        mock_router = MagicMock()
        mock_router.search.return_value = []

        with patch("src.mcp.tools.get_application", return_value=mock_router):
            from src.mcp.tools import search_knowledge

            result = await search_knowledge(query="AI")

        assert result["status"] == "error"
        assert result["issues"][0]["code"] == ErrorCode.RETRIEVAL_BACKEND_FAILED.value

    @pytest.mark.asyncio
    async def test_vector_strategy_delegates_to_application(self):
        mock_application = MagicMock()
        mock_application.search.return_value = self._completed(
            MOCK_SEARCH_RESULTS[:1],
            strategy="vector",
        )

        with patch("src.mcp.tools.get_application", return_value=mock_application):
            from src.mcp.tools import search_knowledge

            result = await search_knowledge(query="语义搜索", strategy="vector")

        assert result["status"] == "success"
        mock_application.search.assert_called_once_with(
            "语义搜索", "vector", 5, auto_token_threshold=5
        )

    @pytest.mark.asyncio
    async def test_provider_factory_failure_has_stable_code(self):
        provider_error = PKVRuntimeError(
            ErrorCode.PROVIDER_CONFIG_INVALID,
            "Provider API Key 未配置",
            stage="provider_configuration",
            recoverable=True,
        )

        mock_application = MagicMock()
        mock_application.search.side_effect = provider_error
        with patch("src.mcp.tools.get_application", return_value=mock_application):
            from src.mcp.tools import search_knowledge

            result = await search_knowledge(query="语义搜索", strategy="vector")

        assert result["status"] == "error"
        assert result["issues"][0] == {
            "code": ErrorCode.PROVIDER_CONFIG_INVALID.value,
            "message": "Provider 配置无效",
            "stage": "provider_configuration",
            "recoverable": True,
            "cause_type": "PKVRuntimeError",
        }

    @pytest.mark.asyncio
    @pytest.mark.parametrize("top_k", [0, -1, True, "5"])
    async def test_invalid_top_k_does_not_call_backend(self, top_k):
        mock_router = MagicMock()

        with patch("src.mcp.tools.get_application", return_value=mock_router):
            from src.mcp.tools import search_knowledge

            result = await search_knowledge(query="AI", top_k=top_k)

        assert result["status"] == "invalid"
        assert result["issues"][0]["code"] == ErrorCode.RETRIEVAL_INVALID_QUERY.value
        mock_router.search.assert_not_called()

    @pytest.mark.asyncio
    async def test_runtime_error_message_canary_is_not_exposed(self, caplog):
        secret = "pkv-canary-secret-93f8"
        runtime_error = PKVRuntimeError(
            ErrorCode.PROVIDER_CONFIG_INVALID,
            f"bad key={secret} path=C:\\Users\\private\\local.yaml",
            stage="provider_configuration",
            recoverable=True,
        )

        mock_application = MagicMock()
        mock_application.search.side_effect = runtime_error
        with patch("src.mcp.tools.get_application", return_value=mock_application):
            from src.mcp.tools import search_knowledge

            result = await search_knowledge(query="语义搜索", strategy="vector")

        rendered = repr(result)
        assert secret not in rendered
        assert "Users" not in rendered
        assert secret not in caplog.text
        assert result["issues"][0]["message"] == "Provider 配置无效"


class TestGetEntry:
    """get_entry Tool 测试。"""

    @pytest.mark.asyncio
    async def test_found_entry(self, tmp_path: Path):
        """正常查找条目应返回完整信息。"""
        vault_dir = tmp_path / "vault"
        vault_dir.mkdir()
        entry_path = vault_dir / "entry.md"
        entry_path.write_text("# entry\n", encoding="utf-8")
        mock_store = MagicMock()
        mock_store.query_by_id.return_value = {
            **MOCK_ENTRY_DB,
            "file_path": str(entry_path),
        }
        mock_md_store = MagicMock()
        mock_md_store.vault_dir = vault_dir
        mock_md_store.load.return_value = Entry(
            title="测试微信文章",
            source_type="wechat",
            content="# 测试文章\n\n这是全文内容",
        )

        with patch("src.mcp.tools.get_sqlite_store", return_value=mock_store), \
             patch("src.mcp.tools.get_markdown_store", return_value=mock_md_store):
            from src.mcp.tools import get_entry
            result = await get_entry(knowledge_id="1")

        assert result["knowledge_id"] == 1
        assert result["status"] == "success"
        assert result["issues"] == []
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
        assert result["status"] == "no_hits"
        assert result["issues"] == []

    @pytest.mark.asyncio
    async def test_invalid_id(self):
        """非数字 ID 应返回 error。"""
        from src.mcp.tools import get_entry
        result = await get_entry(knowledge_id="abc")
        assert "error" in result
        assert result["status"] == "invalid"
        assert result["issues"][0]["code"] == ErrorCode.RETRIEVAL_INVALID_QUERY.value

    @pytest.mark.asyncio
    @pytest.mark.parametrize("knowledge_id", [True, "0", "-1"])
    async def test_rejects_non_positive_or_boolean_id(self, knowledge_id):
        from src.mcp.tools import get_entry

        result = await get_entry(knowledge_id=knowledge_id)

        assert result["status"] == "invalid"
        assert result["issues"][0]["stage"] == "knowledge_id_validation"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "store_error",
        [
            PKVRuntimeError(
                ErrorCode.DATABASE_MISSING,
                "secret=pkv-db-canary path=C:\\Users\\private\\vault.db",
                stage="database_open",
                recoverable=False,
            ),
            RuntimeError("secret=pkv-db-canary path=C:\\Users\\private\\vault.db"),
        ],
    )
    async def test_database_failure_is_stable_and_redacted(self, store_error, caplog):
        with patch("src.mcp.tools.get_sqlite_store", side_effect=store_error):
            from src.mcp.tools import get_entry

            result = await get_entry(knowledge_id="1")

        rendered = repr(result)
        assert result["status"] == "error"
        assert result["issues"][0]["code"] in {
            ErrorCode.DATABASE_MISSING.value,
            ErrorCode.RETRIEVAL_BACKEND_FAILED.value,
        }
        assert "pkv-db-canary" not in rendered
        assert "Users" not in rendered
        assert "pkv-db-canary" not in caplog.text

    @pytest.mark.asyncio
    async def test_malformed_entry_is_stable_serialization_error(self):
        mock_store = MagicMock()
        mock_store.query_by_id.return_value = {"file_path": ""}

        with patch("src.mcp.tools.get_sqlite_store", return_value=mock_store):
            from src.mcp.tools import get_entry

            result = await get_entry(knowledge_id="1")

        assert result["status"] == "error"
        assert result["issues"][0]["stage"] == "entry_lookup"

    @pytest.mark.asyncio
    async def test_markdown_not_found(self, tmp_path: Path):
        """Markdown 文件不存在应优雅降级。"""
        vault_dir = tmp_path / "vault"
        vault_dir.mkdir()
        mock_store = MagicMock()
        mock_store.query_by_id.return_value = {
            **MOCK_ENTRY_DB,
            "file_path": str(vault_dir / "missing.md"),
        }
        mock_md_store = MagicMock()
        mock_md_store.vault_dir = vault_dir
        mock_md_store.load.side_effect = FileNotFoundError("文件不存在")

        with patch("src.mcp.tools.get_sqlite_store", return_value=mock_store), \
             patch("src.mcp.tools.get_markdown_store", return_value=mock_md_store):
            from src.mcp.tools import get_entry
            result = await get_entry(knowledge_id="1")

        assert result["title"] == "测试微信文章"
        assert result["content"] == "(内容不可用)"
        assert result["status"] == "degraded"
        assert result["issues"][0]["code"] == ErrorCode.RESOURCE_NOT_READABLE.value

    @pytest.mark.asyncio
    async def test_markdown_store_returning_none_is_degraded(self, tmp_path: Path):
        vault_dir = tmp_path / "vault"
        vault_dir.mkdir()
        entry_path = vault_dir / "entry.md"
        entry_path.write_text("# entry\n", encoding="utf-8")
        mock_store = MagicMock()
        mock_store.query_by_id.return_value = {
            **MOCK_ENTRY_DB,
            "file_path": str(entry_path),
        }
        mock_md_store = MagicMock(vault_dir=vault_dir)
        mock_md_store.load.return_value = None

        with patch("src.mcp.tools.get_sqlite_store", return_value=mock_store), \
             patch("src.mcp.tools.get_markdown_store", return_value=mock_md_store):
            from src.mcp.tools import get_entry

            result = await get_entry(knowledge_id="1")

        assert result["status"] == "degraded"
        assert result["content"] == "(内容不可用)"
        assert result["issues"][0]["code"] == ErrorCode.RESOURCE_NOT_READABLE.value

    @pytest.mark.asyncio
    async def test_real_frontmatter_only_markdown_is_degraded(self, tmp_path: Path):
        vault_dir = tmp_path / "vault"
        vault_dir.mkdir()
        entry_path = vault_dir / "frontmatter-only.md"
        entry_path.write_text(
            "---\ntitle: 仅元数据\nsource_type: text\n---\n",
            encoding="utf-8",
        )
        mock_store = MagicMock()
        mock_store.query_by_id.return_value = {
            **MOCK_ENTRY_DB,
            "title": "仅元数据",
            "source_type": "text",
            "file_path": str(entry_path),
        }
        markdown_store = MarkdownStore(vault_dir)

        with patch(
            "src.mcp.tools.get_sqlite_store",
            return_value=mock_store,
        ), patch(
            "src.mcp.tools.get_markdown_store",
            return_value=markdown_store,
        ):
            from src.mcp.tools import get_entry

            result = await get_entry(knowledge_id="1")

        assert result["status"] == "degraded"
        assert result["content"] == "(内容不可用)"
        assert result["issues"][0]["code"] == ErrorCode.RESOURCE_NOT_READABLE.value

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad_kind", ["non_string", "wrong_type", "subclass"])
    async def test_malformed_loaded_entry_is_backend_error(
        self,
        tmp_path: Path,
        bad_kind,
        caplog,
    ):
        secret = "api_key_ENTRY_CONTENT_CANARY"
        vault_dir = tmp_path / "vault"
        vault_dir.mkdir()
        entry_path = vault_dir / "entry.md"
        entry_path.write_text("# entry", encoding="utf-8")
        mock_store = MagicMock()
        mock_store.query_by_id.return_value = {
            **MOCK_ENTRY_DB,
            "file_path": str(entry_path),
        }

        class SecretObject:
            def __str__(self):
                return secret

        class EntrySubclass(Entry):
            pass

        if bad_kind == "non_string":
            loaded_entry = Entry(title="safe", source_type="text")
            loaded_entry.content = SecretObject()
        elif bad_kind == "wrong_type":
            loaded_entry = MagicMock(content=secret)
        else:
            loaded_entry = EntrySubclass(
                title="safe",
                source_type="text",
                content="# safe",
            )
        markdown_store = MagicMock(vault_dir=vault_dir)
        markdown_store.load.return_value = loaded_entry

        with patch(
            "src.mcp.tools.get_sqlite_store",
            return_value=mock_store,
        ), patch(
            "src.mcp.tools.get_markdown_store",
            return_value=markdown_store,
        ):
            from src.mcp.tools import get_entry

            result = await get_entry(knowledge_id="1")

        assert result["status"] == "error"
        assert result["issues"][0]["code"] == ErrorCode.RETRIEVAL_BACKEND_FAILED.value
        assert result["issues"][0]["stage"] == "entry_content_read"
        assert secret not in repr(result)
        assert secret not in caplog.text

    @pytest.mark.asyncio
    async def test_missing_markdown_path_is_explicitly_degraded(self):
        mock_store = MagicMock()
        mock_store.query_by_id.return_value = {**MOCK_ENTRY_DB, "file_path": ""}

        with patch("src.mcp.tools.get_sqlite_store", return_value=mock_store):
            from src.mcp.tools import get_entry

            result = await get_entry(knowledge_id="1")

        assert result["status"] == "degraded"
        assert result["issues"][0]["code"] == ErrorCode.RESOURCE_MISSING.value

    @pytest.mark.asyncio
    async def test_markdown_runtime_error_preserves_safe_code(self, tmp_path: Path):
        vault_dir = tmp_path / "vault"
        vault_dir.mkdir()
        entry_path = vault_dir / "entry.md"
        entry_path.write_text("# entry\n", encoding="utf-8")
        mock_store = MagicMock()
        mock_store.query_by_id.return_value = {
            **MOCK_ENTRY_DB,
            "file_path": str(entry_path),
        }
        mock_md_store = MagicMock(vault_dir=vault_dir)
        mock_md_store.load.side_effect = PKVRuntimeError(
            ErrorCode.PATH_OUTSIDE_VAULT,
            "secret=pkv-path-canary C:\\Users\\private",
            stage="entry_content_read",
            recoverable=False,
        )

        with patch("src.mcp.tools.get_sqlite_store", return_value=mock_store), \
             patch("src.mcp.tools.get_markdown_store", return_value=mock_md_store):
            from src.mcp.tools import get_entry

            result = await get_entry(knowledge_id="1")

        assert result["status"] == "degraded"
        assert result["issues"][0]["code"] == ErrorCode.PATH_OUTSIDE_VAULT.value
        assert "pkv-path-canary" not in repr(result)

    @pytest.mark.asyncio
    async def test_markdown_error_message_canary_is_not_exposed(
        self,
        tmp_path: Path,
        caplog,
    ):
        secret = "pkv-entry-canary-a91c"
        vault_dir = tmp_path / "vault"
        vault_dir.mkdir()
        entry_path = vault_dir / "entry.md"
        entry_path.write_text("# entry", encoding="utf-8")
        mock_store = MagicMock()
        mock_store.query_by_id.return_value = {
            **MOCK_ENTRY_DB,
            "file_path": str(entry_path),
        }
        mock_md_store = MagicMock(vault_dir=vault_dir)
        mock_md_store.load.side_effect = RuntimeError(
            f"decode failed secret={secret} C:\\Users\\private"
        )

        with patch("src.mcp.tools.get_sqlite_store", return_value=mock_store), \
             patch("src.mcp.tools.get_markdown_store", return_value=mock_md_store):
            from src.mcp.tools import get_entry

            result = await get_entry(knowledge_id="1")

        rendered = repr(result)
        assert result["status"] == "degraded"
        assert secret not in rendered
        assert "Users" not in rendered
        assert secret not in caplog.text
        assert result["issues"][0]["message"] == "请求的资源不可读取"


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

        assert result["status"] == "success"
        assert result["issues"] == []
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

        assert result["status"] == "no_hits"
        assert result["issues"] == []
        assert result["total_tags"] == 0
        assert result["tags"] == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "store_error",
        [
            ValueError("secret=pkv-tag-canary"),
            PKVRuntimeError(
                ErrorCode.DATABASE_MISSING,
                "secret=pkv-tag-canary C:\\Users\\private",
                stage="database_open",
                recoverable=False,
            ),
            RuntimeError("secret=pkv-tag-canary C:\\Users\\private"),
        ],
    )
    async def test_failure_has_stable_envelope(self, store_error, caplog):
        with patch("src.mcp.tools.get_sqlite_store", side_effect=store_error):
            from src.mcp.tools import list_tags

            result = await list_tags()

        assert result["status"] == "error"
        expected_code = (
            store_error.code.value
            if isinstance(store_error, PKVRuntimeError)
            else ErrorCode.RETRIEVAL_BACKEND_FAILED.value
        )
        assert result["issues"][0]["code"] == expected_code
        assert "pkv-tag-canary" not in repr(result)
        assert "pkv-tag-canary" not in caplog.text

    @pytest.mark.asyncio
    async def test_list_tags_redacts_local_values(self):
        mock_store = MagicMock()
        mock_store.get_all_tags_with_count.return_value = [
            {"name": r"\Windows\System32\private", "count": 1},
        ]

        with patch("src.mcp.tools.get_sqlite_store", return_value=mock_store):
            from src.mcp.tools import list_tags
            result = await list_tags()

        assert result["tags"][0]["name"] == "[redacted-local-reference]"


class TestListEntries:
    """list_entries Tool 测试。"""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("page", "per_page"),
        [(0, 20), (1, True), ("1", 20)],
    )
    async def test_invalid_pagination_is_rejected(self, page, per_page):
        from src.mcp.tools import list_entries

        result = await list_entries(page=page, per_page=per_page)

        assert result["status"] == "invalid"
        assert result["issues"][0]["code"] == ErrorCode.RETRIEVAL_INVALID_QUERY.value

    @pytest.mark.asyncio
    async def test_default_pagination(self):
        """默认分页应返回正确结构。"""
        mock_store = MagicMock()
        mock_store.list_entries.return_value = [MOCK_ENTRY_DB]
        mock_store.count_entries.return_value = 1

        with patch("src.mcp.tools.get_sqlite_store", return_value=mock_store):
            from src.mcp.tools import list_entries
            result = await list_entries()

        assert result["status"] == "success"
        assert result["issues"] == []
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
        assert mock_store.list_entries.call_args.kwargs["limit"] == 100

    @pytest.mark.asyncio
    async def test_empty_page_is_no_hits(self):
        mock_store = MagicMock()
        mock_store.list_entries.return_value = []
        mock_store.count_entries.return_value = 0

        with patch("src.mcp.tools.get_sqlite_store", return_value=mock_store):
            from src.mcp.tools import list_entries

            result = await list_entries()

        assert result["status"] == "no_hits"
        assert result["issues"] == []
        assert result["entries"] == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("sort_by", "sort_order"),
        [
            ("invalid_field", "desc"),
            ("archived_at", "sideways"),
            ([], "desc"),
        ],
    )
    async def test_invalid_sort_is_rejected_before_store(
        self,
        sort_by,
        sort_order,
        caplog,
    ):
        """无效排序参数不得因数据库故障被误报为运行错误。"""
        secret = "pkv-sort-canary"
        with patch(
            "src.mcp.tools.get_sqlite_store",
            side_effect=RuntimeError(f"{secret} C:\\Users\\private"),
        ) as mock_get_store:
            from src.mcp.tools import list_entries

            result = await list_entries(sort_by=sort_by, sort_order=sort_order)

        assert "error" in result
        assert result["status"] == "invalid"
        assert result["issues"][0]["code"] == ErrorCode.RETRIEVAL_INVALID_QUERY.value
        assert secret not in repr(result)
        assert secret not in caplog.text
        mock_get_store.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "store_error",
        [
            PKVRuntimeError(
                ErrorCode.DATABASE_MISSING,
                "secret=pkv-list-canary C:\\Users\\private\\vault.db",
                stage="database_open",
                recoverable=False,
            ),
            RuntimeError("secret=pkv-list-canary C:\\Users\\private\\vault.db"),
        ],
    )
    async def test_store_failure_is_stable_and_redacted(self, store_error, caplog):
        with patch("src.mcp.tools.get_sqlite_store", side_effect=store_error):
            from src.mcp.tools import list_entries

            result = await list_entries()

        rendered = repr(result)
        assert result["status"] == "error"
        assert result["issues"][0]["code"] in {
            ErrorCode.DATABASE_MISSING.value,
            ErrorCode.RETRIEVAL_BACKEND_FAILED.value,
        }
        assert "pkv-list-canary" not in rendered
        assert "Users" not in rendered
        assert "pkv-list-canary" not in caplog.text

    @pytest.mark.asyncio
    async def test_backend_value_error_is_error_not_invalid(self, caplog):
        secret = "pkv-list-value-error-canary"
        mock_store = MagicMock()
        mock_store.list_entries.side_effect = ValueError(secret)

        with patch("src.mcp.tools.get_sqlite_store", return_value=mock_store):
            from src.mcp.tools import list_entries

            result = await list_entries()

        assert result["status"] == "error"
        assert result["issues"][0]["code"] == ErrorCode.RETRIEVAL_BACKEND_FAILED.value
        assert result["issues"][0]["stage"] == "list_entries"
        assert secret not in repr(result)
        assert secret not in caplog.text

    @pytest.mark.asyncio
    async def test_malformed_row_is_stable_serialization_error(self):
        mock_store = MagicMock()
        mock_store.list_entries.return_value = [{}]
        mock_store.count_entries.return_value = 1

        with patch("src.mcp.tools.get_sqlite_store", return_value=mock_store):
            from src.mcp.tools import list_entries

            result = await list_entries()

        assert result["status"] == "error"
        assert result["issues"][0]["stage"] == "list_entries"

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
            "total_entries": 80,
            "by_source_type": [("wechat", 50), ("zhihu", 30)],
            "top_tags": [{"name": "AI", "count": 20}],
        }
        mock_store = MagicMock()
        mock_store.get_statistics.return_value = mock_stats

        with patch("src.mcp.tools.get_sqlite_store", return_value=mock_store):
            from src.mcp.tools import get_stats
            result = await get_stats()

        assert result["status"] == "success"
        assert result["issues"] == []
        assert result["total_entries"] == 80

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "store_error",
        [
            ValueError("secret=pkv-stats-canary"),
            PKVRuntimeError(
                ErrorCode.DATABASE_MISSING,
                "secret=pkv-stats-canary C:\\Users\\private",
                stage="database_open",
                recoverable=False,
            ),
            RuntimeError("secret=pkv-stats-canary C:\\Users\\private"),
        ],
    )
    async def test_failure_has_stable_envelope(self, store_error, caplog):
        with patch("src.mcp.tools.get_sqlite_store", side_effect=store_error):
            from src.mcp.tools import get_stats

            result = await get_stats()

        assert result["status"] == "error"
        expected_code = (
            store_error.code.value
            if isinstance(store_error, PKVRuntimeError)
            else ErrorCode.RETRIEVAL_BACKEND_FAILED.value
        )
        assert result["issues"][0]["code"] == expected_code
        assert "pkv-stats-canary" not in repr(result)
        assert "pkv-stats-canary" not in caplog.text

    @pytest.mark.asyncio
    async def test_non_mapping_statistics_is_stable_error(self):
        mock_store = MagicMock()
        mock_store.get_statistics.return_value = []

        with patch("src.mcp.tools.get_sqlite_store", return_value=mock_store):
            from src.mcp.tools import get_stats

            result = await get_stats()

        assert result["status"] == "error"
        assert result["issues"][0]["stage"] == "get_stats"

    @pytest.mark.asyncio
    async def test_get_stats_redacts_local_values(self):
        mock_store = MagicMock()
        mock_store.get_statistics.return_value = {
            "total_entries": 1,
            "by_source_type": [(r"\??\C:\private", 1)],
            "top_tags": [],
        }

        with patch("src.mcp.tools.get_sqlite_store", return_value=mock_store):
            from src.mcp.tools import get_stats
            result = await get_stats()

        assert result["by_source_type"][0][0] == "[redacted-local-reference]"


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
        assert result["terminal"] == "error"
        assert result["issues"][0]["code"] == ErrorCode.SSRF_TARGET_FORBIDDEN.value
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
        assert result["issues"][0]["code"] == ErrorCode.URL_INVALID.value

    @pytest.mark.asyncio
    async def test_reject_ftp_url(self):
        """FTP URL 应被拒绝。"""
        from src.mcp.tools import archive_url
        result = await archive_url(url="ftp://example.com/file")
        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_successful_archive(self):
        """正常归档应返回成功结果。"""
        mock_result = WorkflowResult(
            success=True,
            terminal="success",
            data={
                "knowledge_id": 42,
                "title": "测试文章",
                "status": "ready",
                "core_committed": True,
                "file_path": "/vault/test.md",
                "tags": ["AI"],
                "summary_one_sentence": "这是摘要",
            },
        )

        from unittest.mock import AsyncMock
        with patch("src.workflow.engine.WorkflowEngine") as MockEngine:
            mock_engine_instance = MagicMock()
            mock_engine_instance.execute_async = AsyncMock(return_value=mock_result)
            MockEngine.return_value = mock_engine_instance

            from src.mcp.tools import archive_url
            result = await archive_url(url="https://example.com/article")

        assert result["success"] is True
        assert result["terminal"] == "success"
        assert result["warnings"] == []
        assert result["issues"] == []
        assert result["knowledge_id"] == 42
        assert result["entry_locator"] == "pkv://entries/42"
        assert "file_path" not in result
        mock_engine_instance.execute_async.assert_awaited_once_with(
            "archive-url",
            {
                "url": "https://example.com/article",
                "skip_review": True,
                "skip_sharpen": True,
            },
        )

    @pytest.mark.asyncio
    async def test_archive_failure(self):
        """归档失败应返回错误信息。"""
        mock_result = WorkflowResult(
            success=False,
            terminal="error",
            errors=["抓取失败"],
            issues=[
                {
                    "code": ErrorCode.WORKFLOW_STEP_FAILED.value,
                    "message": "网页抓取失败",
                    "severity": "error",
                    "stage": "fetch_content",
                    "step_id": "fetch",
                    "recoverable": True,
                }
            ],
        )

        from unittest.mock import AsyncMock
        with patch("src.workflow.engine.WorkflowEngine") as MockEngine:
            mock_engine_instance = MagicMock()
            mock_engine_instance.execute_async = AsyncMock(return_value=mock_result)
            MockEngine.return_value = mock_engine_instance

            from src.mcp.tools import archive_url
            result = await archive_url(url="https://example.com/timeout")

        assert result["success"] is False
        assert result["terminal"] == "error"
        assert result["error"] == "归档失败"
        assert result["issues"][0]["code"] == ErrorCode.WORKFLOW_STEP_FAILED.value
        assert result["issues"][0]["stage"] == "fetch_content"

    @pytest.mark.asyncio
    async def test_success_terminal_requires_exact_committed_knowledge_id(self):
        secret = "pkv-archive-url-result-canary"
        malformed_result = _DiagnosticAccessBombResult(
            terminal="success",
            data={"knowledge_id": True, "title": secret},
        )

        from unittest.mock import AsyncMock
        from src.mcp import tools

        application = MagicMock()
        application.archive_url = AsyncMock(return_value=malformed_result)
        with patch.object(tools, "get_application", return_value=application):
            result = await tools.archive_url(url="https://example.com/article")

        assert result["success"] is False
        assert result["terminal"] == "error"
        assert result["issues"][0]["code"] == ErrorCode.WORKFLOW_STEP_FAILED.value
        assert result["issues"][0]["stage"] == "workflow_result"
        assert "knowledge_id" not in result
        assert secret not in repr(result)
        assert malformed_result.diagnostic_accesses == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("field", "bad_value"),
        [
            ("title", ["pkv-title-canary"]),
            ("tags", ["safe", 7]),
            ("summary_one_sentence", {"secret": "pkv-summary-canary"}),
        ],
    )
    async def test_success_terminal_rejects_malformed_public_fields_before_diagnostics(
        self,
        field,
        bad_value,
    ):
        malformed_result = _DiagnosticAccessBombResult(
            terminal="success",
            data={
                "knowledge_id": 42,
                "status": "ready",
                "core_committed": True,
                field: bad_value,
            },
        )

        from unittest.mock import AsyncMock
        from src.mcp import tools

        application = MagicMock()
        application.archive_url = AsyncMock(return_value=malformed_result)
        with patch.object(tools, "get_application", return_value=application):
            result = await tools.archive_url(url="https://example.com/article")

        assert result["terminal"] == "error"
        assert result["issues"][0]["stage"] == "workflow_result"
        assert malformed_result.diagnostic_accesses == []
        assert "canary" not in repr(result).lower()

    @pytest.mark.asyncio
    async def test_error_terminal_repair_required_forces_do_not_retry(self):
        result_object = WorkflowResult(
            success=False,
            terminal="error",
            data={
                "knowledge_id": 49,
                "status": "repair_required",
                "core_committed": False,
                "do_not_retry": False,
            },
        )

        from unittest.mock import AsyncMock
        with patch("src.workflow.engine.WorkflowEngine") as MockEngine:
            MockEngine.return_value.execute_async = AsyncMock(
                return_value=result_object
            )
            from src.mcp.tools import archive_url

            result = await archive_url(url="https://example.com/article")

        assert result["terminal"] == "error"
        assert result["storage_status"] == "repair_required"
        assert result["knowledge_id"] == 49
        assert result["core_committed"] is False
        assert result["do_not_retry"] is True
        assert result["issues"][0]["code"] == ErrorCode.STORAGE_REPAIR_REQUIRED.value

    @pytest.mark.asyncio
    async def test_error_terminal_incomplete_fatal_data_never_reads_diagnostics(self):
        malformed_result = _DiagnosticAccessBombResult(
            terminal="error",
            data={
                "knowledge_id": 49,
                "status": "repair_required",
                "core_committed": True,
            },
        )

        from unittest.mock import AsyncMock
        from src.mcp import tools

        application = MagicMock()
        application.archive_url = AsyncMock(return_value=malformed_result)
        with patch.object(tools, "get_application", return_value=application):
            result = await tools.archive_url(url="https://example.com/article")

        assert result["terminal"] == "error"
        assert result["issues"][0]["stage"] == "workflow_result"
        assert "knowledge_id" not in result
        assert malformed_result.diagnostic_accesses == []

    @pytest.mark.asyncio
    async def test_success_terminal_rejects_nonempty_errors_without_leak(self):
        secret = "pkv-archive-url-diagnostics-canary"
        malformed_result = WorkflowResult(
            success=True,
            terminal="success",
            errors=[secret],
            data={
                "knowledge_id": 42,
                "title": secret,
                "status": "ready",
                "core_committed": True,
            },
        )

        from unittest.mock import AsyncMock
        with patch("src.workflow.engine.WorkflowEngine") as MockEngine:
            MockEngine.return_value.execute_async = AsyncMock(
                return_value=malformed_result
            )
            from src.mcp.tools import archive_url

            result = await archive_url(url="https://example.com/article")

        assert result["success"] is False
        assert result["terminal"] == "error"
        assert result["issues"][0]["code"] == ErrorCode.WORKFLOW_STEP_FAILED.value
        assert result["issues"][0]["stage"] == "workflow_result"
        assert secret not in repr(result)

    @pytest.mark.asyncio
    async def test_archive_degraded_preserves_storage_terminal_and_warning(self):
        operation_id = "0123456789abcdef0123456789abcdef"
        mock_result = WorkflowResult(
            success=True,
            terminal="degraded",
            warnings=["向量索引写入失败，核心存储已提交"],
            issues=[
                {
                    "code": ErrorCode.STORAGE_VECTOR_FAILED.value,
                    "message": "向量索引写入失败，核心存储已提交",
                    "severity": "warning",
                    "stage": "store_entry",
                    "recoverable": True,
                }
            ],
            data={
                "knowledge_id": 42,
                "title": "测试文章",
                "status": "degraded",
                "operation_id": operation_id,
                "core_committed": True,
                "do_not_retry": True,
                "repair_actions": ["rebuild_vector_index"],
                "storage_errors": [
                    {"code": ErrorCode.STORAGE_VECTOR_FAILED.value}
                ],
            },
        )

        from unittest.mock import AsyncMock
        with patch("src.workflow.engine.WorkflowEngine") as MockEngine:
            MockEngine.return_value.execute_async = AsyncMock(return_value=mock_result)
            from src.mcp.tools import archive_url

            result = await archive_url(url="https://example.com/article")

        assert result["success"] is True
        assert result["terminal"] == "degraded"
        assert result["storage_status"] == "degraded"
        assert result["operation_id"] == operation_id
        assert result["core_committed"] is True
        assert result["do_not_retry"] is True
        assert result["repair_actions"] == ["rebuild_vector_index"]
        assert result["storage_error_codes"] == [ErrorCode.STORAGE_VECTOR_FAILED.value]
        assert result["issues"][0]["severity"] == "warning"

    @pytest.mark.asyncio
    async def test_archive_degraded_redacts_backend_warning_and_issue_prose(self):
        secret = "sk-pkv-archive-url-canary"
        raw_detail = (
            f"processor_resource_limit: boom {secret} "
            "C:\\Users\\private query=private-search"
        )
        mock_result = WorkflowResult(
            success=True,
            terminal="degraded",
            warnings=[raw_detail],
            issues=[
                {
                    "code": ErrorCode.PROCESSOR_RESOURCE_LIMIT.value,
                    "message": raw_detail,
                    "severity": "warning",
                    "stage": "store_entry",
                    "recoverable": True,
                    "count": 7,
                    "limit": 5,
                    "raw_context": raw_detail,
                }
            ],
            data={
                "knowledge_id": 42,
                "title": "安全标题",
                "status": "degraded",
                "core_committed": True,
            },
        )

        from unittest.mock import AsyncMock
        with patch("src.workflow.engine.WorkflowEngine") as MockEngine:
            MockEngine.return_value.execute_async = AsyncMock(return_value=mock_result)
            from src.mcp.tools import archive_url

            result = await archive_url(url="https://example.com/article")

        rendered = repr(result)
        assert result["terminal"] == "degraded"
        assert result["warnings"] == ["处理器资源预算已达上限"]
        assert result["issues"][0]["message"] == "处理器资源预算已达上限"
        assert result["issues"][0]["count"] == 7
        assert result["issues"][0]["limit"] == 5
        assert "raw_context" not in result["issues"][0]
        assert secret not in rendered
        assert "Users" not in rendered
        assert "private-search" not in rendered

    @pytest.mark.asyncio
    async def test_success_terminal_with_repair_required_is_stable_error(self):
        result_object = WorkflowResult(
            success=True,
            terminal="success",
            data={
                "knowledge_id": 47,
                "title": "需修复条目",
                "status": "repair_required",
                "core_committed": True,
                "do_not_retry": False,
                "repair_actions": ["repair_secondary_indexes"],
            },
        )

        from unittest.mock import AsyncMock
        with patch("src.workflow.engine.WorkflowEngine") as MockEngine:
            MockEngine.return_value.execute_async = AsyncMock(
                return_value=result_object
            )
            from src.mcp.tools import archive_url

            result = await archive_url(url="https://example.com/article")

        assert result["success"] is False
        assert result["terminal"] == "error"
        assert result["storage_status"] == "repair_required"
        assert result["do_not_retry"] is True
        assert result["knowledge_id"] == 47
        assert result["issues"][0]["code"] == ErrorCode.STORAGE_REPAIR_REQUIRED.value

    @pytest.mark.asyncio
    async def test_archive_runtime_error_keeps_stable_code(self):
        runtime_error = PKVRuntimeError(
            ErrorCode.SSRF_RESOLUTION_FAILED,
            "目标主机无法安全解析",
            stage="safe_fetch_dns",
            recoverable=True,
        )

        from unittest.mock import AsyncMock
        with patch("src.workflow.engine.WorkflowEngine") as MockEngine:
            MockEngine.return_value.execute_async = AsyncMock(side_effect=runtime_error)
            from src.mcp.tools import archive_url

            result = await archive_url(url="https://example.com/article")

        assert result["success"] is False
        assert result["terminal"] == "error"
        assert result["issues"][0]["code"] == ErrorCode.SSRF_RESOLUTION_FAILED.value
        assert result["issues"][0]["stage"] == "safe_fetch_dns"
        assert result["issues"][0]["recoverable"] is True


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
        assert result["terminal"] == "error"
        assert result["issues"][0]["code"] == ErrorCode.WORKFLOW_CONFIG_INVALID.value
        assert result["error"] == "工作流配置无效"

    @pytest.mark.asyncio
    async def test_reject_too_long_text(self):
        """超长文本应被拒绝。"""
        from src.mcp.tools import archive_text
        result = await archive_text(text="A" * 100001)
        assert result["success"] is False
        assert result["error"] == "工作流配置无效"

    @pytest.mark.asyncio
    async def test_successful_text_archive(self):
        """正常文本归档应返回成功结果。"""
        mock_entry = MagicMock()
        mock_entry.title = "自动标题"
        mock_entry.content = "测试内容"
        mock_entry.tags = ["text"]

        mock_result = WorkflowResult(
            success=True,
            terminal="success",
            data={
                "knowledge_id": 43,
                "title": "自动标题",
                "status": "ready",
                "core_committed": True,
                "file_path": "/vault/test.md",
                "tags": ["text"],
            },
        )

        from unittest.mock import AsyncMock
        with patch("src.processors.text_fallback_processor.TextFallbackProcessor") as MockProcessor, \
             patch("src.workflow.engine.WorkflowEngine") as MockEngine:
            mock_proc_instance = MagicMock()
            mock_proc_instance.process_text = AsyncMock(return_value=mock_entry)
            MockProcessor.return_value = mock_proc_instance

            mock_engine_instance = MagicMock()
            mock_engine_instance.execute_async = AsyncMock(return_value=mock_result)
            MockEngine.return_value = mock_engine_instance

            from src.mcp.tools import archive_text
            result = await archive_text(text="这是一段测试文本内容")

        assert result["success"] is True
        assert result["terminal"] == "success"
        assert result["warnings"] == []
        assert result["issues"] == []
        assert result["knowledge_id"] == 43
        assert result["entry_locator"] == "pkv://entries/43"
        assert "file_path" not in result
        call = mock_engine_instance.execute_async.await_args
        assert call.args[0] == "archive-text"
        assert call.args[1]["skip_review"] is True
        assert call.args[1]["skip_sharpen"] is True

    @pytest.mark.asyncio
    async def test_custom_title_override(self):
        """提供 title 应覆盖自动标题。"""
        mock_entry = MagicMock()
        mock_entry.title = "自动标题"
        mock_entry.content = "测试内容"
        mock_entry.tags = ["text"]

        mock_result = WorkflowResult(
            success=True,
            terminal="success",
            data={
                "knowledge_id": 44,
                "title": "自定义标题",
                "status": "ready",
                "core_committed": True,
                "file_path": "/vault/test.md",
                "tags": ["text"],
            },
        )

        from unittest.mock import AsyncMock
        with patch("src.processors.text_fallback_processor.TextFallbackProcessor") as MockProcessor, \
             patch("src.workflow.engine.WorkflowEngine") as MockEngine:
            mock_proc_instance = MagicMock()
            mock_proc_instance.process_text = AsyncMock(return_value=mock_entry)
            MockProcessor.return_value = mock_proc_instance

            mock_engine_instance = MagicMock()
            mock_engine_instance.execute_async = AsyncMock(return_value=mock_result)
            MockEngine.return_value = mock_engine_instance

            from src.mcp.tools import archive_text
            await archive_text(text="内容", title="自定义标题")

        # 验证 title 被覆盖
        assert mock_entry.title == "自定义标题"

    @pytest.mark.asyncio
    async def test_text_workflow_failure_preserves_do_not_retry(self):
        operation_id = "1234567890abcdef1234567890abcdef"
        mock_entry = MagicMock(title="标题", content="内容", tags=[])
        mock_result = WorkflowResult(
            success=False,
            terminal="error",
            errors=["存储需修复"],
            issues=[
                {
                    "code": ErrorCode.STORAGE_REPAIR_REQUIRED.value,
                    "message": "核心存储已提交，需先修复",
                    "severity": "error",
                    "stage": "store_entry",
                    "recoverable": False,
                }
            ],
            data={
                "knowledge_id": 45,
                "status": "repair_required",
                "operation_id": operation_id,
                "core_committed": True,
                "do_not_retry": True,
                "repair_actions": ["repair_secondary_indexes"],
            },
        )

        from unittest.mock import AsyncMock
        with patch("src.processors.text_fallback_processor.TextFallbackProcessor") as MockProcessor, \
             patch("src.workflow.engine.WorkflowEngine") as MockEngine:
            MockProcessor.return_value.process_text = AsyncMock(return_value=mock_entry)
            MockEngine.return_value.execute_async = AsyncMock(return_value=mock_result)
            from src.mcp.tools import archive_text

            result = await archive_text(text="需要归档的内容")

        assert result["success"] is False
        assert result["terminal"] == "error"
        assert result["knowledge_id"] == 45
        assert result["entry_locator"] == "pkv://entries/45"
        assert result["storage_status"] == "repair_required"
        assert result["operation_id"] == operation_id
        assert result["do_not_retry"] is True
        assert result["issues"][0]["code"] == ErrorCode.STORAGE_REPAIR_REQUIRED.value

    @pytest.mark.asyncio
    async def test_degraded_terminal_requires_committed_knowledge_id(self):
        secret = "pkv-archive-text-result-canary"
        mock_entry = MagicMock(title="安全标题", content="内容", tags=[])
        malformed_result = _DiagnosticAccessBombResult(
            terminal="degraded",
            data={"title": secret},
        )

        from unittest.mock import AsyncMock
        from src.mcp import tools

        application = MagicMock()
        application.archive_text = AsyncMock(return_value=malformed_result)
        with patch.object(tools, "get_application", return_value=application):
            result = await tools.archive_text(text="需要归档的内容")

        assert result["success"] is False
        assert result["terminal"] == "error"
        assert result["issues"][0]["code"] == ErrorCode.WORKFLOW_STEP_FAILED.value
        assert result["issues"][0]["stage"] == "workflow_result"
        assert "knowledge_id" not in result
        assert secret not in repr(result)
        assert malformed_result.diagnostic_accesses == []

    @pytest.mark.asyncio
    async def test_degraded_terminal_requires_diagnostic_without_leak(self):
        secret = "pkv-archive-text-diagnostics-canary"
        mock_entry = MagicMock(title="安全标题", content="内容", tags=[])
        malformed_result = WorkflowResult(
            success=True,
            terminal="degraded",
            errors=[],
            warnings=[],
            issues=[],
            data={
                "knowledge_id": 46,
                "title": secret,
                "status": "degraded",
                "core_committed": True,
            },
        )

        from unittest.mock import AsyncMock
        with patch(
            "src.processors.text_fallback_processor.TextFallbackProcessor"
        ) as MockProcessor, patch(
            "src.workflow.engine.WorkflowEngine"
        ) as MockEngine:
            MockProcessor.return_value.process_text = AsyncMock(return_value=mock_entry)
            MockEngine.return_value.execute_async = AsyncMock(
                return_value=malformed_result
            )
            from src.mcp.tools import archive_text

            result = await archive_text(text="需要归档的内容")

        assert result["success"] is False
        assert result["terminal"] == "error"
        assert result["issues"][0]["code"] == ErrorCode.WORKFLOW_STEP_FAILED.value
        assert result["issues"][0]["stage"] == "workflow_result"
        assert secret not in repr(result)

    @pytest.mark.asyncio
    async def test_path_shaped_text_never_reads_local_file_or_exposes_content(
        self,
        tmp_path,
    ):
        from unittest.mock import AsyncMock
        from src.mcp.resources import get_entry_content
        from src.mcp.tools import archive_text
        from src.processors.text_fallback_processor import TextFallbackProcessor

        secret = "pkv-local-file-content-canary"
        external_file = tmp_path / "external-secret.txt"
        external_file.write_text(secret, encoding="utf-8")
        fake_ai = MagicMock()
        fake_ai.summarize.return_value = "路径形状文本"
        fake_ai.extract_tags.return_value = ["text"]
        processor = TextFallbackProcessor(deepseek_client=fake_ai)
        workflow = MagicMock()
        workflow.execute_async = AsyncMock(
            return_value=WorkflowResult(
                success=True,
                terminal="success",
                data={
                    "knowledge_id": 91,
                    "title": "路径形状文本",
                    "tags": ["text"],
                    "status": "ready",
                    "core_committed": True,
                },
            )
        )

        with patch(
            "src.processors.text_fallback_processor.TextFallbackProcessor",
            return_value=processor,
        ), patch(
            "src.workflow.engine.WorkflowEngine",
            return_value=workflow,
        ), patch.object(
            Path,
            "exists",
            side_effect=AssertionError("unexpected local path lookup"),
        ) as exists, patch.object(
            Path,
            "read_text",
            side_effect=AssertionError("unexpected local file read"),
        ) as read_text:
            result = await archive_text(text=str(external_file))

        assert result["terminal"] == "success"
        exists.assert_not_called()
        read_text.assert_not_called()
        workflow_payload = workflow.execute_async.await_args.args[1]
        archived_entry = workflow_payload["entry"]
        assert secret not in archived_entry.content

        vault_dir = tmp_path / "vault"
        vault_dir.mkdir()
        persisted_path = vault_dir / "entry.md"
        persisted_path.write_text(archived_entry.content, encoding="utf-8")
        store = MagicMock()
        store.query_by_id.return_value = {
            "knowledge_id": 91,
            "title": "路径形状文本",
            "source_type": "text",
            "file_path": str(persisted_path),
        }
        markdown_store = MagicMock(vault_dir=vault_dir)
        markdown_store.load.return_value = archived_entry
        with patch(
            "src.mcp.resources.get_sqlite_store",
            return_value=store,
        ), patch(
            "src.mcp.resources.get_markdown_store",
            return_value=markdown_store,
        ):
            resource_content = await get_entry_content("91")

        assert secret not in resource_content

    @pytest.mark.asyncio
    async def test_explicit_local_import_path_never_reaches_mcp_resource(
        self,
        tmp_path,
    ):
        from src.mcp.resources import get_entry_content
        from src.processors.text_fallback_processor import TextFallbackProcessor

        private_root = tmp_path / "PRIVATE-IMPORT-PATH-CANARY"
        private_root.mkdir()
        imported_file = private_root / "note.md"
        imported_file.write_text("# Imported title\nSafe body", encoding="utf-8")
        fake_ai = MagicMock()
        fake_ai.summarize.return_value = "安全摘要"
        fake_ai.extract_tags.return_value = ["text"]

        archived_entry = await TextFallbackProcessor(
            deepseek_client=fake_ai
        ).process_file(imported_file)

        assert archived_entry.source_url is None
        assert archived_entry.metadata["source_url"] is None
        assert str(private_root) not in repr(archived_entry)

        vault_dir = tmp_path / "vault-explicit-import"
        vault_dir.mkdir()
        persisted_path = vault_dir / "entry.md"
        persisted_path.write_text(archived_entry.content, encoding="utf-8")
        store = MagicMock()
        store.query_by_id.return_value = {
            "knowledge_id": 92,
            "title": archived_entry.title,
            "source_type": "text",
            "file_path": str(persisted_path),
        }
        markdown_store = MagicMock(vault_dir=vault_dir)
        markdown_store.load.return_value = archived_entry
        with patch(
            "src.mcp.resources.get_sqlite_store",
            return_value=store,
        ), patch(
            "src.mcp.resources.get_markdown_store",
            return_value=markdown_store,
        ):
            resource_content = await get_entry_content("92")

        assert "PRIVATE-IMPORT-PATH-CANARY" not in resource_content
        assert str(private_root) not in resource_content

    @pytest.mark.asyncio
    async def test_degraded_terminal_with_rejected_storage_is_stable_error(self):
        mock_entry = MagicMock(title="安全标题", content="内容", tags=[])
        result_object = WorkflowResult(
            success=True,
            terminal="degraded",
            warnings=["存储操作被拒绝"],
            data={
                "knowledge_id": 48,
                "title": "拒绝条目",
                "status": "rejected",
                "core_committed": False,
                "do_not_retry": False,
            },
        )

        from unittest.mock import AsyncMock
        with patch(
            "src.processors.text_fallback_processor.TextFallbackProcessor"
        ) as MockProcessor, patch(
            "src.workflow.engine.WorkflowEngine"
        ) as MockEngine:
            MockProcessor.return_value.process_text = AsyncMock(return_value=mock_entry)
            MockEngine.return_value.execute_async = AsyncMock(return_value=result_object)
            from src.mcp.tools import archive_text

            result = await archive_text(text="需要归档的内容")

        assert result["success"] is False
        assert result["terminal"] == "error"
        assert result["storage_status"] == "rejected"
        assert result["do_not_retry"] is False
        assert result["core_committed"] is False
        assert result["knowledge_id"] == 48
        assert result["issues"][0]["code"] == ErrorCode.WORKFLOW_STEP_FAILED.value

    @pytest.mark.asyncio
    async def test_text_archive_redacts_backend_warning_and_issue_prose(self):
        secret = "sk-pkv-archive-text-canary"
        raw_detail = (
            f"processor_resource_limit: boom {secret} "
            "C:\\Users\\private query=private-search"
        )
        mock_entry = MagicMock(title="安全标题", content="内容", tags=[])
        mock_result = WorkflowResult(
            success=True,
            terminal="degraded",
            warnings=[raw_detail],
            issues=[
                {
                    "code": ErrorCode.PROCESSOR_RESOURCE_LIMIT.value,
                    "message": raw_detail,
                    "severity": "warning",
                    "stage": "store_entry",
                    "recoverable": True,
                    "count": True,
                    "limit": 5,
                    "raw_context": raw_detail,
                }
            ],
            data={
                "knowledge_id": 46,
                "title": "安全标题",
                "status": "degraded",
                "core_committed": True,
            },
        )

        from unittest.mock import AsyncMock
        with patch("src.processors.text_fallback_processor.TextFallbackProcessor") as MockProcessor, \
             patch("src.workflow.engine.WorkflowEngine") as MockEngine:
            MockProcessor.return_value.process_text = AsyncMock(return_value=mock_entry)
            MockEngine.return_value.execute_async = AsyncMock(return_value=mock_result)
            from src.mcp.tools import archive_text

            result = await archive_text(text="需要归档的内容")

        rendered = repr(result)
        assert result["terminal"] == "degraded"
        assert result["warnings"] == ["处理器资源预算已达上限"]
        assert result["issues"][0]["message"] == "处理器资源预算已达上限"
        assert "count" not in result["issues"][0]
        assert result["issues"][0]["limit"] == 5
        assert "raw_context" not in result["issues"][0]
        assert secret not in rendered
        assert "Users" not in rendered
        assert "private-search" not in rendered


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
        assert result["status"] == "invalid"
        assert result["issues"][0]["code"] == ErrorCode.RETRIEVAL_INVALID_QUERY.value

    @pytest.mark.asyncio
    @pytest.mark.parametrize("knowledge_id", [True, "0", "-1"])
    async def test_non_positive_or_boolean_id_is_rejected(self, knowledge_id):
        with patch("src.mcp.tools.get_sqlite_store") as mock_get_store:
            from src.mcp.tools import get_related

            result = await get_related(knowledge_id=knowledge_id)

        assert result["status"] == "invalid"
        mock_get_store.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("limit", [0, True, "5"])
    async def test_invalid_limit_is_rejected(self, limit):
        from src.mcp.tools import get_related

        result = await get_related(knowledge_id="1", limit=limit)

        assert result["status"] == "invalid"
        assert result["issues"][0]["stage"] == "limit_validation"

    @pytest.mark.asyncio
    async def test_entry_not_found(self):
        """不存在的条目应返回 error。"""
        mock_store = MagicMock()
        mock_store.query_by_id.return_value = None

        with patch("src.mcp.tools.get_sqlite_store", return_value=mock_store):
            from src.mcp.tools import get_related
            result = await get_related(knowledge_id="999")

        assert "error" in result
        assert result["status"] == "no_hits"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "store_error",
        [
            PKVRuntimeError(
                ErrorCode.DATABASE_MISSING,
                "secret=pkv-related-canary C:\\Users\\private\\vault.db",
                stage="database_open",
                recoverable=False,
            ),
            RuntimeError("secret=pkv-related-canary C:\\Users\\private\\vault.db"),
        ],
    )
    async def test_entry_lookup_failure_is_stable_and_redacted(
        self,
        store_error,
        caplog,
    ):
        with patch("src.mcp.tools.get_sqlite_store", side_effect=store_error):
            from src.mcp.tools import get_related

            result = await get_related(knowledge_id="1")

        rendered = repr(result)
        assert result["status"] == "error"
        assert result["issues"][0]["code"] in {
            ErrorCode.DATABASE_MISSING.value,
            ErrorCode.RETRIEVAL_BACKEND_FAILED.value,
        }
        assert "pkv-related-canary" not in rendered
        assert "Users" not in rendered
        assert "pkv-related-canary" not in caplog.text

    @pytest.mark.asyncio
    async def test_no_vector_index(self):
        """无向量索引时应优雅降级。"""
        mock_store = MagicMock()
        mock_store.query_by_id.return_value = MOCK_ENTRY_DB
        mock_application = MagicMock()
        mock_application.readonly_vector_store = None

        with patch("src.mcp.tools.get_sqlite_store", return_value=mock_store), \
             patch("src.mcp.tools.get_application", return_value=mock_application):
            from src.mcp.tools import get_related
            result = await get_related(knowledge_id="1")

        assert result["results"] == []
        assert result["status"] == "degraded"
        assert result["issues"][0]["code"] == ErrorCode.RETRIEVAL_INDEX_UNAVAILABLE.value
        assert "暂无向量索引" in result.get("message", "")

    @pytest.mark.asyncio
    async def test_limit_clamped(self):
        """limit 应被限制在 [1, 20]。"""
        mock_store = MagicMock()
        mock_store.query_by_id.return_value = MOCK_ENTRY_DB

        mock_vector_store = MagicMock()
        mock_vector_store.get_doc_vector.return_value = None
        mock_application = MagicMock()
        mock_application.readonly_vector_store = mock_vector_store

        with patch("src.mcp.tools.get_sqlite_store", return_value=mock_store), \
             patch("src.mcp.tools.get_application", return_value=mock_application):
            from src.mcp.tools import get_related
            result = await get_related(knowledge_id="1", limit=100)

        # 结果应明确标记降级，而不是把缺失向量伪装成普通空命中。
        assert result["status"] == "degraded"
        assert result["issues"][0]["code"] == ErrorCode.RETRIEVAL_INDEX_UNAVAILABLE.value
        mock_vector_store.get_doc_vector.assert_called_once_with(1)

    @pytest.mark.asyncio
    async def test_vector_search_exception(self):
        """向量搜索异常应优雅降级。"""
        mock_store = MagicMock()
        mock_store.query_by_id.return_value = MOCK_ENTRY_DB
        mock_vector_store = MagicMock()
        mock_vector_store.get_doc_vector.side_effect = Exception("索引加载失败")
        mock_application = MagicMock()
        mock_application.readonly_vector_store = mock_vector_store

        with patch("src.mcp.tools.get_sqlite_store", return_value=mock_store), \
             patch("src.mcp.tools.get_application", return_value=mock_application):
            from src.mcp.tools import get_related
            result = await get_related(knowledge_id="1")

        assert result["results"] == []
        assert result["status"] == "error"
        assert result["issues"][0]["code"] == ErrorCode.RETRIEVAL_BACKEND_FAILED.value
        assert "不可用" in result.get("message", "")

    @pytest.mark.asyncio
    async def test_vector_runtime_error_preserves_provider_or_index_code(self):
        mock_store = MagicMock()
        mock_store.query_by_id.return_value = MOCK_ENTRY_DB
        runtime_error = PKVRuntimeError(
            ErrorCode.RETRIEVAL_INDEX_UNAVAILABLE,
            "向量索引不可用",
            stage="vector_index_load",
            recoverable=True,
        )
        mock_vector_store = MagicMock()
        mock_vector_store.get_doc_vector.side_effect = runtime_error
        mock_application = MagicMock()
        mock_application.readonly_vector_store = mock_vector_store

        with patch("src.mcp.tools.get_sqlite_store", return_value=mock_store), \
             patch("src.mcp.tools.get_application", return_value=mock_application):
            from src.mcp.tools import get_related

            result = await get_related(knowledge_id="1")

        assert result["status"] == "error"
        assert result["issues"][0]["code"] == ErrorCode.RETRIEVAL_INDEX_UNAVAILABLE.value
        assert result["issues"][0]["stage"] == "vector_index_load"


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
            evidence_payload={"raw_target": "./beta.md"},
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

        assert result["status"] == "success"
        assert result["issues"] == []
        assert result["seed_knowledge_id"] == 1
        assert result["total_nodes"] == 2
        assert result["schema_version"] == "phase_b.v1"
        assert result["implementation_level"] == "baseline"
        assert result["evidence_count"] == 1
        assert "confidence" in result
        assert "coverage" in result
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

        assert result["status"] == "success"
        assert result["issues"] == []
        assert result["found"] is True
        assert result["explanation_type"] == "direct"
        assert result["schema_version"] == "phase_b.v1"
        assert result["implementation_level"] == "baseline"
        assert result["evidence_count"] == 1
        assert result["confidence"] == 0.9
        assert result["coverage"] == 1.0
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
        relation = RelationRecord(
            relation_id=9,
            source_knowledge_id=1,
            target_knowledge_id=2,
            relation_type=RelationType.RELATED_DOCUMENT,
            relation_source_type=RelationSourceType.FRONTMATTER_RELATED_DOCS,
            evidence_payload={"field": "related_docs"},
        )
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
                    citation_locator=build_entry_locator(1),
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
                    relation_path=[relation],
                    relation_evidence_items=[
                        {
                            "step_index": 0,
                            "relation_type": "related_document",
                            "relation_source_type": "frontmatter_related_docs",
                            "direction": "directed",
                            "weight": 1.0,
                            "source_knowledge_id": 1,
                            "target_knowledge_id": 2,
                            "evidence_payload": {"field": "related_docs"},
                        }
                    ],
                    citation_locator=build_entry_locator(2),
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

        assert result["status"] == "success"
        assert result["issues"] == []
        assert result["found"] is True
        assert result["seed_knowledge_id"] == 1
        assert result["total_evidence"] == 2
        assert result["related_evidence_count"] == 1
        assert result["schema_version"] == "phase_b.v1"
        assert result["implementation_level"] == "baseline"
        assert result["evidence_count"] == 2
        assert "confidence" in result
        assert "coverage" in result
        mock_service.collect_evidence.assert_called_once_with(
            question="Alpha 和 Beta 有什么关系？",
            top_k=5,
            relation_max_depth=2,
            include_chunks=False,
        )

    @pytest.mark.asyncio
    async def test_success_with_include_chunks(self):
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
                    content_preview="Alpha chunk",
                    chunk_id=101,
                    chunk_index=0,
                    chunk_text="Alpha chunk",
                    retrieval_rank=1,
                    retrieval_score=0.95,
                    ranking_score=0.91,
                    coverage_score=0.80,
                    freshness_score=0.70,
                    relation_score=1.0,
                    is_seed=True,
                    citation_locator=build_chunk_locator(1, chunk_id=101),
                ),
            ],
            summary="围绕问题共聚合 1 条证据",
            chunk_retrieval_status="success",
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
                include_chunks=True,
            )

        assert result["status"] == "success"
        assert result["issues"] == []
        assert result["evidence"][0]["chunk_id"] == 101
        assert result["evidence"][0]["chunk_index"] == 0
        assert result["evidence"][0]["chunk_text"] == "Alpha chunk"
        assert result["evidence"][0]["ranking_score"] == 0.91
        assert result["evidence"][0]["coverage_score"] == 0.8
        assert result["chunk_retrieval_status"] == "success"
        assert result["schema_version"] == "phase_b.v1"
        assert result["implementation_level"] == "baseline"
        mock_service.collect_evidence.assert_called_once_with(
            question="Alpha 和 Beta 有什么关系？",
            top_k=5,
            relation_max_depth=2,
            include_chunks=True,
        )

    @pytest.mark.asyncio
    async def test_include_chunks_degraded_is_observable(self):
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
                    relation_score=1.0,
                    is_seed=True,
                    citation_locator=build_entry_locator(1),
                )
            ],
            summary="围绕问题共聚合 1 条证据",
            limitation_notes=[
                "chunk_degraded[search_error] chunk 检索异常，已降级为文档级证据"
            ],
            chunk_retrieval_status="search_error",
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
                include_chunks=True,
            )

        assert result["status"] == "degraded"
        assert result["issues"][0]["code"] == ErrorCode.RETRIEVAL_BACKEND_FAILED.value
        assert result["found"] is True
        assert result["chunk_retrieval_status"] == "search_error"
        assert (
            "chunk_degraded[search_error] chunk 检索异常，已降级为文档级证据"
            in result["limitation_notes"]
        )
        mock_service.collect_evidence.assert_called_once_with(
            question="Alpha 和 Beta 有什么关系？",
            top_k=5,
            relation_max_depth=2,
            include_chunks=True,
        )

    @pytest.mark.asyncio
    async def test_include_chunks_path_unavailable_is_observable(self):
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
                    relation_score=1.0,
                    is_seed=True,
                    citation_locator=build_entry_locator(1),
                )
            ],
            summary="围绕问题共聚合 1 条证据",
            limitation_notes=[
                "chunk_degraded[path_unavailable] chunk 检索路径不可用，已降级为文档级证据"
            ],
            chunk_retrieval_status="path_unavailable",
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
                include_chunks=True,
            )

        assert result["status"] == "degraded"
        assert result["issues"][0]["code"] == ErrorCode.RETRIEVAL_INDEX_UNAVAILABLE.value
        assert result["found"] is True
        assert result["chunk_retrieval_status"] == "path_unavailable"
        assert (
            "chunk_degraded[path_unavailable] chunk 检索路径不可用，已降级为文档级证据"
            in result["limitation_notes"]
        )
        mock_service.collect_evidence.assert_called_once_with(
            question="Alpha 和 Beta 有什么关系？",
            top_k=5,
            relation_max_depth=2,
            include_chunks=True,
        )


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
        mock_service.find_bridges.return_value = _valid_bridge_result()

        with patch("src.mcp.tools.get_exploration_service", return_value=mock_service):
            from src.mcp.tools import find_bridges

            result = await find_bridges(seed_knowledge_id="1", top_k=5, max_depth=2)

        assert result["status"] == "success"
        assert result["issues"] == []
        assert result["found"] is True
        assert result["total_bridges"] == 1
        assert result["schema_version"] == "phase_b.v1"
        assert result["implementation_level"] == "partial"
        assert result["evidence_count"] == 1
        assert "confidence" in result
        assert "coverage" in result
        assert "evidence_sources" in result
        assert "structural_bridge_score" in result["items"][0]
        assert "semantic_bridge_score" in result["items"][0]
        assert result["items"][0]["evidence_path"][0]["hop_index"] == 1
        mock_service.find_bridges.assert_called_once()


class TestTimelineOf:
    """timeline_of Tool 测试。"""

    @pytest.mark.asyncio
    async def test_reject_empty_topic(self):
        from src.mcp.tools import timeline_of

        result = await timeline_of(topic="  ")

        assert "error" in result

    @pytest.mark.asyncio
    async def test_reject_invalid_sort_order(self):
        from src.mcp.tools import timeline_of

        result = await timeline_of(topic="AI", sort_order="newest")

        assert result["status"] == "invalid"
        assert result["issues"][0]["code"] == ErrorCode.RETRIEVAL_INVALID_QUERY.value
        assert result["error"] == "检索参数无效"

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
                    time_value="2026-03-01 08:00:00",
                    event_time="2026-03-01 08:00:00",
                    published_at="2026-03-02 08:00:00",
                    archived_at="2026-03-10 10:00:00",
                    time_source="event_time",
                    time_source_field="event_time",
                    time_precision="structured_field",
                    source_type="generic",
                    source_url="https://example.test/alpha",
                    source="https://example.test/alpha",
                    citation_locator="pkv://entries/1/metadata/event_time",
                    abstract="Alpha 摘要",
                    tags=["AI"],
                    retrieval_score=0.91,
                )
            ],
            summary="时间线已生成",
            inferred_time_field="event_time",
            time_source_priority=["event_time", "published_at", "archived_at"],
            evidence_sources=TIMELINE_EVIDENCE_SOURCES,
            limitation_notes=["partial"],
        )

        with patch("src.mcp.tools.get_exploration_service", return_value=mock_service):
            from src.mcp.tools import timeline_of

            result = await timeline_of(topic="AI Timeline", top_k=5, sort_order="asc")

        assert result["status"] == "success"
        assert result["issues"] == []
        assert result["found"] is True
        assert result["total_points"] == 1
        assert result["schema_version"] == "phase_b.v1"
        assert result["implementation_level"] == "partial"
        assert result["evidence_count"] == 1
        assert "confidence" in result
        assert "coverage" in result
        assert "evidence_sources" in result
        assert "time_source_priority" in result
        assert "time_source" in result["items"][0]
        assert result["items"][0]["time_value"] == "2026-03-01 08:00:00"
        assert result["items"][0]["event_time"] == "2026-03-01 08:00:00"
        assert result["items"][0]["published_at"] == "2026-03-02 08:00:00"
        assert result["items"][0]["archived_at"] == "2026-03-10 10:00:00"
        assert result["items"][0]["source"] == "https://example.test/alpha"
        assert (
            result["items"][0]["citation_locator"]
            == "pkv://entries/1/metadata/event_time"
        )
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
        candidates_a = [
            ContrastCandidateItem(
                knowledge_id=1,
                title="Alpha",
                abstract="Alpha 摘要",
                archived_at="2026-03-10 10:00:00",
                source_type="generic",
                source=build_entry_locator(1),
                citation_locator=build_entry_locator(1),
                tags=["AI", "共同"],
                retrieval_score=0.93,
            )
        ]
        candidates_b = [
            ContrastCandidateItem(
                knowledge_id=2,
                title="Beta",
                abstract="Beta 摘要",
                archived_at="2026-03-11 10:00:00",
                source_type="generic",
                source=build_entry_locator(2),
                citation_locator=build_entry_locator(2),
                tags=["时间线", "共同"],
                retrieval_score=0.87,
            )
        ]
        mock_service.contrast.return_value = ContrastResult(
            topic_a="Topic A",
            topic_b="Topic B",
            found=True,
            topic_a_candidates=candidates_a,
            topic_b_candidates=candidates_b,
            shared_tags=["共同"],
            only_a_tags=["AI"],
            only_b_tags=["时间线"],
            overlap_knowledge_ids=[],
            comparison_dimensions=_empty_contrast_dimensions(
                candidates_a,
                candidates_b,
            ),
            summary="对比完成",
            evidence_sources=CONTRAST_EVIDENCE_SOURCES,
            limitation_notes=["partial"],
        )

        with patch("src.mcp.tools.get_exploration_service", return_value=mock_service):
            from src.mcp.tools import contrast

            result = await contrast(topic_a="Topic A", topic_b="Topic B", top_k=5)

        assert result["status"] == "success"
        assert result["issues"] == []
        assert result["found"] is True
        assert result["schema_version"] == "phase_b.v1"
        assert result["implementation_level"] == "partial"
        assert result["evidence_count"] == 2
        assert "confidence" in result
        assert "coverage" in result
        assert "evidence_sources" in result
        assert "comparison_dimensions" in result
        assert result["comparison_dimensions"]["provenance"]["shared_tags"]
        assert result["shared_tags"] == ["共同"]
        mock_service.contrast.assert_called_once()


def _empty_relation_domain_result(handler_name, kwargs):
    if handler_name == "query_subgraph":
        return RelationSubgraphResult(
            seed_knowledge_id=1,
            max_depth=kwargs.get("depth", 2),
            nodes=[RelationSubgraphNode(knowledge_id=1, depth=0)],
            edges=[],
        )
    if handler_name == "explain_relation":
        max_depth = kwargs.get("max_depth", 2)
        return RelationExplanationResult(
            source_knowledge_id=1,
            target_knowledge_id=2,
            found=False,
            explanation_type="not_found",
            hops=0,
            summary=f"未找到 1 与 2 在 {max_depth} 跳内的关系解释",
        )
    if handler_name == "collect_evidence":
        return CollectedEvidenceResult(
            question=kwargs["question"].strip(),
            found=False,
        )
    if handler_name == "find_bridges":
        return BridgeDiscoveryResult(
            seed_knowledge_id=1,
            found=False,
            max_depth=kwargs.get("max_depth", 2),
            evidence_sources=BRIDGE_EVIDENCE_SOURCES,
            limitation_notes=["partial"],
        )
    if handler_name == "timeline_of":
        return TimelineResult(
            topic=kwargs["topic"].strip(),
            found=False,
            time_source_priority=["event_time", "published_at", "archived_at"],
            evidence_sources=TIMELINE_EVIDENCE_SOURCES,
            limitation_notes=["partial"],
        )
    if handler_name == "contrast":
        return ContrastResult(
            topic_a=kwargs["topic_a"].strip(),
            topic_b=kwargs["topic_b"].strip(),
            found=False,
            comparison_dimensions=_empty_contrast_dimensions(),
            evidence_sources=CONTRAST_EVIDENCE_SOURCES,
            limitation_notes=["partial"],
        )
    raise AssertionError(f"unknown relation handler fixture: {handler_name}")


class TestReadonlyRelationEnvelopeMatrix:
    """All six relation/exploration adapters share the same public envelope rules."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("handler_name", "factory_path", "kwargs"),
        [
            (
                "query_subgraph",
                "src.mcp.tools.get_relation_query_service",
                {"knowledge_id": "1", "depth": 0},
            ),
            (
                "query_subgraph",
                "src.mcp.tools.get_relation_query_service",
                {"knowledge_id": "1", "max_nodes": True},
            ),
            (
                "explain_relation",
                "src.mcp.tools.get_relation_query_service",
                {"source_knowledge_id": "0", "target_knowledge_id": "2"},
            ),
            (
                "explain_relation",
                "src.mcp.tools.get_relation_query_service",
                {"source_knowledge_id": "1", "target_knowledge_id": "2", "max_depth": False},
            ),
            (
                "collect_evidence",
                "src.mcp.tools.get_evidence_collection_service",
                {"question": "问题", "top_k": -1},
            ),
            (
                "collect_evidence",
                "src.mcp.tools.get_evidence_collection_service",
                {"question": "问题", "relation_max_depth": True},
            ),
            (
                "find_bridges",
                "src.mcp.tools.get_exploration_service",
                {"seed_knowledge_id": True},
            ),
            (
                "find_bridges",
                "src.mcp.tools.get_exploration_service",
                {"seed_knowledge_id": "1", "top_k": 0},
            ),
            (
                "timeline_of",
                "src.mcp.tools.get_exploration_service",
                {"topic": "AI", "top_k": False},
            ),
            (
                "contrast",
                "src.mcp.tools.get_exploration_service",
                {"topic_a": "A", "topic_b": "B", "top_k": -1},
            ),
        ],
    )
    async def test_invalid_numeric_inputs_never_construct_service(
        self,
        handler_name,
        factory_path,
        kwargs,
    ):
        from src.mcp import tools as mcp_tools

        with patch(factory_path) as factory:
            result = await getattr(mcp_tools, handler_name)(**kwargs)

        assert result["status"] == "invalid"
        assert result["issues"][0]["code"] == ErrorCode.RETRIEVAL_INVALID_QUERY.value
        factory.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("handler_name", "kwargs"),
        [
            (
                "query_subgraph",
                {"knowledge_id": "1", "relation_types": "references"},
            ),
            (
                "query_subgraph",
                {"knowledge_id": "1", "relation_types": [""]},
            ),
            (
                "query_subgraph",
                {"knowledge_id": "1", "relation_types": [True]},
            ),
            (
                "query_subgraph",
                {
                    "knowledge_id": "1",
                    "relation_types": ["pkv-relation-filter-canary"],
                },
            ),
            (
                "explain_relation",
                {
                    "source_knowledge_id": "1",
                    "target_knowledge_id": "2",
                    "relation_types": "references",
                },
            ),
            (
                "explain_relation",
                {
                    "source_knowledge_id": "1",
                    "target_knowledge_id": "2",
                    "relation_types": ["   "],
                },
            ),
            (
                "explain_relation",
                {
                    "source_knowledge_id": "1",
                    "target_knowledge_id": "2",
                    "relation_types": [object()],
                },
            ),
            (
                "explain_relation",
                {
                    "source_knowledge_id": "1",
                    "target_knowledge_id": "2",
                    "relation_types": ["pkv-relation-filter-canary"],
                },
            ),
        ],
    )
    async def test_invalid_relation_types_never_construct_service(
        self,
        handler_name,
        kwargs,
    ):
        from src.mcp import tools as mcp_tools

        with patch("src.mcp.tools.get_relation_query_service") as factory:
            result = await getattr(mcp_tools, handler_name)(**kwargs)

        assert result["status"] == "invalid"
        assert result["issues"][0]["code"] == ErrorCode.RETRIEVAL_INVALID_QUERY.value
        assert "pkv-relation-filter-canary" not in repr(result)
        factory.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("handler_name", "service_method", "kwargs"),
        [
            (
                "query_subgraph",
                "query_subgraph",
                {
                    "knowledge_id": "1",
                    "relation_types": [" references ", "version_of"],
                },
            ),
            (
                "explain_relation",
                "explain_relation",
                {
                    "source_knowledge_id": "1",
                    "target_knowledge_id": "2",
                    "relation_types": [" references ", "version_of"],
                },
            ),
        ],
    )
    async def test_relation_types_are_normalized_before_service_call(
        self,
        handler_name,
        service_method,
        kwargs,
    ):
        from src.mcp import tools as mcp_tools

        service = MagicMock()
        getattr(service, service_method).return_value = _empty_relation_domain_result(
            handler_name,
            kwargs,
        )
        with patch(
            "src.mcp.tools.get_relation_query_service",
            return_value=service,
        ):
            result = await getattr(mcp_tools, handler_name)(**kwargs)

        assert result["status"] == "no_hits"
        call_kwargs = getattr(service, service_method).call_args.kwargs
        assert call_kwargs["relation_types"] == ["references", "version_of"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("handler_name", "factory_path", "service_method", "kwargs"),
        [
            (
                "query_subgraph",
                "src.mcp.tools.get_relation_query_service",
                "query_subgraph",
                {"knowledge_id": "1"},
            ),
            (
                "explain_relation",
                "src.mcp.tools.get_relation_query_service",
                "explain_relation",
                {"source_knowledge_id": "1", "target_knowledge_id": "2"},
            ),
            (
                "collect_evidence",
                "src.mcp.tools.get_evidence_collection_service",
                "collect_evidence",
                {"question": "没有命中的问题"},
            ),
            (
                "find_bridges",
                "src.mcp.tools.get_exploration_service",
                "find_bridges",
                {"seed_knowledge_id": "1"},
            ),
            (
                "timeline_of",
                "src.mcp.tools.get_exploration_service",
                "timeline_of",
                {"topic": "没有命中的主题"},
            ),
            (
                "contrast",
                "src.mcp.tools.get_exploration_service",
                "contrast",
                {"topic_a": "A", "topic_b": "B"},
            ),
        ],
    )
    async def test_empty_domain_result_maps_to_no_hits(
        self,
        handler_name,
        factory_path,
        service_method,
        kwargs,
    ):
        from src.mcp import tools as mcp_tools

        service = MagicMock()
        domain_result = _empty_relation_domain_result(handler_name, kwargs)
        getattr(service, service_method).return_value = domain_result
        with patch(factory_path, return_value=service):
            result = await getattr(mcp_tools, handler_name)(**kwargs)

        assert result["status"] == "no_hits"
        assert result["issues"] == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "factory_error",
        [
            ValueError("secret=pkv-relation-canary"),
            PKVRuntimeError(
                ErrorCode.DATABASE_MISSING,
                "secret=pkv-relation-canary C:\\Users\\private",
                stage="database_open",
                recoverable=False,
            ),
            RuntimeError("secret=pkv-relation-canary C:\\Users\\private"),
        ],
    )
    async def test_service_factory_failures_are_stable_and_redacted(
        self,
        factory_error,
        caplog,
    ):
        with patch(
            "src.mcp.tools.get_relation_query_service",
            side_effect=factory_error,
        ):
            from src.mcp.tools import query_subgraph

            result = await query_subgraph(knowledge_id="1")

        assert result["status"] == "error"
        expected_code = (
            factory_error.code.value
            if isinstance(factory_error, PKVRuntimeError)
            else ErrorCode.RETRIEVAL_BACKEND_FAILED.value
        )
        assert result["issues"][0]["code"] == expected_code
        assert "pkv-relation-canary" not in repr(result)
        assert "pkv-relation-canary" not in caplog.text

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("handler_name", "service_method", "kwargs", "domain_result", "expected_stage"),
        [
            (
                "timeline_of",
                "timeline_of",
                {"topic": "AI"},
                _timeline_degraded_result(found=True),
                "timeline_retrieval",
            ),
            (
                "timeline_of",
                "timeline_of",
                {"topic": "AI"},
                _timeline_degraded_result(found=False),
                "timeline_retrieval",
            ),
            (
                "contrast",
                "contrast",
                {"topic_a": "A", "topic_b": "B"},
                _contrast_degraded_result(side="a", found=True),
                "contrast_topic_a_retrieval",
            ),
            (
                "contrast",
                "contrast",
                {"topic_a": "A", "topic_b": "B"},
                _contrast_degraded_result(side="b", found=False),
                "contrast_topic_b_retrieval",
            ),
        ],
    )
    async def test_exploration_retrieval_markers_map_to_degraded(
        self,
        handler_name,
        service_method,
        kwargs,
        domain_result,
        expected_stage,
    ):
        from src.mcp import tools as mcp_tools

        service = MagicMock()
        getattr(service, service_method).return_value = domain_result
        with patch("src.mcp.tools.get_exploration_service", return_value=service):
            result = await getattr(mcp_tools, handler_name)(**kwargs)

        assert result["status"] == "degraded"
        assert result["issues"][0]["code"] == ErrorCode.PROVIDER_UNAVAILABLE.value
        assert result["issues"][0]["stage"] == expected_stage
