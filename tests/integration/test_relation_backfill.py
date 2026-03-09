"""
Integration tests for relation backfill.
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
def relation_env(tmp_path: Path):
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
    beta_path.write_text("# Beta\n\n正文", encoding="utf-8")
    gamma_path.write_text("# Gamma\n\n正文", encoding="utf-8")

    alpha_id = _insert_entry(db_path, alpha_path, "Alpha", "https://example.com/a")
    beta_id = _insert_entry(db_path, beta_path, "Beta", "https://example.com/b")
    gamma_id = _insert_entry(db_path, gamma_path, "Gamma", "https://example.com/c")

    return {
        "db_path": db_path,
        "vault_dir": vault_dir,
        "alpha_path": alpha_path,
        "alpha_id": alpha_id,
        "beta_id": beta_id,
        "gamma_id": gamma_id,
    }


def test_relation_backfill_dry_run_does_not_write(relation_env):
    service = RelationBackfillService(
        db_path=relation_env["db_path"],
        vault_dir=relation_env["vault_dir"],
    )
    relation_store = RelationStore(relation_env["db_path"])

    report = service.backfill(apply=False)
    rows = relation_store.list_relations_for_knowledge(
        relation_env["alpha_id"], direction=RelationQueryDirection.BOTH
    )

    assert report.scanned_entries == 3
    assert report.processed_entries == 3
    assert report.extracted_relations == 2
    assert report.applied_relations == 0
    assert rows == []


def test_relation_backfill_apply_writes_low_ambiguity_relations(relation_env):
    service = RelationBackfillService(
        db_path=relation_env["db_path"],
        vault_dir=relation_env["vault_dir"],
    )
    relation_store = RelationStore(relation_env["db_path"])

    report = service.backfill(
        knowledge_ids=[relation_env["alpha_id"]],
        apply=True,
    )
    rows = relation_store.list_relations_for_knowledge(
        relation_env["alpha_id"], direction=RelationQueryDirection.OUTGOING
    )

    assert report.scanned_entries == 1
    assert report.extracted_relations == 2
    assert report.applied_relations == 2
    assert len(rows) == 2
    assert {row.relation_type for row in rows} == {
        RelationType.REFERENCES,
        RelationType.RELATED_DOCUMENT,
    }


def test_relation_backfill_rerun_syncs_outgoing_relations(relation_env):
    service = RelationBackfillService(
        db_path=relation_env["db_path"],
        vault_dir=relation_env["vault_dir"],
    )
    relation_store = RelationStore(relation_env["db_path"])

    first_report = service.backfill(
        knowledge_ids=[relation_env["alpha_id"]],
        apply=True,
    )
    relation_env["alpha_path"].write_text(
        "---\n"
        "title: Alpha\n"
        "---\n"
        "# Alpha\n\n正文已删除引用\n",
        encoding="utf-8",
    )

    second_report = service.backfill(
        knowledge_ids=[relation_env["alpha_id"]],
        apply=True,
    )
    rows = relation_store.list_relations_for_knowledge(
        relation_env["alpha_id"], direction=RelationQueryDirection.OUTGOING
    )

    assert first_report.applied_relations == 2
    assert second_report.deleted_relations == 2
    assert second_report.applied_relations == 0
    assert rows == []
