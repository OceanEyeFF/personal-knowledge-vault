"""
Integration tests for relation backfill + query pipeline.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest
import yaml

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.relations.extractors import RelationBackfillService  # noqa: E402
from src.relations.evidence_service import EvidenceCollectionService  # noqa: E402
from src.relations.exploration_service import ExplorationService  # noqa: E402
from src.relations.models import RelationQueryDirection, RelationType  # noqa: E402
from src.relations.query_service import RelationQueryService  # noqa: E402
from src.retrieval.result import SearchResult  # noqa: E402
from src.storage.markdown_store import MarkdownStore  # noqa: E402
from src.storage.sqlite_store import SQLiteStore  # noqa: E402
from src.storage.relation_store import RelationStore  # noqa: E402

BASE_SQL = PROJECT_ROOT / "scripts/migrations/001_initial_schema.sql"
RELATION_SQL = PROJECT_ROOT / "scripts/migrations/006_add_relations_foundation.sql"
REGRESSION_DATASET = PROJECT_ROOT / "tests/fixtures/phase_b_5_4_min_regression.yaml"


def _apply_sql(db_path: Path, sql_path: Path) -> None:
    sql = sql_path.read_text(encoding="utf-8")
    conn = sqlite3.connect(str(db_path))
    conn.executescript(sql)
    conn.close()


def _insert_entry(
    db_path: Path,
    file_path: Path,
    title: str,
    source_url: str,
) -> int:
    conn = sqlite3.connect(str(db_path))
    cursor = conn.execute(
        """
        INSERT INTO knowledge_items (
            title,
            source_type,
            source_url,
            file_path,
            content,
            summary_one_sentence,
            summary_100_words,
            tags,
            keywords
        ) VALUES (?, 'generic', ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            title,
            source_url,
            str(file_path),
            file_path.read_text(encoding="utf-8"),
            f"{title} 摘要",
            f"{title} 详细摘要",
            "测试",
            "test",
        ),
    )
    conn.commit()
    knowledge_id = int(cursor.lastrowid)
    conn.close()
    return knowledge_id


@pytest.fixture
def relation_pipeline_env(tmp_path: Path):
    db_path = tmp_path / "test.db"
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir(parents=True, exist_ok=True)

    _apply_sql(db_path, BASE_SQL)
    _apply_sql(db_path, RELATION_SQL)

    alpha_path = vault_dir / "alpha.md"
    beta_path = vault_dir / "beta.md"
    gamma_path = vault_dir / "gamma.md"
    delta_path = vault_dir / "delta.md"

    alpha_path.write_text(
        "---\n"
        "title: Alpha\n"
        "related_docs:\n"
        "  - gamma.md\n"
        "---\n"
        "# Alpha\n\n请参考 [Beta](./beta.md)\n",
        encoding="utf-8",
    )
    beta_path.write_text("# Beta\n\n回链到 [Alpha](./alpha.md)\n", encoding="utf-8")
    gamma_path.write_text("# Gamma\n\n继续参考 [Delta](./delta.md)\n", encoding="utf-8")
    delta_path.write_text("# Delta\n\n正文", encoding="utf-8")

    alpha_id = _insert_entry(db_path, alpha_path, "Alpha", "https://example.com/a")
    beta_id = _insert_entry(db_path, beta_path, "Beta", "https://example.com/b")
    gamma_id = _insert_entry(db_path, gamma_path, "Gamma", "https://example.com/c")
    delta_id = _insert_entry(db_path, delta_path, "Delta", "https://example.com/d")

    return {
        "db_path": db_path,
        "vault_dir": vault_dir,
        "alpha_id": alpha_id,
        "beta_id": beta_id,
        "gamma_id": gamma_id,
        "delta_id": delta_id,
    }


def test_relation_query_service_reads_grouped_results_from_backfill(relation_pipeline_env):
    service = RelationBackfillService(
        db_path=relation_pipeline_env["db_path"],
        vault_dir=relation_pipeline_env["vault_dir"],
    )
    relation_store = RelationStore(relation_pipeline_env["db_path"])
    query_service = RelationQueryService(relation_store)

    report = service.backfill(apply=True)
    result = query_service.list_relations(
        seed_knowledge_id=relation_pipeline_env["alpha_id"],
        direction=RelationQueryDirection.BOTH,
    )

    assert report.applied_relations == 4
    assert result.total == 3
    assert list(result.grouped_items.keys()) == [
        RelationType.REFERENCES.value,
        RelationType.RELATED_DOCUMENT.value,
    ]
    assert len(result.grouped_items[RelationType.REFERENCES.value]) == 2
    assert len(result.grouped_items[RelationType.RELATED_DOCUMENT.value]) == 1


def test_relation_query_service_can_find_relations_between_two_entries(relation_pipeline_env):
    service = RelationBackfillService(
        db_path=relation_pipeline_env["db_path"],
        vault_dir=relation_pipeline_env["vault_dir"],
    )
    query_service = RelationQueryService(
        RelationStore(relation_pipeline_env["db_path"])
    )

    service.backfill(apply=True)
    result = query_service.get_relations_between(
        relation_pipeline_env["alpha_id"],
        relation_pipeline_env["beta_id"],
    )

    assert result.total == 2
    assert list(result.grouped_items.keys()) == [RelationType.REFERENCES.value]
    assert {
        (item.source_knowledge_id, item.target_knowledge_id)
        for item in result.items
    } == {
        (relation_pipeline_env["alpha_id"], relation_pipeline_env["beta_id"]),
        (relation_pipeline_env["beta_id"], relation_pipeline_env["alpha_id"]),
    }


def test_relation_query_service_can_expand_backfilled_subgraph(relation_pipeline_env):
    service = RelationBackfillService(
        db_path=relation_pipeline_env["db_path"],
        vault_dir=relation_pipeline_env["vault_dir"],
    )
    query_service = RelationQueryService(
        RelationStore(relation_pipeline_env["db_path"])
    )

    service.backfill(apply=True)
    result = query_service.query_subgraph(
        seed_knowledge_id=relation_pipeline_env["alpha_id"],
        depth=2,
    )

    assert [(node.knowledge_id, node.depth) for node in result.nodes] == [
        (relation_pipeline_env["alpha_id"], 0),
        (relation_pipeline_env["beta_id"], 1),
        (relation_pipeline_env["gamma_id"], 1),
        (relation_pipeline_env["delta_id"], 2),
    ]
    assert result.total_edges == 4
    assert list(result.grouped_edges.keys()) == [
        RelationType.REFERENCES.value,
        RelationType.RELATED_DOCUMENT.value,
    ]


def test_relation_query_service_can_explain_backfilled_relation_path(
    relation_pipeline_env,
):
    service = RelationBackfillService(
        db_path=relation_pipeline_env["db_path"],
        vault_dir=relation_pipeline_env["vault_dir"],
    )
    query_service = RelationQueryService(
        RelationStore(relation_pipeline_env["db_path"])
    )

    service.backfill(apply=True)
    result = query_service.explain_relation(
        relation_pipeline_env["alpha_id"],
        relation_pipeline_env["delta_id"],
        max_depth=2,
    )

    assert result.found is True
    assert result.explanation_type == "path"
    assert result.hops == 2
    assert result.intermediate_knowledge_ids == [relation_pipeline_env["gamma_id"]]
    assert [item["relation_type"] for item in result.evidence_items] == [
        RelationType.RELATED_DOCUMENT.value,
        RelationType.REFERENCES.value,
    ] or [item["relation_type"] for item in result.evidence_items] == [
        RelationType.REFERENCES.value,
        RelationType.RELATED_DOCUMENT.value,
    ]


class StubQueryRouter:
    def __init__(self, results):
        self.results = results

    def search(self, query: str, limit: int = 10):
        return self.results[:limit]


def _load_regression_cases():
    payload = yaml.safe_load(REGRESSION_DATASET.read_text(encoding="utf-8"))
    return payload["cases"]


def _resolve_knowledge_ids(env: dict, keys: list[str]) -> list[int]:
    return [int(env[key]) for key in keys]


def test_evidence_collection_service_can_collect_backfilled_evidence(
    relation_pipeline_env,
):
    backfill_service = RelationBackfillService(
        db_path=relation_pipeline_env["db_path"],
        vault_dir=relation_pipeline_env["vault_dir"],
    )
    backfill_service.backfill(apply=True)

    query_router = StubQueryRouter(
        [
            SearchResult(
                knowledge_id=relation_pipeline_env["alpha_id"],
                title="Alpha",
                score=0.95,
                highlight="Alpha 摘要",
                metadata={"source_type": "generic", "tags": "测试"},
            ),
            SearchResult(
                knowledge_id=relation_pipeline_env["delta_id"],
                title="Delta",
                score=0.88,
                highlight="Delta 摘要",
                metadata={"source_type": "generic", "tags": "测试"},
            ),
        ]
    )
    evidence_service = EvidenceCollectionService(
        query_router=query_router,
        sqlite_store=SQLiteStore(relation_pipeline_env["db_path"]),
        markdown_store=MarkdownStore(relation_pipeline_env["vault_dir"]),
        relation_query_service=RelationQueryService(
            RelationStore(relation_pipeline_env["db_path"])
        ),
    )

    result = evidence_service.collect_evidence(
        question="Alpha 和 Delta 有什么证据链？",
        top_k=2,
        relation_max_depth=2,
    )

    assert result.found is True
    assert result.seed_knowledge_id == relation_pipeline_env["alpha_id"]
    assert result.total_evidence == 2
    assert result.related_evidence_count == 1
    assert result.evidence[0].is_seed is True
    assert "Alpha" in result.evidence[0].content_preview

    delta_item = result.evidence[1]
    assert delta_item.knowledge_id == relation_pipeline_env["delta_id"]
    assert delta_item.relation_found is True
    assert delta_item.relation_explanation_type == "path"
    assert delta_item.relation_hops == 2
    assert delta_item.relation_path


def test_exploration_service_can_find_partial_bridge_candidates(
    relation_pipeline_env,
):
    backfill_service = RelationBackfillService(
        db_path=relation_pipeline_env["db_path"],
        vault_dir=relation_pipeline_env["vault_dir"],
    )
    backfill_service.backfill(apply=True)

    exploration_service = ExplorationService(
        query_router=StubQueryRouter([]),
        sqlite_store=SQLiteStore(relation_pipeline_env["db_path"]),
        relation_query_service=RelationQueryService(
            RelationStore(relation_pipeline_env["db_path"])
        ),
    )

    result = exploration_service.find_bridges(
        seed_knowledge_id=relation_pipeline_env["alpha_id"],
        top_k=5,
        max_depth=2,
    )

    assert result.found is True
    assert result.implementation_level == "partial"
    assert result.schema_version == "phase_b.v1"
    assert result.evidence_sources == [
        "relation_subgraph",
        "entry_tags",
        "entry_title_summary",
    ]
    assert [item.knowledge_id for item in result.items] == [relation_pipeline_env["gamma_id"]]
    assert result.items[0].connected_knowledge_ids == sorted(
        [relation_pipeline_env["alpha_id"], relation_pipeline_env["delta_id"]]
    )
    assert result.items[0].structural_bridge_score > 0
    assert result.items[0].semantic_bridge_score > 0


def test_exploration_service_can_build_partial_timeline(relation_pipeline_env):
    exploration_service = ExplorationService(
        query_router=StubQueryRouter(
            [
                SearchResult(
                    knowledge_id=relation_pipeline_env["beta_id"],
                    title="Beta",
                    score=0.85,
                    highlight="Beta 摘要",
                    metadata={"source_type": "generic", "tags": "测试"},
                ),
                SearchResult(
                    knowledge_id=relation_pipeline_env["alpha_id"],
                    title="Alpha",
                    score=0.92,
                    highlight="Alpha 摘要",
                    metadata={"source_type": "generic", "tags": "测试"},
                ),
            ]
        ),
        sqlite_store=SQLiteStore(relation_pipeline_env["db_path"]),
        relation_query_service=RelationQueryService(
            RelationStore(relation_pipeline_env["db_path"])
        ),
    )

    result = exploration_service.timeline_of(
        topic="Alpha 时间线",
        top_k=5,
        sort_order="asc",
    )

    assert result.found is True
    assert result.implementation_level == "partial"
    assert result.schema_version == "phase_b.v1"
    assert [item.knowledge_id for item in result.items] == [
        relation_pipeline_env["alpha_id"],
        relation_pipeline_env["beta_id"],
    ]
    assert result.time_source_priority == ["event_time", "published_at", "archived_at"]
    assert all(item.time_source == "archived_at" for item in result.items)


def test_exploration_service_can_build_partial_contrast(relation_pipeline_env):
    conn = sqlite3.connect(str(relation_pipeline_env["db_path"]))
    conn.execute(
        "UPDATE knowledge_items SET tags = ? WHERE knowledge_id = ?",
        ("测试,共同", relation_pipeline_env["alpha_id"]),
    )
    conn.execute(
        "UPDATE knowledge_items SET tags = ? WHERE knowledge_id = ?",
        ("桥接,共同", relation_pipeline_env["gamma_id"]),
    )
    conn.execute(
        "UPDATE knowledge_items SET tags = ? WHERE knowledge_id = ?",
        ("终点,共同", relation_pipeline_env["delta_id"]),
    )
    conn.commit()
    conn.close()

    class ContrastRouter:
        def search(self, query: str, limit: int = 10):
            if query == "Topic A":
                return [
                    SearchResult(
                        knowledge_id=relation_pipeline_env["alpha_id"],
                        title="Alpha",
                        score=0.92,
                        highlight="Alpha 摘要",
                        metadata={"source_type": "generic", "tags": "测试,共同"},
                    ),
                    SearchResult(
                        knowledge_id=relation_pipeline_env["gamma_id"],
                        title="Gamma",
                        score=0.78,
                        highlight="Gamma 摘要",
                        metadata={"source_type": "generic", "tags": "桥接,共同"},
                    ),
                ]
            return [
                SearchResult(
                    knowledge_id=relation_pipeline_env["delta_id"],
                    title="Delta",
                    score=0.88,
                    highlight="Delta 摘要",
                    metadata={"source_type": "generic", "tags": "终点,共同"},
                ),
                SearchResult(
                    knowledge_id=relation_pipeline_env["gamma_id"],
                    title="Gamma",
                    score=0.75,
                    highlight="Gamma 摘要",
                    metadata={"source_type": "generic", "tags": "桥接,共同"},
                ),
            ]

    exploration_service = ExplorationService(
        query_router=ContrastRouter(),
        sqlite_store=SQLiteStore(relation_pipeline_env["db_path"]),
        relation_query_service=RelationQueryService(
            RelationStore(relation_pipeline_env["db_path"])
        ),
    )

    result = exploration_service.contrast(
        topic_a="Topic A",
        topic_b="Topic B",
        top_k=5,
    )

    assert result.found is True
    assert result.implementation_level == "partial"
    assert result.schema_version == "phase_b.v1"
    assert result.shared_tags == ["共同", "桥接"]
    assert result.only_a_tags == ["测试"]
    assert result.only_b_tags == ["终点"]
    assert result.overlap_knowledge_ids == [relation_pipeline_env["gamma_id"]]
    assert result.comparison_dimensions["shared_tags_count"] == 2
    assert result.evidence_sources == [
        "query_results",
        "entry_tags",
        "entry_summary",
    ]


def test_phase_b_5_4_min_regression_dataset_is_executable(relation_pipeline_env):
    backfill_service = RelationBackfillService(
        db_path=relation_pipeline_env["db_path"],
        vault_dir=relation_pipeline_env["vault_dir"],
    )
    relation_store = RelationStore(relation_pipeline_env["db_path"])
    query_service = RelationQueryService(relation_store)

    for case in _load_regression_cases():
        action = case["action"]
        expect = case["expect"]

        if action == "backfill":
            result = backfill_service.backfill(apply=case["args"]["apply"])
            assert result.applied_relations == expect["applied_relations"], case["case_id"]
            assert result.extracted_relations == expect["extracted_relations"], case["case_id"]
            assert result.total_references == expect["total_references"], case["case_id"]
            assert result.coverage_rate == expect["coverage_rate"], case["case_id"]
            assert result.noise_rate == expect["noise_rate"], case["case_id"]
            assert result.conflict_rate == expect["conflict_rate"], case["case_id"]
            continue

        if action == "list_relations":
            result = query_service.list_relations(
                seed_knowledge_id=relation_pipeline_env[case["args"]["seed_key"]],
                direction=RelationQueryDirection(case["args"]["direction"]),
            )
            assert result.total == expect["total"], case["case_id"]
            assert list(result.grouped_items.keys()) == expect["grouped_keys"], case["case_id"]
            continue

        if action == "relations_between":
            result = query_service.get_relations_between(
                relation_pipeline_env[case["args"]["source_key"]],
                relation_pipeline_env[case["args"]["target_key"]],
            )
            assert result.total == expect["total"], case["case_id"]
            assert list(result.grouped_items.keys()) == expect["grouped_keys"], case["case_id"]
            continue

        if action == "query_subgraph":
            result = query_service.query_subgraph(
                seed_knowledge_id=relation_pipeline_env[case["args"]["seed_key"]],
                depth=case["args"]["depth"],
            )
            assert [node.knowledge_id for node in result.nodes] == _resolve_knowledge_ids(
                relation_pipeline_env,
                expect["node_keys"],
            ), case["case_id"]
            assert result.total_edges == expect["total_edges"], case["case_id"]
            continue

        if action == "explain_relation":
            result = query_service.explain_relation(
                relation_pipeline_env[case["args"]["source_key"]],
                relation_pipeline_env[case["args"]["target_key"]],
                max_depth=case["args"]["max_depth"],
            )
            assert result.found is expect["found"], case["case_id"]
            assert result.explanation_type == expect["explanation_type"], case["case_id"]
            assert result.hops == expect["hops"], case["case_id"]
            assert result.intermediate_knowledge_ids == _resolve_knowledge_ids(
                relation_pipeline_env,
                expect["intermediate_keys"],
            ), case["case_id"]
            continue

        if action == "collect_evidence":
            evidence_service = EvidenceCollectionService(
                query_router=StubQueryRouter(
                    [
                        SearchResult(
                            knowledge_id=relation_pipeline_env["alpha_id"],
                            title="Alpha",
                            score=0.95,
                            highlight="Alpha 摘要",
                            metadata={"source_type": "generic", "tags": "测试"},
                        ),
                        SearchResult(
                            knowledge_id=relation_pipeline_env["delta_id"],
                            title="Delta",
                            score=0.88,
                            highlight="Delta 摘要",
                            metadata={"source_type": "generic", "tags": "测试"},
                        ),
                    ]
                ),
                sqlite_store=SQLiteStore(relation_pipeline_env["db_path"]),
                markdown_store=MarkdownStore(relation_pipeline_env["vault_dir"]),
                relation_query_service=query_service,
            )
            result = evidence_service.collect_evidence(
                question=case["args"]["question"],
                top_k=case["args"]["top_k"],
                relation_max_depth=case["args"]["relation_max_depth"],
            )
            assert result.found is expect["found"], case["case_id"]
            assert result.total_evidence == expect["total_evidence"], case["case_id"]
            assert result.related_evidence_count == expect["related_evidence_count"], case["case_id"]
            assert result.seed_knowledge_id == relation_pipeline_env[expect["seed_key"]], case["case_id"]
            assert result.evidence[1].knowledge_id == relation_pipeline_env[expect["related_key"]], case["case_id"]
            assert result.evidence[1].relation_hops == expect["relation_hops"], case["case_id"]
            continue

        if action == "find_bridges":
            exploration_service = ExplorationService(
                query_router=StubQueryRouter([]),
                sqlite_store=SQLiteStore(relation_pipeline_env["db_path"]),
                relation_query_service=query_service,
            )
            result = exploration_service.find_bridges(
                seed_knowledge_id=relation_pipeline_env[case["args"]["seed_key"]],
                top_k=case["args"]["top_k"],
                max_depth=case["args"]["max_depth"],
            )
            assert result.found is expect["found"], case["case_id"]
            assert result.implementation_level == expect["implementation_level"], case["case_id"]
            assert [item.knowledge_id for item in result.items] == _resolve_knowledge_ids(
                relation_pipeline_env,
                expect["item_keys"],
            ), case["case_id"]
            continue

        if action == "timeline_of":
            exploration_service = ExplorationService(
                query_router=StubQueryRouter(
                    [
                        SearchResult(
                            knowledge_id=relation_pipeline_env["beta_id"],
                            title="Beta",
                            score=0.85,
                            highlight="Beta 摘要",
                            metadata={"source_type": "generic", "tags": "测试"},
                        ),
                        SearchResult(
                            knowledge_id=relation_pipeline_env["alpha_id"],
                            title="Alpha",
                            score=0.92,
                            highlight="Alpha 摘要",
                            metadata={"source_type": "generic", "tags": "测试"},
                        ),
                    ]
                ),
                sqlite_store=SQLiteStore(relation_pipeline_env["db_path"]),
                relation_query_service=query_service,
            )
            result = exploration_service.timeline_of(
                topic=case["args"]["topic"],
                top_k=case["args"]["top_k"],
                sort_order=case["args"]["sort_order"],
            )
            assert result.found is expect["found"], case["case_id"]
            assert result.implementation_level == expect["implementation_level"], case["case_id"]
            assert [item.knowledge_id for item in result.items] == _resolve_knowledge_ids(
                relation_pipeline_env,
                expect["item_keys"],
            ), case["case_id"]
            assert result.time_source_priority == expect["time_source_priority"], case["case_id"]
            continue

        if action == "contrast":
            conn = sqlite3.connect(str(relation_pipeline_env["db_path"]))
            conn.execute(
                "UPDATE knowledge_items SET tags = ? WHERE knowledge_id = ?",
                ("测试,共同", relation_pipeline_env["alpha_id"]),
            )
            conn.execute(
                "UPDATE knowledge_items SET tags = ? WHERE knowledge_id = ?",
                ("桥接,共同", relation_pipeline_env["gamma_id"]),
            )
            conn.execute(
                "UPDATE knowledge_items SET tags = ? WHERE knowledge_id = ?",
                ("终点,共同", relation_pipeline_env["delta_id"]),
            )
            conn.commit()
            conn.close()

            class ContrastRouter:
                def search(self, query: str, limit: int = 10):
                    if query == "Topic A":
                        return [
                            SearchResult(
                                knowledge_id=relation_pipeline_env["alpha_id"],
                                title="Alpha",
                                score=0.92,
                                highlight="Alpha 摘要",
                                metadata={"source_type": "generic", "tags": "测试,共同"},
                            ),
                            SearchResult(
                                knowledge_id=relation_pipeline_env["gamma_id"],
                                title="Gamma",
                                score=0.78,
                                highlight="Gamma 摘要",
                                metadata={"source_type": "generic", "tags": "桥接,共同"},
                            ),
                        ]
                    return [
                        SearchResult(
                            knowledge_id=relation_pipeline_env["delta_id"],
                            title="Delta",
                            score=0.88,
                            highlight="Delta 摘要",
                            metadata={"source_type": "generic", "tags": "终点,共同"},
                        ),
                        SearchResult(
                            knowledge_id=relation_pipeline_env["gamma_id"],
                            title="Gamma",
                            score=0.75,
                            highlight="Gamma 摘要",
                            metadata={"source_type": "generic", "tags": "桥接,共同"},
                        ),
                    ]

            exploration_service = ExplorationService(
                query_router=ContrastRouter(),
                sqlite_store=SQLiteStore(relation_pipeline_env["db_path"]),
                relation_query_service=query_service,
            )
            result = exploration_service.contrast(
                topic_a=case["args"]["topic_a"],
                topic_b=case["args"]["topic_b"],
                top_k=case["args"]["top_k"],
            )
            assert result.found is expect["found"], case["case_id"]
            assert result.implementation_level == expect["implementation_level"], case["case_id"]
            assert result.shared_tags == expect["shared_tags"], case["case_id"]
            assert result.only_a_tags == expect["only_a_tags"], case["case_id"]
            assert result.only_b_tags == expect["only_b_tags"], case["case_id"]
            assert result.overlap_knowledge_ids == _resolve_knowledge_ids(
                relation_pipeline_env,
                expect["overlap_keys"],
            ), case["case_id"]
            continue

        if action == "rerun_cleanup":
            first_report = backfill_service.backfill(
                knowledge_ids=[relation_pipeline_env[case["args"]["seed_key"]]],
                apply=True,
            )
            alpha_path = relation_pipeline_env["vault_dir"] / "alpha.md"
            alpha_path.write_text(
                "---\n"
                "title: Alpha\n"
                "---\n"
                "# Alpha\n\n正文已删除引用\n",
                encoding="utf-8",
            )
            second_report = backfill_service.backfill(
                knowledge_ids=[relation_pipeline_env[case["args"]["seed_key"]]],
                apply=True,
            )
            rows = relation_store.list_relations_for_knowledge(
                relation_pipeline_env[case["args"]["seed_key"]],
                direction=RelationQueryDirection.OUTGOING,
            )
            assert first_report.applied_relations == expect["first_applied_relations"], case["case_id"]
            assert second_report.deleted_relations == expect["deleted_relations"], case["case_id"]
            assert second_report.applied_relations == expect["applied_relations"], case["case_id"]
            assert rows == [], case["case_id"]
            continue

        raise AssertionError(f"未知回归样例 action: {action}")
