"""
Base processor unit tests.
"""

import sys
from pathlib import Path

# Add project root to Python path
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

import pytest
from bs4 import BeautifulSoup

from src.processors.base import BaseProcessor
from src.storage.markdown_store import Entry


class DummyProcessor(BaseProcessor):
    """Concrete processor for testing base behavior."""

    @classmethod
    def can_handle(cls, url: str) -> bool:
        return True

    async def process(self, url: str) -> Entry:
        return Entry(title="Dummy", source_type="generic", content="Hello")


class MissingProcessProcessor(BaseProcessor):
    """Subclass that intentionally omits process implementation."""

    @classmethod
    def can_handle(cls, url: str) -> bool:
        return True


def test_base_is_abstract():
    """BaseProcessor cannot be instantiated directly."""
    with pytest.raises(TypeError):
        BaseProcessor()


def test_missing_abstract_methods():
    """Subclass missing abstract methods should fail instantiation."""
    with pytest.raises(TypeError):
        MissingProcessProcessor()


def test_html_to_markdown():
    """Default HTML to Markdown conversion preserves key structure."""
    processor = DummyProcessor()
    html = "<h1>Title</h1><p>Hello</p><ul><li>Item</li></ul><pre><code>print('hi')</code></pre>"
    markdown = processor._html_to_markdown(html)

    assert "# Title" in markdown
    assert "Hello" in markdown
    assert "- Item" in markdown or "* Item" in markdown
    assert "print('hi')" in markdown


def test_extract_metadata_from_meta():
    """Metadata extraction should read Open Graph and standard meta fields."""
    processor = DummyProcessor()
    html = """
    <html><head>
    <meta property="og:title" content="OG Title"/>
    <meta name="author" content="Alice"/>
    <meta property="article:published_time" content="2026-02-14"/>
    <meta name="description" content="Summary"/>
    </head><body></body></html>
    """
    soup = BeautifulSoup(html, "lxml")
    metadata = processor._extract_metadata(soup)

    assert metadata["title"] == "OG Title"
    assert metadata["author"] == "Alice"
    assert metadata["published_time"] == "2026-02-14"
    assert metadata["description"] == "Summary"


def test_extract_metadata_fallback_title():
    """Metadata extraction should fall back to <title> when og:title missing."""
    processor = DummyProcessor()
    html = "<html><head><title>Fallback</title></head><body><h1>Ignored</h1></body></html>"
    soup = BeautifulSoup(html, "lxml")
    metadata = processor._extract_metadata(soup)

    assert metadata["title"] == "Fallback"
