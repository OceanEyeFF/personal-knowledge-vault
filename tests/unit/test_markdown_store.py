"""Direct lifecycle contracts for the Markdown primary store."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pytest

from src.storage.markdown_store import Entry, MarkdownStore


def test_markdown_store_round_trips_metadata_and_unicode(tmp_path: Path) -> None:
    store = MarkdownStore(tmp_path / "vault")
    entry = Entry(
        title='测试：AI / "知识" 工作流',
        source_type="ai_chat",
        source_url="https://example.test/source?id=7",
        event_time=date(2026, 7, 29),
        published_at=datetime(2026, 7, 29, 8, 9, 10),
        archived_at=["", "2026-07-30 11:12:13"],
        tags=["AI", "知识管理", "含 空格"],
        keywords=["agent", "证据"],
        abstract="一段摘要",
        summary_one_sentence="一句话摘要。",
        summary_100_words="较长摘要。",
        search_strategy="hybrid",
        related_docs=["pkv://entries/1", "pkv://entries/2"],
        reading_status="reviewed",
        rating=5,
        notes="人工备注\n第二行",
        content="# 标题\n\n正文包含 Unicode：你好，世界 🌏\n",
    )

    saved_path = store.save(entry)
    loaded = store.load(saved_path)

    assert saved_path.parent == store.vault_dir / "ai_chat"
    assert saved_path.suffix == ".md"
    assert saved_path.is_file()
    assert loaded.to_dict() == entry.to_dict()
    assert loaded.content == entry.content.rstrip("\n")


def test_duplicate_title_preserves_both_files(tmp_path: Path) -> None:
    store = MarkdownStore(tmp_path / "vault")
    first_entry = Entry(
        title="同名条目",
        source_type="text",
        archived_at="2026-07-30 10:00:00",
        content="first",
    )
    second_entry = Entry(
        title="同名条目",
        source_type="text",
        archived_at="2026-07-30 10:00:01",
        content="second",
    )

    first_path = store.save(first_entry)
    second_path = store.save(second_entry)

    assert first_path != second_path
    assert first_path.is_file()
    assert second_path.is_file()
    assert store.load(first_path).content == "first"
    assert store.load(second_path).content == "second"


def test_list_all_filters_by_subdirectory(tmp_path: Path) -> None:
    store = MarkdownStore(tmp_path / "vault")
    wechat_path = store.save(
        Entry(title="微信", source_type="wechat", content="wechat")
    )
    text_path = store.save(
        Entry(title="文本", source_type="text", content="text")
    )
    ignored = store.vault_dir / "text" / "ignored.txt"
    ignored.write_text("not markdown", encoding="utf-8")

    assert set(store.list_all()) == {wechat_path, text_path}
    assert store.list_all("wechat") == [wechat_path]
    assert store.list_all("missing") == []


def test_delete_is_idempotent_and_load_missing_is_explicit(tmp_path: Path) -> None:
    store = MarkdownStore(tmp_path / "vault")
    saved_path = store.save(
        Entry(title="待删除", source_type="text", content="content")
    )

    store.delete(saved_path)
    store.delete(saved_path)

    assert not saved_path.exists()
    with pytest.raises(FileNotFoundError, match="文件不存在"):
        store.load(saved_path)


def test_load_minimal_frontmatter_uses_documented_defaults(
    tmp_path: Path,
) -> None:
    store = MarkdownStore(tmp_path / "vault")
    source = store.vault_dir / "minimal.md"
    source.write_text(
        "---\n"
        "title: 最小条目\n"
        "---\n"
        "正文\n",
        encoding="utf-8",
    )

    loaded = store.load(source)

    assert loaded.title == "最小条目"
    assert loaded.source_type == "personal"
    assert loaded.tags == []
    assert loaded.keywords == []
    assert loaded.content == "正文"
