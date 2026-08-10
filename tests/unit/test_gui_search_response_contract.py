"""Strict GUI boundary tests for the five-state retrieval contract."""

from __future__ import annotations

import pytest

from src.gui.utils.search_response_contract import is_strict_search_response
from src.retrieval.result import (
    RetrievalIssue,
    SearchResponse,
    SearchResult,
    is_strict_search_response as is_neutral_strict_search_response,
)
from src.runtime.errors import ErrorCode


RESULT = SearchResult(
    knowledge_id=1,
    title="Synthetic result",
    score=0.8,
    highlight="synthetic",
    metadata={},
)
INVALID_ISSUE = RetrievalIssue(
    code=ErrorCode.RETRIEVAL_INVALID_QUERY,
    message="invalid query",
    stage="query_validation",
    recoverable=True,
)
ERROR_ISSUE = RetrievalIssue(
    code=ErrorCode.RETRIEVAL_BACKEND_FAILED,
    message="backend unavailable",
    stage="bm25_query",
)


def _unchecked_response(
    *,
    status: object,
    results: object = (),
    issues: object = (),
    strategy: object = "bm25",
) -> SearchResponse:
    response = object.__new__(SearchResponse)
    object.__setattr__(response, "status", status)
    object.__setattr__(response, "results", results)
    object.__setattr__(response, "issues", issues)
    object.__setattr__(response, "strategy", strategy)
    return response


def test_validator_accepts_every_valid_terminal_state() -> None:
    responses = (
        SearchResponse.completed((RESULT,), strategy="bm25"),
        SearchResponse.completed((), strategy="bm25"),
        SearchResponse.invalid("invalid query", strategy="bm25"),
        SearchResponse.failed_response(ERROR_ISSUE, strategy="bm25"),
        SearchResponse.degraded_response(
            (RESULT,),
            (ERROR_ISSUE,),
            strategy="bm25",
        ),
        SearchResponse.degraded_response(
            (),
            (ERROR_ISSUE,),
            strategy="bm25",
        ),
    )

    assert all(is_strict_search_response(response) for response in responses)


@pytest.mark.parametrize(
    ("case_name", "response"),
    (
        (
            "success_requires_results",
            _unchecked_response(status="success"),
        ),
        (
            "success_forbids_issues",
            _unchecked_response(
                status="success",
                results=(RESULT,),
                issues=(ERROR_ISSUE,),
            ),
        ),
        (
            "no_hits_forbids_results",
            _unchecked_response(status="no_hits", results=(RESULT,)),
        ),
        (
            "no_hits_forbids_issues",
            _unchecked_response(status="no_hits", issues=(ERROR_ISSUE,)),
        ),
        (
            "invalid_forbids_results",
            _unchecked_response(
                status="invalid",
                results=(RESULT,),
                issues=(INVALID_ISSUE,),
            ),
        ),
        (
            "invalid_requires_issues",
            _unchecked_response(status="invalid"),
        ),
        (
            "invalid_requires_invalid_query_code",
            _unchecked_response(status="invalid", issues=(ERROR_ISSUE,)),
        ),
        (
            "error_forbids_results",
            _unchecked_response(
                status="error",
                results=(RESULT,),
                issues=(ERROR_ISSUE,),
            ),
        ),
        (
            "error_requires_issues",
            _unchecked_response(status="error"),
        ),
        (
            "degraded_requires_issues",
            _unchecked_response(status="degraded", results=(RESULT,)),
        ),
        (
            "status_must_be_known",
            _unchecked_response(status="partial"),
        ),
        (
            "strategy_must_be_nonempty",
            _unchecked_response(
                status="success",
                results=(RESULT,),
                strategy=" ",
            ),
        ),
        (
            "results_must_be_tuple",
            _unchecked_response(status="success", results=[RESULT]),
        ),
        (
            "issues_must_be_tuple",
            _unchecked_response(status="error", issues=[ERROR_ISSUE]),
        ),
        (
            "results_must_be_contract_objects",
            _unchecked_response(status="success", results=(object(),)),
        ),
        (
            "issues_must_be_contract_objects",
            _unchecked_response(status="error", issues=(object(),)),
        ),
    ),
    ids=lambda value: value if isinstance(value, str) else None,
)
def test_validator_rejects_every_invariant_violation(
    case_name: str,
    response: SearchResponse,
) -> None:
    del case_name
    assert is_strict_search_response(response) is False


def test_validator_turns_nested_validation_exception_into_failure() -> None:
    class ExplodingScore:
        def __ge__(self, other: object) -> bool:
            del other
            raise RuntimeError("must not escape adapter validation")

    malformed_result = object.__new__(SearchResult)
    object.__setattr__(malformed_result, "knowledge_id", 1)
    object.__setattr__(malformed_result, "title", "Synthetic")
    object.__setattr__(malformed_result, "score", ExplodingScore())
    object.__setattr__(malformed_result, "highlight", "")
    object.__setattr__(malformed_result, "metadata", {})
    response = _unchecked_response(
        status="success",
        results=(malformed_result,),
    )

    assert is_strict_search_response(response) is False


def test_validator_rejects_structural_mock_instead_of_weakening_contract() -> None:
    class ResponseLookalike:
        status = "success"
        results = (RESULT,)
        issues = ()
        strategy = "bm25"

    assert is_strict_search_response(ResponseLookalike()) is False


class _StrSubclass(str):
    pass


class _DictSubclass(dict):
    pass


def _fresh_result() -> SearchResult:
    return SearchResult(1, "Synthetic result", 0.8, "synthetic", {})


def _fresh_issue() -> RetrievalIssue:
    return RetrievalIssue(
        code=ErrorCode.RETRIEVAL_BACKEND_FAILED,
        message="backend unavailable",
        stage="bm25_query",
        recoverable=True,
        cause_type="RuntimeError",
    )


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
def test_gui_mirror_rejects_every_corrupted_result_field(
    field: str,
    value: object,
) -> None:
    result = _fresh_result()
    response = SearchResponse.completed((result,), strategy="bm25")
    object.__setattr__(result, field, value)

    assert is_strict_search_response(response) is False
    assert is_neutral_strict_search_response(response) is False


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
def test_gui_mirror_rejects_every_corrupted_issue_field(
    field: str,
    value: object,
) -> None:
    issue = _fresh_issue()
    response = SearchResponse.failed_response(issue, strategy="bm25")
    object.__setattr__(issue, field, value)

    assert is_strict_search_response(response) is False
    assert is_neutral_strict_search_response(response) is False


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
def test_gui_mirror_rejects_unsafe_or_non_exact_strategy(strategy: object) -> None:
    response = SearchResponse.completed((_fresh_result(),), strategy="bm25")
    object.__setattr__(response, "strategy", strategy)

    assert is_strict_search_response(response) is False
    assert is_neutral_strict_search_response(response) is False


@pytest.mark.parametrize(
    "metadata",
    (
        {1: "non-string-key"},
        {"nested": {"value": object()}},
        {"nested": [float("nan")]},
        {"tags": ["valid", object()]},
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
def test_gui_mirror_rejects_invalid_or_inconsistent_metadata(
    metadata: dict[object, object],
) -> None:
    result = _fresh_result()
    response = SearchResponse.completed((result,), strategy="bm25")
    object.__setattr__(result, "metadata", metadata)

    assert is_strict_search_response(response) is False
    assert is_neutral_strict_search_response(response) is False


@pytest.mark.parametrize(
    "metadata",
    (
        {},
        {"word_count": None},
        {"word_count": 0},
        {"word_count": 5},
        {"knowledge_id": 1, "vector_distance": 0.25},
        {"nested": {"safe": [None, True, 1, 0.5, "value"]}},
    ),
)
def test_gui_mirror_accepts_valid_metadata_shape(
    metadata: dict[str, object],
) -> None:
    result = SearchResult(1, "Synthetic result", 0.8, "synthetic", metadata)
    response = SearchResponse.completed((result,), strategy="bm25")

    assert is_strict_search_response(response) is True
    assert is_neutral_strict_search_response(response) is True


def test_gui_mirror_rejects_cyclic_metadata_after_construction() -> None:
    result = _fresh_result()
    response = SearchResponse.completed((result,), strategy="bm25")
    metadata: dict[str, object] = {}
    metadata["cycle"] = metadata
    object.__setattr__(result, "metadata", metadata)

    assert is_strict_search_response(response) is False
    assert is_neutral_strict_search_response(response) is False
