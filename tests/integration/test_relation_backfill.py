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
from src.relations.models import (  # noqa: E402
    RelationQueryDirection,
    RelationRecord,
    RelationSourceType,
    RelationType,
)
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


@pytest.fixture
def frontmatter_field_env(tmp_path: Path):
    db_path = tmp_path / "frontmatter.db"
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir(parents=True, exist_ok=True)

    _apply_sql(db_path, BASE_SQL)
    _apply_sql(db_path, RELATION_SQL)

    outline_path = vault_dir / "outline.md"
    chapter_path = vault_dir / "chapter-1.md"
    beta_path = vault_dir / "beta.md"
    beta_v2_path = vault_dir / "beta-v2.md"

    outline_path.write_text(
        "---\n"
        "title: Outline\n"
        "children:\n"
        "  - chapter-1.md\n"
        "---\n"
        "# Outline\n\n正文\n",
        encoding="utf-8",
    )
    chapter_path.write_text("# Chapter 1\n\n正文", encoding="utf-8")
    beta_path.write_text("# Beta\n\n正文", encoding="utf-8")
    beta_v2_path.write_text(
        "---\n"
        "title: Beta V2\n"
        "version_of: beta.md\n"
        "---\n"
        "# Beta V2\n\n正文\n",
        encoding="utf-8",
    )

    outline_id = _insert_entry(db_path, outline_path, "Outline", "https://example.com/o")
    chapter_id = _insert_entry(db_path, chapter_path, "Chapter 1", "https://example.com/c1")
    beta_id = _insert_entry(db_path, beta_path, "Beta", "https://example.com/b")
    beta_v2_id = _insert_entry(db_path, beta_v2_path, "Beta V2", "https://example.com/b2")

    return {
        "db_path": db_path,
        "vault_dir": vault_dir,
        "outline_path": outline_path,
        "beta_v2_path": beta_v2_path,
        "outline_id": outline_id,
        "chapter_id": chapter_id,
        "beta_id": beta_id,
        "beta_v2_id": beta_v2_id,
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
    assert report.total_references == 2
    assert report.resolved_references == 2
    assert report.invalid_references == 0
    assert report.unresolved_references == 0
    assert report.conflicted_relations == 0
    assert report.coverage_rate == 1.0
    assert report.noise_rate == 0.0
    assert report.conflict_rate == 0.0
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
    assert report.mode == "apply"
    assert report.knowledge_scope == [relation_env["alpha_id"]]
    assert report.extensions["execution"]["apply"] is True


def test_relation_backfill_report_contains_quality_gate_context(relation_env):
    service = RelationBackfillService(
        db_path=relation_env["db_path"],
        vault_dir=relation_env["vault_dir"],
    )

    report = service.backfill(
        knowledge_ids=[relation_env["alpha_id"]],
        apply=True,
    )
    gate = report.evaluate_quality_gate(
        min_coverage=0.9,
        max_noise=0.1,
        max_conflict=0.0,
    )
    payload = report.to_dict(include_definitions=False)

    assert gate["configured"] is True
    assert gate["passed"] is True
    assert payload["mode"] == "apply"
    assert payload["knowledge_scope"] == [relation_env["alpha_id"]]
    assert payload["quality_gate"]["passed"] is True
    assert payload["extensions"]["execution"]["relation_table_exists"] is True
    assert "# 关系回填质量报告" in report.to_markdown()


def test_relation_backfill_conflict_detection_is_relation_type_scoped(relation_env):
    service = RelationBackfillService(
        db_path=relation_env["db_path"],
        vault_dir=relation_env["vault_dir"],
    )
    relation_store = RelationStore(relation_env["db_path"])

    # 插入“同一对节点，但不同 relation_type”的高优先级既有关系。
    # conflict 检测应只在同一 relation_type 内比较优先级，避免误判。
    relation_store.upsert_relation(
        RelationRecord(
            source_knowledge_id=relation_env["alpha_id"],
            target_knowledge_id=relation_env["beta_id"],
            relation_type=RelationType.VERSION_OF,
            relation_source_type=RelationSourceType.MANUAL,
            evidence_payload={"note": "manual_version_of"},
        )
    )

    report = service.backfill(
        knowledge_ids=[relation_env["alpha_id"]],
        apply=True,
    )

    assert report.total_references == 2
    assert report.resolved_references == 2
    assert report.conflicted_relations == 0
    assert report.conflict_rate == 0.0


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


def test_relation_backfill_apply_writes_frontmatter_field_relations(frontmatter_field_env):
    service = RelationBackfillService(
        db_path=frontmatter_field_env["db_path"],
        vault_dir=frontmatter_field_env["vault_dir"],
    )
    relation_store = RelationStore(frontmatter_field_env["db_path"])

    report = service.backfill(apply=True)
    outline_rows = relation_store.list_relations_for_knowledge(
        frontmatter_field_env["outline_id"],
        direction=RelationQueryDirection.OUTGOING,
        relation_source_types=[RelationSourceType.FRONTMATTER_FIELD],
    )
    version_rows = relation_store.list_relations_for_knowledge(
        frontmatter_field_env["beta_v2_id"],
        direction=RelationQueryDirection.OUTGOING,
        relation_source_types=[RelationSourceType.FRONTMATTER_FIELD],
    )

    assert report.total_references == 2
    assert report.resolved_references == 2
    assert report.invalid_references == 0
    assert report.unresolved_references == 0
    assert report.by_source_type[RelationSourceType.FRONTMATTER_FIELD.value]["resolved"] == 2
    assert len(outline_rows) == 1
    assert outline_rows[0].relation_type == RelationType.PARENT_OF
    assert outline_rows[0].target_knowledge_id == frontmatter_field_env["chapter_id"]
    assert outline_rows[0].evidence_payload["field"] == "children"
    assert len(version_rows) == 1
    assert version_rows[0].relation_type == RelationType.VERSION_OF
    assert version_rows[0].target_knowledge_id == frontmatter_field_env["beta_id"]
    assert version_rows[0].evidence_payload["field"] == "version_of"


def test_relation_backfill_rerun_syncs_frontmatter_field_relations(frontmatter_field_env):
    service = RelationBackfillService(
        db_path=frontmatter_field_env["db_path"],
        vault_dir=frontmatter_field_env["vault_dir"],
    )
    relation_store = RelationStore(frontmatter_field_env["db_path"])

    first_report = service.backfill(apply=True)
    frontmatter_field_env["outline_path"].write_text(
        "---\n"
        "title: Outline\n"
        "---\n"
        "# Outline\n\n正文已删除子文档\n",
        encoding="utf-8",
    )
    frontmatter_field_env["beta_v2_path"].write_text(
        "---\n"
        "title: Beta V2\n"
        "---\n"
        "# Beta V2\n\n正文已删除版本关联\n",
        encoding="utf-8",
    )

    second_report = service.backfill(
        knowledge_ids=[
            frontmatter_field_env["outline_id"],
            frontmatter_field_env["beta_v2_id"],
        ],
        apply=True,
    )
    remaining_rows = relation_store.list_relations_for_knowledge(
        frontmatter_field_env["outline_id"],
        direction=RelationQueryDirection.OUTGOING,
        relation_source_types=[RelationSourceType.FRONTMATTER_FIELD],
    ) + relation_store.list_relations_for_knowledge(
        frontmatter_field_env["beta_v2_id"],
        direction=RelationQueryDirection.OUTGOING,
        relation_source_types=[RelationSourceType.FRONTMATTER_FIELD],
    )

    assert first_report.applied_relations == 2
    assert second_report.deleted_relations == 2
    assert second_report.applied_relations == 0
    assert remaining_rows == []
