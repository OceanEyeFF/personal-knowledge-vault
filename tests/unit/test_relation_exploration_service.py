"""
Unit tests for ExplorationService.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.relations.exploration_service import ExplorationService  # noqa: E402
from src.relations.models import (  # noqa: E402
    RelationExplanationResult,
    RelationRecord,
    RelationSourceType,
    RelationSubgraphNode,
    RelationSubgraphResult,
    RelationType,
    TimelinePoint,
)
from src.retrieval.result import (  # noqa: E402
    RetrievalIssue,
    SearchResponse,
    SearchResult,
)
from src.runtime.errors import ErrorCode, PKVRuntimeError  # noqa: E402


class StubQueryRouter:
    def __init__(self, mapping):
        self.mapping = mapping

    def search(self, query: str, limit: int = 10):
        configured = self.mapping.get(query, [])
        if isinstance(configured, SearchResponse):
            return configured
        return SearchResponse.completed(configured[:limit], strategy="stub")


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
    assert len(result.items[0].evidence_path) == 2
    seed_edge, frontier_edge = result.items[0].evidence_path
    assert seed_edge["evidence_roles"] == ["seed_path", "candidate_adjacency"]
    assert seed_edge["from_knowledge_id"] == 1
    assert seed_edge["to_knowledge_id"] == 3
    assert seed_edge["citation_locator"] == (
        "pkv://relations/by-edge/1/3/related_document/frontmatter_related_docs"
    )
    assert frontier_edge["evidence_roles"] == ["candidate_adjacency"]
    assert frontier_edge["from_knowledge_id"] == 3
    assert frontier_edge["to_knowledge_id"] == 4
    assert frontier_edge["citation_locator"] == (
        "pkv://relations/by-edge/3/4/references/markdown_link"
    )
    support = result.items[0].supporting_subgraph
    assert support["candidate_connected_knowledge_ids"] == [1, 4]
    assert support["disconnected_neighbor_pairs"] == [
        {
            "left_knowledge_id": 1,
            "right_knowledge_id": 4,
            "connected_within_scope": False,
        }
    ]
    assert len(support["edges"]) == 2
    semantic_inputs = support["semantic_score_inputs"]
    assert semantic_inputs["candidate"] == {
        "knowledge_id": 3,
        "citation_locator": "pkv://entries/3",
        "metadata_locator": "pkv://entries/3/metadata",
        "token_count": semantic_inputs["candidate"]["token_count"],
    }
    assert semantic_inputs["candidate"]["token_count"] > 0
    assert {
        item["knowledge_id"] for item in semantic_inputs["comparisons"]
    } == {1, 4}
    assert all(
        item["citation_locator"].startswith("pkv://entries/")
        for item in semantic_inputs["comparisons"]
    )
    assert all(
        item["metadata_locator"].endswith("/metadata")
        for item in semantic_inputs["comparisons"]
    )
    assert round(semantic_inputs["semantic_score"], 4) == (
        result.items[0].semantic_bridge_score
    )
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


def test_find_bridges_discloses_truncated_subgraph(exploration_service):
    relation_service = exploration_service.relation_query_service
    original_query_subgraph = relation_service.query_subgraph

    def truncated_query_subgraph(**kwargs):
        subgraph = original_query_subgraph(**kwargs)
        subgraph.truncated = True
        return subgraph

    relation_service.query_subgraph = truncated_query_subgraph
    result = exploration_service.find_bridges(
        seed_knowledge_id=1,
        top_k=3,
        max_depth=2,
    )
    payload = result.to_dict()

    assert payload["subgraph_truncated"] is True
    assert payload["subgraph_max_nodes"] == 100
    assert payload["subgraph_max_edges"] == 300
    assert payload["subgraph_node_count"] == 3
    assert payload["subgraph_edge_count"] == 2
    assert any("候选集合和未发现结论均不完整" in note for note in result.limitation_notes)


def test_bridge_and_contrast_exclude_unsafe_relation_paths(tmp_path: Path):
    """公开 relation locator 不能经过 vault 外的中间条目。"""
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    alpha_path = vault_dir / "alpha.md"
    delta_path = vault_dir / "delta.md"
    alpha_path.write_text("# Alpha", encoding="utf-8")
    delta_path.write_text("# Delta", encoding="utf-8")
    outside_path = tmp_path / "outside.md"
    outside_path.write_text("# Private", encoding="utf-8")
    edge_alpha_outside = RelationRecord(
        source_knowledge_id=1,
        target_knowledge_id=2,
        relation_type=RelationType.REFERENCES,
        relation_source_type=RelationSourceType.MANUAL,
    )
    edge_outside_delta = RelationRecord(
        source_knowledge_id=2,
        target_knowledge_id=3,
        relation_type=RelationType.REFERENCES,
        relation_source_type=RelationSourceType.MANUAL,
    )

    class UnsafePathRelationService:
        def query_subgraph(self, seed_knowledge_id: int, depth: int = 2, **kwargs):
            return RelationSubgraphResult(
                seed_knowledge_id=seed_knowledge_id,
                max_depth=depth,
                nodes=[
                    RelationSubgraphNode(knowledge_id=1, depth=0),
                    RelationSubgraphNode(knowledge_id=2, depth=1),
                    RelationSubgraphNode(knowledge_id=3, depth=2),
                ],
                edges=[edge_alpha_outside, edge_outside_delta],
            )

        def explain_relation(
            self,
            source_knowledge_id: int,
            target_knowledge_id: int,
            max_depth: int = 2,
        ):
            return RelationExplanationResult(
                source_knowledge_id=source_knowledge_id,
                target_knowledge_id=target_knowledge_id,
                found=True,
                explanation_type="path",
                hops=2,
                path=[edge_alpha_outside, edge_outside_delta],
                supporting_relations=[edge_alpha_outside, edge_outside_delta],
            )

    query_router = StubQueryRouter(
        {
            "Topic A": [
                SearchResult(
                    knowledge_id=1,
                    title="Alpha",
                    score=0.9,
                    highlight="Alpha",
                    metadata={"tags": "shared"},
                )
            ],
            "Topic B": [
                SearchResult(
                    knowledge_id=3,
                    title="Delta",
                    score=0.8,
                    highlight="Delta",
                    metadata={"tags": "shared"},
                )
            ],
        }
    )
    sqlite_store = StubSQLiteStore(
        {
            1: {
                "knowledge_id": 1,
                "title": "Alpha",
                "summary_one_sentence": "Alpha",
                "tags": "shared",
                "file_path": str(alpha_path),
            },
            2: {
                "knowledge_id": 2,
                "title": "Private",
                "summary_one_sentence": "Private",
                "tags": "private",
                "file_path": str(outside_path),
            },
            3: {
                "knowledge_id": 3,
                "title": "Delta",
                "summary_one_sentence": "Delta",
                "tags": "shared",
                "file_path": str(delta_path),
            },
        }
    )
    service = ExplorationService(
        query_router=query_router,
        sqlite_store=sqlite_store,
        relation_query_service=UnsafePathRelationService(),
        vault_dir=vault_dir,
    )

    bridge = service.find_bridges(seed_knowledge_id=1, top_k=3, max_depth=2)
    contrast = service.contrast("Topic A", "Topic B", top_k=2)

    assert bridge.found is False
    assert bridge.items == []
    assert bridge.subgraph_edge_count == 0
    assert any("关系边未通过 vault 文件边界校验" in note for note in bridge.limitation_notes)
    relation_summary = contrast.comparison_dimensions["relation_graph_signal"]
    assert relation_summary["connected_candidate_pairs_count"] == 0
    assert contrast.comparison_dimensions["provenance"]["relation_graph_signal"] == []
    assert any("候选关系路径未通过 vault 文件边界校验" in note for note in contrast.limitation_notes)


def test_timeline_of_sorts_by_archived_at(exploration_service):
    result = exploration_service.timeline_of(topic="时间线", top_k=5, sort_order="asc")

    assert result.found is True
    assert result.implementation_level == "partial"
    assert result.schema_version == "phase_b.v1"
    assert result.time_source_priority == ["event_time", "published_at", "archived_at"]
    assert [item.knowledge_id for item in result.items] == [1, 2]
    assert result.inferred_time_field == "archived_at"
    assert all(item.time_source == "archived_at" for item in result.items)
    assert [item.source for item in result.items] == [
        "pkv://entries/1",
        "pkv://entries/2",
    ]
    assert [item.citation_locator for item in result.items] == [
        "pkv://entries/1/metadata/archived_at",
        "pkv://entries/2/metadata/archived_at",
    ]


def test_timeline_of_preserves_retrieval_error_as_error():
    message_canary = "MESSAGE_CANARY_private-token"
    stage_canary = "STAGE_CANARY_private-path"
    response = SearchResponse.failed_response(
        RetrievalIssue(
            code=ErrorCode.RETRIEVAL_INDEX_UNAVAILABLE,
            message=message_canary,
            stage=stage_canary,
            recoverable=True,
        ),
        strategy="bm25",
    )
    service = ExplorationService(
        query_router=StubQueryRouter({"故障时间线": response}),
        sqlite_store=StubSQLiteStore({}),
        relation_query_service=StubRelationQueryService(),
    )

    with pytest.raises(PKVRuntimeError) as raised:
        service.timeline_of("故障时间线")

    assert raised.value.code == ErrorCode.RETRIEVAL_INDEX_UNAVAILABLE
    assert raised.value.stage == "timeline_retrieval"
    assert raised.value.recoverable is True
    assert str(raised.value) == "检索服务暂不可用"
    assert message_canary not in repr(raised.value.to_dict())
    assert stage_canary not in repr(raised.value.to_dict())


def test_timeline_of_discloses_empty_degraded_retrieval():
    message_canary = "MESSAGE_CANARY_private-token"
    stage_canary = "STAGE_CANARY_private-path"
    response = SearchResponse.degraded_response(
        [],
        [
            RetrievalIssue(
                code=ErrorCode.PROVIDER_UNAVAILABLE,
                message=message_canary,
                stage=stage_canary,
                recoverable=True,
            )
        ],
        strategy="hybrid",
    )
    service = ExplorationService(
        query_router=StubQueryRouter({"降级时间线": response}),
        sqlite_store=StubSQLiteStore({}),
        relation_query_service=StubRelationQueryService(),
    )

    result = service.timeline_of("降级时间线")

    assert result.found is False
    assert any(
        "timeline_retrieval_degraded[provider_unavailable]" in note
        for note in result.limitation_notes
    )
    assert message_canary not in repr(result.to_dict())
    assert stage_canary not in repr(result.to_dict())


def test_timeline_does_not_turn_corrupted_no_hits_into_not_found():
    malformed_response = SearchResponse.completed((), strategy="bm25")
    object.__setattr__(
        malformed_response,
        "strategy",
        "bm25\r\nSTRATEGY_CANARY",
    )
    service = ExplorationService(
        query_router=StubQueryRouter({"损坏时间线": malformed_response}),
        sqlite_store=StubSQLiteStore({}),
        relation_query_service=StubRelationQueryService(),
    )

    with pytest.raises(PKVRuntimeError) as raised:
        service.timeline_of("损坏时间线")

    assert raised.value.code is ErrorCode.RETRIEVAL_BACKEND_FAILED
    assert raised.value.stage == "timeline_retrieval"
    assert str(raised.value) == "检索服务返回了无效响应"
    assert "STRATEGY_CANARY" not in str(raised.value)


def test_timeline_rejects_corrupted_success_result_before_consumption():
    malformed_result = SearchResult(1, "Alpha", 0.8, "", {})
    malformed_response = SearchResponse.completed(
        (malformed_result,),
        strategy="bm25",
    )
    object.__setattr__(malformed_result, "score", float("nan"))
    service = ExplorationService(
        query_router=StubQueryRouter({"损坏命中": malformed_response}),
        sqlite_store=StubSQLiteStore({}),
        relation_query_service=StubRelationQueryService(),
    )

    with pytest.raises(PKVRuntimeError) as raised:
        service.timeline_of("损坏命中")

    assert raised.value.code is ErrorCode.RETRIEVAL_BACKEND_FAILED
    assert raised.value.stage == "timeline_retrieval"


def test_timeline_of_marks_mixed_inferred_field_for_multi_source_timeline():
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
    assert result.inferred_time_field == "mixed"


def test_timeline_of_marks_mixed_inferred_field_for_event_and_archived_sources():
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
    assert result.inferred_time_field == "mixed"


def test_timeline_of_marks_mixed_inferred_field_for_event_and_published_sources():
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
    assert result.inferred_time_field == "mixed"


def test_timeline_of_does_not_cite_transient_legacy_published_time():
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

    assert result.items[0].time_source == "archived_at"
    assert result.items[0].time_source_field == "archived_at"
    assert result.items[0].time_value == "2026-03-10 09:00:00"
    assert result.items[0].published_at == ""
    assert result.items[0].citation_locator == (
        "pkv://entries/7/metadata/archived_at"
    )


@pytest.mark.parametrize("field_name", ["published_time", "publish_time"])
def test_timeline_of_cites_persisted_legacy_frontmatter_field(
    tmp_path,
    field_name,
):
    value = "2026-03-04 10:00:00"
    markdown_path = tmp_path / f"{field_name}.md"
    markdown_path.write_text(
        f"---\n{field_name}: '{value}'\n---\n# Legacy\n",
        encoding="utf-8",
    )
    query_router = StubQueryRouter(
        {
            "持久旧字段": [
                SearchResult(
                    knowledge_id=7,
                    title="Legacy",
                    score=0.88,
                    highlight="Legacy 摘要",
                    metadata={field_name: value},
                ),
            ]
        }
    )
    service = ExplorationService(
        query_router=query_router,
        sqlite_store=StubSQLiteStore(
            {
                7: {
                    "knowledge_id": 7,
                    "title": "Legacy",
                    "file_path": str(markdown_path),
                    "archived_at": "2026-03-10 09:00:00",
                }
            }
        ),
        relation_query_service=StubRelationQueryService(),
        vault_dir=tmp_path,
    )

    result = service.timeline_of(topic="持久旧字段", top_k=1, sort_order="asc")

    assert result.items[0].time_source == "published_at"
    assert result.items[0].time_source_field == field_name
    assert result.items[0].time_value == value
    assert result.items[0].citation_locator == (
        f"pkv://entries/7/metadata/{field_name}"
    )


def test_timeline_of_degrades_when_frontmatter_is_unreadable(tmp_path):
    markdown_path = tmp_path / "broken.md"
    markdown_path.write_bytes(b"\xff\xfe\x00")
    query_router = StubQueryRouter(
        {
            "损坏旧字段": [
                SearchResult(
                    knowledge_id=7,
                    title="Broken",
                    score=0.88,
                    highlight="Broken 摘要",
                    metadata={"published_time": "2026-03-04 10:00:00"},
                ),
            ]
        }
    )
    service = ExplorationService(
        query_router=query_router,
        sqlite_store=StubSQLiteStore(
            {
                7: {
                    "knowledge_id": 7,
                    "title": "Broken",
                    "file_path": str(markdown_path),
                    "archived_at": "2026-03-10 09:00:00",
                }
            }
        ),
        relation_query_service=StubRelationQueryService(),
        vault_dir=tmp_path,
    )

    result = service.timeline_of(topic="损坏旧字段", top_k=1, sort_order="asc")

    assert result.items[0].time_source == "archived_at"
    assert result.items[0].time_source_field == "archived_at"
    assert any("损坏 frontmatter" in note for note in result.limitation_notes)


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


def test_timeline_without_persisted_time_uses_honest_entry_fallback():
    query_router = StubQueryRouter(
        {
            "无时间": [
                SearchResult(
                    knowledge_id=7,
                    title="No Time",
                    score=0.8,
                    highlight="没有时间字段",
                    metadata={
                        "source_url": "file:///C:/Users/fixture/no-time.md",
                    },
                ),
            ]
        }
    )
    service = ExplorationService(
        query_router=query_router,
        sqlite_store=StubSQLiteStore(
            {
                7: {
                    "knowledge_id": 7,
                    "title": "No Time",
                    "source_url": "file:///C:/Users/fixture/no-time.md",
                    "archived_at": "",
                }
            }
        ),
        relation_query_service=StubRelationQueryService(),
    )

    result = service.timeline_of(topic="无时间", top_k=1, sort_order="asc")
    public = result.to_dict()
    item = public["items"][0]

    assert result.inferred_time_field == "unavailable"
    assert item["time_source"] == "unavailable"
    assert item["time_precision"] == "unavailable"
    assert item["time_source_field"] == ""
    assert item["time_value"] == ""
    assert item["citation_locator"] == "pkv://entries/7"
    assert item["source_url"] == ""
    assert item["source"] == "pkv://entries/7"
    assert any("不作为精确时间点" in note for note in public["limitation_notes"])


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
    assert [item.knowledge_id for item in desc_result.items] == [5, 1, 3, 2, 4]


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
    provenance = result.comparison_dimensions["provenance"]
    assert provenance["shared_tags"]["图谱"]["topic_a"][0] == {
        "topic_side": "topic_a",
        "knowledge_id": 1,
        "source": "pkv://entries/1",
        "source_url": "",
        "citation_locator": "pkv://entries/1",
    }
    assert provenance["only_a_tags"]["AI"][0]["knowledge_id"] == 1
    assert provenance["only_b_tags"]["时间线"][0]["knowledge_id"] == 2
    assert provenance["overlap_knowledge_ids"]["3"]["topic_a"][
        "citation_locator"
    ] == "pkv://entries/3"
    assert provenance["relation_graph_signal"]
    assert all(
        pair["evidence_path"]
        for pair in provenance["relation_graph_signal"]
    )
    assert result.topic_a_candidates[0].relation_signal_score > 0
    assert result.topic_b_candidates[0].relation_types == ["references"]


def test_contrast_discloses_each_degraded_retrieval_side():
    issue = RetrievalIssue(
        code=ErrorCode.RETRIEVAL_INDEX_UNAVAILABLE,
        message="索引暂不可用",
        stage="bm25_search",
        recoverable=True,
    )
    service = ExplorationService(
        query_router=StubQueryRouter(
            {
                "主题A": SearchResponse.degraded_response(
                    [], [issue], strategy="hybrid"
                ),
                "主题B": [],
            }
        ),
        sqlite_store=StubSQLiteStore({}),
        relation_query_service=StubRelationQueryService(),
    )

    result = service.contrast("主题A", "主题B")

    assert result.found is False
    assert any(
        "contrast_topic_a_retrieval_degraded[retrieval_index_unavailable]" in note
        for note in result.limitation_notes
    )


def test_timeline_of_rejects_empty_topic(exploration_service):
    with pytest.raises(ValueError):
        exploration_service.timeline_of("   ")


def test_exploration_services_reject_invalid_inputs(exploration_service) -> None:
    with pytest.raises(ValueError):
        exploration_service.find_bridges(seed_knowledge_id=0)
    with pytest.raises(ValueError):
        exploration_service.find_bridges(seed_knowledge_id=1, top_k=0)
    with pytest.raises(ValueError):
        exploration_service.find_bridges(seed_knowledge_id=1, max_depth=0)
    with pytest.raises(ValueError):
        exploration_service.timeline_of("时间线", top_k=0)
    with pytest.raises(ValueError):
        exploration_service.timeline_of("时间线", sort_order="middle")
    with pytest.raises(ValueError):
        exploration_service.contrast("  ", "主题B")
    with pytest.raises(ValueError):
        exploration_service.contrast("主题A", "  ")
    with pytest.raises(ValueError):
        exploration_service.contrast("主题A", "主题B", top_k=0)


def test_exploration_helper_fallback_branches(
    exploration_service,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        exploration_service.text_processor,
        "tokenize_chinese",
        lambda text: "   ",
    )

    assert exploration_service._parse_tags(["图谱", "", " 桥接 "]) == ["图谱", "桥接"]
    assert exploration_service._entry_tokens({}) == set()
    assert exploration_service._entry_tokens({"title": "Alpha-Beta"}) == {"alpha", "beta"}
    assert exploration_service._compute_semantic_bridge_score({}, {}, set(), {}) == 0.0
    assert (
        exploration_service._compute_semantic_bridge_score(
            {},
            {"title": "Alpha"},
            {99},
            {99: {}},
        )
        == 0.0
    )
    assert exploration_service._token_overlap(set(), {"alpha"}) == 0.0
    assert exploration_service._infer_timeline_source([], []) == "unavailable"
    assert (
        exploration_service._infer_timeline_source(
            [
                TimelinePoint(
                    knowledge_id=9,
                    title="NoTime",
                    time_value="",
                    time_source="unavailable",
                    retrieval_score=0.1,
                )
            ],
            ["event_time", "published_at", "archived_at"],
        )
        == "unavailable"
    )
    assert ExplorationService._parse_time_sort_key("2026-03-01T08:00:00Z")[:2] == (0, 0)


def test_extract_relation_types_falls_back_to_evidence_items() -> None:
    explanation = SimpleNamespace(
        path=[],
        supporting_relations=[],
        evidence_items=[
            {"relation_type": "references"},
            {"relation_type": ""},
            {"relation_type": "related_document"},
        ],
    )

    assert ExplorationService._extract_relation_types(explanation) == [
        "references",
        "related_document",
    ]
