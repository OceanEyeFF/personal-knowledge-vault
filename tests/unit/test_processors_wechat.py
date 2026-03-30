# ruff: noqa: E402

"""
Wechat processor unit tests.
"""

import sys
from pathlib import Path

# Add project root to Python path
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from unittest.mock import AsyncMock, patch

import pytest

from src.processors.wechat_processor import WechatProcessor


@pytest.fixture
def wechat_html() -> str:
    """Load Wechat HTML fixture."""
    fixture_path = Path(__file__).parent.parent / "fixtures" / "wechat_sample.html"
    return fixture_path.read_text(encoding="utf-8")


def test_wechat_can_handle():
    """can_handle should recognize Wechat URLs."""
    assert WechatProcessor.can_handle("https://mp.weixin.qq.com/s/test")
    assert not WechatProcessor.can_handle("https://example.com")


@pytest.mark.asyncio
async def test_wechat_process(wechat_html: str):
    """Processor should extract Wechat metadata and content."""
    processor = WechatProcessor()

    with patch.object(processor, "_fetch_html", new=AsyncMock(return_value=wechat_html)):
        entry = await processor.process("https://mp.weixin.qq.com/s/test")

    assert entry.title == "Wechat Sample Title"
    assert entry.source_type == "wechat"
    assert entry.source_url == "https://mp.weixin.qq.com/s/test"
    assert "Wechat main content paragraph." in entry.content

    metadata = getattr(entry, "metadata", {})
    assert metadata.get("author") == "Wechat Author"
    assert metadata.get("published_time") == "2026-02-14 12:00:00"
    assert entry.published_at == "2026-02-14 12:00:00"


@pytest.mark.asyncio
async def test_wechat_process_without_publish_time_keeps_published_at_empty():
    processor = WechatProcessor()
    html = """
    <html>
      <head><title>Wechat No Time</title></head>
      <body>
        <div id="img-content">
          <p>Wechat main content paragraph.</p>
        </div>
      </body>
    </html>
    """

    with (
        patch.object(processor, "_fetch_html", new=AsyncMock(return_value=html)),
        patch.object(processor, "_download_images", new=AsyncMock(return_value=None)),
    ):
        entry = await processor.process("https://mp.weixin.qq.com/s/no-time")

    metadata = getattr(entry, "metadata", {})
    assert "published_time" not in metadata
    assert entry.published_at is None
