"""
Unit tests for SQLiteStore query methods.
"""

import sys
from pathlib import Path

# Add project root to Python path
PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest

from src.storage.sqlite_store import SQLiteStore
from src.storage.markdown_store import Entry


@pytest.fixture
def store(tmp_path: Path) -> SQLiteStore:
    """Create a fresh SQLiteStore for testing."""
    db_path = tmp_path / "test.db"
    store = SQLiteStore(db_path)
    store.initialize()
    return store


@pytest.fixture
def store_with_data(store: SQLiteStore, tmp_path: Path) -> SQLiteStore:
    """Create a store with test data (3 entries with different source types and tags)."""
    entries = [
        Entry(
            title="微信文章1",
            source_type="wechat",
            source_url="https://mp.weixin.qq.com/s/article1",
            tags=["AI", "ML"],
            keywords=["artificial intelligence"],
            abstract="测试微信文章",
            content="# 微信文章1\n\n这是测试内容",
            word_count=100,
        ),
        Entry(
            title="知乎回答1",
            source_type="zhihu",
            source_url="https://www.zhihu.com/answer/123",
            tags=["AI", "NLP"],
            keywords=["natural language"],
            abstract="测试知乎回答",
            content="# 知乎回答1\n\n这是测试内容",
            word_count=200,
        ),
        Entry(
            title="通用网页1",
            source_type="generic",
            source_url="https://example.com/article",
            tags=["Python"],
            keywords=["programming"],
            abstract="测试通用网页",
            content="# 通用网页1\n\n这是测试内容",
            word_count=150,
        ),
    ]
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    for entry in entries:
        md_path = vault_dir / f"{entry.source_type}" / "test.md"
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text(
            f"---\ntitle: {entry.title}\n---\n{entry.content}",
            encoding="utf-8",
        )
        store.insert_entry(entry, str(md_path))
    return store


def test_query_by_url_found(store_with_data: SQLiteStore) -> None:
    """query_by_url should return a matching entry."""
    result = store_with_data.query_by_url("https://mp.weixin.qq.com/s/article1")

    assert result is not None
    assert result["title"] == "微信文章1"


def test_query_by_url_not_found(store_with_data: SQLiteStore) -> None:
    """query_by_url should return None when no match exists."""
    result = store_with_data.query_by_url("https://example.com/missing")

    assert result is None


def test_list_entries_default(store_with_data: SQLiteStore) -> None:
    """list_entries should return all entries with default params."""
    rows = store_with_data.list_entries()

    assert len(rows) == 3
    assert {row["title"] for row in rows} == {"微信文章1", "知乎回答1", "通用网页1"}


def test_list_entries_with_source_type_filter(store_with_data: SQLiteStore) -> None:
    """list_entries should filter by source_type."""
    rows = store_with_data.list_entries(source_type="wechat")

    assert len(rows) == 1
    assert rows[0]["source_type"] == "wechat"
    assert rows[0]["title"] == "微信文章1"


def test_list_entries_with_tag_filter(store_with_data: SQLiteStore) -> None:
    """list_entries should filter by tag."""
    rows = store_with_data.list_entries(tag="AI")

    assert len(rows) == 2
    assert {row["title"] for row in rows} == {"微信文章1", "知乎回答1"}


def test_list_entries_sort_by_title_asc(store_with_data: SQLiteStore) -> None:
    """list_entries should sort by title ascending."""
    rows = store_with_data.list_entries(sort_by="title", sort_order="asc")
    titles = [row["title"] for row in rows]

    assert titles == sorted(["微信文章1", "知乎回答1", "通用网页1"])


def test_list_entries_invalid_sort_by(store_with_data: SQLiteStore) -> None:
    """list_entries should raise ValueError for invalid sort_by."""
    with pytest.raises(ValueError):
        store_with_data.list_entries(sort_by="invalid_column")


def test_list_entries_invalid_sort_order(store_with_data: SQLiteStore) -> None:
    """list_entries should raise ValueError for invalid sort_order."""
    with pytest.raises(ValueError):
        store_with_data.list_entries(sort_order="invalid")


def test_count_entries_total(store_with_data: SQLiteStore) -> None:
    """count_entries should return total entries."""
    assert store_with_data.count_entries() == 3


def test_count_entries_with_filter(store_with_data: SQLiteStore) -> None:
    """count_entries should support source_type filtering."""
    assert store_with_data.count_entries(source_type="wechat") == 1


def test_count_entries_by_source_type(store_with_data: SQLiteStore) -> None:
    """count_entries_by_source_type should return counts per source type."""
    rows = store_with_data.count_entries_by_source_type()
    counts = {source_type: count for source_type, count in rows}

    assert counts == {"wechat": 1, "zhihu": 1, "generic": 1}


def test_get_all_tags_with_count(store_with_data: SQLiteStore) -> None:
    """get_all_tags_with_count should return tag counts ordered by count desc."""
    rows = store_with_data.get_all_tags_with_count()
    counts = {row["name"]: row["count"] for row in rows}
    count_values = [row["count"] for row in rows]

    assert counts["AI"] == 2
    assert counts["ML"] == 1
    assert counts["NLP"] == 1
    assert counts["Python"] == 1
    assert count_values == sorted(count_values, reverse=True)


def test_get_all_tags_with_count_limit(store_with_data: SQLiteStore) -> None:
    """get_all_tags_with_count should respect limit."""
    rows = store_with_data.get_all_tags_with_count(limit=2)

    assert len(rows) == 2
    assert rows[0]["name"] == "AI"
    assert rows[0]["count"] == 2


def test_get_statistics(store_with_data: SQLiteStore) -> None:
    """get_statistics should return expected summary keys."""
    stats = store_with_data.get_statistics()

    assert stats["total_entries"] == 3
    assert "by_source_type" in stats
    assert "top_tags" in stats

    counts = {source_type: count for source_type, count in stats["by_source_type"]}
    assert counts == {"wechat": 1, "zhihu": 1, "generic": 1}


def test_chunk_crud_roundtrip(store: SQLiteStore, tmp_path: Path) -> None:
    """chunk CRUD should support insert, query, count and delete."""
    entry = Entry(
        title="Chunked Entry",
        source_type="generic",
        source_url="https://example.com/chunked",
        tags=["chunk"],
        keywords=["chunk"],
        content="chunk content",
    )
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    md_path = vault_dir / "chunked.md"
    md_path.write_text("# Chunked Entry\n\nchunk content", encoding="utf-8")
    knowledge_id = store.insert_entry(entry, str(md_path))

    inserted_count = store.insert_chunks(knowledge_id, ["chunk a", "chunk b"])

    assert inserted_count == 2
    assert store.count_chunks(knowledge_id) == 2

    chunks = store.get_chunks_by_knowledge_id(knowledge_id)
    assert [chunk["chunk_index"] for chunk in chunks] == [0, 1]
    assert [chunk["chunk_text"] for chunk in chunks] == ["chunk a", "chunk b"]

    chunk = store.get_chunk_by_index(knowledge_id, 1)
    assert chunk is not None
    assert chunk["chunk_text"] == "chunk b"

    deleted_count = store.delete_chunks_by_knowledge_id(knowledge_id)
    assert deleted_count == 2
    assert store.count_chunks(knowledge_id) == 0
