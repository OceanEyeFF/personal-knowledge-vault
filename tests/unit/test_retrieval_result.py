"""Unit contracts for the W2 five-state retrieval response."""

from __future__ import annotations

import pytest

from src.retrieval.result import (
    RetrievalIssue,
    SearchResponse,
    SearchResult,
    is_strict_search_response,
)
from src.runtime.errors import ErrorCode


def _result(knowledge_id: int = 1) -> SearchResult:
    return SearchResult(
        knowledge_id=knowledge_id,
        title=f"entry-{knowledge_id}",
        score=0.8,
        highlight="摘要",
        metadata={"source_type": "test"},
    )


def _issue(
    code: ErrorCode = ErrorCode.RETRIEVAL_BACKEND_FAILED,
) -> RetrievalIssue:
    return RetrievalIssue(
        code=code,
        message="检索暂时不可用",
        stage="test_backend",
        recoverable=True,
        cause_type="RuntimeError",
    )


def test_search_result_is_frozen_and_validates_score() -> None:
    result = _result()

    with pytest.raises(AttributeError):
        result.score = 0.9
    with pytest.raises(ValueError, match="分数必须"):
        SearchResult(1, "bad", 1.1, "", {})
    with pytest.raises(ValueError, match="分数必须"):
        SearchResult(1, "bad", -0.1, "", {})


def test_completed_constructs_success_and_no_hits() -> None:
    success = SearchResponse.completed([_result()], strategy="bm25")
    no_hits = SearchResponse.completed([], strategy="bm25")

    assert success.status == "success"
    assert success.results == (_result(),)
    assert success.has_results is True
    assert success.ok is True
    assert success.failed is False

    assert no_hits.status == "no_hits"
    assert no_hits.results == ()
    assert no_hits.has_results is False
    assert no_hits.ok is True
    assert no_hits.failed is False


def test_invalid_error_and_degraded_are_distinct() -> None:
    invalid = SearchResponse.invalid("查询文本不能为空", strategy="router")
    error = SearchResponse.failed_response(_issue(), strategy="vector")
    degraded = SearchResponse.degraded_response(
        [_result()],
        [_issue(ErrorCode.RETRIEVAL_INDEX_UNAVAILABLE)],
        strategy="hybrid",
    )

    assert invalid.status == "invalid"
    assert invalid.error_code is ErrorCode.RETRIEVAL_INVALID_QUERY
    assert invalid.ok is False
    assert invalid.failed is False

    assert error.status == "error"
    assert error.error_code is ErrorCode.RETRIEVAL_BACKEND_FAILED
    assert error.error_message == "检索暂时不可用"
    assert error.error_type == "RuntimeError"
    assert error.failed is True

    assert degraded.status == "degraded"
    assert degraded.results == (_result(),)
    assert degraded.error_code is ErrorCode.RETRIEVAL_INDEX_UNAVAILABLE
    assert degraded.ok is False
    assert degraded.failed is False


def test_response_rejects_list_and_truthiness_compatibility() -> None:
    response = SearchResponse.completed([_result()], strategy="bm25")

    assert response != [_result()]
    with pytest.raises(TypeError, match="没有隐式真值"):
        bool(response)
    with pytest.raises(TypeError):
        len(response)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        iter(response)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        response[0]  # type: ignore[index]


def test_response_invariants_reject_contradictory_states() -> None:
    with pytest.raises(ValueError, match="success 必须"):
        SearchResponse(status="success", strategy="bm25")
    with pytest.raises(ValueError, match="no_hits"):
        SearchResponse(status="no_hits", results=(_result(),), strategy="bm25")
    with pytest.raises(ValueError, match="invalid 必须"):
        SearchResponse(status="invalid", strategy="router")
    with pytest.raises(ValueError, match="RETRIEVAL_INVALID_QUERY"):
        SearchResponse(status="invalid", strategy="router", issues=(_issue(),))
    with pytest.raises(ValueError, match="error 必须"):
        SearchResponse(status="error", strategy="vector")
    with pytest.raises(ValueError, match="degraded 必须"):
        SearchResponse(status="degraded", strategy="hybrid")


def test_exception_text_is_not_exposed_by_issue_or_public_payload() -> None:
    canary = "CANARY_SECRET_TOKEN_123"
    absolute_path = r"C:\\Users\\private\\vault\\knowledge.db"
    exc = RuntimeError(f"{canary} at {absolute_path}")

    issue = RetrievalIssue.from_exception(
        exc,
        fallback_code=ErrorCode.RETRIEVAL_BACKEND_FAILED,
        public_message="检索后端不可用",
        stage="bm25_backend",
        recoverable=True,
    )
    response = SearchResponse.failed_response(issue, strategy="bm25")
    public_payload = repr(response.to_dict())

    assert response.error_message == "检索后端不可用"
    assert response.error_type == "RuntimeError"
    assert canary not in public_payload
    assert absolute_path not in public_payload
    assert response.to_dict()["issues"] == [
        {
            "code": "retrieval_backend_failed",
            "message": "检索后端不可用",
            "stage": "bm25_backend",
            "recoverable": True,
            "cause_type": "RuntimeError",
        }
    ]


class _StrSubclass(str):
    pass


class _FloatSubclass(float):
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
        ("score", _FloatSubclass(0.5)),
        ("score", float("nan")),
        ("score", float("inf")),
        ("highlight", _StrSubclass("highlight")),
        ("metadata", _DictSubclass()),
    ),
)
def test_search_result_constructor_rejects_non_exact_field_shapes(
    field: str,
    value: object,
) -> None:
    values = {
        "knowledge_id": 1,
        "title": "title",
        "score": 0.5,
        "highlight": "highlight",
        "metadata": {},
    }
    values[field] = value

    with pytest.raises((TypeError, ValueError)):
        SearchResult(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "metadata",
    (
        {1: "non-string-key"},
        {"nested": {"value": object()}},
        {"nested": [float("nan")]},
        {"tags": ["valid", object()]},
        {"source_type": object()},
        {"source_type": None},
        {"source_type": " \t"},
        {"source_url": 7},
        {"summary_one_sentence": []},
        {"chunk_id": True},
        {"chunk_id": 0},
        {"chunk_index": -1},
        {"word_count": True},
        {"word_count": "5"},
        {"word_count": -1},
        {"vector_distance": float("inf")},
        {"knowledge_id": 2},
    ),
)
def test_search_result_rejects_non_json_or_invalid_public_metadata(
    metadata: dict[object, object],
) -> None:
    with pytest.raises(TypeError, match="metadata"):
        SearchResult(1, "title", 0.5, "highlight", metadata)  # type: ignore[arg-type]


def test_search_result_rejects_cyclic_metadata() -> None:
    metadata: dict[str, object] = {}
    metadata["cycle"] = metadata

    with pytest.raises(TypeError, match="metadata"):
        SearchResult(1, "title", 0.5, "highlight", metadata)


def test_strict_validator_rechecks_mutated_nested_metadata() -> None:
    result = SearchResult(
        1,
        "title",
        0.5,
        "highlight",
        {"nested": {"safe": True}, "tags": ["alpha"]},
    )
    response = SearchResponse.completed((result,), strategy="bm25")
    result.metadata["nested"]["unsafe"] = object()  # type: ignore[index]

    assert is_strict_search_response(response) is False


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("code", ErrorCode.RETRIEVAL_BACKEND_FAILED.value),
        ("message", _StrSubclass("message")),
        ("message", " \t"),
        ("stage", _StrSubclass("stage")),
        ("stage", "\n"),
        ("recoverable", 1),
        ("cause_type", _StrSubclass("RuntimeError")),
        ("cause_type", "RuntimeError\r\nCANARY"),
        ("cause_type", "X" * 97),
    ),
)
def test_retrieval_issue_constructor_rejects_non_exact_or_unsafe_fields(
    field: str,
    value: object,
) -> None:
    values = {
        "code": ErrorCode.RETRIEVAL_BACKEND_FAILED,
        "message": "message",
        "stage": "stage",
        "recoverable": True,
        "cause_type": "RuntimeError",
    }
    values[field] = value

    with pytest.raises((TypeError, ValueError)):
        RetrievalIssue(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "strategy",
    (
        _StrSubclass("bm25"),
        "BM25",
        "bm25-hybrid",
        "bm25__hybrid",
        "bm25_",
        "bm25\r\nCANARY",
        "s" * 65,
    ),
)
def test_search_response_rejects_non_exact_or_unsafe_strategy(
    strategy: object,
) -> None:
    with pytest.raises(ValueError, match="strategy"):
        SearchResponse.completed((_result(),), strategy=strategy)  # type: ignore[arg-type]


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
def test_strict_validator_rejects_corrupted_frozen_search_result(
    field: str,
    value: object,
) -> None:
    result = _result()
    response = SearchResponse.completed((result,), strategy="bm25")
    object.__setattr__(result, field, value)

    assert is_strict_search_response(response) is False


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
def test_strict_validator_rejects_corrupted_frozen_retrieval_issue(
    field: str,
    value: object,
) -> None:
    issue = _issue()
    response = SearchResponse.failed_response(issue, strategy="bm25")
    object.__setattr__(issue, field, value)

    assert is_strict_search_response(response) is False


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("status", _StrSubclass("success")),
        ("results", [_result()]),
        ("strategy", _StrSubclass("bm25")),
        ("strategy", "bm25\r\nCANARY"),
        ("issues", []),
    ),
)
def test_strict_validator_rejects_corrupted_frozen_response_shape(
    field: str,
    value: object,
) -> None:
    response = SearchResponse.completed((_result(),), strategy="bm25")
    object.__setattr__(response, field, value)

    assert is_strict_search_response(response) is False


def test_strict_validator_accepts_both_builtin_numeric_score_types() -> None:
    integer_score = SearchResult(1, "one", 1, "", {})
    float_score = SearchResult(2, "two", 0.5, "", {})

    assert is_strict_search_response(
        SearchResponse.completed((integer_score, float_score), strategy="bm25")
    )


def test_search_result_allows_missing_or_null_word_count_metadata() -> None:
    missing = SearchResult(1, "one", 1, "", {})
    null_count = SearchResult(2, "two", 0.5, "", {"word_count": None})

    assert is_strict_search_response(
        SearchResponse.completed((missing, null_count), strategy="bm25")
    )


def test_contract_violations_do_not_format_untrusted_values() -> None:
    class ExplodingValue:
        def __str__(self) -> str:
            raise AssertionError("untrusted __str__ must not run")

    with pytest.raises(ValueError) as score_error:
        SearchResult(1, "one", ExplodingValue(), "", {})  # type: ignore[arg-type]

    assert str(score_error.value) == "分数必须是 [0.0, 1.0] 范围内的有限数值"


def test_iterable_failures_are_replaced_with_fixed_contract_errors() -> None:
    canary = "ITERABLE_CANARY_token=secret"

    class ExplodingIterable:
        def __iter__(self):
            raise RuntimeError(canary)

    with pytest.raises(TypeError) as captured:
        SearchResponse.completed(ExplodingIterable(), strategy="bm25")  # type: ignore[arg-type]

    assert str(captured.value) == "results 必须是可迭代集合"
    assert canary not in str(captured.value)


def test_exception_class_name_is_sanitized_before_issue_publication() -> None:
    unsafe_error_type = type("UnsafeError\r\nCAUSE_CANARY", (RuntimeError,), {})

    issue = RetrievalIssue.from_exception(
        unsafe_error_type("private detail"),
        fallback_code=ErrorCode.RETRIEVAL_BACKEND_FAILED,
        public_message="检索后端不可用",
        stage="bm25_backend",
        recoverable=True,
    )

    assert issue.cause_type == "Exception"
    assert "CAUSE_CANARY" not in repr(issue.to_dict())
