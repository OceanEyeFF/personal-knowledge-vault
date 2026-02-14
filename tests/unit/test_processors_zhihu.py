"""
Zhihu processor unit tests.
"""

import sys
from pathlib import Path

# Add project root to Python path
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from unittest.mock import AsyncMock, patch

import pytest

from src.processors.zhihu_processor import ZhihuProcessor


@pytest.fixture
def zhihu_html() -> str:
    """Load Zhihu HTML fixture."""
    fixture_path = Path(__file__).parent.parent / "fixtures" / "zhihu_sample.html"
    return fixture_path.read_text(encoding="utf-8")


def test_zhihu_can_handle():
    """can_handle should recognize Zhihu URLs."""
    assert ZhihuProcessor.can_handle("https://www.zhihu.com/question/123456")
    assert not ZhihuProcessor.can_handle("https://example.com")


@pytest.mark.asyncio
async def test_zhihu_process(zhihu_html: str):
    """Processor should extract the best answer content."""
    processor = ZhihuProcessor()

    with patch.object(processor, "_fetch_html", new=AsyncMock(return_value=zhihu_html)):
        entry = await processor.process("https://www.zhihu.com/question/123456")

    assert entry.title == "Why is the sky blue?"
    assert entry.source_type == "zhihu"
    assert entry.source_url == "https://www.zhihu.com/question/123456"
    assert "Best answer content." in entry.content
    assert "Low score answer." not in entry.content

    metadata = getattr(entry, "metadata", {})
    assert metadata.get("author") == "Zhihu Author"
    assert metadata.get("published_time") == "2026-02-13 09:00:00"
