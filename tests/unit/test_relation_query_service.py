"""
Unit tests for RelationQueryService.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.relations.models import RelationQueryDirection, RelationRecord, RelationSourceType, RelationType  # noqa: E402
from src.relations.query_service import RelationQueryService  # noqa: E402
from src.storage.relation_store import RelationStore  # noqa: E402

BASE_SCHEMA_PATH = PROJECT_ROOT / "scripts" / "migrations" / "001_initial_schema.sql"
RELATION_SCHEMA_PATH = (
    PROJECT_ROOT / "scripts" / "migrations" / "006_add_relations_foundation.sql"
)


def _apply_sql(db_path: Path, sql_path: Path) -> None:
    sql = sql_path.read_text(encoding="utf-8")
    conn = sqlite3.connect(str(db_path))
    conn.executescript(sql)
    conn.close()


def _insert_entry(db_path: Path, title: str) -> int:
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
            f"https://example.com/{title.lower()}",
            f"{title}.md",
            f"# {title}\n\n测试内容",
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
def query_env(tmp_path: Path):
    db_path = tmp_path / "test.db"
    _apply_sql(db_path, BASE_SCHEMA_PATH)
    _apply_sql(db_path, RELATION_SCHEMA_PATH)

    relation_store = RelationStore(db_path)
    query_service = RelationQueryService(relation_store)

    alpha_id = _insert_entry(db_path, "Alpha")
    beta_id = _insert_entry(db_path, "Beta")
    gamma_id = _insert_entry(db_path, "Gamma")
    delta_id = _insert_entry(db_path, "Delta")
    outline_id = _insert_entry(db_path, "Outline")
    chapter_id = _insert_entry(db_path, "Chapter")
    version_base_id = _insert_entry(db_path, "VersionBase")
    version_v2_id = _insert_entry(db_path, "VersionV2")

    relation_store.upsert_relation(
        RelationRecord(
            source_knowledge_id=gamma_id,
            target_knowledge_id=alpha_id,
            relation_type=RelationType.REFERENCES,
            relation_source_type=RelationSourceType.MARKDOWN_LINK,
            weight=2.0,
            evidence_payload={"href": "./alpha.md"},
        )
    )
    relation_store.upsert_relation(
        RelationRecord(
            source_knowledge_id=alpha_id,
            target_knowledge_id=gamma_id,
            relation_type=RelationType.REFERENCES,
            relation_source_type=RelationSourceType.MARKDOWN_LINK,
            weight=1.5,
            evidence_payload={"href": "./gamma.md"},
        )
    )
    relation_store.upsert_relation(
        RelationRecord(
            source_knowledge_id=beta_id,
            target_knowledge_id=alpha_id,
            relation_type=RelationType.REFERENCES,
            relation_source_type=RelationSourceType.MARKDOWN_LINK,
            weight=0.5,
            evidence_payload={"href": "./alpha.md"},
        )
    )
    relation_store.upsert_relation(
        RelationRecord(
            source_knowledge_id=alpha_id,
            target_knowledge_id=beta_id,
            relation_type=RelationType.RELATED_DOCUMENT,
            relation_source_type=RelationSourceType.FRONTMATTER_RELATED_DOCS,
            weight=1.0,
            evidence_payload={"field": "related_docs"},
        )
    )
    relation_store.upsert_relation(
        RelationRecord(
            source_knowledge_id=gamma_id,
            target_knowledge_id=delta_id,
            relation_type=RelationType.RELATED_DOCUMENT,
            relation_source_type=RelationSourceType.FRONTMATTER_RELATED_DOCS,
            weight=3.0,
            evidence_payload={"field": "related_docs"},
        )
    )
    relation_store.upsert_relation(
        RelationRecord(
            source_knowledge_id=outline_id,
            target_knowledge_id=chapter_id,
            relation_type=RelationType.PARENT_OF,
            relation_source_type=RelationSourceType.FRONTMATTER_FIELD,
            weight=1.2,
            evidence_payload={"field": "children"},
        )
    )
    relation_store.upsert_relation(
        RelationRecord(
            source_knowledge_id=version_v2_id,
            target_knowledge_id=version_base_id,
            relation_type=RelationType.VERSION_OF,
            relation_source_type=RelationSourceType.FRONTMATTER_FIELD,
            weight=1.1,
            evidence_payload={"field": "version_of"},
        )
    )

    return {
        "query_service": query_service,
        "alpha_id": alpha_id,
        "beta_id": beta_id,
        "gamma_id": gamma_id,
        "delta_id": delta_id,
        "outline_id": outline_id,
        "chapter_id": chapter_id,
        "version_base_id": version_base_id,
        "version_v2_id": version_v2_id,
    }


def test_list_relations_groups_by_type_and_sorts_within_group(query_env):
    query_service = query_env["query_service"]
    alpha_id = query_env["alpha_id"]

    result = query_service.list_relations(
        seed_knowledge_id=alpha_id,
        direction=RelationQueryDirection.BOTH,
    )

    assert result.total == 4
    assert list(result.grouped_items.keys()) == [
        RelationType.REFERENCES.value,
        RelationType.RELATED_DOCUMENT.value,
    ]
    assert [item.weight for item in result.grouped_items[RelationType.REFERENCES.value]] == [
        2.0,
        1.5,
        0.5,
    ]
    assert result.items[0].relation_type == RelationType.REFERENCES
    assert result.items[-1].relation_type == RelationType.RELATED_DOCUMENT


def test_get_neighbors_can_filter_relation_type(query_env):
    query_service = query_env["query_service"]
    alpha_id = query_env["alpha_id"]

    result = query_service.get_neighbors(
        seed_knowledge_id=alpha_id,
        relation_types=[RelationType.RELATED_DOCUMENT],
        limit=10,
    )

    assert result.total == 1
    assert list(result.grouped_items.keys()) == [RelationType.RELATED_DOCUMENT.value]
    assert result.items[0].target_knowledge_id == query_env["beta_id"]


def test_get_relations_between_returns_bidirectional_matches(query_env):
    query_service = query_env["query_service"]
    alpha_id = query_env["alpha_id"]
    gamma_id = query_env["gamma_id"]

    result = query_service.get_relations_between(alpha_id, gamma_id)

    assert result.total == 2
    assert list(result.grouped_items.keys()) == [RelationType.REFERENCES.value]
    assert {
        (item.source_knowledge_id, item.target_knowledge_id)
        for item in result.items
    } == {
        (alpha_id, gamma_id),
        (gamma_id, alpha_id),
    }
    assert result.to_dict()["grouped_items"][RelationType.REFERENCES.value][0]["weight"] == 2.0


def test_list_relations_can_filter_frontmatter_field_source(query_env):
    query_service = query_env["query_service"]

    result = query_service.list_relations(
        seed_knowledge_id=query_env["outline_id"],
        direction=RelationQueryDirection.OUTGOING,
        relation_source_types=[RelationSourceType.FRONTMATTER_FIELD],
    )

    assert result.total == 1
    assert list(result.grouped_items.keys()) == [RelationType.PARENT_OF.value]
    assert result.items[0].target_knowledge_id == query_env["chapter_id"]


def test_explain_relation_supports_frontmatter_field_direct_edges(query_env):
    query_service = query_env["query_service"]

    result = query_service.explain_relation(
        query_env["version_v2_id"],
        query_env["version_base_id"],
    )

    assert result.found is True
    assert result.explanation_type == "direct"
    assert result.hops == 1
    assert result.summary == (
        f"{query_env['version_v2_id']} -[{RelationType.VERSION_OF.value}]-> "
        f"{query_env['version_base_id']}"
    )
    assert result.evidence_items[0]["relation_source_type"] == (
        RelationSourceType.FRONTMATTER_FIELD.value
    )


def test_query_subgraph_can_expand_to_second_hop(query_env):
    query_service = query_env["query_service"]
    alpha_id = query_env["alpha_id"]
    delta_id = query_env["delta_id"]

    result = query_service.query_subgraph(seed_knowledge_id=alpha_id, depth=2)

    assert result.total_nodes == 4
    assert result.total_edges == 5
    assert result.truncated is False
    assert [(node.knowledge_id, node.depth) for node in result.nodes] == [
        (query_env["alpha_id"], 0),
        (query_env["beta_id"], 1),
        (query_env["gamma_id"], 1),
        (delta_id, 2),
    ]
    assert list(result.grouped_edges.keys()) == [
        RelationType.REFERENCES.value,
        RelationType.RELATED_DOCUMENT.value,
    ]
    assert result.to_dict()["nodes"][-1] == {
        "knowledge_id": delta_id,
        "depth": 2,
    }


def test_query_subgraph_respects_depth_limit(query_env):
    query_service = query_env["query_service"]
    alpha_id = query_env["alpha_id"]
    delta_id = query_env["delta_id"]

    result = query_service.query_subgraph(seed_knowledge_id=alpha_id, depth=1)

    assert [node.knowledge_id for node in result.nodes] == [
        alpha_id,
        query_env["beta_id"],
        query_env["gamma_id"],
    ]
    assert all(node.knowledge_id != delta_id for node in result.nodes)


def test_explain_relation_returns_direct_explanation(query_env):
    query_service = query_env["query_service"]

    result = query_service.explain_relation(
        query_env["alpha_id"],
        query_env["gamma_id"],
    )

    assert result.found is True
    assert result.explanation_type == "direct"
    assert result.hops == 1
    assert len(result.path) == 1
    assert len(result.supporting_relations) == 2
    assert result.summary == (
        f"{query_env['alpha_id']} <-[{RelationType.REFERENCES.value}]- "
        f"{query_env['gamma_id']}"
    )
    assert result.evidence_items[0]["relation_type"] == RelationType.REFERENCES.value


def test_explain_relation_can_fallback_to_two_hop_path(query_env):
    query_service = query_env["query_service"]

    result = query_service.explain_relation(
        query_env["alpha_id"],
        query_env["delta_id"],
        max_depth=2,
    )

    assert result.found is True
    assert result.explanation_type == "path"
    assert result.hops == 2
    assert result.intermediate_knowledge_ids == [query_env["gamma_id"]]
    assert result.summary == (
        f"{query_env['alpha_id']} <-[{RelationType.REFERENCES.value}]- "
        f"{query_env['gamma_id']} -[{RelationType.RELATED_DOCUMENT.value}]-> "
        f"{query_env['delta_id']}"
    )
    assert [item["relation_type"] for item in result.evidence_items] == [
        RelationType.REFERENCES.value,
        RelationType.RELATED_DOCUMENT.value,
    ]
