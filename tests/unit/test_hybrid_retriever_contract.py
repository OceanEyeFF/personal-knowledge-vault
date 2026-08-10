"""W2 hybrid branch aggregation contracts."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from src.retrieval.hybrid_retriever import HybridRetriever
from src.retrieval.result import RetrievalIssue, SearchResponse, SearchResult
from src.runtime.errors import ErrorCode


FIXTURE_PATH = (
    Path(__file__).parent.parent / "fixtures" / "w2" / "retrieval" / "v1" / "contract.json"
)


def _result(knowledge_id: int, *, source: str) -> SearchResult:
    return SearchResult(
        knowledge_id=knowledge_id,
        title=f"entry-{knowledge_id}",
        score=0.8,
        highlight="",
        metadata={"source": source},
    )


def _error(strategy: str, message: str) -> SearchResponse:
    return SearchResponse.failed_response(
        RetrievalIssue(
            code=ErrorCode.RETRIEVAL_BACKEND_FAILED,
            message=message,
            stage=f"{strategy}_backend",
            recoverable=True,
        ),
        strategy=strategy,
    )


def _fixture_response(status: str, strategy: str, knowledge_id: int) -> SearchResponse:
    if status == "success":
        return SearchResponse.completed(
            [_result(knowledge_id, source=strategy)],
            strategy=strategy,
        )
    if status == "no_hits":
        return SearchResponse.completed((), strategy=strategy)
    if status == "error":
        return _error(strategy, f"{strategy} fixture failure")
    raise AssertionError(f"fixture status 不受支持: {status}")


def _matrix_response(state: str, strategy: str, knowledge_id: int) -> SearchResponse:
    if state == "success":
        return SearchResponse.completed(
            (_result(knowledge_id, source=strategy),),
            strategy=strategy,
        )
    if state == "no_hits":
        return SearchResponse.completed((), strategy=strategy)
    if state == "invalid":
        return SearchResponse.invalid("invalid query", strategy=strategy)
    if state == "error":
        return _error(strategy, f"{strategy} failure")
    issue = RetrievalIssue(
        code=ErrorCode.RETRIEVAL_INDEX_UNAVAILABLE,
        message=f"{strategy} incomplete",
        stage=f"{strategy}_incomplete",
        recoverable=True,
    )
    if state == "degraded_results":
        return SearchResponse.degraded_response(
            (_result(knowledge_id, source=strategy),),
            (issue,),
            strategy=strategy,
        )
    if state == "degraded_empty":
        return SearchResponse.degraded_response((), (issue,), strategy=strategy)
    raise AssertionError(f"matrix state 不受支持: {state}")


_HYBRID_CARTESIAN_EXPECTED = {
    "success": {
        "success": "success",
        "no_hits": "success",
        "invalid": "degraded",
        "error": "degraded",
        "degraded_results": "degraded",
        "degraded_empty": "degraded",
    },
    "no_hits": {
        "success": "success",
        "no_hits": "no_hits",
        "invalid": "degraded",
        "error": "degraded",
        "degraded_results": "degraded",
        "degraded_empty": "degraded",
    },
    "invalid": {
        "success": "degraded",
        "no_hits": "degraded",
        "invalid": "invalid",
        "error": "error",
        "degraded_results": "degraded",
        "degraded_empty": "degraded",
    },
    "error": {
        "success": "degraded",
        "no_hits": "degraded",
        "invalid": "error",
        "error": "error",
        "degraded_results": "degraded",
        "degraded_empty": "degraded",
    },
    "degraded_results": {
        "success": "degraded",
        "no_hits": "degraded",
        "invalid": "degraded",
        "error": "degraded",
        "degraded_results": "degraded",
        "degraded_empty": "degraded",
    },
    "degraded_empty": {
        "success": "degraded",
        "no_hits": "degraded",
        "invalid": "degraded",
        "error": "degraded",
        "degraded_results": "degraded",
        "degraded_empty": "degraded",
    },
}


@pytest.fixture
def retriever() -> HybridRetriever:
    instance = HybridRetriever.__new__(HybridRetriever)
    instance.bm25_retriever = Mock()
    instance.vector_retriever = Mock()
    instance.bm25_weight = 0.4
    instance.vector_weight = 0.6
    instance.rrf_k = 60
    return instance


def test_both_healthy_branches_return_success(retriever: HybridRetriever) -> None:
    retriever.bm25_retriever.search.return_value = SearchResponse.completed(
        [_result(1, source="bm25")],
        strategy="bm25",
    )
    retriever.vector_retriever.search.return_value = SearchResponse.completed(
        [_result(2, source="vector")],
        strategy="vector",
    )

    response = retriever.search("alpha beta", limit=2)

    assert response.status == "success"
    assert response.strategy == "hybrid"
    assert {item.knowledge_id for item in response.results} == {1, 2}
    assert response.issues == ()


def test_both_healthy_no_hits_return_no_hits(retriever: HybridRetriever) -> None:
    retriever.bm25_retriever.search.return_value = SearchResponse.completed(
        (), strategy="bm25"
    )
    retriever.vector_retriever.search.return_value = SearchResponse.completed(
        (), strategy="vector"
    )

    response = retriever.search("alpha beta")

    assert response.status == "no_hits"
    assert response.results == ()


def test_hybrid_status_cartesian_matrix_preserves_usable_partial_results(
    retriever: HybridRetriever,
) -> None:
    result_states = {"success", "degraded_results"}

    for bm25_state, expected_row in _HYBRID_CARTESIAN_EXPECTED.items():
        for vector_state, expected_status in expected_row.items():
            retriever.bm25_retriever.search.return_value = _matrix_response(
                bm25_state,
                "bm25",
                101,
            )
            retriever.vector_retriever.search.return_value = _matrix_response(
                vector_state,
                "vector",
                202,
            )

            response = retriever.search("alpha beta", limit=3)
            case_id = f"bm25={bm25_state},vector={vector_state}"

            assert response.status == expected_status, case_id
            expected_ids = set()
            if bm25_state in result_states:
                expected_ids.add(101)
            if vector_state in result_states:
                expected_ids.add(202)
            assert {item.knowledge_id for item in response.results} == expected_ids, case_id

            if expected_status == "degraded":
                assert response.issues, case_id
            elif expected_status in {"success", "no_hits"}:
                assert response.issues == (), case_id


@pytest.mark.parametrize("healthy_has_results", [True, False])
def test_single_branch_failure_is_degraded_even_when_other_branch_has_no_hits(
    retriever: HybridRetriever,
    healthy_has_results: bool,
) -> None:
    bm25_results = [_result(1, source="bm25")] if healthy_has_results else []
    retriever.bm25_retriever.search.return_value = SearchResponse.completed(
        bm25_results,
        strategy="bm25",
    )
    retriever.vector_retriever.search.return_value = _error(
        "vector", "向量分支不可用"
    )

    response = retriever.search("alpha beta")

    assert response.status == "degraded"
    assert response.strategy == "hybrid"
    assert response.error_code is ErrorCode.RETRIEVAL_BACKEND_FAILED
    assert response.results == tuple(bm25_results)


def test_degraded_branch_partial_results_are_not_discarded(
    retriever: HybridRetriever,
) -> None:
    retriever.bm25_retriever.search.return_value = SearchResponse.completed(
        [_result(1, source="bm25")],
        strategy="bm25",
    )
    retriever.vector_retriever.search.return_value = SearchResponse.degraded_response(
        [_result(2, source="vector")],
        [
            RetrievalIssue(
                code=ErrorCode.RETRIEVAL_METADATA_INCONSISTENT,
                message="部分向量命中缺少元数据",
                stage="vector_metadata_mapping",
                recoverable=True,
            )
        ],
        strategy="vector",
    )

    response = retriever.search("alpha beta", limit=2)

    assert response.status == "degraded"
    assert {item.knowledge_id for item in response.results} == {1, 2}
    assert response.error_code is ErrorCode.RETRIEVAL_METADATA_INCONSISTENT


def test_both_branch_failures_return_error_with_deterministic_issue_order(
    retriever: HybridRetriever,
) -> None:
    retriever.bm25_retriever.search.return_value = _error("bm25", "BM25 不可用")
    retriever.vector_retriever.search.return_value = _error("vector", "向量不可用")

    response = retriever.search("alpha beta")

    assert response.status == "error"
    assert response.results == ()
    assert [issue.stage for issue in response.issues] == [
        "bm25_backend",
        "vector_backend",
    ]


def test_raised_branch_exception_becomes_degraded_not_silent_empty(
    retriever: HybridRetriever,
) -> None:
    retriever.bm25_retriever.search.return_value = SearchResponse.completed(
        [_result(1, source="bm25")],
        strategy="bm25",
    )
    retriever.vector_retriever.search.side_effect = RuntimeError("provider outage")

    response = retriever.search("alpha beta")

    assert response.status == "degraded"
    assert response.results[0].knowledge_id == 1
    assert response.error_code is ErrorCode.RETRIEVAL_BACKEND_FAILED
    assert response.error_message == "vector 检索分支不可用"


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
def test_malformed_branch_result_is_never_fused_or_reported_as_success(
    retriever: HybridRetriever,
    field: str,
    value: object,
) -> None:
    malformed_result = _result(1, source="bm25")
    malformed_response = SearchResponse.completed(
        (malformed_result,),
        strategy="bm25",
    )
    object.__setattr__(malformed_result, field, value)
    retriever.bm25_retriever.search.return_value = malformed_response
    retriever.vector_retriever.search.return_value = SearchResponse.completed(
        (_result(2, source="vector"),),
        strategy="vector",
    )
    original_combine = retriever._combine_results
    retriever._combine_results = Mock(wraps=original_combine)

    response = retriever.search("alpha beta", limit=2)

    assert response.status == "degraded"
    assert [item.knowledge_id for item in response.results] == [2]
    assert response.error_code is ErrorCode.RETRIEVAL_BACKEND_FAILED
    assert response.error_type == "InvalidSearchResponse"
    passed_bm25_results = retriever._combine_results.call_args.args[0]
    assert passed_bm25_results == ()


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
def test_malformed_branch_issue_becomes_explicit_degradation(
    retriever: HybridRetriever,
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
    retriever.bm25_retriever.search.return_value = malformed_response
    retriever.vector_retriever.search.return_value = SearchResponse.completed(
        (),
        strategy="vector",
    )

    response = retriever.search("alpha beta")

    assert response.status == "degraded"
    assert response.results == ()
    assert response.error_code is ErrorCode.RETRIEVAL_BACKEND_FAILED
    assert response.error_type == "InvalidSearchResponse"


def test_branch_with_wrong_strategy_becomes_degraded_not_success(
    retriever: HybridRetriever,
) -> None:
    retriever.bm25_retriever.search.return_value = SearchResponse.completed(
        (_result(1, source="bm25"),),
        strategy="vector",
    )
    retriever.vector_retriever.search.return_value = SearchResponse.completed(
        (_result(2, source="vector"),),
        strategy="vector",
    )

    response = retriever.search("alpha beta", limit=2)

    assert response.status == "degraded"
    assert [item.knowledge_id for item in response.results] == [2]
    assert response.error_type == "SearchStrategyMismatch"


def test_both_malformed_branches_return_error(
    retriever: HybridRetriever,
) -> None:
    malformed_bm25 = SearchResponse.completed(
        (_result(1, source="bm25"),),
        strategy="bm25",
    )
    object.__setattr__(malformed_bm25.results[0], "knowledge_id", True)
    malformed_vector = SearchResponse.completed(
        (_result(2, source="vector"),),
        strategy="vector",
    )
    object.__setattr__(malformed_vector, "strategy", "vector\r\nCANARY")
    retriever.bm25_retriever.search.return_value = malformed_bm25
    retriever.vector_retriever.search.return_value = malformed_vector

    response = retriever.search("alpha beta", limit=2)

    assert response.status == "error"
    assert response.results == ()
    assert [issue.stage for issue in response.issues] == [
        "hybrid_bm25_protocol",
        "hybrid_vector_protocol",
    ]


def test_fusion_exception_is_error_not_escaped_or_empty(
    retriever: HybridRetriever,
) -> None:
    retriever.bm25_retriever.search.return_value = SearchResponse.completed(
        [_result(1, source="bm25")],
        strategy="bm25",
    )
    retriever.vector_retriever.search.return_value = SearchResponse.completed(
        [_result(2, source="vector")],
        strategy="vector",
    )
    retriever._combine_results = Mock(side_effect=RuntimeError("fusion canary"))

    response = retriever.search("alpha beta")

    assert response.status == "error"
    assert response.error_code is ErrorCode.RETRIEVAL_BACKEND_FAILED
    assert response.error_message == "混合检索结果融合失败"
    assert "fusion canary" not in repr(response.to_dict())


@pytest.mark.parametrize("query,limit", [("", 10), ("  ", 10), ("q", 0)])
def test_invalid_request_skips_both_branches(
    retriever: HybridRetriever,
    query,
    limit,
) -> None:
    response = retriever.search(query, limit)

    assert response.status == "invalid"
    retriever.bm25_retriever.search.assert_not_called()
    retriever.vector_retriever.search.assert_not_called()


def test_constructor_keeps_embedder_factory_lazy(tmp_path: Path) -> None:
    factory = Mock(side_effect=AssertionError("factory must stay lazy"))

    HybridRetriever(
        tmp_path / "knowledge.db",
        tmp_path / "vectors",
        embedder_factory=factory,
    )

    factory.assert_not_called()


def test_versioned_fixture_drives_hybrid_status_matrix(
    retriever: HybridRetriever,
) -> None:
    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    assert fixture["schema_version"] == "pkv.retrieval.fixture.v1"
    assert [item["knowledge_id"] for item in fixture["corpus"]] == [101, 202, 303]
    assert all(
        len(item["vector"]) == fixture["embedding_dimension"]
        for item in fixture["corpus"]
    )

    for case in fixture["hybrid_status_matrix"]:
        retriever.bm25_retriever.reset_mock()
        retriever.vector_retriever.reset_mock()
        retriever.bm25_retriever.search.return_value = _fixture_response(
            case["bm25"], "bm25", 101
        )
        retriever.vector_retriever.search.return_value = _fixture_response(
            case["vector"], "vector", 303
        )

        response = retriever.search("alpha beta", limit=3)

        assert response.status == case["expected"], case["case_id"]
