"""
Unit tests for relation extractors.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.runtime import ErrorCode, PKVRuntimeError  # noqa: E402
from src.relations.extractors import (  # noqa: E402
    BackfillReport,
    RelationBackfillService,
    extract_frontmatter_relation_fields,
    extract_frontmatter_related_docs,
    extract_markdown_link_references,
    parse_front_matter,
)
from src.relations.models import RelationSourceType, RelationType  # noqa: E402
from src.storage.markdown_store import Entry  # noqa: E402
from src.storage.migration_manager import MigrationManager  # noqa: E402
from src.storage.sqlite_store import SQLiteStore  # noqa: E402


MIGRATIONS_DIR = PROJECT_ROOT / "scripts" / "migrations"


def test_parse_front_matter_returns_metadata_and_body():
    markdown_text = (
        "---\n"
        "title: Alpha\n"
        "related_docs:\n"
        "  - beta.md\n"
        "---\n"
        "# Alpha\n\nBody"
    )

    metadata, body = parse_front_matter(markdown_text)

    assert metadata["title"] == "Alpha"
    assert metadata["related_docs"] == ["beta.md"]
    assert body.startswith("# Alpha")


def test_extract_markdown_link_references_skips_external_and_anchor_links():
    markdown_text = (
        "# Alpha\n"
        "[内部链接](./beta.md)\n"
        "[带标题](./gamma.md \"Gamma\")\n"
        "[外部](https://example.com)\n"
        "[锚点](#section)\n"
        "![图片](./image.png)\n"
    )

    refs, issues = extract_markdown_link_references(markdown_text)

    assert len(refs) == 2
    assert len(issues) == 2
    assert refs[0].relation_type == RelationType.REFERENCES
    assert refs[0].relation_source_type == RelationSourceType.MARKDOWN_LINK
    assert refs[0].raw_target == "./beta.md"
    assert refs[1].raw_target == "./gamma.md"


def test_extract_frontmatter_related_docs_handles_list_only():
    markdown_text = (
        "---\n"
        "related_docs:\n"
        "  - docs/beta.md\n"
        "  - docs/gamma.md\n"
        "---\n"
        "Body"
    )

    refs, issues = extract_frontmatter_related_docs(markdown_text)

    assert len(refs) == 2
    assert issues == []
    assert refs[0].relation_type == RelationType.RELATED_DOCUMENT
    assert refs[0].relation_source_type == RelationSourceType.FRONTMATTER_RELATED_DOCS
    assert refs[0].raw_target == "docs/beta.md"


def test_extract_frontmatter_relation_fields_handles_children_and_version_of():
    markdown_text = (
        "---\n"
        "children:\n"
        "  - docs/chapter-1.md\n"
        "  - ./chapter-2.md\n"
        "version_of: beta.md\n"
        "---\n"
        "Body"
    )

    refs, issues = extract_frontmatter_relation_fields(markdown_text)

    assert issues == []
    assert len(refs) == 3
    assert [ref.relation_type for ref in refs] == [
        RelationType.PARENT_OF,
        RelationType.PARENT_OF,
        RelationType.VERSION_OF,
    ]
    assert all(
        ref.relation_source_type == RelationSourceType.FRONTMATTER_FIELD
        for ref in refs
    )
    assert refs[0].evidence_payload["field"] == "children"
    assert refs[2].evidence_payload["field"] == "version_of"


def test_extract_frontmatter_relation_fields_rejects_invalid_values():
    markdown_text = (
        "---\n"
        "children: chapter-1.md\n"
        "version_of: https://example.com/base.md\n"
        "---\n"
        "Body"
    )

    refs, issues = extract_frontmatter_relation_fields(markdown_text)

    assert refs == []
    assert len(issues) == 2
    assert issues[0].relation_type == RelationType.PARENT_OF
    assert issues[0].reason == "invalid_field_type"
    assert issues[0].detail["field"] == "children"
    assert issues[1].relation_type == RelationType.VERSION_OF
    assert issues[1].reason == "external_link"
    assert issues[1].detail["field"] == "version_of"


def test_backfill_report_quality_gate_and_markdown_summary():
    report = BackfillReport(
        mode="apply",
        knowledge_scope=[1, 2, 3],
        scanned_entries=3,
        processed_entries=3,
        extracted_relations=4,
        applied_relations=4,
        total_references=10,
        resolved_references=9,
        invalid_references=1,
        conflict_samples=[
            {
                "source_knowledge_id": 1,
                "target_knowledge_id": 2,
                "relation_source_type": RelationSourceType.MARKDOWN_LINK.value,
                "relation_type": RelationType.REFERENCES.value,
            }
        ],
        extensions={"execution": {"apply": True}},
    )

    gate = report.evaluate_quality_gate(
        min_coverage=0.8,
        max_noise=0.2,
        max_conflict=0.1,
    )
    markdown = report.to_markdown()
    payload = report.to_dict(include_definitions=False)

    assert gate["configured"] is True
    assert gate["passed"] is True
    assert payload["quality_gate"]["passed"] is True
    assert payload["metric_definitions"] == {}
    assert payload["mode"] == "apply"
    assert payload["knowledge_scope"] == [1, 2, 3]
    assert payload["conflict_samples"][0]["target_knowledge_id"] == 2
    assert "## 质量门禁" in markdown
    assert "coverage_rate" in markdown
    assert "## 冲突样本" in markdown
    assert "## 扩展上下文" in markdown


def test_backfill_rejects_database_path_outside_vault_without_reading_it(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    db_path = data_root / "db" / "vault.db"
    vault_dir = data_root / "vault"
    vault_dir.mkdir(parents=True)
    MigrationManager(db_path, MIGRATIONS_DIR).initialize_fresh()

    outside = tmp_path / "outside.md"
    outside.write_text("outside sentinel", encoding="utf-8")
    SQLiteStore(db_path).insert_entry(
        Entry(title="unsafe", source_type="text", content="must not be used"),
        str(outside),
    )

    with pytest.raises(PKVRuntimeError) as exc_info:
        RelationBackfillService(db_path, vault_dir).backfill(apply=False)

    assert exc_info.value.code is ErrorCode.PATH_OUTSIDE_VAULT
    assert outside.read_text(encoding="utf-8") == "outside sentinel"


def test_backfill_report_quality_gate_can_fail():
    report = BackfillReport(
        total_references=10,
        resolved_references=6,
        invalid_references=3,
        unresolved_references=1,
        conflicted_relations=2,
    )

    gate = report.evaluate_quality_gate(
        min_coverage=0.8,
        max_noise=0.2,
        max_conflict=0.1,
    )

    assert gate["configured"] is True
    assert gate["passed"] is False
    assert [item["name"] for item in gate["failed_checks"]] == [
        "coverage_rate",
        "noise_rate",
        "conflict_rate",
    ]


def test_load_entries_closes_connection_when_query_fails(
    tmp_path: Path, monkeypatch
) -> None:
    """_load_entries 查询任一步失败也必须关闭连接（恰好一次）。"""
    data_root = tmp_path / "data"
    db_path = data_root / "db" / "vault.db"
    vault_dir = data_root / "vault"
    vault_dir.mkdir(parents=True)
    MigrationManager(db_path, MIGRATIONS_DIR).initialize_fresh()

    service = RelationBackfillService(db_path, vault_dir)

    real_connect = sqlite3.connect
    opened: list[sqlite3.Connection] = []
    close_counts: dict[sqlite3.Connection, int] = {}

    class TrackingConnection(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            if isinstance(sql, str) and "SELECT knowledge_id" in sql:
                raise sqlite3.OperationalError("simulated query failure")
            return super().execute(sql, parameters)

        def close(self):
            close_counts[self] = close_counts.get(self, 0) + 1
            super().close()

    def tracked_connect(database, *args, **kwargs):
        conn = real_connect(database, *args, factory=TrackingConnection, **kwargs)
        opened.append(conn)
        return conn

    monkeypatch.setattr(sqlite3, "connect", tracked_connect)

    with pytest.raises(sqlite3.OperationalError):
        service._load_entries()

    assert len(opened) == 1
    assert close_counts.get(opened[0], 0) == 1
