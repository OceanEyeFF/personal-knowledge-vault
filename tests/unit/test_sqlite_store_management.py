"""
SQLiteStore 管理与会话路径测试。
"""

from __future__ import annotations

from contextlib import contextmanager
import sqlite3
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.storage.markdown_store import Entry  # noqa: E402
from src.storage.sqlite_store import FTS_TABLE_NAME, SQLiteStore  # noqa: E402


MIGRATIONS_DIR = PROJECT_ROOT / "scripts" / "migrations"


@pytest.fixture
def store(tmp_path: Path) -> SQLiteStore:
    db_path = tmp_path / "test.db"
    instance = SQLiteStore(db_path)
    instance.initialize()
    return instance


def _apply_chat_schema(store: SQLiteStore) -> None:
    sql = (MIGRATIONS_DIR / "004_add_chat_sessions.sql").read_text(encoding="utf-8")
    with sqlite3.connect(str(store.db_path)) as conn:
        conn.executescript(sql)


def test_get_connection_rolls_back_and_reraises(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "rollback.db")
    with sqlite3.connect(str(store.db_path)) as conn:
        conn.execute("CREATE TABLE rollback_test (id INTEGER PRIMARY KEY, value TEXT)")

    with pytest.raises(RuntimeError, match="boom"):
        with store.get_connection() as conn:
            conn.execute(
                "INSERT INTO rollback_test (id, value) VALUES (?, ?)",
                (1, "will rollback"),
            )
            raise RuntimeError("boom")

    with sqlite3.connect(str(store.db_path)) as conn:
        row = conn.execute("SELECT value FROM rollback_test WHERE id = 1").fetchone()

    assert row is None


def test_timeline_columns_and_fts_helpers_cover_legacy_contract(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy.db"
    store = SQLiteStore(db_path)

    with sqlite3.connect(str(db_path)) as raw_conn:
        raw_conn.row_factory = sqlite3.Row
        raw_conn.executescript(
            f"""
            CREATE TABLE knowledge_items (
                knowledge_id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL
            );
            CREATE VIRTUAL TABLE {FTS_TABLE_NAME} USING fts5(
                title,
                summary_100_words,
                keywords,
                tags,
                content=knowledge_items,
                content_rowid=knowledge_id
            );
            CREATE TRIGGER knowledge_items_ai AFTER INSERT ON knowledge_items BEGIN
                INSERT INTO {FTS_TABLE_NAME}(rowid, title, summary_100_words, keywords, tags)
                VALUES (new.knowledge_id, new.title, '', '', '');
            END;
            """
        )

        store._ensure_timeline_time_columns(raw_conn)
        assert store._fts_uses_external_content(raw_conn, FTS_TABLE_NAME) is True
        assert store._sqlite_object_exists(raw_conn, "table", FTS_TABLE_NAME) is True
        assert "content=knowledge_items" in store._sqlite_object_sql(
            raw_conn, "table", FTS_TABLE_NAME
        ).lower()

        rebuild_needed = store._ensure_fts5_contract(raw_conn)
        raw_conn.commit()

    assert rebuild_needed is True

    with sqlite3.connect(str(db_path)) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(knowledge_items)")}
        table_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            (FTS_TABLE_NAME,),
        ).fetchone()[0]

    assert {"event_time", "published_at"}.issubset(columns)
    assert "content=knowledge_items" not in table_sql.lower()


def test_chunk_and_query_validation_paths(store: SQLiteStore) -> None:
    assert store.insert_chunks(knowledge_id=1, chunks=[]) == 0
    assert store.insert_chunks(knowledge_id=1, chunks=["   ", ""]) == 0
    with pytest.raises(ValueError):
        store.insert_chunks(knowledge_id=0, chunks=["chunk"])
    with pytest.raises(ValueError):
        store.get_chunks_by_knowledge_id(0)
    with pytest.raises(ValueError):
        store.get_chunk_by_index(0, 0)
    with pytest.raises(ValueError):
        store.get_chunk_by_index(1, -1)
    with pytest.raises(ValueError):
        store.delete_chunks_by_knowledge_id(0)
    with pytest.raises(ValueError):
        store.count_chunks(0)

    assert store.query_by_id(999) is None
    assert store.table_exists("knowledge_items") is True
    assert store.table_exists("missing_table") is False


def test_query_helpers_raise_on_storage_errors(
    store: SQLiteStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    @contextmanager
    def _broken_connection():
        class BrokenConn:
            def execute(self, *_args: object, **_kwargs: object) -> None:
                raise sqlite3.OperationalError("boom")

        yield BrokenConn()

    monkeypatch.setattr(store, "get_connection", _broken_connection)

    with pytest.raises(sqlite3.OperationalError):
        store.query_by_url("https://example.com/fail")
    with pytest.raises(sqlite3.OperationalError):
        store.list_entries()
    with pytest.raises(sqlite3.OperationalError):
        store.count_entries()
    with pytest.raises(sqlite3.OperationalError):
        store.count_entries_by_source_type()
    with pytest.raises(sqlite3.OperationalError):
        store.get_all_tags_with_count()

    monkeypatch.setattr(
        store,
        "count_entries",
        lambda *args, **kwargs: (_ for _ in ()).throw(sqlite3.OperationalError("boom")),
    )
    with pytest.raises(sqlite3.OperationalError):
        store.get_statistics()


def test_chat_session_lifecycle_and_stats(store: SQLiteStore, tmp_path: Path) -> None:
    _apply_chat_schema(store)

    entry = Entry(
        title="Chat Linked Entry",
        source_type="generic",
        source_url="https://example.com/chat-linked",
        tags=["chat"],
        keywords=["chat"],
        content="chat content",
    )
    file_path = tmp_path / "chat-linked.md"
    file_path.write_text("# Chat Linked Entry\n\nchat content", encoding="utf-8")
    knowledge_id = store.insert_entry(entry, str(file_path))

    store.create_session("session-1", "Alpha Session")
    store.create_session("session-2", "Beta Session")
    store.update_session(
        "session-1",
        messages=[{"role": "user", "content": "hello"}],
        total_tokens=12,
        round_count=1,
        summary="summary",
    )

    session = store.get_session("session-1")
    assert session is not None
    assert session["messages"] == [{"role": "user", "content": "hello"}]
    assert session["summary"] == "summary"
    assert store.get_session("missing-session") is None

    active_sessions = store.list_sessions()
    assert [item["session_id"] for item in active_sessions] == ["session-1", "session-2"]

    store.archive_session("session-2", is_archived=True)
    archived_sessions = store.list_sessions(is_archived=True)
    assert [item["session_id"] for item in archived_sessions] == ["session-2"]

    store.link_session_to_knowledge("session-1", knowledge_id)
    linked_session = store.get_session("session-1")
    assert linked_session is not None
    assert linked_session["knowledge_id"] == knowledge_id

    stats = store.get_session_stats()
    assert stats["total_sessions"] == 2
    assert stats["active_sessions"] == 1
    assert stats["archived_sessions"] == 1
    assert stats["total_tokens"] == 12
    assert stats["total_rounds"] == 1

    all_stats = store.get_all_sessions_stats()
    assert {item["session_id"] for item in all_stats} == {"session-1", "session-2"}

    store.delete_session("session-2")
    remaining = store.get_all_sessions_stats()
    assert [item["session_id"] for item in remaining] == ["session-1"]
