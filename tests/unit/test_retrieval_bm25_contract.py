"""W2 BM25 five-state and redaction contracts."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

import pytest

from src.retrieval.bm25_retriever import BM25Retriever
from src.runtime.errors import ErrorCode
from src.storage.markdown_store import Entry
from src.storage.sqlite_store import SQLiteStore


@pytest.mark.parametrize(
    "query,limit",
    [("", 10), ("  ", 10), (None, 10), (123, 10), ("alpha", 0), ("alpha", True)],
)
def test_invalid_bm25_request_is_not_no_hits(
    tmp_path: Path,
    query,
    limit,
) -> None:
    response = BM25Retriever(tmp_path / "knowledge.db").search(query, limit)

    assert response.status == "invalid"
    assert response.error_code is ErrorCode.RETRIEVAL_INVALID_QUERY
    assert response.results == ()


def test_bm25_success_and_no_hits_are_distinct(tmp_path: Path) -> None:
    db_path = tmp_path / "knowledge.db"
    store = SQLiteStore(db_path)
    store.initialize()
    knowledge_id = store.insert_entry(
        Entry(
            title="Alpha Systems",
            content="Alpha systems body",
            summary_one_sentence="Alpha summary",
            summary_100_words="Alpha systems summary",
            source_type="test",
            source_url="https://example.test/alpha",
            tags=["alpha"],
            keywords="alpha,systems",
        ),
        str(tmp_path / "alpha.md"),
    )
    retriever = BM25Retriever(db_path)

    success = retriever.search("alpha", limit=5)
    no_hits = retriever.search("definitelyabsenttoken", limit=5)

    assert success.status == "success"
    assert success.strategy == "bm25"
    assert success.results[0].knowledge_id == knowledge_id
    assert no_hits.status == "no_hits"
    assert no_hits.results == ()


def test_bm25_backend_exception_is_error_and_redacted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canary = "CANARY_FTS_KEY"
    private_path = r"C:\\private\\knowledge.db"
    retriever = BM25Retriever(tmp_path / "knowledge.db")

    @contextmanager
    def broken_connection():
        raise RuntimeError(f"{canary} at {private_path}")
        yield

    monkeypatch.setattr(retriever.store, "get_connection", broken_connection)

    response = retriever.search("alpha")

    assert response.status == "error"
    assert response.error_code is ErrorCode.RETRIEVAL_BACKEND_FAILED
    assert response.error_message == "BM25 检索后端不可用"
    assert response.error_type == "RuntimeError"
    assert canary not in repr(response.to_dict())
    assert private_path not in repr(response.to_dict())


def test_bm25_malformed_row_is_metadata_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    retriever = BM25Retriever(tmp_path / "knowledge.db")

    class _Cursor:
        @staticmethod
        def fetchall():
            return [(1, "incomplete")]

    class _Connection:
        @staticmethod
        def execute(sql, params):
            return _Cursor()

    @contextmanager
    def connection():
        yield _Connection()

    monkeypatch.setattr(retriever.store, "get_connection", connection)

    response = retriever.search("alpha")

    assert response.status == "error"
    assert response.error_code is ErrorCode.RETRIEVAL_METADATA_INCONSISTENT
    assert response.error_message == "BM25 检索结果元数据不一致"


def test_fts_control_syntax_is_sanitized_to_invalid_not_backend_error(
    tmp_path: Path,
) -> None:
    retriever = BM25Retriever(tmp_path / "knowledge.db")

    response = retriever.search('"*" ( ) : ^ + - OR NEAR')

    assert response.status == "invalid"
    assert retriever._build_match_query('"*" ( ) : ^ + - OR NEAR') == ""
    # Plain words remain searchable while every FTS control character/operator
    # is removed; no MATCH syntax can escape through the query parameter.
    assert retriever._build_match_query('title:"*"') == "title"
