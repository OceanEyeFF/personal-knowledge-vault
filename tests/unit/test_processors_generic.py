"""
Generic processor unit tests.
"""

import sys
from pathlib import Path

# Add project root to Python path
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from unittest.mock import AsyncMock, patch

import pytest

from src.processors.generic_processor import GenericProcessor


@pytest.fixture
def generic_html() -> str:
    """Load generic HTML fixture."""
    fixture_path = Path(__file__).parent.parent / "fixtures" / "generic_sample.html"
    return fixture_path.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_generic_processor_process(generic_html: str):
    """Processor should extract title and main content."""
    processor = GenericProcessor()

    with patch.object(processor, "_fetch_html", new=AsyncMock(return_value=generic_html)):
        entry = await processor.process("https://example.com/article")

    assert entry.title == "OG Sample Title"
    assert entry.source_type == "generic"
    assert entry.source_url == "https://example.com/article"
    assert "main content of the article" in entry.content

    metadata = getattr(entry, "metadata", {})
    assert metadata.get("author") == "Test Author"
    assert metadata.get("published_time") == "2026-02-14 10:00:00"
