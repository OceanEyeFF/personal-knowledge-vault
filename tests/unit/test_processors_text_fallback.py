"""
Text fallback processor unit tests.
"""

import sys
from pathlib import Path

# Add project root to Python path
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from unittest.mock import MagicMock, Mock, patch

import pytest

from src.processors.text_fallback_processor import DialogueMessage, TextFallbackProcessor
from src.processors.local_file_reader import read_local_text_file
from src.runtime.errors import PKVRuntimeError


def _mock_deepseek():
    mock_client = Mock()
    mock_client.summarize.return_value = "Mock summary"
    mock_client.extract_tags.return_value = ["tag1", "tag2", "tag3"]
    return mock_client


def test_text_fallback_can_handle():
    """can_handle should accept non-URL text inputs."""
    assert TextFallbackProcessor.can_handle("Just some notes")
    assert TextFallbackProcessor.can_handle("notes.txt")
    assert not TextFallbackProcessor.can_handle("https://example.com")
    assert not TextFallbackProcessor.can_handle("   ")


@pytest.mark.asyncio
async def test_text_fallback_process_dialogue():
    """Processor should parse dialogue-style text."""
    dialogue_text = """Alice: Hi
Bob: Hello
Alice: Did you finish the tests?
Bob: Yes, they passed.
"""
    processor = TextFallbackProcessor()

    with patch("src.processors.text_fallback_processor.DeepSeekClient") as mock_client_cls:
        mock_client_cls.return_value = _mock_deepseek()
        entry = await processor.process(dialogue_text)

    assert entry.title == "对话记录 - Alice、Bob"
    assert entry.source_type == "text"  # 实现已统一使用 "text"，测试同步更新
    assert entry.source_url is None
    assert entry.summary_100_words == "Mock summary"
    assert entry.tags == ["tag1", "tag2", "tag3"]
    assert "## 对话摘要" in entry.content
    assert "## 对话内容" in entry.content
    assert "**Alice**:" in entry.content
    assert "**Bob**:" in entry.content

    metadata = getattr(entry, "metadata", {})
    assert metadata.get("text_type") == "dialogue"
    assert metadata.get("message_count") == 4
    assert metadata.get("participants") == ["Alice", "Bob"]


@pytest.mark.asyncio
async def test_text_fallback_process_article():
    """Processor should parse article-style text."""
    article_text = """# Sample Article
This is the first paragraph about the release.
It mentions coverage expectations.
"""
    processor = TextFallbackProcessor()

    with patch("src.processors.text_fallback_processor.DeepSeekClient") as mock_client_cls:
        mock_client_cls.return_value = _mock_deepseek()
        entry = await processor.process(article_text)

    assert entry.title == "Sample Article"
    assert entry.source_type == "text"  # 实现已统一使用 "text"，测试同步更新
    assert entry.summary_100_words == "Mock summary"
    assert "first paragraph" in entry.content
    assert "coverage expectations" in entry.content

    metadata = getattr(entry, "metadata", {})
    assert metadata.get("text_type") == "article"
    assert metadata.get("message_count") == 0
    assert metadata.get("participants") == []


@pytest.mark.asyncio
async def test_text_fallback_can_use_deterministic_summary_without_constructing_ai_client():
    """R4 archive defers paid/network enrichment until after primary storage."""

    processor = TextFallbackProcessor(allow_ai_enrichment=False)
    with patch(
        "src.processors.text_fallback_processor.DeepSeekClient",
        side_effect=AssertionError("AI client must not be constructed"),
    ):
        entry = await processor.process("# Local title\nLocal body")

    assert entry.title == "Local title"
    assert entry.summary_100_words == "# Local title\nLocal body"
    assert entry.tags


@pytest.mark.asyncio
async def test_text_fallback_process_empty_input():
    """Blank input should return an empty entry."""
    processor = TextFallbackProcessor()
    entry = await processor.process("   ")

    assert entry.title == "未命名文本"
    assert entry.summary_100_words == "内容为空。"

    metadata = getattr(entry, "metadata", {})
    assert metadata.get("text_type") == "empty"


@pytest.mark.asyncio
async def test_text_fallback_path_string_is_raw_text_by_default(tmp_path: Path):
    """A path-shaped raw value must never trigger an implicit local file read."""
    file_path = tmp_path / "secret.txt"
    file_path.write_text("TOP_SECRET_FILE_CONTENT", encoding="utf-8")
    processor = TextFallbackProcessor(deepseek_client=_mock_deepseek())

    with (
        patch.object(Path, "exists", side_effect=AssertionError("unexpected path lookup")) as exists,
        patch.object(Path, "read_text", side_effect=AssertionError("unexpected file read")) as read_text,
    ):
        entry = await processor.process(str(file_path))

    exists.assert_not_called()
    read_text.assert_not_called()
    assert entry.content == str(file_path)
    assert entry.source_url is None
    assert "TOP_SECRET_FILE_CONTENT" not in entry.content


@pytest.mark.asyncio
async def test_text_fallback_process_text_never_reads_path(tmp_path: Path):
    """The explicit raw-text seam must preserve path-shaped text verbatim."""
    file_path = tmp_path / "raw-text-canary.txt"
    file_path.write_text("SHOULD_NOT_BE_READ", encoding="utf-8")
    processor = TextFallbackProcessor(deepseek_client=_mock_deepseek())

    with patch.object(Path, "read_text") as read_text:
        entry = await processor.process_text(str(file_path))

    read_text.assert_not_called()
    assert entry.content == str(file_path)
    assert entry.source_url is None


@pytest.mark.asyncio
async def test_text_fallback_process_file_requires_explicit_opt_in(tmp_path: Path):
    """Explicit file imports should retain the legacy file-loading capability."""
    file_path = tmp_path / "sample.txt"
    file_path.write_text("# Imported note\nImported body", encoding="utf-8")
    processor = TextFallbackProcessor(deepseek_client=_mock_deepseek())

    entry = await processor.process_file(file_path)

    assert entry.content == "# Imported note\nImported body"
    assert entry.source_url is None
    assert entry.metadata["source_url"] is None
    assert entry.title == "Imported note"
    assert str(tmp_path) not in repr(entry)


@pytest.mark.asyncio
async def test_text_fallback_empty_file_never_persists_local_path(tmp_path: Path):
    """Even an empty explicit import must not turn its path into provenance."""
    private_root = tmp_path / "PRIVATE-PATH-CANARY"
    private_root.mkdir()
    file_path = private_root / "empty-note.md"
    file_path.write_text("", encoding="utf-8")
    processor = TextFallbackProcessor(deepseek_client=_mock_deepseek())

    entry = await processor.process_file(file_path)

    assert entry.title == "empty-note"
    assert entry.source_url is None
    assert entry.metadata["source_url"] is None
    assert str(private_root) not in repr(entry)


def test_local_file_reader_uses_descriptor_verified_nofollow_open(tmp_path: Path):
    file_path = tmp_path / "note.md"
    handle = MagicMock()
    handle.read.return_value = "verified content"
    context = MagicMock()
    context.__enter__.return_value = handle
    context.__exit__.return_value = False

    with patch(
        "src.processors.local_file_reader.open_user_file_nofollow",
        return_value=context,
    ) as safe_open:
        content = read_local_text_file(file_path, errors="ignore")

    assert content == "verified content"
    safe_open.assert_called_once_with(
        file_path,
        "r",
        label="本地导入源文件",
        encoding="utf-8",
        errors="ignore",
    )


@pytest.mark.asyncio
async def test_text_fallback_process_file_rejects_symlink(tmp_path: Path):
    target = tmp_path / "target.md"
    target.write_text("# Target\nsecret", encoding="utf-8")
    link = tmp_path / "link.md"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink unavailable: {exc}")

    processor = TextFallbackProcessor(deepseek_client=_mock_deepseek())
    with pytest.raises(PKVRuntimeError):
        await processor.process_file(link)


@pytest.mark.asyncio
async def test_text_fallback_resolve_text_unicode_error(tmp_path: Path):
    """_resolve_text should retry with errors=ignore on decode failure."""
    file_path = tmp_path / "sample.txt"
    file_path.write_bytes(b"\xff\xfe")

    processor = TextFallbackProcessor()

    def read_text_side_effect(*args, **kwargs):
        if "errors" not in kwargs:
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "boom")
        return "Recovered text"

    with patch(
        "src.processors.text_fallback_processor.read_local_text_file",
        side_effect=read_text_side_effect,
    ):
        text, source = await processor._resolve_text(
            str(file_path),
            allow_local_file=True,
        )

    assert text == "Recovered text"
    assert source == file_path


def test_text_fallback_detect_text_type_variants():
    """_detect_text_type should cover article and dialogue branches."""
    processor = TextFallbackProcessor()

    assert processor._detect_text_type("") == "article"
    assert (
        processor._detect_text_type("Alice: hi\nBob: hi\nCarol: hi") == "article"
    )
    assert (
        processor._detect_text_type("Alice: a\nAlice: b\nAlice: c") == "dialogue"
    )
    assert (
        processor._detect_text_type("Alice: hi\nAlice: ok") == "dialogue"
    )


def test_text_fallback_parse_dialogue_edges():
    """Dialogue parsing should handle blank lines and unknown speakers."""
    processor = TextFallbackProcessor()
    processor._parse_dialogue("Alice: Hi\n\nBob: Hello")

    messages_unknown = processor._parse_dialogue("Just a narrative line")
    assert messages_unknown[0].speaker == "Unknown"
    assert processor._normalize_speaker("") == ""


def test_text_fallback_helper_methods(tmp_path: Path):
    """Helper methods should cover title extraction and tagging logic."""
    processor = TextFallbackProcessor()
    messages = [
        DialogueMessage(speaker="Alice", message=""),
        DialogueMessage(speaker="Bob", message="Hello"),
    ]

    conversation_text = processor._build_conversation_text(messages)
    assert conversation_text == "Bob: Hello"

    title = processor._build_dialogue_title(tmp_path / "sample.txt", messages)
    assert "sample" in title

    article_title = processor._extract_article_title(
        "\nAlice: Hi\nBody",
        tmp_path / "fallback.txt",
    )
    assert article_title == "fallback"

    markdown = processor._build_dialogue_markdown(
        "Test",
        "Summary",
        [DialogueMessage(speaker="Alice", message="")],
    )
    assert "**Alice**:\n>" in markdown

    assert processor._dedupe_tags(["a", " ", "a", "b", "b"]) == ["a", "b"]
    assert processor._extract_keywords("Alpha beta beta gamma", limit=2)[0] == "beta"
    assert processor._extract_keywords("123 !!!") == []


def test_text_fallback_generate_summary_fallbacks():
    """Summary generation should fall back for empty text or DeepSeek errors."""
    processor = TextFallbackProcessor()

    summary, tags = processor._generate_summary_and_tags("", "article", [])
    assert summary == "内容为空。"
    assert "empty" in tags

    long_text = "Alice: " + ("x" * 250)
    messages = [DialogueMessage(speaker="Alice", message="Hello")]

    with patch("src.processors.text_fallback_processor.DeepSeekClient") as mock_client_cls:
        mock_client = Mock()
        mock_client.summarize.side_effect = RuntimeError("boom")
        mock_client.extract_tags.side_effect = RuntimeError("boom")
        mock_client_cls.return_value = mock_client
        summary, tags = processor._generate_summary_and_tags(long_text, "dialogue", messages)

    assert "对话参与者" in summary
    assert summary.endswith("...")
    assert "dialogue" in tags
    assert "conversation" in tags

    article_tags = processor._fallback_tags("Alpha beta gamma", "article", [])
    assert "text" in article_tags


@pytest.mark.asyncio
async def test_text_fallback_summary_truncation():
    """summary_one_sentence should be truncated to 50 chars."""
    processor = TextFallbackProcessor()
    dialogue_text = "Alice: Hello\nBob: Hi"
    long_summary = "S" * 80

    with patch("src.processors.text_fallback_processor.DeepSeekClient") as mock_client_cls:
        mock_client = Mock()
        mock_client.summarize.return_value = long_summary
        mock_client.extract_tags.return_value = ["tag1"]
        mock_client_cls.return_value = mock_client
        entry = await processor.process(dialogue_text)

    assert entry.summary_one_sentence == long_summary[:50]
