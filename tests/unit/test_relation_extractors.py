"""
Unit tests for relation extractors.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.relations.extractors import (  # noqa: E402
    BackfillReport,
    extract_frontmatter_related_docs,
    extract_markdown_link_references,
    parse_front_matter,
)
from src.relations.models import RelationSourceType, RelationType  # noqa: E402


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
