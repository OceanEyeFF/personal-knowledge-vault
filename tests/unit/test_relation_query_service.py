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

    return {
        "query_service": query_service,
        "alpha_id": alpha_id,
        "beta_id": beta_id,
        "gamma_id": gamma_id,
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
