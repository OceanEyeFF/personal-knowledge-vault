"""
Unit tests for SQLiteStore query methods.
"""

from contextlib import contextmanager
import sqlite3
import sys
from pathlib import Path

# Add project root to Python path
PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest

from src.storage.sqlite_store import CURRENT_FTS_TRIGGER_NAMES, FTS_TABLE_NAME, LEGACY_FTS_TABLE_NAME, SQLiteStore
from src.storage.markdown_store import Entry

MIGRATIONS_DIR = PROJECT_ROOT / "scripts" / "migrations"


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
    chunk_by_id = store.get_chunk_by_id(chunk["chunk_id"])
    assert chunk_by_id == chunk

    deleted_count = store.delete_chunks_by_knowledge_id(knowledge_id)
    assert deleted_count == 2
    assert store.count_chunks(knowledge_id) == 0


def test_insert_entry_preserves_timeline_time_fields(
    store: SQLiteStore,
    tmp_path: Path,
) -> None:
    """insert_entry should persist event_time/published_at for timeline rebuilding."""
    entry = Entry(
        title="Timeline Entry",
        source_type="generic",
        source_url="https://example.com/timeline",
        event_time="2026-03-01 08:00:00",
        published_at="2026-03-02 09:30:00",
        archived_at="2026-03-10 10:00:00",
        tags=["timeline"],
        keywords=["timeline"],
        content="timeline content",
    )
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    md_path = vault_dir / "timeline.md"
    md_path.write_text("# Timeline Entry\n\ntimeline content", encoding="utf-8")

    knowledge_id = store.insert_entry(entry, str(md_path))
    row = store.query_by_id(knowledge_id)

    assert row is not None
    assert row["event_time"] == "2026-03-01 08:00:00"
    assert row["published_at"] == "2026-03-02 09:30:00"
    assert row["archived_at"] == "2026-03-10 10:00:00"


def _ensure_chat_sessions_schema(store: SQLiteStore) -> None:
    sql = (MIGRATIONS_DIR / "004_add_chat_sessions.sql").read_text(encoding="utf-8")
    with store.get_connection() as conn:
        conn.executescript(sql)


def test_get_connection_rolls_back_on_exception(store: SQLiteStore) -> None:
    with pytest.raises(RuntimeError):
        with store.get_connection() as conn:
            conn.execute("INSERT INTO tags (name, count) VALUES ('rolled-back', 1)")
            raise RuntimeError("boom")

    with store.get_connection() as conn:
        row = conn.execute(
            "SELECT name FROM tags WHERE name = 'rolled-back'"
        ).fetchone()

    assert row is None


def test_internal_schema_helpers_upgrade_legacy_fts_contract(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy.db"
    with sqlite3.connect(str(db_path)) as conn:
        conn.executescript(
            f"""
            CREATE TABLE knowledge_items (
                knowledge_id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                archived_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE VIRTUAL TABLE {LEGACY_FTS_TABLE_NAME} USING fts5(title);
            CREATE TRIGGER knowledge_fts_insert AFTER INSERT ON knowledge_items BEGIN
                INSERT INTO {LEGACY_FTS_TABLE_NAME}(rowid, title) VALUES (new.knowledge_id, new.title);
            END;
            CREATE TRIGGER knowledge_fts_update AFTER UPDATE ON knowledge_items BEGIN
                INSERT INTO {LEGACY_FTS_TABLE_NAME}({LEGACY_FTS_TABLE_NAME}, rowid, title)
                VALUES ('delete', old.knowledge_id, old.title);
            END;
            CREATE TRIGGER knowledge_fts_delete AFTER DELETE ON knowledge_items BEGIN
                INSERT INTO {LEGACY_FTS_TABLE_NAME}({LEGACY_FTS_TABLE_NAME}, rowid, title)
                VALUES ('delete', old.knowledge_id, old.title);
            END;
            CREATE VIRTUAL TABLE {FTS_TABLE_NAME} USING fts5(
                title,
                content=knowledge_items,
                content_rowid=knowledge_id
            );
            """
        )

    store = SQLiteStore(db_path)
    with store.get_connection() as conn:
        assert SQLiteStore._sqlite_object_exists(conn, "table", LEGACY_FTS_TABLE_NAME) is True
        assert SQLiteStore._fts_uses_external_content(conn, FTS_TABLE_NAME) is True
        assert SQLiteStore._sqlite_object_sql(conn, "trigger", "missing_trigger") == ""

        store._ensure_timeline_time_columns(conn)
        needs_rebuild = store._ensure_fts5_contract(conn)

    assert needs_rebuild is True

    with store.get_connection() as conn:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(knowledge_items)")}
        assert {"event_time", "published_at"}.issubset(columns)
        assert SQLiteStore._sqlite_object_exists(conn, "table", LEGACY_FTS_TABLE_NAME) is False
        assert SQLiteStore._sqlite_object_exists(conn, "table", FTS_TABLE_NAME) is True
        assert SQLiteStore._fts_uses_external_content(conn, FTS_TABLE_NAME) is False
        for trigger_name in CURRENT_FTS_TRIGGER_NAMES:
            assert SQLiteStore._sqlite_object_exists(conn, "trigger", trigger_name) is True


def test_public_rebuild_fts5_index_handles_empty_and_existing_rows(
    store: SQLiteStore,
    tmp_path: Path,
) -> None:
    store.rebuild_fts5_index()
    with store.get_connection() as conn:
        empty_count = conn.execute(
            f"SELECT COUNT(*) FROM {FTS_TABLE_NAME}"
        ).fetchone()[0]

    entry = Entry(
        title="Rebuild",
        source_type="generic",
        source_url="https://example.com/rebuild",
        tags=["fts"],
        keywords=["fts"],
        content="rebuild content",
    )
    file_path = tmp_path / "rebuild.md"
    file_path.write_text("# Rebuild\n\ncontent", encoding="utf-8")
    knowledge_id = store.insert_entry(entry, str(file_path))

    store.rebuild_fts5_index()

    with store.get_connection() as conn:
        rebuilt_row = conn.execute(
            f"SELECT title FROM {FTS_TABLE_NAME} WHERE rowid = ?",
            (knowledge_id,),
        ).fetchone()

    assert empty_count == 0
    assert rebuilt_row is not None
    assert "rebuild" in rebuilt_row[0].lower()


def test_chunk_and_entry_validation_paths(store: SQLiteStore, tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        store.insert_chunks(0, ["chunk"])
    assert store.insert_chunks(1, []) == 0
    assert store.insert_chunks(1, ["   ", "\n"]) == 0

    with pytest.raises(ValueError):
        store.get_chunks_by_knowledge_id(0)
    with pytest.raises(ValueError):
        store.get_chunk_by_index(1, -1)
    with pytest.raises(ValueError):
        store.delete_chunks_by_knowledge_id(0)
    with pytest.raises(ValueError):
        store.count_chunks(0)

    assert store.query_by_id(9999) is None
    assert store.delete_entry(9999) is False
    assert store.table_exists("chat_sessions") is False

    entry = Entry(
        title="Delete Me",
        source_type="generic",
        source_url="https://example.com/delete-me",
        tags=["cleanup"],
        keywords=["cleanup"],
        content="cleanup content",
    )
    file_path = tmp_path / "delete.md"
    file_path.write_text("# Delete\n\ncleanup", encoding="utf-8")
    knowledge_id = store.insert_entry(entry, str(file_path))
    assert store.insert_chunks(knowledge_id, ["chunk"]) == 1

    assert store.delete_entry(knowledge_id) is True
    assert store.query_by_id(knowledge_id) is None

    with store.get_connection() as conn:
        tag_count = conn.execute("SELECT COUNT(*) FROM tags").fetchone()[0]
        fts_count = conn.execute(
            f"SELECT COUNT(*) FROM {FTS_TABLE_NAME} WHERE rowid = ?",
            (knowledge_id,),
        ).fetchone()[0]

    assert tag_count == 0
    assert fts_count == 0


@pytest.mark.parametrize(
    ("method_name", "args"),
    [
        ("query_by_url", ("https://example.com/fail",)),
        ("list_entries", ()),
        ("count_entries", ()),
        ("count_entries_by_source_type", ()),
        ("get_all_tags_with_count", ()),
        ("get_statistics", ()),
    ],
)
def test_query_wrappers_propagate_storage_errors(
    store: SQLiteStore,
    monkeypatch: pytest.MonkeyPatch,
    method_name: str,
    args: tuple[object, ...],
) -> None:
    @contextmanager
    def _broken_connection():
        raise RuntimeError("database unavailable")
        yield

    monkeypatch.setattr(store, "get_connection", _broken_connection)

    with pytest.raises(RuntimeError):
        getattr(store, method_name)(*args)


def test_chat_session_crud_and_stats_roundtrip(
    store: SQLiteStore,
    tmp_path: Path,
) -> None:
    _ensure_chat_sessions_schema(store)

    entry = Entry(
        title="Session Entry",
        source_type="generic",
        source_url="https://example.com/session-entry",
        tags=["chat"],
        keywords=["chat"],
        content="linked content",
    )
    file_path = tmp_path / "session.md"
    file_path.write_text("# Session Entry\n\nlinked content", encoding="utf-8")
    knowledge_id = store.insert_entry(entry, str(file_path))

    store.create_session("session-1", "First Session")
    store.create_session("session-2", "Second Session")
    store.update_session(
        "session-1",
        messages=[{"role": "user", "content": "hello"}],
        total_tokens=12,
        round_count=1,
        summary="summary",
    )
    store.archive_session("session-2", is_archived=True)
    store.link_session_to_knowledge("session-1", knowledge_id)

    session = store.get_session("session-1")
    active_sessions = store.list_sessions(is_archived=False, limit=10)
    archived_sessions = store.list_sessions(is_archived=True, limit=10)
    stats = store.get_session_stats()
    overview = store.get_all_sessions_stats()

    assert store.table_exists("chat_sessions") is True
    assert session is not None
    assert session["messages"] == [{"role": "user", "content": "hello"}]
    assert session["knowledge_id"] == knowledge_id
    assert {row["session_id"] for row in active_sessions} == {"session-1"}
    assert {row["session_id"] for row in archived_sessions} == {"session-2"}
    assert stats["total_sessions"] == 2
    assert stats["active_sessions"] == 1
    assert stats["archived_sessions"] == 1
    assert stats["total_tokens"] == 12
    assert stats["total_rounds"] == 1
    assert {row["session_id"] for row in overview} == {"session-1", "session-2"}

    store.archive_session("session-2", is_archived=False)
    assert {row["session_id"] for row in store.list_sessions(is_archived=False, limit=10)} == {
        "session-1",
        "session-2",
    }

    store.delete_session("session-2")
    assert store.get_session("session-2") is None
