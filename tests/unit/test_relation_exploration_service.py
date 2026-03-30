"""
Unit tests for ExplorationService.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.relations.exploration_service import ExplorationService  # noqa: E402
from src.relations.models import RelationRecord, RelationSourceType, RelationSubgraphNode, RelationSubgraphResult, RelationType  # noqa: E402
from src.retrieval.result import SearchResult  # noqa: E402


class StubQueryRouter:
    def __init__(self, mapping):
        self.mapping = mapping

    def search(self, query: str, limit: int = 10):
        return self.mapping.get(query, [])[:limit]


class StubSQLiteStore:
    def __init__(self, entries):
        self.entries = entries

    def query_by_id(self, knowledge_id: int):
        return self.entries.get(knowledge_id)


class StubRelationQueryService:
    def query_subgraph(
        self,
        seed_knowledge_id: int,
        depth: int = 2,
        per_node_limit: int = 100,
        max_nodes: int = 100,
        max_edges: int = 300,
        group_by_relation_type: bool = False,
    ):
        edge_alpha_gamma = RelationRecord(
            source_knowledge_id=1,
            target_knowledge_id=3,
            relation_type=RelationType.RELATED_DOCUMENT,
            relation_source_type=RelationSourceType.FRONTMATTER_RELATED_DOCS,
            evidence_payload={"field": "related_docs"},
        )
        edge_gamma_delta = RelationRecord(
            source_knowledge_id=3,
            target_knowledge_id=4,
            relation_type=RelationType.REFERENCES,
            relation_source_type=RelationSourceType.MARKDOWN_LINK,
            evidence_payload={"href": "./delta.md"},
        )
        return RelationSubgraphResult(
            seed_knowledge_id=seed_knowledge_id,
            max_depth=depth,
            nodes=[
                RelationSubgraphNode(knowledge_id=1, depth=0),
                RelationSubgraphNode(knowledge_id=3, depth=1),
                RelationSubgraphNode(knowledge_id=4, depth=2),
            ],
            edges=[edge_alpha_gamma, edge_gamma_delta],
            grouped_edges={},
            truncated=False,
        )


@pytest.fixture
def exploration_service():
    query_router = StubQueryRouter(
        {
            "时间线": [
                SearchResult(
                    knowledge_id=1,
                    title="Alpha",
                    score=0.9,
                    highlight="Alpha 摘要",
                    metadata={"source_type": "generic", "tags": "AI,图谱"},
                ),
                SearchResult(
                    knowledge_id=2,
                    title="Beta",
                    score=0.8,
                    highlight="Beta 摘要",
                    metadata={"source_type": "generic", "tags": "图谱"},
                ),
            ],
            "主题A": [
                SearchResult(
                    knowledge_id=1,
                    title="Alpha",
                    score=0.9,
                    highlight="Alpha 摘要",
                    metadata={"source_type": "generic", "tags": "AI,图谱"},
                ),
                SearchResult(
                    knowledge_id=3,
                    title="Gamma",
                    score=0.7,
                    highlight="Gamma 摘要",
                    metadata={"source_type": "generic", "tags": "桥接,图谱"},
                ),
            ],
            "主题B": [
                SearchResult(
                    knowledge_id=2,
                    title="Beta",
                    score=0.88,
                    highlight="Beta 摘要",
                    metadata={"source_type": "generic", "tags": "图谱,时间线"},
                ),
                SearchResult(
                    knowledge_id=3,
                    title="Gamma",
                    score=0.76,
                    highlight="Gamma 摘要",
                    metadata={"source_type": "generic", "tags": "桥接,图谱"},
                ),
            ],
        }
    )
    sqlite_store = StubSQLiteStore(
        {
            1: {
                "knowledge_id": 1,
                "title": "Alpha",
                "summary_one_sentence": "Alpha 一句话摘要",
                "archived_at": "2026-03-10 09:00:00",
                "source_type": "generic",
                "tags": "AI,图谱",
            },
            2: {
                "knowledge_id": 2,
                "title": "Beta",
                "summary_one_sentence": "Beta 一句话摘要",
                "archived_at": "2026-03-11 09:00:00",
                "source_type": "generic",
                "tags": "图谱,时间线",
            },
            3: {
                "knowledge_id": 3,
                "title": "Gamma",
                "summary_one_sentence": "Gamma 一句话摘要",
                "archived_at": "2026-03-09 09:00:00",
                "source_type": "generic",
                "tags": "桥接,图谱",
            },
            4: {
                "knowledge_id": 4,
                "title": "Delta",
                "summary_one_sentence": "Delta 一句话摘要",
                "archived_at": "2026-03-12 09:00:00",
                "source_type": "generic",
                "tags": "终点",
            },
        }
    )
    return ExplorationService(
        query_router=query_router,
        sqlite_store=sqlite_store,
        relation_query_service=StubRelationQueryService(),
    )


def test_find_bridges_returns_middle_node(exploration_service):
    result = exploration_service.find_bridges(seed_knowledge_id=1, top_k=3, max_depth=2)

    assert result.found is True
    assert result.implementation_level == "partial"
    assert result.schema_version == "phase_b.v1"
    assert result.evidence_sources == [
        "relation_subgraph",
        "entry_tags",
        "entry_title_summary",
    ]
    assert result.total_bridges == 1
    assert result.items[0].knowledge_id == 3
    assert result.items[0].connected_knowledge_ids == [1, 4]
    assert result.items[0].structural_bridge_score > 0
    assert result.items[0].semantic_bridge_score > 0
    assert result.limitation_notes


def test_timeline_of_sorts_by_archived_at(exploration_service):
    result = exploration_service.timeline_of(topic="时间线", top_k=5, sort_order="asc")

    assert result.found is True
    assert result.implementation_level == "partial"
    assert result.schema_version == "phase_b.v1"
    assert result.time_source_priority == ["event_time", "published_at", "archived_at"]
    assert [item.knowledge_id for item in result.items] == [1, 2]
    assert result.inferred_time_field == "archived_at"
    assert all(item.time_source == "archived_at" for item in result.items)


def test_timeline_of_desc_keeps_missing_time_items_last():
    query_router = StubQueryRouter(
        {
            "时间线": [
                SearchResult(
                    knowledge_id=1,
                    title="Alpha",
                    score=0.9,
                    highlight="Alpha 摘要",
                    metadata={},
                ),
                SearchResult(
                    knowledge_id=2,
                    title="Beta",
                    score=0.8,
                    highlight="Beta 摘要",
                    metadata={},
                ),
            ]
        }
    )
    sqlite_store = StubSQLiteStore(
        {
            1: {"knowledge_id": 1, "title": "Alpha", "archived_at": "2026-03-10 09:00:00"},
            2: {"knowledge_id": 2, "title": "Beta", "archived_at": ""},
        }
    )
    service = ExplorationService(
        query_router=query_router,
        sqlite_store=sqlite_store,
        relation_query_service=StubRelationQueryService(),
    )

    result = service.timeline_of(topic="时间线", top_k=5, sort_order="desc")

    assert [item.knowledge_id for item in result.items] == [1, 2]


def test_contrast_returns_shared_and_distinct_tags(exploration_service):
    result = exploration_service.contrast(topic_a="主题A", topic_b="主题B", top_k=3)

    assert result.found is True
    assert result.implementation_level == "partial"
    assert result.schema_version == "phase_b.v1"
    assert result.shared_tags == ["图谱", "桥接"]
    assert result.only_a_tags == ["AI"]
    assert result.only_b_tags == ["时间线"]
    assert result.overlap_knowledge_ids == [3]
    assert result.evidence_sources == [
        "query_results",
        "entry_tags",
        "entry_summary",
    ]
    assert result.comparison_dimensions["shared_tags_count"] == 2
    assert result.comparison_dimensions["overlap_knowledge_count"] == 1


def test_timeline_of_rejects_empty_topic(exploration_service):
    with pytest.raises(ValueError):
        exploration_service.timeline_of("   ")
