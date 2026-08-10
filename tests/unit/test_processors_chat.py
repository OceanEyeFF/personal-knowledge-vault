"""
Chat processor unit tests.
"""

import sys
from pathlib import Path

# Add project root to Python path
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from unittest.mock import Mock, patch

import pytest

from src.processors.chat_processor import ChatProcessor


@pytest.fixture
def chat_text_path() -> Path:
    """Path to text chat fixture."""
    return Path(__file__).parent.parent / "fixtures" / "chat_sample.txt"


@pytest.fixture
def chat_json_path() -> Path:
    """Path to JSON chat fixture."""
    return Path(__file__).parent.parent / "fixtures" / "chat_sample.json"


def _mock_deepseek():
    mock_client = Mock()
    mock_client.summarize.return_value = "Mock summary"
    mock_client.extract_tags.return_value = ["tag1", "tag2", "tag3"]
    return mock_client


def test_chat_can_handle():
    """can_handle should recognize supported chat file types."""
    assert ChatProcessor.can_handle("sample.txt")
    assert ChatProcessor.can_handle("sample.json")
    assert not ChatProcessor.can_handle("https://example.com")


@pytest.mark.asyncio
async def test_chat_process_default_never_reads_local_file(
    chat_text_path: Path,
):
    processor = ChatProcessor(deepseek_client=_mock_deepseek())
    with patch(
        "src.processors.chat_processor.read_local_text_file",
        side_effect=AssertionError("implicit local read"),
    ) as reader:
        with pytest.raises(ValueError, match="process_file"):
            await processor.process(str(chat_text_path))
    reader.assert_not_called()


@pytest.mark.asyncio
async def test_chat_process_text(chat_text_path: Path):
    """Processor should parse text chat and build Markdown transcript."""
    processor = ChatProcessor()

    with patch("src.processors.chat_processor.DeepSeekClient") as mock_client_cls:
        mock_client_cls.return_value = _mock_deepseek()
        entry = await processor.process_file(chat_text_path)

    assert entry.title == "聊天记录 - chat_sample"
    assert entry.source_type == "chat"
    assert entry.source_url is None
    assert entry.metadata["source_url"] is None
    assert entry.summary_100_words == "Mock summary"
    assert entry.tags == ["tag1", "tag2", "tag3"]
    assert "## 对话摘要" in entry.content
    assert "## 对话内容" in entry.content
    assert "**Alice** (2026-01-01 10:00):" in entry.content
    assert "> Hi Alice." in entry.content
    assert "> Let's discuss the plan." in entry.content

    metadata = getattr(entry, "metadata", {})
    assert metadata.get("message_count") == 3
    assert "Alice" in metadata.get("participants", [])


@pytest.mark.asyncio
async def test_chat_process_json(chat_json_path: Path):
    """Processor should parse JSON chat logs."""
    processor = ChatProcessor()

    with patch("src.processors.chat_processor.DeepSeekClient") as mock_client_cls:
        mock_client_cls.return_value = _mock_deepseek()
        entry = await processor.process_file(chat_json_path)

    assert entry.title == "聊天记录 - chat_sample"
    assert "JSON message one." in entry.content
    assert "JSON message two." in entry.content
