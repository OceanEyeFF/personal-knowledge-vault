"""Behavior contracts for QueryRouter strategy selection and result ordering."""

from unittest.mock import MagicMock

import pytest

from src.retrieval.query_router import QueryRouter
from src.retrieval.result import SearchResult


def _result(knowledge_id: int, score: float) -> SearchResult:
    return SearchResult(
        knowledge_id=knowledge_id,
        title=f"entry-{knowledge_id}",
        score=score,
        highlight="",
        metadata={},
    )


@pytest.fixture
def router() -> QueryRouter:
    instance = QueryRouter.__new__(QueryRouter)
    instance.token_threshold = 3
    instance.text_processor = MagicMock()
    instance.bm25_retriever = MagicMock()
    instance.hybrid_retriever = MagicMock()
    return instance


def test_short_query_uses_only_bm25_and_preserves_full_order(router) -> None:
    expected = [_result(3, 0.9), _result(1, 0.8), _result(2, 0.8)]
    router.text_processor.tokenize_chinese.return_value = "short query"
    router.bm25_retriever.search.return_value = expected

    actual = router.search("short query", limit=3)

    assert actual == expected
    assert [item.knowledge_id for item in actual] == [3, 1, 2]
    router.bm25_retriever.search.assert_called_once_with("short query", 3)
    router.hybrid_retriever.search.assert_not_called()


def test_threshold_query_uses_only_hybrid_and_preserves_full_order(router) -> None:
    expected = [_result(9, 0.7), _result(4, 0.6)]
    router.text_processor.tokenize_chinese.return_value = "one two three"
    router.hybrid_retriever.search.return_value = expected

    actual = router.search("semantic query", limit=2)

    assert actual == expected
    assert [item.knowledge_id for item in actual] == [9, 4]
    router.hybrid_retriever.search.assert_called_once_with("semantic query", 2)
    router.bm25_retriever.search.assert_not_called()


@pytest.mark.parametrize("query", ["", " ", "\t\n"])
def test_blank_query_skips_tokenization_and_retrievers(router, query: str) -> None:
    assert router.search(query, limit=5) == []
    router.text_processor.tokenize_chinese.assert_not_called()
    router.bm25_retriever.search.assert_not_called()
    router.hybrid_retriever.search.assert_not_called()
