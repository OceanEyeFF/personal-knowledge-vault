"""
Unit tests for relation extractors.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.relations.extractors import (  # noqa: E402
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

    refs = extract_markdown_link_references(markdown_text)

    assert len(refs) == 2
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

    refs = extract_frontmatter_related_docs(markdown_text)

    assert len(refs) == 2
    assert refs[0].relation_type == RelationType.RELATED_DOCUMENT
    assert refs[0].relation_source_type == RelationSourceType.FRONTMATTER_RELATED_DOCS
    assert refs[0].raw_target == "docs/beta.md"
