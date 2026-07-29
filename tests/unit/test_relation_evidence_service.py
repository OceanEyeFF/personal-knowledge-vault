"""
Unit tests for EvidenceCollectionService.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.relations.evidence_service import EvidenceCollectionService  # noqa: E402
from src.relations.models import (  # noqa: E402
    CollectedEvidenceItem,
    RelationExplanationResult,
    RelationRecord,
    RelationSourceType,
    RelationType,
)
from src.retrieval.result import SearchResult  # noqa: E402
from src.storage.markdown_store import Entry  # noqa: E402


class StubQueryRouter:
    def __init__(self, results):
        self.results = results

    def search(self, query: str, limit: int = 10):
        return self.results[:limit]


class StubSQLiteStore:
    def __init__(self, entries):
        self.entries = entries

    def query_by_id(self, knowledge_id: int):
        return self.entries.get(knowledge_id)


class StubMarkdownStore:
    def __init__(self, content_map):
        self.content_map = {
            Path(file_path): content for file_path, content in content_map.items()
        }

    def load(self, file_path: Path):
        return Entry(
            title=f"Loaded-{file_path.stem}",
            source_type="generic",
            content=self.content_map[file_path],
        )


class StubRelationQueryService:
    def explain_relation(
        self,
        source_knowledge_id: int,
        target_knowledge_id: int,
        max_depth: int = 2,
        per_node_limit: int = 100,
    ):
        if target_knowledge_id == 2:
            relation = RelationRecord(
                source_knowledge_id=source_knowledge_id,
                target_knowledge_id=target_knowledge_id,
                relation_type=RelationType.RELATED_DOCUMENT,
                relation_source_type=RelationSourceType.FRONTMATTER_RELATED_DOCS,
                evidence_payload={"field": "related_docs"},
            )
            return RelationExplanationResult(
                source_knowledge_id=source_knowledge_id,
                target_knowledge_id=target_knowledge_id,
                found=True,
                explanation_type="direct",
                hops=1,
                path=[relation],
                supporting_relations=[relation],
                summary=(
                    f"{source_knowledge_id} -[{RelationType.RELATED_DOCUMENT.value}]-> "
                    f"{target_knowledge_id}"
                ),
                evidence_items=[
                    {
                        "step_index": 0,
                        "relation_type": RelationType.RELATED_DOCUMENT.value,
                        "relation_source_type": RelationSourceType.FRONTMATTER_RELATED_DOCS.value,
                        "direction": relation.direction.value,
                        "weight": relation.weight,
                        "source_knowledge_id": source_knowledge_id,
                        "target_knowledge_id": target_knowledge_id,
                        "evidence_payload": {"field": "related_docs"},
                    }
                ],
            )

        return RelationExplanationResult(
            source_knowledge_id=source_knowledge_id,
            target_knowledge_id=target_knowledge_id,
            found=False,
            explanation_type="not_found",
            hops=0,
            summary="未找到关系解释",
        )


class StubChunkSearcher:
    def __init__(self, results):
        self.results = results

    def search_chunks(self, query: str, limit: int = 10):
        return self.results[:limit]


class StubFailingChunkSearcher:
    def search_chunks(self, query: str, limit: int = 10):
        raise RuntimeError("chunk backend unavailable")


def _make_evidence_item(
    knowledge_id: int,
    *,
    retrieval_score: float = 0.5,
    is_seed: bool = False,
    chunk_index: int | None = None,
    chunk_text: str = "",
    content_preview: str = "",
    archived_at: str = "2026-03-10 10:00:00",
) -> CollectedEvidenceItem:
    return CollectedEvidenceItem(
        knowledge_id=knowledge_id,
        title=f"Item-{knowledge_id}",
        abstract="abstract",
        source_type="generic",
        archived_at=archived_at,
        tags=["AI"],
        content_preview=content_preview,
        chunk_index=chunk_index,
        chunk_text=chunk_text,
        retrieval_rank=1,
        retrieval_score=retrieval_score,
        is_seed=is_seed,
    )


def test_collect_evidence_aggregates_search_results_and_relation_hints():
    query_router = StubQueryRouter(
        [
            SearchResult(
                knowledge_id=1,
                title="Alpha",
                score=0.95,
                highlight="Alpha 摘要",
                metadata={"source_type": "generic", "tags": "AI,测试"},
            ),
            SearchResult(
                knowledge_id=2,
                title="Beta",
                score=0.82,
                highlight="Beta 摘要",
                metadata={"source_type": "generic", "tags": "知识图谱"},
            ),
        ]
    )
    sqlite_store = StubSQLiteStore(
        {
            1: {
                "knowledge_id": 1,
                "title": "Alpha",
                "summary_one_sentence": "Alpha 一句话摘要",
                "source_type": "generic",
                "archived_at": "2026-03-10 10:00:00",
                "tags": "AI,测试",
                "source_url": "https://example.com/alpha",
                "file_path": "/tmp/alpha.md",
            },
            2: {
                "knowledge_id": 2,
                "title": "Beta",
                "summary_one_sentence": "Beta 一句话摘要",
                "source_type": "generic",
                "archived_at": "2026-03-10 10:10:00",
                "tags": "知识图谱",
                "source_url": "https://example.com/beta",
                "file_path": "/tmp/beta.md",
            },
        }
    )
    markdown_store = StubMarkdownStore(
        {
            "/tmp/alpha.md": "# Alpha\n\nAlpha full content",
            "/tmp/beta.md": "# Beta\n\nBeta full content",
        }
    )
    service = EvidenceCollectionService(
        query_router=query_router,
        sqlite_store=sqlite_store,
        markdown_store=markdown_store,
        relation_query_service=StubRelationQueryService(),
    )

    result = service.collect_evidence(
        question="Alpha 和 Beta 有什么关联？",
        top_k=2,
        relation_max_depth=2,
    )

    assert result.found is True
    assert result.seed_knowledge_id == 1
    assert result.total_evidence == 2
    assert result.related_evidence_count == 1
    assert result.chunk_retrieval_status == "not_requested"
    assert result.evidence[0].is_seed is True
    assert result.evidence[0].content_preview == "# Alpha Alpha full content"
    assert result.evidence[1].relation_found is True
    assert result.evidence[1].relation_explanation_type == "direct"
    assert result.evidence[1].relation_summary == "1 -[related_document]-> 2"


def test_collect_evidence_prefers_chunk_preview_and_exposes_chunk_fields():
    query_router = StubQueryRouter(
        [
            SearchResult(
                knowledge_id=1,
                title="Alpha",
                score=0.95,
                highlight="Alpha 摘要",
                metadata={"source_type": "generic", "tags": "AI,测试"},
            ),
            SearchResult(
                knowledge_id=2,
                title="Beta",
                score=0.82,
                highlight="Beta 摘要",
                metadata={"source_type": "generic", "tags": "知识图谱"},
            ),
        ]
    )
    sqlite_store = StubSQLiteStore(
        {
            1: {
                "knowledge_id": 1,
                "title": "Alpha",
                "summary_one_sentence": "Alpha 一句话摘要",
                "source_type": "generic",
                "archived_at": "2026-03-10 10:00:00",
                "tags": "AI,测试",
                "source_url": "https://example.com/alpha",
                "file_path": "/tmp/alpha.md",
            },
            2: {
                "knowledge_id": 2,
                "title": "Beta",
                "summary_one_sentence": "Beta 一句话摘要",
                "source_type": "generic",
                "archived_at": "2026-03-10 10:10:00",
                "tags": "知识图谱",
                "source_url": "https://example.com/beta",
                "file_path": "/tmp/beta.md",
            },
        }
    )
    markdown_store = StubMarkdownStore(
        {
            "/tmp/alpha.md": "# Alpha\n\nAlpha full content",
            "/tmp/beta.md": "# Beta\n\nBeta full content",
        }
    )
    chunk_searcher = StubChunkSearcher(
        [
            SearchResult(
                knowledge_id=1,
                title="Alpha",
                score=0.93,
                highlight="Alpha chunk",
                metadata={"chunk_id": 101, "chunk_index": 0, "chunk_text": "Alpha chunk"},
            ),
            SearchResult(
                knowledge_id=2,
                title="Beta",
                score=0.88,
                highlight="Beta chunk",
                metadata={"chunk_id": 201, "chunk_index": 1, "chunk_text": "Beta chunk"},
            ),
        ]
    )
    service = EvidenceCollectionService(
        query_router=query_router,
        sqlite_store=sqlite_store,
        markdown_store=markdown_store,
        relation_query_service=StubRelationQueryService(),
        chunk_searcher=chunk_searcher,
    )

    result = service.collect_evidence(
        question="Alpha 和 Beta 有什么关联？",
        top_k=2,
        relation_max_depth=2,
        include_chunks=True,
    )

    assert result.found is True
    assert result.evidence[0].knowledge_id == 1
    assert result.evidence[0].content_preview == "Alpha chunk"
    assert result.evidence[0].chunk_id == 101
    assert result.evidence[0].chunk_index == 0
    assert result.evidence[0].chunk_text == "Alpha chunk"
    assert result.evidence[0].citation_source == "https://example.com/alpha"
    assert result.evidence[0].citation_locator == "pkv://entries/1/chunks/101"
    assert result.evidence[0].ranking_score > 0
    assert result.evidence[0].coverage_score > 0
    assert result.chunk_retrieval_status == "success"
    assert result.limitation_notes == []
    assert result.evidence[1].content_preview == "Beta chunk"
    assert result.evidence[1].chunk_index == 1
    assert result.evidence[1].citation_source == "https://example.com/beta"
    assert result.evidence[1].citation_locator == "pkv://entries/2/chunks/201"


def test_collect_evidence_deduplicates_same_chunk():
    service = EvidenceCollectionService(
        query_router=StubQueryRouter(
            [
                SearchResult(
                    knowledge_id=1,
                    title="Alpha",
                    score=0.95,
                    highlight="Alpha 摘要",
                    metadata={},
                )
            ]
        ),
        sqlite_store=StubSQLiteStore(
            {
                1: {
                    "knowledge_id": 1,
                    "title": "Alpha",
                    "summary_one_sentence": "Alpha 一句话摘要",
                    "source_type": "generic",
                    "archived_at": "2026-03-10 10:00:00",
                    "tags": "AI,测试",
                    "source_url": "https://example.com/alpha",
                    "file_path": "/tmp/alpha.md",
                }
            }
        ),
        markdown_store=StubMarkdownStore({"/tmp/alpha.md": "# Alpha\n\nAlpha full content"}),
        relation_query_service=StubRelationQueryService(),
        chunk_searcher=StubChunkSearcher(
            [
                SearchResult(
                    knowledge_id=1,
                    title="Alpha",
                    score=0.93,
                    highlight="Alpha chunk",
                    metadata={"chunk_id": 101, "chunk_index": 0, "chunk_text": "Alpha chunk"},
                ),
                SearchResult(
                    knowledge_id=1,
                    title="Alpha",
                    score=0.91,
                    highlight="Alpha chunk duplicate",
                    metadata={"chunk_id": 101, "chunk_index": 0, "chunk_text": "Alpha chunk"},
                ),
            ]
        ),
    )

    result = service.collect_evidence(question="Alpha?", top_k=3, include_chunks=True)

    assert result.total_evidence == 1
    assert result.evidence[0].chunk_index == 0


def test_collect_evidence_deduplicates_near_duplicate_chunk_text():
    service = EvidenceCollectionService(
        query_router=StubQueryRouter(
            [
                SearchResult(
                    knowledge_id=1,
                    title="Alpha",
                    score=0.95,
                    highlight="Alpha 摘要",
                    metadata={},
                )
            ]
        ),
        sqlite_store=StubSQLiteStore(
            {
                1: {
                    "knowledge_id": 1,
                    "title": "Alpha",
                    "summary_one_sentence": "Alpha 一句话摘要",
                    "source_type": "generic",
                    "archived_at": "2026-03-10 10:00:00",
                    "tags": "AI,测试",
                    "source_url": "https://example.com/alpha",
                    "file_path": "/tmp/alpha.md",
                }
            }
        ),
        markdown_store=StubMarkdownStore({"/tmp/alpha.md": "# Alpha\n\nAlpha full content"}),
        relation_query_service=StubRelationQueryService(),
        chunk_searcher=StubChunkSearcher(
            [
                SearchResult(
                    knowledge_id=1,
                    title="Alpha",
                    score=0.93,
                    highlight="Alpha chunk",
                    metadata={
                        "chunk_id": 101,
                        "chunk_index": 0,
                        "chunk_text": "Alpha chunk about graphs and retrieval",
                    },
                ),
                SearchResult(
                    knowledge_id=1,
                    title="Alpha",
                    score=0.91,
                    highlight="Alpha chunk duplicate",
                    metadata={
                        "chunk_id": 102,
                        "chunk_index": 1,
                        "chunk_text": "Alpha chunk about graphs and retrieval.",
                    },
                ),
            ]
        ),
    )

    result = service.collect_evidence(question="Alpha?", top_k=3, include_chunks=True)

    assert result.total_evidence == 1
    assert result.evidence[0].chunk_id == 101


def test_collect_evidence_default_keeps_document_preview_when_chunks_available():
    service = EvidenceCollectionService(
        query_router=StubQueryRouter(
            [
                SearchResult(
                    knowledge_id=1,
                    title="Alpha",
                    score=0.95,
                    highlight="Alpha 摘要",
                    metadata={},
                )
            ]
        ),
        sqlite_store=StubSQLiteStore(
            {
                1: {
                    "knowledge_id": 1,
                    "title": "Alpha",
                    "summary_one_sentence": "Alpha 一句话摘要",
                    "source_type": "generic",
                    "archived_at": "2026-03-10 10:00:00",
                    "tags": "AI,测试",
                    "source_url": "https://example.com/alpha",
                    "file_path": "/tmp/alpha.md",
                }
            }
        ),
        markdown_store=StubMarkdownStore({"/tmp/alpha.md": "# Alpha\n\nAlpha full content"}),
        relation_query_service=StubRelationQueryService(),
        chunk_searcher=StubChunkSearcher(
            [
                SearchResult(
                    knowledge_id=1,
                    title="Alpha",
                    score=0.93,
                    highlight="Alpha chunk",
                    metadata={"chunk_id": 101, "chunk_index": 0, "chunk_text": "Alpha chunk"},
                )
            ]
        ),
    )

    result = service.collect_evidence(question="Alpha?", top_k=3)

    assert result.total_evidence == 1
    assert result.evidence[0].content_preview == "# Alpha Alpha full content"
    assert result.evidence[0].chunk_id is None
    assert result.evidence[0].chunk_index is None
    assert result.evidence[0].chunk_text == ""


def test_collect_evidence_ranks_non_seed_items_by_multi_factor_score():
    query_router = StubQueryRouter(
        [
            SearchResult(
                knowledge_id=1,
                title="Alpha",
                score=0.95,
                highlight="Alpha 摘要",
                metadata={"source_type": "generic", "tags": "AI,测试"},
            ),
            SearchResult(
                knowledge_id=2,
                title="Beta",
                score=0.80,
                highlight="Beta 摘要",
                metadata={"source_type": "generic", "tags": "知识图谱"},
            ),
            SearchResult(
                knowledge_id=3,
                title="Gamma",
                score=0.84,
                highlight="Gamma 摘要",
                metadata={"source_type": "generic", "tags": "杂项"},
            ),
        ]
    )
    sqlite_store = StubSQLiteStore(
        {
            1: {
                "knowledge_id": 1,
                "title": "Alpha",
                "summary_one_sentence": "Alpha 一句话摘要",
                "source_type": "generic",
                "archived_at": "2026-03-10 10:00:00",
                "tags": "AI,测试",
                "source_url": "https://example.com/alpha",
                "file_path": "/tmp/alpha.md",
            },
            2: {
                "knowledge_id": 2,
                "title": "Beta",
                "summary_one_sentence": "Beta 一句话摘要",
                "source_type": "generic",
                "archived_at": "2026-03-28 10:10:00",
                "tags": "知识图谱",
                "source_url": "https://example.com/beta",
                "file_path": "/tmp/beta.md",
            },
            3: {
                "knowledge_id": 3,
                "title": "Gamma",
                "summary_one_sentence": "Gamma 一句话摘要",
                "source_type": "generic",
                "archived_at": "2026-03-12 10:10:00",
                "tags": "杂项",
                "source_url": "https://example.com/gamma",
                "file_path": "/tmp/gamma.md",
            },
        }
    )
    markdown_store = StubMarkdownStore(
        {
            "/tmp/alpha.md": "# Alpha\n\nAlpha full content",
            "/tmp/beta.md": "# Beta\n\nBeta full content",
            "/tmp/gamma.md": "# Gamma\n\nGamma full content",
        }
    )
    chunk_searcher = StubChunkSearcher(
        [
            SearchResult(
                knowledge_id=1,
                title="Alpha",
                score=0.90,
                highlight="Alpha chunk",
                metadata={"chunk_id": 101, "chunk_index": 0, "chunk_text": "Alpha chunk"},
            ),
            SearchResult(
                knowledge_id=3,
                title="Gamma",
                score=0.92,
                highlight="Gamma chunk",
                metadata={"chunk_id": 301, "chunk_index": 0, "chunk_text": "Random unrelated text"},
            ),
            SearchResult(
                knowledge_id=2,
                title="Beta",
                score=0.75,
                highlight="Beta chunk",
                metadata={
                    "chunk_id": 201,
                    "chunk_index": 0,
                    "chunk_text": "Beta discusses Alpha knowledge graph relation",
                },
            ),
        ]
    )

    service = EvidenceCollectionService(
        query_router=query_router,
        sqlite_store=sqlite_store,
        markdown_store=markdown_store,
        relation_query_service=StubRelationQueryService(),
        chunk_searcher=chunk_searcher,
    )

    result = service.collect_evidence(
        question="Alpha knowledge graph relation",
        top_k=3,
        relation_max_depth=2,
        include_chunks=True,
    )

    assert [item.knowledge_id for item in result.evidence] == [1, 2, 3]
    assert result.evidence[1].ranking_score > result.evidence[2].ranking_score
    assert result.evidence[1].relation_score > result.evidence[2].relation_score


def test_collect_evidence_returns_not_found_when_search_empty():
    service = EvidenceCollectionService(
        query_router=StubQueryRouter([]),
        sqlite_store=StubSQLiteStore({}),
        markdown_store=StubMarkdownStore({}),
        relation_query_service=StubRelationQueryService(),
    )

    result = service.collect_evidence(question="不存在的问题", top_k=3)

    assert result.found is False
    assert result.total_evidence == 0
    assert "未找到" in result.summary


def test_collect_evidence_distinguishes_no_chunk_hits_without_degradation():
    query_router = StubQueryRouter(
        [
            SearchResult(
                knowledge_id=1,
                title="Alpha",
                score=0.95,
                highlight="Alpha 摘要",
                metadata={"source_type": "generic", "tags": "AI,测试"},
            )
        ]
    )
    sqlite_store = StubSQLiteStore(
        {
            1: {
                "knowledge_id": 1,
                "title": "Alpha",
                "summary_one_sentence": "Alpha 一句话摘要",
                "source_type": "generic",
                "archived_at": "2026-03-10 10:00:00",
                "tags": "AI,测试",
                "source_url": "https://example.com/alpha",
                "file_path": "/tmp/alpha.md",
            }
        }
    )
    markdown_store = StubMarkdownStore({"/tmp/alpha.md": "# Alpha\n\nAlpha full content"})
    service = EvidenceCollectionService(
        query_router=query_router,
        sqlite_store=sqlite_store,
        markdown_store=markdown_store,
        relation_query_service=StubRelationQueryService(),
        chunk_searcher=StubChunkSearcher([]),
    )

    result = service.collect_evidence(question="Alpha?", top_k=3, include_chunks=True)

    assert result.found is True
    assert result.chunk_retrieval_status == "no_hits"
    assert result.limitation_notes == []


def test_collect_evidence_marks_chunk_degradation_on_exception(caplog):
    query_router = StubQueryRouter(
        [
            SearchResult(
                knowledge_id=1,
                title="Alpha",
                score=0.95,
                highlight="Alpha 摘要",
                metadata={"source_type": "generic", "tags": "AI,测试"},
            )
        ]
    )
    sqlite_store = StubSQLiteStore(
        {
            1: {
                "knowledge_id": 1,
                "title": "Alpha",
                "summary_one_sentence": "Alpha 一句话摘要",
                "source_type": "generic",
                "archived_at": "2026-03-10 10:00:00",
                "tags": "AI,测试",
                "source_url": "https://example.com/alpha",
                "file_path": "/tmp/alpha.md",
            }
        }
    )
    markdown_store = StubMarkdownStore({"/tmp/alpha.md": "# Alpha\n\nAlpha full content"})
    service = EvidenceCollectionService(
        query_router=query_router,
        sqlite_store=sqlite_store,
        markdown_store=markdown_store,
        relation_query_service=StubRelationQueryService(),
        chunk_searcher=StubFailingChunkSearcher(),
    )

    with caplog.at_level(logging.ERROR):
        result = service.collect_evidence(question="Alpha?", top_k=3, include_chunks=True)

    assert result.found is True
    assert result.chunk_retrieval_status == "search_error"
    assert (
        "chunk_degraded[search_error] chunk 检索异常，已降级为文档级证据"
        in result.limitation_notes
    )
    assert (
        "chunk_degraded[search_error] chunk 检索异常，已降级为文档级证据"
        in caplog.text
    )


def test_collect_evidence_marks_chunk_path_unavailable_degradation(caplog):
    query_router = StubQueryRouter(
        [
            SearchResult(
                knowledge_id=1,
                title="Alpha",
                score=0.95,
                highlight="Alpha 摘要",
                metadata={"source_type": "generic", "tags": "AI,测试"},
            )
        ]
    )
    sqlite_store = StubSQLiteStore(
        {
            1: {
                "knowledge_id": 1,
                "title": "Alpha",
                "summary_one_sentence": "Alpha 一句话摘要",
                "source_type": "generic",
                "archived_at": "2026-03-10 10:00:00",
                "tags": "AI,测试",
                "source_url": "https://example.com/alpha",
                "file_path": "/tmp/alpha.md",
            }
        }
    )
    markdown_store = StubMarkdownStore({"/tmp/alpha.md": "# Alpha\n\nAlpha full content"})
    service = EvidenceCollectionService(
        query_router=query_router,
        sqlite_store=sqlite_store,
        markdown_store=markdown_store,
        relation_query_service=StubRelationQueryService(),
        chunk_searcher=None,
    )

    with caplog.at_level(logging.WARNING):
        result = service.collect_evidence(question="Alpha?", top_k=3, include_chunks=True)

    assert result.found is True
    assert result.chunk_retrieval_status == "path_unavailable"
    assert (
        "chunk_degraded[path_unavailable] chunk 检索路径不可用，已降级为文档级证据"
        in result.limitation_notes
    )
    assert (
        "chunk_degraded[path_unavailable] chunk 检索路径不可用，已降级为文档级证据"
        in caplog.text
    )


def test_collect_evidence_rejects_empty_question():
    service = EvidenceCollectionService(
        query_router=StubQueryRouter([]),
        sqlite_store=StubSQLiteStore({}),
        markdown_store=StubMarkdownStore({}),
        relation_query_service=StubRelationQueryService(),
    )

    with pytest.raises(ValueError):
        service.collect_evidence("   ")


def test_collect_evidence_appends_document_hits_not_represented_by_chunks():
    service = EvidenceCollectionService(
        query_router=StubQueryRouter(
            [
                SearchResult(
                    knowledge_id=1,
                    title="Alpha",
                    score=0.95,
                    highlight="Alpha 摘要",
                    metadata={},
                ),
                SearchResult(
                    knowledge_id=2,
                    title="Beta",
                    score=0.84,
                    highlight="Beta 摘要",
                    metadata={},
                ),
            ]
        ),
        sqlite_store=StubSQLiteStore(
            {
                1: {
                    "knowledge_id": 1,
                    "title": "Alpha",
                    "summary_one_sentence": "Alpha 摘要",
                    "source_type": "generic",
                    "archived_at": "2026-03-10 10:00:00",
                    "file_path": "/tmp/alpha.md",
                },
                2: {
                    "knowledge_id": 2,
                    "title": "Beta",
                    "summary_one_sentence": "Beta 摘要",
                    "source_type": "generic",
                    "archived_at": "2026-03-11 10:00:00",
                    "file_path": "/tmp/beta.md",
                },
            }
        ),
        markdown_store=StubMarkdownStore(
            {
                "/tmp/alpha.md": "# Alpha\n\nAlpha full content",
                "/tmp/beta.md": "# Beta\n\nBeta full content",
            }
        ),
        relation_query_service=StubRelationQueryService(),
        chunk_searcher=StubChunkSearcher(
            [
                SearchResult(
                    knowledge_id=1,
                    title="Alpha",
                    score=0.91,
                    highlight="Alpha chunk",
                    metadata={"chunk_id": 101, "chunk_index": 0, "chunk_text": "Alpha chunk"},
                ),
            ]
        ),
    )

    result = service.collect_evidence(
        question="Alpha 和 Beta 的关系？",
        top_k=3,
        include_chunks=True,
    )

    assert [item.knowledge_id for item in result.evidence] == [1, 2]
    assert result.evidence[0].chunk_index == 0
    assert result.evidence[1].chunk_index is None
    assert result.evidence[1].content_preview == "# Beta Beta full content"


def test_deduplicate_replaces_lower_score_duplicate_and_detects_exact_text_match():
    service = EvidenceCollectionService(
        query_router=StubQueryRouter([]),
        sqlite_store=StubSQLiteStore({}),
        markdown_store=StubMarkdownStore({}),
        relation_query_service=StubRelationQueryService(),
    )
    lower = _make_evidence_item(
        2,
        retrieval_score=0.3,
        chunk_index=0,
        chunk_text="alpha chunk",
    )
    higher = _make_evidence_item(
        2,
        retrieval_score=0.8,
        chunk_index=0,
        chunk_text="alpha chunk",
    )

    deduplicated = service._deduplicate_evidence_items([lower, higher])

    assert len(deduplicated) == 1
    assert deduplicated[0].retrieval_score == 0.8
    assert service._find_duplicate_index(
        [_make_evidence_item(3, content_preview="Same text")],
        _make_evidence_item(4, content_preview="Same text"),
    ) == 0


def test_evidence_helper_fallback_branches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = EvidenceCollectionService(
        query_router=StubQueryRouter([]),
        sqlite_store=StubSQLiteStore({}),
        markdown_store=StubMarkdownStore({}),
        relation_query_service=StubRelationQueryService(),
    )
    seed = _make_evidence_item(1, retrieval_score=0.9, is_seed=True)
    non_seed_1 = _make_evidence_item(2, retrieval_score=0.7)
    non_seed_2 = _make_evidence_item(3, retrieval_score=0.6)

    assert service._rank_evidence_items("question", []) == []
    assert service._trim_evidence_items([seed, non_seed_1, non_seed_2], 2) == [
        seed,
        non_seed_1,
    ]
    assert service._compute_coverage_score("", seed) == 0.0
    assert service._compute_coverage_score(
        "graph relation",
        _make_evidence_item(
            5,
            content_preview="",
            chunk_text="",
            archived_at="2026-03-12 10:00:00",
        ),
    ) == 0.0
    assert service._compute_freshness_score(seed, None) == 0.0
    assert service._compute_freshness_score(
        _make_evidence_item(6, archived_at="broken"),
        service._parse_timestamp("2026-03-12 10:00:00"),
    ) == 0.0
    assert service._parse_timestamp("") is None
    assert service._parse_timestamp("broken") is None
    assert service._get_newest_timestamp(
        [_make_evidence_item(7, archived_at=""), _make_evidence_item(8, archived_at="broken")]
    ) is None

    monkeypatch.setattr(service.text_processor, "tokenize_chinese", lambda text: "   ")
    assert service._tokenize_text("Alpha-Graph 2026") == {"alpha", "graph", "2026"}
    assert service._load_content_preview("") == ""
    assert service._load_content_preview("/tmp/missing.md") == ""
    assert service._parse_tags(["AI", "", " 图谱 "]) == ["AI", "图谱"]
