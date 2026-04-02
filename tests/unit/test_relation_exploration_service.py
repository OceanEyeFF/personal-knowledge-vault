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
from src.relations.models import RelationExplanationResult, RelationRecord, RelationSourceType, RelationSubgraphNode, RelationSubgraphResult, RelationType  # noqa: E402
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

    def explain_relation(
        self,
        source_knowledge_id: int,
        target_knowledge_id: int,
        max_depth: int = 2,
    ):
        relation_type_mapping = {
            frozenset({1, 3}): RelationType.RELATED_DOCUMENT,
            frozenset({2, 3}): RelationType.REFERENCES,
        }
        relation_type = relation_type_mapping.get(
            frozenset({source_knowledge_id, target_knowledge_id})
        )
        if relation_type is None:
            return RelationExplanationResult(
                source_knowledge_id=source_knowledge_id,
                target_knowledge_id=target_knowledge_id,
                found=False,
                explanation_type="none",
                hops=0,
                summary="",
            )

        relation_source_type = (
            RelationSourceType.FRONTMATTER_RELATED_DOCS
            if relation_type == RelationType.RELATED_DOCUMENT
            else RelationSourceType.MARKDOWN_LINK
        )
        relation_record = RelationRecord(
            source_knowledge_id=source_knowledge_id,
            target_knowledge_id=target_knowledge_id,
            relation_type=relation_type,
            relation_source_type=relation_source_type,
            evidence_payload={"stub": True},
        )
        return RelationExplanationResult(
            source_knowledge_id=source_knowledge_id,
            target_knowledge_id=target_knowledge_id,
            found=True,
            explanation_type="path",
            hops=1,
            path=[relation_record],
            supporting_relations=[relation_record],
            summary="stub relation",
            evidence_items=[{"relation_type": relation_type.value}],
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
        "graph_bridge_signal",
        "entry_tags",
        "entry_title_summary",
    ]
    assert result.total_bridges == 1
    assert result.items[0].knowledge_id == 3
    assert result.items[0].connected_knowledge_ids == [1, 4]
    assert result.items[0].structural_bridge_score > 0
    assert result.items[0].graph_bridge_score > 0
    assert result.items[0].semantic_bridge_score > 0
    assert result.limitation_notes


def test_find_bridges_can_keep_graph_only_candidate():
    service = ExplorationService(
        query_router=StubQueryRouter({}),
        sqlite_store=StubSQLiteStore(
            {
                1: {
                    "knowledge_id": 1,
                    "title": "Alpha",
                    "summary_one_sentence": "主入口",
                    "tags": "AI",
                },
                3: {
                    "knowledge_id": 3,
                    "title": "Gamma",
                    "summary_one_sentence": "独立桥节点",
                    "tags": "桥节点",
                },
                4: {
                    "knowledge_id": 4,
                    "title": "Delta",
                    "summary_one_sentence": "终点主题",
                    "tags": "终点",
                },
            }
        ),
        relation_query_service=StubRelationQueryService(),
    )

    result = service.find_bridges(seed_knowledge_id=1, top_k=3, max_depth=2)

    assert result.found is True
    assert result.items[0].knowledge_id == 3
    assert result.items[0].graph_bridge_score >= 0.6
    assert result.items[0].semantic_bridge_score == 0.0


def test_timeline_of_sorts_by_archived_at(exploration_service):
    result = exploration_service.timeline_of(topic="时间线", top_k=5, sort_order="asc")

    assert result.found is True
    assert result.implementation_level == "partial"
    assert result.schema_version == "phase_b.v1"
    assert result.time_source_priority == ["event_time", "published_at", "archived_at"]
    assert [item.knowledge_id for item in result.items] == [1, 2]
    assert result.inferred_time_field == "archived_at"
    assert all(item.time_source == "archived_at" for item in result.items)


def test_timeline_of_prefers_real_time_sources_over_archived_at():
    query_router = StubQueryRouter(
        {
            "真实时间线": [
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
                SearchResult(
                    knowledge_id=3,
                    title="Gamma",
                    score=0.7,
                    highlight="Gamma 摘要",
                    metadata={},
                ),
            ]
        }
    )
    sqlite_store = StubSQLiteStore(
        {
            1: {
                "knowledge_id": 1,
                "title": "Alpha",
                "event_time": "2026-03-01 08:00:00",
                "published_at": "2026-03-05 09:00:00",
                "archived_at": "2026-03-10 09:00:00",
            },
            2: {
                "knowledge_id": 2,
                "title": "Beta",
                "published_at": "2026-03-02 09:00:00",
                "archived_at": "2026-03-11 09:00:00",
            },
            3: {
                "knowledge_id": 3,
                "title": "Gamma",
                "archived_at": "2026-03-03 09:00:00",
            },
        }
    )
    service = ExplorationService(
        query_router=query_router,
        sqlite_store=sqlite_store,
        relation_query_service=StubRelationQueryService(),
    )

    result = service.timeline_of(topic="真实时间线", top_k=5, sort_order="asc")

    assert [item.knowledge_id for item in result.items] == [1, 2, 3]
    assert [item.time_source for item in result.items] == [
        "event_time",
        "published_at",
        "archived_at",
    ]
    assert [item.time_value for item in result.items] == [
        "2026-03-01 08:00:00",
        "2026-03-02 09:00:00",
        "2026-03-03 09:00:00",
    ]
    assert result.inferred_time_field == "event_time"


def test_timeline_of_prefers_best_available_inferred_field_for_mixed_sources():
    query_router = StubQueryRouter(
        {
            "混合时间线": [
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
            1: {
                "knowledge_id": 1,
                "title": "Alpha",
                "archived_at": "2026-03-01 08:00:00",
            },
            2: {
                "knowledge_id": 2,
                "title": "Beta",
                "event_time": "2026-03-05 09:00:00",
                "archived_at": "2026-03-10 09:00:00",
            },
        }
    )
    service = ExplorationService(
        query_router=query_router,
        sqlite_store=sqlite_store,
        relation_query_service=StubRelationQueryService(),
    )

    result = service.timeline_of(topic="混合时间线", top_k=5, sort_order="asc")

    assert [item.knowledge_id for item in result.items] == [1, 2]
    assert result.inferred_time_field == "event_time"


def test_timeline_of_prefers_event_time_for_inferred_field_when_available():
    query_router = StubQueryRouter(
        {
            "事件与发布时间": [
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
            1: {
                "knowledge_id": 1,
                "title": "Alpha",
                "event_time": "2026-03-01 08:00:00",
                "archived_at": "2026-03-10 09:00:00",
            },
            2: {
                "knowledge_id": 2,
                "title": "Beta",
                "published_at": "2026-03-02 09:00:00",
                "archived_at": "2026-03-11 09:00:00",
            },
        }
    )
    service = ExplorationService(
        query_router=query_router,
        sqlite_store=sqlite_store,
        relation_query_service=StubRelationQueryService(),
    )

    result = service.timeline_of(topic="事件与发布时间", top_k=5, sort_order="asc")

    assert [item.knowledge_id for item in result.items] == [1, 2]
    assert [item.time_source for item in result.items] == ["event_time", "published_at"]
    assert result.inferred_time_field == "event_time"


def test_timeline_of_accepts_legacy_published_time_metadata_key():
    query_router = StubQueryRouter(
        {
            "旧发布时间": [
                SearchResult(
                    knowledge_id=7,
                    title="Legacy",
                    score=0.88,
                    highlight="Legacy 摘要",
                    metadata={"published_time": "2026-03-04 10:00:00"},
                ),
            ]
        }
    )
    sqlite_store = StubSQLiteStore(
        {
            7: {
                "knowledge_id": 7,
                "title": "Legacy",
                "archived_at": "2026-03-10 09:00:00",
            }
        }
    )
    service = ExplorationService(
        query_router=query_router,
        sqlite_store=sqlite_store,
        relation_query_service=StubRelationQueryService(),
    )

    result = service.timeline_of(topic="旧发布时间", top_k=5, sort_order="asc")

    assert result.items[0].time_source == "published_at"
    assert result.items[0].time_value == "2026-03-04 10:00:00"
    assert result.items[0].published_at == "2026-03-04 10:00:00"


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


def test_timeline_of_sorting_handles_parseable_unparseable_and_missing_time():
    query_router = StubQueryRouter(
        {
            "边界时间线": [
                SearchResult(
                    knowledge_id=1,
                    title="Parseable-A",
                    score=0.95,
                    highlight="A",
                    metadata={},
                ),
                SearchResult(
                    knowledge_id=2,
                    title="Unparseable-Z",
                    score=0.9,
                    highlight="B",
                    metadata={},
                ),
                SearchResult(
                    knowledge_id=3,
                    title="Unparseable-A",
                    score=0.85,
                    highlight="C",
                    metadata={},
                ),
                SearchResult(
                    knowledge_id=4,
                    title="Missing",
                    score=0.8,
                    highlight="D",
                    metadata={},
                ),
                SearchResult(
                    knowledge_id=5,
                    title="Parseable-B",
                    score=0.75,
                    highlight="E",
                    metadata={},
                ),
            ]
        }
    )
    sqlite_store = StubSQLiteStore(
        {
            1: {"knowledge_id": 1, "title": "Parseable-A", "archived_at": "2026-03-10 09:00:00"},
            2: {"knowledge_id": 2, "title": "Unparseable-Z", "archived_at": "zzz"},
            3: {"knowledge_id": 3, "title": "Unparseable-A", "archived_at": "abc"},
            4: {"knowledge_id": 4, "title": "Missing", "archived_at": ""},
            5: {"knowledge_id": 5, "title": "Parseable-B", "archived_at": "2026-03-11 09:00:00"},
        }
    )
    service = ExplorationService(
        query_router=query_router,
        sqlite_store=sqlite_store,
        relation_query_service=StubRelationQueryService(),
    )

    asc_result = service.timeline_of(topic="边界时间线", top_k=5, sort_order="asc")
    desc_result = service.timeline_of(topic="边界时间线", top_k=5, sort_order="desc")

    assert [item.knowledge_id for item in asc_result.items] == [1, 5, 3, 2, 4]
    assert [item.knowledge_id for item in desc_result.items] == [5, 1, 2, 3, 4]


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
        "relation_graph",
        "entry_tags",
        "entry_summary",
    ]
    assert result.comparison_dimensions["shared_tags_count"] == 2
    assert result.comparison_dimensions["overlap_knowledge_count"] == 1
    assert result.comparison_dimensions["relation_graph_signal"] == {
        "connected_candidate_pairs_count": 2,
        "topic_a_connected_candidate_count": 2,
        "topic_b_connected_candidate_count": 2,
        "shared_relation_types": ["references", "related_document"],
        "max_relation_hops": 1,
    }
    assert result.topic_a_candidates[0].relation_signal_score > 0
    assert result.topic_b_candidates[0].relation_types == ["references"]


def test_timeline_of_rejects_empty_topic(exploration_service):
    with pytest.raises(ValueError):
        exploration_service.timeline_of("   ")
