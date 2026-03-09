"""
Integration tests for relation backfill + query pipeline.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.relations.extractors import RelationBackfillService  # noqa: E402
from src.relations.models import RelationQueryDirection, RelationType  # noqa: E402
from src.relations.query_service import RelationQueryService  # noqa: E402
from src.storage.relation_store import RelationStore  # noqa: E402

BASE_SQL = PROJECT_ROOT / "scripts/migrations/001_initial_schema.sql"
RELATION_SQL = PROJECT_ROOT / "scripts/migrations/006_add_relations_foundation.sql"


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
    gamma_path.write_text("# Gamma\n\n正文", encoding="utf-8")

    alpha_id = _insert_entry(db_path, alpha_path, "Alpha", "https://example.com/a")
    beta_id = _insert_entry(db_path, beta_path, "Beta", "https://example.com/b")
    gamma_id = _insert_entry(db_path, gamma_path, "Gamma", "https://example.com/c")

    return {
        "db_path": db_path,
        "vault_dir": vault_dir,
        "alpha_id": alpha_id,
        "beta_id": beta_id,
        "gamma_id": gamma_id,
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

    assert report.applied_relations == 3
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
