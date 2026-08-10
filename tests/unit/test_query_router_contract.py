"""W2 QueryRouter strategy, propagation, and lazy-provider contracts."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, Mock

import pytest

from src.retrieval.query_router import QueryRouter
from src.retrieval.result import RetrievalIssue, SearchResponse, SearchResult
from src.runtime.errors import ErrorCode


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


def test_short_query_uses_only_bm25_and_preserves_response(router) -> None:
    expected = SearchResponse.completed(
        [_result(3, 0.9), _result(1, 0.8), _result(2, 0.8)],
        strategy="bm25",
    )
    router.text_processor.tokenize_chinese.return_value = "short query"
    router.bm25_retriever.search.return_value = expected

    actual = router.search("short query", limit=3)

    assert actual is expected
    assert [item.knowledge_id for item in actual.results] == [3, 1, 2]
    router.bm25_retriever.search.assert_called_once_with("short query", 3)
    router.hybrid_retriever.search.assert_not_called()


def test_threshold_query_uses_only_hybrid_and_preserves_degraded_state(router) -> None:
    expected = SearchResponse.degraded_response(
        [_result(9, 0.7)],
        [
            RetrievalIssue(
                code=ErrorCode.RETRIEVAL_INDEX_UNAVAILABLE,
                message="向量索引尚不可用",
                stage="vector_index_load",
                recoverable=True,
            )
        ],
        strategy="hybrid",
    )
    router.text_processor.tokenize_chinese.return_value = "one two three"
    router.hybrid_retriever.search.return_value = expected

    actual = router.search("semantic query", limit=2)

    assert actual is expected
    assert actual.status == "degraded"
    assert [item.knowledge_id for item in actual.results] == [9]
    router.hybrid_retriever.search.assert_called_once_with("semantic query", 2)
    router.bm25_retriever.search.assert_not_called()


@pytest.mark.parametrize("query", ["", " ", "\t\n", None])
def test_blank_query_returns_invalid_and_skips_dependencies(router, query) -> None:
    response = router.search(query, limit=5)

    assert response.status == "invalid"
    assert response.error_code is ErrorCode.RETRIEVAL_INVALID_QUERY
    router.text_processor.tokenize_chinese.assert_not_called()
    router.bm25_retriever.search.assert_not_called()
    router.hybrid_retriever.search.assert_not_called()


def test_tokenizer_exception_returns_error_with_safe_message(router) -> None:
    canary = "CANARY_TOKENIZER_PATH_C:\\private"
    router.text_processor.tokenize_chinese.side_effect = RuntimeError(canary)

    response = router.search("query")

    assert response.status == "error"
    assert response.strategy == "router"
    assert response.error_code is ErrorCode.RETRIEVAL_BACKEND_FAILED
    assert response.error_message == "查询路由暂时不可用"
    assert canary not in repr(response.to_dict())


def test_downstream_exception_returns_error_not_no_hits(router) -> None:
    router.text_processor.tokenize_chinese.return_value = "short"
    router.bm25_retriever.search.side_effect = RuntimeError("fts unavailable")

    response = router.search("short")

    assert response.status == "error"
    assert response.strategy == "bm25"
    assert response.results == ()
    assert response.error_code is ErrorCode.RETRIEVAL_BACKEND_FAILED


def test_invalid_downstream_type_returns_error(router) -> None:
    router.text_processor.tokenize_chinese.return_value = "short"
    router.bm25_retriever.search.return_value = []

    response = router.search("short")

    assert response.status == "error"
    assert response.error_message == "bm25 检索返回无效响应"
    assert response.error_type == "InvalidSearchResponse"


class _StrSubclass(str):
    pass


class _DictSubclass(dict):
    pass


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("knowledge_id", True),
        ("knowledge_id", 0),
        ("title", _StrSubclass("title")),
        ("score", True),
        ("score", float("nan")),
        ("score", float("inf")),
        ("highlight", _StrSubclass("highlight")),
        ("metadata", _DictSubclass()),
    ),
)
def test_router_fails_closed_on_corrupted_result_fields(
    router: QueryRouter,
    field: str,
    value: object,
) -> None:
    malformed_result = _result(1, 0.8)
    malformed_response = SearchResponse.completed(
        (malformed_result,),
        strategy="bm25",
    )
    object.__setattr__(malformed_result, field, value)
    router.text_processor.tokenize_chinese.return_value = "short"
    router.bm25_retriever.search.return_value = malformed_response

    response = router.search("short")

    assert response.status == "error"
    assert response.strategy == "bm25"
    assert response.results == ()
    assert response.error_code is ErrorCode.RETRIEVAL_BACKEND_FAILED
    assert response.error_type == "InvalidSearchResponse"


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("code", ErrorCode.RETRIEVAL_BACKEND_FAILED.value),
        ("message", _StrSubclass("message")),
        ("message", ""),
        ("stage", _StrSubclass("stage")),
        ("stage", " "),
        ("recoverable", 1),
        ("cause_type", _StrSubclass("RuntimeError")),
        ("cause_type", "RuntimeError\r\nCANARY"),
        ("cause_type", "X" * 97),
    ),
)
def test_router_fails_closed_on_corrupted_issue_fields(
    router: QueryRouter,
    field: str,
    value: object,
) -> None:
    malformed_issue = RetrievalIssue(
        code=ErrorCode.RETRIEVAL_BACKEND_FAILED,
        message="backend unavailable",
        stage="bm25_backend",
        recoverable=True,
        cause_type="RuntimeError",
    )
    malformed_response = SearchResponse.failed_response(
        malformed_issue,
        strategy="bm25",
    )
    object.__setattr__(malformed_issue, field, value)
    router.text_processor.tokenize_chinese.return_value = "short"
    router.bm25_retriever.search.return_value = malformed_response

    response = router.search("short")

    assert response.status == "error"
    assert response.strategy == "bm25"
    assert response.results == ()
    assert response.error_code is ErrorCode.RETRIEVAL_BACKEND_FAILED
    assert response.error_type == "InvalidSearchResponse"


def test_router_rejects_valid_shape_with_wrong_strategy(router: QueryRouter) -> None:
    router.text_processor.tokenize_chinese.return_value = "short"
    router.bm25_retriever.search.return_value = SearchResponse.completed(
        (_result(1, 0.8),),
        strategy="hybrid",
    )

    response = router.search("short")

    assert response.status == "error"
    assert response.strategy == "bm25"
    assert response.results == ()
    assert response.error_type == "SearchStrategyMismatch"


def test_router_rejects_injected_strategy_without_logging_canary(
    router: QueryRouter,
    caplog: pytest.LogCaptureFixture,
) -> None:
    canary = "STRATEGY_CANARY"
    malformed = SearchResponse.completed((_result(1, 0.8),), strategy="bm25")
    object.__setattr__(malformed, "strategy", f"bm25\r\n{canary}")
    router.text_processor.tokenize_chinese.return_value = "short"
    router.bm25_retriever.search.return_value = malformed

    response = router.search("short")

    assert response.status == "error"
    assert response.error_type == "InvalidSearchResponse"
    assert canary not in caplog.text


def test_constructor_and_short_route_do_not_initialize_embedding_provider(
    tmp_path: Path,
) -> None:
    factory = Mock(side_effect=AssertionError("provider must stay lazy"))
    router = QueryRouter(
        tmp_path / "knowledge.db",
        tmp_path / "vectors",
        token_threshold=3,
        embedder_factory=factory,
    )
    router.text_processor = Mock()
    router.text_processor.tokenize_chinese.return_value = "short"
    router.bm25_retriever = Mock()
    router.bm25_retriever.search.return_value = SearchResponse.completed(
        [_result(1, 0.8)],
        strategy="bm25",
    )

    response = router.search("short")

    assert response.status == "success"
    factory.assert_not_called()
