"""
Unit tests for BM25Retriever edge paths.
"""

from __future__ import annotations

from contextlib import contextmanager
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.retrieval.bm25_retriever import BM25Retriever  # noqa: E402


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _FakeConnection:
    def __init__(self, rows):
        self._rows = rows
        self.executed: list[tuple[str, tuple[object, ...]]] = []

    def execute(self, sql: str, params: tuple[object, ...]):
        self.executed.append((sql, params))
        return _FakeCursor(self._rows)


def test_search_returns_empty_for_blank_query(tmp_path: Path) -> None:
    retriever = BM25Retriever(tmp_path / "blank.db")

    assert retriever.search("   ") == []


def test_search_returns_empty_when_tokenized_query_is_empty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    retriever = BM25Retriever(tmp_path / "empty-token.db")
    monkeypatch.setattr(
        retriever.text_processor,
        "tokenize_chinese",
        lambda query: '"" *',
    )

    assert retriever.search("特殊字符") == []


def test_search_normalizes_positive_bm25_scores_and_builds_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    retriever = BM25Retriever(tmp_path / "ranked.db")
    fake_conn = _FakeConnection(
        [
            (
                7,
                "Ranked",
                "",
                "summary 100",
                "generic",
                "https://example.com/ranked",
                "AI",
                "graph",
                "/tmp/ranked.md",
                "2026-03-10 10:00:00",
                "2026-03-11 10:00:00",
                5.0,
                "",
            )
        ]
    )

    @contextmanager
    def _fake_get_connection():
        yield fake_conn

    monkeypatch.setattr(retriever, "_build_match_query", lambda query: "ranked")
    monkeypatch.setattr(retriever.store, "get_connection", _fake_get_connection)

    results = retriever.search("ranked", limit=1)

    assert len(results) == 1
    assert results[0].knowledge_id == 7
    assert results[0].score == 0.5
    assert results[0].highlight == "summary 100"
    assert results[0].metadata["bm25_score"] == 5.0
    assert fake_conn.executed[0][1] == ("ranked", 1)


def test_search_returns_empty_on_storage_exception(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    retriever = BM25Retriever(tmp_path / "error.db")

    @contextmanager
    def _broken_get_connection():
        raise RuntimeError("fts unavailable")
        yield

    monkeypatch.setattr(retriever, "_build_match_query", lambda query: "alpha")
    monkeypatch.setattr(retriever.store, "get_connection", _broken_get_connection)

    assert retriever.search("alpha") == []


def test_helper_methods_cover_sanitization_and_normalization(tmp_path: Path) -> None:
    retriever = BM25Retriever(tmp_path / "helpers.db")

    assert retriever._build_match_query('Alpha "Beta"*') == "Alpha Beta"
    assert retriever._sanitize_token('  "tag"*  ') == "tag"
    assert retriever._normalize_score(-25.0, 1) == 0.5
    assert retriever._normalize_score(2.0, 1) == 0.2
