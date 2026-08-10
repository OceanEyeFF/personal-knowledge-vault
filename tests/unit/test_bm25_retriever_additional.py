"""
BM25Retriever 额外白盒覆盖测试。
"""

from __future__ import annotations

import sqlite3
import sys
from contextlib import contextmanager
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.retrieval.bm25_retriever import BM25Retriever  # noqa: E402


def test_search_returns_empty_for_blank_query(tmp_path: Path) -> None:
    retriever = BM25Retriever(tmp_path / "test.db")

    response = retriever.search("   ")

    assert response.results == ()
    assert response.status == "invalid"


def test_search_returns_empty_when_all_tokens_are_sanitized(
    tmp_path: Path,
) -> None:
    retriever = BM25Retriever(tmp_path / "test.db")
    retriever.text_processor.tokenize_chinese = lambda _: '" * ""'  # type: ignore[method-assign]

    response = retriever.search("特殊符号")

    assert response.results == ()
    assert response.status == "invalid"


def test_search_returns_empty_when_store_raises(tmp_path: Path) -> None:
    retriever = BM25Retriever(tmp_path / "test.db")
    retriever.text_processor.tokenize_chinese = lambda _: "alpha"  # type: ignore[method-assign]

    @contextmanager
    def broken_connection():
        raise sqlite3.OperationalError("boom")
        yield

    retriever.store.get_connection = broken_connection  # type: ignore[assignment]

    response = retriever.search("alpha")

    assert response.results == ()
    assert response.status == "error"
    assert response.error_type == "OperationalError"


def test_helper_methods_cover_sanitize_and_positive_score_branch(tmp_path: Path) -> None:
    retriever = BM25Retriever(tmp_path / "test.db")
    retriever.text_processor.tokenize_chinese = (  # type: ignore[method-assign]
        lambda _: 'alpha "*" beta'
    )

    assert retriever._build_match_query("alpha") == "alpha beta"
    assert retriever._sanitize_token(' "beta*" ') == "beta"
    assert retriever._normalize_score(3.0, rank=1) == 0.3
