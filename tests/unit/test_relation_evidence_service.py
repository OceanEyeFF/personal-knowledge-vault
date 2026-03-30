"""
Unit tests for EvidenceCollectionService.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.relations.evidence_service import EvidenceCollectionService  # noqa: E402
from src.relations.models import RelationExplanationResult, RelationRecord, RelationSourceType, RelationType  # noqa: E402
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
        self.content_map = content_map

    def load(self, file_path: Path):
        return Entry(
            title=f"Loaded-{file_path.stem}",
            source_type="generic",
            content=self.content_map[str(file_path)],
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
    assert result.evidence[0].ranking_score > 0
    assert result.evidence[0].coverage_score > 0
    assert result.evidence[1].content_preview == "Beta chunk"
    assert result.evidence[1].chunk_index == 1


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


def test_collect_evidence_rejects_empty_question():
    service = EvidenceCollectionService(
        query_router=StubQueryRouter([]),
        sqlite_store=StubSQLiteStore({}),
        markdown_store=StubMarkdownStore({}),
        relation_query_service=StubRelationQueryService(),
    )

    with pytest.raises(ValueError):
        service.collect_evidence("   ")
