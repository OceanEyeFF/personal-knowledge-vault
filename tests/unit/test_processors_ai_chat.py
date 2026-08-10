"""
AI chat processor unit tests.
"""

import sys
from pathlib import Path

# Add project root to Python path
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from unittest.mock import Mock, patch

import pytest

from src.processors.ai_chat_processor import AIChatMessage, AIChatProcessor


@pytest.fixture
def chatgpt_html_path() -> Path:
    """Path to ChatGPT HTML fixture."""
    return Path(__file__).parent.parent / "fixtures" / "ai_chat" / "chatgpt_export.html"


@pytest.fixture
def chatgpt_md_path() -> Path:
    """Path to ChatGPT Markdown fixture."""
    return Path(__file__).parent.parent / "fixtures" / "ai_chat" / "chatgpt_export.md"


@pytest.fixture
def deepseek_html_path() -> Path:
    """Path to DeepSeek HTML fixture."""
    return Path(__file__).parent.parent / "fixtures" / "ai_chat" / "deepseek_export.html"


@pytest.fixture
def deepseek_md_path() -> Path:
    """Path to DeepSeek Markdown fixture."""
    return Path(__file__).parent.parent / "fixtures" / "ai_chat" / "deepseek_export.md"


def _mock_deepseek():
    mock_client = Mock()
    mock_client.summarize.return_value = "Mock summary"
    mock_client.extract_tags.return_value = ["tag1", "tag2", "tag3"]
    return mock_client


def test_ai_chat_can_handle_file(
    chatgpt_html_path: Path,
    chatgpt_md_path: Path,
    deepseek_html_path: Path,
    deepseek_md_path: Path,
):
    """can_handle classifies content and never dereferences path-shaped input."""
    paths = (
        chatgpt_html_path,
        chatgpt_md_path,
        deepseek_html_path,
        deepseek_md_path,
    )
    assert all(not AIChatProcessor.can_handle(str(path)) for path in paths)
    assert all(
        AIChatProcessor.can_handle(path.read_text(encoding="utf-8"))
        for path in paths
    )
    assert not AIChatProcessor.can_handle("https://example.com")
    assert not AIChatProcessor.can_handle("HTTPS://EXAMPLE.COM/export.json")


def test_ai_chat_can_handle_text():
    """can_handle should recognize raw AI chat text."""
    chatgpt_text = "**You**: Hello\n**ChatGPT**: Hi"
    deepseek_text = "### 用户\nHello\n### DeepSeek\nHi"

    assert AIChatProcessor.can_handle(chatgpt_text)
    assert AIChatProcessor.can_handle(deepseek_text)


@pytest.mark.asyncio
async def test_ai_chat_process_text_never_probes_local_filesystem():
    raw = "**You**: Hello\n**ChatGPT**: Hi"
    processor = AIChatProcessor(deepseek_client=_mock_deepseek())
    with (
        patch.object(
            Path,
            "exists",
            side_effect=AssertionError("implicit path probe"),
        ) as exists,
        patch(
            "src.processors.ai_chat_processor.read_local_text_file",
            side_effect=AssertionError("implicit file read"),
        ) as reader,
    ):
        entry = await processor.process_text(raw)

    exists.assert_not_called()
    reader.assert_not_called()
    assert entry.source_url is None
    assert "Hello" in entry.content


def test_ai_chat_can_handle_edge_cases(tmp_path: Path):
    """can_handle rejects blanks without probing a path-shaped raw value."""
    assert not AIChatProcessor.can_handle("   ")

    sample_path = tmp_path / "sample.html"
    sample_path.write_text("<div data-turn='user'>Hi</div>", encoding="utf-8")

    with (
        patch.object(
            Path,
            "exists",
            side_effect=AssertionError("classification path probe"),
        ) as exists,
        patch(
            "src.processors.ai_chat_processor.read_local_text_file",
            side_effect=AssertionError("classification file read"),
        ) as reader,
    ):
        assert not AIChatProcessor.can_handle(str(sample_path))

    exists.assert_not_called()
    reader.assert_not_called()


@pytest.mark.asyncio
async def test_ai_chat_process_chatgpt_html(chatgpt_html_path: Path):
    """Processor should parse ChatGPT HTML exports."""
    processor = AIChatProcessor()

    with patch("src.processors.ai_chat_processor.DeepSeekClient") as mock_client_cls:
        mock_client_cls.return_value = _mock_deepseek()
        entry = await processor.process_file(chatgpt_html_path)

    assert entry.title == "AI 对话 - Project Planning"
    assert entry.source_type == "ai_chat"
    assert entry.source_url is None
    assert entry.summary_100_words == "Mock summary"
    assert entry.tags == ["tag1", "tag2", "tag3"]
    assert "## 对话摘要" in entry.content
    assert "## 对话内容" in entry.content
    assert "**You**:" in entry.content
    assert "**ChatGPT**:" in entry.content
    assert "draft a plan" in entry.content

    metadata = getattr(entry, "metadata", {})
    assert metadata.get("ai_platform") == "ChatGPT"
    assert metadata.get("format") == "html"
    assert metadata.get("message_count") == 3
    assert metadata.get("participants") == ["ChatGPT", "You"]
    assert metadata.get("source_url") is None


@pytest.mark.asyncio
async def test_ai_chat_process_chatgpt_md(chatgpt_md_path: Path):
    """Processor should parse ChatGPT Markdown exports."""
    processor = AIChatProcessor()

    with patch("src.processors.ai_chat_processor.DeepSeekClient") as mock_client_cls:
        mock_client_cls.return_value = _mock_deepseek()
        entry = await processor.process_file(chatgpt_md_path)

    assert entry.title == "AI 对话 - Summarize the plan"
    assert entry.source_type == "ai_chat"
    assert entry.source_url is None
    assert entry.summary_100_words == "Mock summary"
    assert "Summarize the plan" in entry.content
    assert "Focus on tests and delivery" in entry.content

    metadata = getattr(entry, "metadata", {})
    assert metadata.get("ai_platform") == "ChatGPT"
    assert metadata.get("format") == "markdown"
    assert metadata.get("message_count") == 2
    assert metadata.get("participants") == ["ChatGPT", "You"]


@pytest.mark.asyncio
async def test_ai_chat_process_deepseek_html(deepseek_html_path: Path):
    """Processor should parse DeepSeek HTML exports."""
    processor = AIChatProcessor()

    with patch("src.processors.ai_chat_processor.DeepSeekClient") as mock_client_cls:
        mock_client_cls.return_value = _mock_deepseek()
        entry = await processor.process_file(deepseek_html_path)

    assert entry.title == "AI 对话 - DeepSeek Session"
    assert entry.source_type == "ai_chat"
    assert entry.source_url is None
    assert entry.summary_100_words == "Mock summary"
    assert "**用户**:" in entry.content
    assert "**DeepSeek AI**:" in entry.content
    assert "Checklist item one" in entry.content

    metadata = getattr(entry, "metadata", {})
    assert metadata.get("ai_platform") == "DeepSeek"
    assert metadata.get("format") == "html"
    assert metadata.get("message_count") == 2
    assert metadata.get("participants") == ["DeepSeek AI", "用户"]


@pytest.mark.asyncio
async def test_ai_chat_process_deepseek_md(deepseek_md_path: Path):
    """Processor should parse DeepSeek Markdown exports."""
    processor = AIChatProcessor()

    with patch("src.processors.ai_chat_processor.DeepSeekClient") as mock_client_cls:
        mock_client_cls.return_value = _mock_deepseek()
        entry = await processor.process_file(deepseek_md_path)

    assert entry.title == "AI 对话 - Outline the checklist"
    assert entry.source_type == "ai_chat"
    assert entry.source_url is None
    assert entry.summary_100_words == "Mock summary"
    assert "Outline the checklist" in entry.content
    assert "verify coverage" in entry.content

    metadata = getattr(entry, "metadata", {})
    assert metadata.get("ai_platform") == "DeepSeek"
    assert metadata.get("format") == "markdown"
    assert metadata.get("message_count") == 2
    assert metadata.get("participants") == ["DeepSeek AI", "用户"]


@pytest.mark.asyncio
async def test_ai_chat_load_content_errors():
    """_load_content should raise for empty input or missing files."""
    processor = AIChatProcessor()

    with pytest.raises(ValueError, match="Input cannot be empty"):
        await processor._load_content("   ")

    with pytest.raises(FileNotFoundError):
        await processor._load_content("missing_export.html", allow_local_file=True)

    with pytest.raises(FileNotFoundError):
        await processor.process_file("missing-export-without-known-suffix")


def test_ai_chat_helper_methods():
    """Helper methods should handle edge cases."""
    processor = AIChatProcessor()

    assert not AIChatProcessor._looks_like_ai_chat("plain notes")
    assert processor._normalize_role("") == ""
    assert processor._normalize_role("system") == ""
    assert processor._normalize_deepseek_role("") == ""
    assert processor._normalize_deepseek_role("主持人") == ""
    assert processor._role_from_class(["system"]) == ""

    with pytest.raises(ValueError):
        processor._detect_format("just text")

    assert processor._parse_messages("text", "unknown", "markdown") == []
    assert processor._shorten_title("A" * 40, max_length=10) == "A" * 10

    assistant_msg = AIChatMessage(role="assistant", content="Assistant note")
    assert processor._infer_topic([assistant_msg], "content", "markdown") == "Assistant note"
    assert processor._infer_topic([], "content", "markdown") == ""


def test_ai_chat_parse_chatgpt_html_variants():
    """ChatGPT HTML parsing should handle data-turn and class fallbacks."""
    processor = AIChatProcessor()
    html_turn = """
    <html><body>
      <script>var x = 1;</script>
      <div data-turn="user"><div class="markdown"><p>User one</p></div></div>
      <div data-turn="system"><div class="markdown"><p>Skip me</p></div></div>
    </body></html>
    """

    messages_turn = processor._parse_chatgpt_html(html_turn)

    assert [msg.role for msg in messages_turn] == ["user"]
    assert "one" in messages_turn[0].content

    html_class = """
    <html><body>
      <div data-message-author-role="" data-turn="" class="assistant">
        <div class="markdown"><p>Assistant via class</p></div>
      </div>
      <div data-message-author-role=""></div>
    </body></html>
    """

    messages_class = processor._parse_chatgpt_html(html_class)

    assert [msg.role for msg in messages_class] == ["assistant"]
    assert "via class" in messages_class[0].content


@pytest.mark.asyncio
async def test_ai_chat_process_empty_fallback(tmp_path: Path):
    """Fallback handling should derive topic from file stem and keep empty content."""
    html_path = tmp_path / "empty_chat.html"
    html_path.write_text("<html><body><div data-turn='system'></div></body></html>", encoding="utf-8")

    processor = AIChatProcessor()
    entry = await processor.process_file(html_path)

    assert entry.title == "AI 对话 - empty_chat"
    assert entry.summary_100_words == "对话内容为空。"
    assert "**You**:\n>" in entry.content


def test_ai_chat_generate_summary_fallback_and_tags():
    """Summary generation should fall back when DeepSeek fails."""
    processor = AIChatProcessor()
    long_text = "User: " + ("x" * 400)

    with patch("src.processors.ai_chat_processor.DeepSeekClient") as mock_client_cls:
        mock_client = Mock()
        mock_client.summarize.side_effect = RuntimeError("boom")
        mock_client.extract_tags.side_effect = RuntimeError("boom")
        mock_client_cls.return_value = mock_client
        summary, tags = processor._generate_summary_and_tags(long_text)

    assert summary.endswith("...")
    assert "User" in tags
    assert "chat" in tags
