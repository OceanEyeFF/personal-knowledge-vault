"""
SQLiteStore 额外白盒覆盖测试。
"""

from __future__ import annotations

import sqlite3
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.storage.markdown_store import Entry  # noqa: E402
from src.storage.sqlite_store import FTS_TABLE_NAME, LEGACY_FTS_TABLE_NAME, SQLiteStore  # noqa: E402


@pytest.fixture
def store(tmp_path: Path) -> SQLiteStore:
    sqlite_store = SQLiteStore(tmp_path / "test.db")
    sqlite_store.initialize()
    return sqlite_store


def _create_chat_sessions_table(store: SQLiteStore) -> None:
    with store.get_connection() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS chat_sessions (
                session_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                messages TEXT NOT NULL,
                summary TEXT,
                total_tokens INTEGER DEFAULT 0,
                round_count INTEGER DEFAULT 0,
                is_archived BOOLEAN DEFAULT 0,
                knowledge_id INTEGER,
                FOREIGN KEY (knowledge_id) REFERENCES knowledge_items(knowledge_id) ON DELETE SET NULL
            );
            """
        )


def _insert_entry(store: SQLiteStore, source_url: str = "https://example.com/entry") -> int:
    return store.insert_entry(
        Entry(
            title="Test Entry",
            source_type="generic",
            source_url=source_url,
            tags=["AI"],
            keywords=["alpha"],
            content="Alpha content",
        ),
        f"/tmp/{Path(source_url).name}.md",
    )


def test_get_connection_rolls_back_and_reraises(store: SQLiteStore) -> None:
    with pytest.raises(sqlite3.OperationalError):
        with store.get_connection() as conn:
            conn.execute("INSERT INTO tags (name, count) VALUES ('RollbackTag', 1)")
            conn.execute("INSERT INTO missing_table VALUES (1)")

    assert all(tag["name"] != "RollbackTag" for tag in store.get_all_tags_with_count())


def test_helper_methods_cover_old_schema_timeline_columns_and_fts_contract(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "legacy.db"
    store = SQLiteStore(db_path)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(
        f"""
        CREATE TABLE knowledge_items (
            knowledge_id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL
        );
        CREATE VIRTUAL TABLE {LEGACY_FTS_TABLE_NAME} USING fts5(
            knowledge_id UNINDEXED,
            title
        );
        CREATE TRIGGER knowledge_fts_insert
        AFTER INSERT ON knowledge_items BEGIN
            INSERT INTO {LEGACY_FTS_TABLE_NAME} (knowledge_id, title)
            VALUES (new.knowledge_id, new.title);
        END;
        CREATE VIRTUAL TABLE {FTS_TABLE_NAME} USING fts5(
            title,
            content=knowledge_items,
            content_rowid=knowledge_id
        );
        """
    )

    store._ensure_timeline_time_columns(conn)
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(knowledge_items)")}
    assert {"event_time", "published_at"}.issubset(columns)
    assert store._sqlite_object_exists(conn, "table", LEGACY_FTS_TABLE_NAME) is True
    assert store._fts_uses_external_content(conn, FTS_TABLE_NAME) is True
    assert "content_rowid=knowledge_id" in store._sqlite_object_sql(conn, "table", FTS_TABLE_NAME).lower()

    rebuild_required = store._ensure_fts5_contract(conn)
    assert rebuild_required is True
    assert store._sqlite_object_exists(conn, "table", LEGACY_FTS_TABLE_NAME) is False
    assert store._fts_uses_external_content(conn, FTS_TABLE_NAME) is False
    conn.close()


def test_chunk_validation_and_empty_paths(store: SQLiteStore) -> None:
    assert store.insert_chunks(1, []) == 0
    assert store.insert_chunks(1, [" ", None, "\n"]) == 0
    assert store.count_chunks() == 0
    assert store.query_by_id(99999) is None
    assert store.table_exists("knowledge_items") is True

    with pytest.raises(ValueError):
        store.insert_chunks(0, ["chunk"])
    with pytest.raises(ValueError):
        store.get_chunks_by_knowledge_id(0)
    with pytest.raises(ValueError):
        store.get_chunk_by_index(1, -1)
    with pytest.raises(ValueError):
        store.get_chunk_by_index(0, 0)
    with pytest.raises(ValueError):
        store.delete_chunks_by_knowledge_id(0)
    with pytest.raises(ValueError):
        store.count_chunks(0)


def test_query_and_statistics_methods_reraise_when_connection_breaks(
    store: SQLiteStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @contextmanager
    def broken_connection():
        raise sqlite3.OperationalError("boom")
        yield

    monkeypatch.setattr(store, "get_connection", broken_connection)

    failing_calls = [
        lambda: store.query_by_url("https://example.com/x"),
        lambda: store.list_entries(),
        lambda: store.count_entries(tag="AI"),
        store.count_entries_by_source_type,
        store.get_all_tags_with_count,
        store.get_statistics,
    ]

    for call in failing_calls:
        with pytest.raises(sqlite3.OperationalError):
            call()


def test_chat_session_methods_cover_crud_archive_link_and_stats(store: SQLiteStore) -> None:
    _create_chat_sessions_table(store)
    knowledge_id = _insert_entry(store)

    store.create_session("s1", "Session 1")
    assert store.get_session("missing") is None

    store.update_session(
        "s1",
        messages=[{"role": "user", "content": "你好"}],
        total_tokens=42,
        round_count=1,
        summary="summary",
    )
    session = store.get_session("s1")
    assert session is not None
    assert session["messages"][0]["content"] == "你好"
    assert session["total_tokens"] == 42

    active_sessions = store.list_sessions()
    assert len(active_sessions) == 1
    assert active_sessions[0]["session_id"] == "s1"

    store.archive_session("s1", is_archived=True)
    archived_sessions = store.list_sessions(is_archived=True)
    assert archived_sessions[0]["is_archived"] == 1

    store.archive_session("s1", is_archived=False)
    store.link_session_to_knowledge("s1", knowledge_id)
    session = store.get_session("s1")
    assert session is not None
    assert session["knowledge_id"] == knowledge_id

    stats = store.get_session_stats()
    assert stats["total_sessions"] == 1
    assert stats["active_sessions"] == 1
    assert stats["archived_sessions"] == 0
    assert stats["total_tokens"] == 42
    assert stats["total_rounds"] == 1

    all_stats = store.get_all_sessions_stats()
    assert len(all_stats) == 1
    assert all_stats[0]["session_id"] == "s1"

    store.delete_session("s1")
    assert store.get_session("s1") is None
