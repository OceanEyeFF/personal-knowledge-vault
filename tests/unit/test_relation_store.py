"""
Unit tests for RelationStore.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.relations.models import (  # noqa: E402
    RelationQueryDirection,
    RelationRecord,
    RelationSourceType,
    RelationType,
)
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


def _insert_entry(db_path: Path, title: str, source_url: str) -> int:
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
def stores(tmp_path: Path):
    db_path = tmp_path / "test.db"
    _apply_sql(db_path, BASE_SCHEMA_PATH)
    _apply_sql(db_path, RELATION_SCHEMA_PATH)

    relation_store = RelationStore(db_path)
    return {
        "db_path": db_path,
        "relations": relation_store,
    }


@pytest.fixture
def populated_relations(stores):
    db_path = stores["db_path"]
    relation_store = stores["relations"]

    alpha_id = _insert_entry(db_path, "Alpha", "https://example.com/a")
    beta_id = _insert_entry(db_path, "Beta", "https://example.com/b")
    gamma_id = _insert_entry(db_path, "Gamma", "https://example.com/c")

    relation_store.upsert_relation(
        RelationRecord(
            source_knowledge_id=alpha_id,
            target_knowledge_id=beta_id,
            relation_type=RelationType.REFERENCES,
            relation_source_type=RelationSourceType.MARKDOWN_LINK,
            evidence_payload={"href": "./beta.md", "anchor_text": "Beta"},
        )
    )
    relation_store.upsert_relation(
        RelationRecord(
            source_knowledge_id=beta_id,
            target_knowledge_id=gamma_id,
            relation_type=RelationType.RELATED_DOCUMENT,
            relation_source_type=RelationSourceType.FRONTMATTER_RELATED_DOCS,
            evidence_payload={"field": "related_docs"},
        )
    )

    return {
        "alpha_id": alpha_id,
        "beta_id": beta_id,
        "gamma_id": gamma_id,
        **stores,
    }


def test_relation_record_rejects_self_relation():
    with pytest.raises(ValueError):
        RelationRecord(
            source_knowledge_id=1,
            target_knowledge_id=1,
            relation_type=RelationType.REFERENCES,
            relation_source_type=RelationSourceType.MARKDOWN_LINK,
        )


def test_upsert_relation_inserts_and_loads_record(stores):
    db_path = stores["db_path"]
    relation_store = stores["relations"]

    alpha_id = _insert_entry(db_path, "Alpha", "https://example.com/a")
    beta_id = _insert_entry(db_path, "Beta", "https://example.com/b")

    relation_id = relation_store.upsert_relation(
        RelationRecord(
            source_knowledge_id=alpha_id,
            target_knowledge_id=beta_id,
            relation_type=RelationType.REFERENCES,
            relation_source_type=RelationSourceType.MARKDOWN_LINK,
            evidence_payload={"href": "./beta.md"},
        )
    )

    loaded = relation_store.get_relation(relation_id)

    assert loaded is not None
    assert loaded.relation_type == RelationType.REFERENCES
    assert loaded.relation_source_type == RelationSourceType.MARKDOWN_LINK
    assert loaded.evidence_payload["href"] == "./beta.md"


def test_upsert_relation_updates_existing_row(populated_relations):
    relation_store = populated_relations["relations"]
    alpha_id = populated_relations["alpha_id"]
    beta_id = populated_relations["beta_id"]

    first = relation_store.list_relations_for_knowledge(
        alpha_id, direction=RelationQueryDirection.OUTGOING
    )[0]

    relation_id = relation_store.upsert_relation(
        RelationRecord(
            source_knowledge_id=alpha_id,
            target_knowledge_id=beta_id,
            relation_type=RelationType.REFERENCES,
            relation_source_type=RelationSourceType.MARKDOWN_LINK,
            weight=2.0,
            evidence_payload={"href": "./beta.md", "anchor_text": "Updated"},
        )
    )

    second = relation_store.get_relation(relation_id)

    assert relation_id == first.relation_id
    assert second is not None
    assert second.weight == 2.0
    assert second.evidence_payload["anchor_text"] == "Updated"


def test_list_relations_for_knowledge_supports_direction(populated_relations):
    relation_store = populated_relations["relations"]
    alpha_id = populated_relations["alpha_id"]
    beta_id = populated_relations["beta_id"]

    outgoing = relation_store.list_relations_for_knowledge(
        beta_id, direction=RelationQueryDirection.OUTGOING
    )
    incoming = relation_store.list_relations_for_knowledge(
        beta_id, direction=RelationQueryDirection.INCOMING
    )
    both = relation_store.list_relations_for_knowledge(
        beta_id, direction=RelationQueryDirection.BOTH
    )

    assert len(outgoing) == 1
    assert outgoing[0].target_knowledge_id != beta_id
    assert len(incoming) == 1
    assert incoming[0].source_knowledge_id == alpha_id
    assert len(both) == 2


def test_list_relations_for_knowledge_can_filter_by_type(populated_relations):
    relation_store = populated_relations["relations"]
    beta_id = populated_relations["beta_id"]

    rows = relation_store.list_relations_for_knowledge(
        beta_id,
        relation_types=[RelationType.RELATED_DOCUMENT],
    )

    assert len(rows) == 1
    assert rows[0].relation_type == RelationType.RELATED_DOCUMENT


def test_delete_relations_by_source_type(populated_relations):
    relation_store = populated_relations["relations"]
    beta_id = populated_relations["beta_id"]

    deleted = relation_store.delete_relations_by_source_type(
        RelationSourceType.MARKDOWN_LINK.value
    )
    remaining = relation_store.list_relations_for_knowledge(
        beta_id, direction=RelationQueryDirection.BOTH
    )

    assert deleted == 1
    assert len(remaining) == 1
    assert remaining[0].relation_source_type == RelationSourceType.FRONTMATTER_RELATED_DOCS
