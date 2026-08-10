"""W2 vector retrieval state, metadata, and lazy-provider contracts."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import Mock

import numpy as np
import pytest

from src.retrieval.result import SearchResponse
from src.retrieval.vector_retriever import VectorRetriever
from src.runtime.errors import ErrorCode
from src.storage.vector_store import VectorStore


FIXTURE_PATH = (
    Path(__file__).parent.parent / "fixtures" / "w2" / "retrieval" / "v1" / "contract.json"
)


class _Embedder:
    dim = 3

    def __init__(self, vector=None) -> None:
        self.vector = [1.0, 0.0, 0.0] if vector is None else vector
        self.calls: list[str] = []

    def embed_document(self, text: str):
        self.calls.append(text)
        return self.vector


def _retriever(tmp_path: Path, *, embedder=None, factory=None) -> VectorRetriever:
    return VectorRetriever(
        tmp_path / "knowledge.db",
        tmp_path / "vectors",
        embedder,
        embedder_factory=factory,
    )


def _store(*, doc_hits=(), chunk_hits=()) -> Mock:
    store = Mock()
    store.dim = 3
    store.search_doc.return_value = list(doc_hits)
    store.search_chunk.return_value = list(chunk_hits)
    return store


def _metadata(knowledge_id: int) -> dict[str, object]:
    return {
        "knowledge_id": knowledge_id,
        "title": f"entry-{knowledge_id}",
        "summary_one_sentence": f"summary-{knowledge_id}",
        "summary_100_words": "",
        "source_type": "test",
        "source_url": None,
        "tags": "",
        "keywords": "",
        "file_path": f"entry-{knowledge_id}.md",
        "archived_at": None,
        "updated_at": None,
    }


@pytest.mark.parametrize("query,limit", [("", 10), ("   ", 10), ("q", 0), ("q", True)])
def test_invalid_request_is_explicit_and_does_not_touch_provider(
    tmp_path: Path,
    query,
    limit,
) -> None:
    factory = Mock(side_effect=AssertionError("provider must stay lazy"))
    retriever = _retriever(tmp_path, factory=factory)

    response = retriever.search(query, limit)

    assert response.status == "invalid"
    assert response.error_code is ErrorCode.RETRIEVAL_INVALID_QUERY
    factory.assert_not_called()


def test_missing_index_is_error_and_keeps_provider_lazy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = Mock(side_effect=AssertionError("provider must stay lazy"))
    retriever = _retriever(tmp_path, factory=factory)
    monkeypatch.setattr(VectorStore, "has_index_artifacts", lambda path: False)

    response = retriever.search("semantic query")

    assert response.status == "error"
    assert response.error_code is ErrorCode.RETRIEVAL_INDEX_UNAVAILABLE
    assert response.results == ()
    factory.assert_not_called()


@pytest.mark.parametrize(
    "missing_pair,search_method",
    [
        ("doc_vectors", "search"),
        ("chunk_vectors", "search_chunks"),
    ],
)
def test_missing_target_pair_is_error_without_recreating_files_or_provider(
    tmp_path: Path,
    missing_pair: str,
    search_method: str,
) -> None:
    vector_dir = tmp_path / "vectors"
    store = VectorStore(vector_dir, dim=3)
    store.add_doc_vector(1, np.asarray([1.0, 0.0, 0.0], dtype=np.float32))
    store.add_chunk_vector(
        1,
        0,
        np.asarray([1.0, 0.0, 0.0], dtype=np.float32),
    )
    index_path = vector_dir / f"{missing_pair}.idx"
    metadata_path = vector_dir / f"{missing_pair}_metadata.json"
    index_path.unlink()
    metadata_path.unlink()
    factory = Mock(side_effect=AssertionError("provider must stay lazy"))
    retriever = VectorRetriever(
        tmp_path / "knowledge.db",
        vector_dir,
        embedder_factory=factory,
    )

    response = getattr(retriever, search_method)("semantic query")

    assert response.status == "error"
    assert response.error_code is ErrorCode.RETRIEVAL_INDEX_UNAVAILABLE
    assert response.issues[0].stage == "vector_index_pair_load"
    assert response.results == ()
    assert not index_path.exists()
    assert not metadata_path.exists()
    factory.assert_not_called()


def test_provider_factory_is_called_only_on_first_real_semantic_search(
    tmp_path: Path,
) -> None:
    embedder = _Embedder()
    factory = Mock(return_value=embedder)
    retriever = _retriever(tmp_path, factory=factory)
    retriever.vector_store = _store()

    first = retriever.search("first")
    second = retriever.search("second")

    assert first.status == "no_hits"
    assert second.status == "no_hits"
    factory.assert_called_once_with()
    assert embedder.calls == ["first", "second"]


def test_provider_failure_is_error_and_does_not_publish_raw_exception(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    canary = "CANARY_EMBEDDING_KEY"
    private_path = r"C:\\private\\vectors"
    factory = Mock(side_effect=RuntimeError(f"{canary} {private_path}"))
    retriever = _retriever(tmp_path, factory=factory)
    retriever.vector_store = _store()
    caplog.set_level(logging.ERROR, logger="src.retrieval.vector_retriever")

    response = retriever.search("semantic query")

    assert response.status == "error"
    assert response.error_code is ErrorCode.PROVIDER_UNAVAILABLE
    assert response.error_message == "Embedding Provider 不可用"
    assert canary not in repr(response.to_dict())
    assert private_path not in repr(response.to_dict())
    assert "RuntimeError" in caplog.text
    assert canary not in caplog.text
    assert private_path not in caplog.text


@pytest.mark.parametrize(
    "provider_vector",
    [
        None,
        [1.0, 0.0],
        [0.0, 0.0, 0.0],
        [1e-30, 0.0, 0.0],
        [3e38, 3e38, 3e38],
        ["not", "numeric", "values"],
        [True, False, False],
        [1.0, float("nan"), 0.0],
        [1e300, 0.0, 0.0],
        [1.0 + 1.0j, 0.0, 0.0],
        np.asarray([1.0, 0.0, 0.0], dtype=object),
        np.asarray([[1.0, 0.0, 0.0]], dtype=np.float32),
    ],
)
@pytest.mark.parametrize(
    ("search_method", "store_method", "expected_strategy"),
    [
        ("search", "search_doc", "vector"),
        ("search_chunks", "search_chunk", "vector_chunks"),
    ],
)
def test_invalid_provider_output_has_protocol_error_before_index_search(
    tmp_path: Path,
    provider_vector,
    search_method: str,
    store_method: str,
    expected_strategy: str,
) -> None:
    retriever = _retriever(tmp_path, embedder=_Embedder())
    retriever.embedder.vector = provider_vector
    retriever.vector_store = _store()

    response = getattr(retriever, search_method)("semantic query")

    assert response.status == "error"
    assert response.strategy == expected_strategy
    assert response.error_code is ErrorCode.PROVIDER_PROTOCOL_FAILED
    assert response.error_message == "Embedding Provider 返回无效响应"
    assert response.issues[0].stage == "embedding_protocol"
    getattr(retriever.vector_store, store_method).assert_not_called()


@pytest.mark.parametrize("declared_dim", [None, "auto"])
def test_auto_dimension_still_binds_query_shape_to_loaded_index(
    tmp_path: Path,
    declared_dim,
) -> None:
    embedder = _Embedder([1.0, 0.0])
    embedder.dim = declared_dim
    retriever = _retriever(tmp_path, embedder=embedder)
    retriever.vector_store = _store()

    response = retriever.search("semantic query")

    assert response.status == "error"
    assert response.error_code is ErrorCode.PROVIDER_PROTOCOL_FAILED
    assert response.issues[0].stage == "embedding_protocol"
    assert embedder.calls == ["semantic query"]
    retriever.vector_store.search_doc.assert_not_called()


@pytest.mark.parametrize("declared_dim", [3, None, "auto"])
def test_fixed_and_auto_dimensions_preserve_valid_float32_query(
    tmp_path: Path,
    declared_dim,
) -> None:
    provider_vector = np.asarray([1.0, 0.0, 0.0], dtype=np.float64)
    embedder = _Embedder(provider_vector)
    embedder.dim = declared_dim
    retriever = _retriever(tmp_path, embedder=embedder)
    retriever.vector_store = _store()

    response = retriever.search("semantic query")

    assert response.status == "no_hits"
    passed_vector = retriever.vector_store.search_doc.call_args.args[0]
    assert passed_vector.dtype == np.float32
    assert passed_vector.flags.c_contiguous
    assert not np.shares_memory(passed_vector, provider_vector)
    np.testing.assert_array_equal(passed_vector, [1.0, 0.0, 0.0])


def test_invalid_query_vector_error_does_not_publish_raw_provider_value(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    canary = "CANARY_QUERY_VECTOR_SECRET"

    class _ExplodingVector:
        def __array__(self, dtype=None):
            raise RuntimeError(canary)

    retriever = _retriever(tmp_path, embedder=_Embedder(_ExplodingVector()))
    retriever.vector_store = _store()
    caplog.set_level(logging.ERROR, logger="src.retrieval.vector_retriever")

    response = retriever.search("semantic query")

    assert response.status == "error"
    assert response.error_code is ErrorCode.PROVIDER_PROTOCOL_FAILED
    assert response.issues[0].stage == "embedding_protocol"
    assert canary not in repr(response.to_dict())
    assert canary not in caplog.text
    retriever.vector_store.search_doc.assert_not_called()


def test_provider_dimension_mismatch_is_protocol_error_with_specific_safe_message(
    tmp_path: Path,
) -> None:
    embedder = _Embedder()
    embedder.dim = 4
    retriever = _retriever(tmp_path, embedder=embedder)
    retriever.vector_store = _store()

    response = retriever.search("semantic query")

    assert response.status == "error"
    assert response.error_code is ErrorCode.PROVIDER_PROTOCOL_FAILED
    assert response.error_message == "Embedding Provider 响应与向量索引不兼容"
    assert response.issues[0].stage == "embedding_protocol"
    assert embedder.calls == []


def test_partial_metadata_mapping_is_degraded(tmp_path: Path) -> None:
    retriever = _retriever(tmp_path, embedder=_Embedder())
    retriever.vector_store = _store(doc_hits=[(1, 0.1), (2, 0.2)])
    retriever._get_metadata = Mock(side_effect=[_metadata(1), None])

    response = retriever.search("semantic query", limit=2)

    assert response.status == "degraded"
    assert [result.knowledge_id for result in response.results] == [1]
    assert response.error_code is ErrorCode.RETRIEVAL_METADATA_INCONSISTENT
    assert response.results[0].score == pytest.approx(0.9)


def test_all_vector_hits_without_metadata_are_error(tmp_path: Path) -> None:
    retriever = _retriever(tmp_path, embedder=_Embedder())
    retriever.vector_store = _store(doc_hits=[(99, 0.1)])
    retriever._get_metadata = Mock(return_value=None)

    response = retriever.search("semantic query")

    assert response.status == "error"
    assert response.error_code is ErrorCode.RETRIEVAL_METADATA_INCONSISTENT
    assert response.results == ()


def test_metadata_backend_exception_is_observable_and_redacted(tmp_path: Path) -> None:
    canary = "CANARY_DB_SECRET"
    retriever = _retriever(tmp_path, embedder=_Embedder())
    retriever.vector_store = _store(doc_hits=[(1, 0.1)])
    retriever._get_metadata = Mock(side_effect=RuntimeError(canary))

    response = retriever.search("semantic query")

    assert response.status == "error"
    assert response.error_code is ErrorCode.RETRIEVAL_BACKEND_FAILED
    assert response.error_message == "向量命中元数据读取失败"
    assert canary not in repr(response.to_dict())


def test_index_search_exception_is_error_not_no_hits(tmp_path: Path) -> None:
    retriever = _retriever(tmp_path, embedder=_Embedder())
    retriever.vector_store = _store()
    retriever.vector_store.search_doc.side_effect = RuntimeError("index corrupted")

    response = retriever.search("semantic query")

    assert response.status == "error"
    assert response.error_code is ErrorCode.RETRIEVAL_BACKEND_FAILED
    assert response.results == ()


@pytest.mark.parametrize(
    "backend_value",
    (None, False, {}, (), ((item for item in ()),)),
)
def test_document_backend_only_exact_list_can_represent_hits_or_no_hits(
    tmp_path: Path,
    backend_value,
) -> None:
    retriever = _retriever(tmp_path, embedder=_Embedder())
    retriever.vector_store = _store()
    if type(backend_value) is tuple and len(backend_value) == 1:
        backend_value = backend_value[0]
    retriever.vector_store.search_doc.return_value = backend_value

    response = retriever.search("semantic query")

    assert response.status == "error"
    assert response.error_code is ErrorCode.RETRIEVAL_METADATA_INCONSISTENT
    assert response.results == ()


@pytest.mark.parametrize(
    "raw_hit",
    (
        [1, 0.1],
        (True, 0.1),
        ("1", 0.1),
        (0, 0.1),
        (1, True),
        (1, float("nan")),
        (1, float("inf")),
        (1, 0.1, "extra"),
    ),
)
def test_document_hit_shape_is_exact_and_never_coerces_ids(
    tmp_path: Path,
    raw_hit,
) -> None:
    retriever = _retriever(tmp_path, embedder=_Embedder())
    retriever.vector_store = _store()
    retriever.vector_store.search_doc.return_value = [raw_hit]
    retriever._get_metadata = Mock(
        side_effect=AssertionError("invalid hits must not reach metadata")
    )

    response = retriever.search("semantic query")

    assert response.status == "error"
    assert response.error_code is ErrorCode.RETRIEVAL_METADATA_INCONSISTENT
    retriever._get_metadata.assert_not_called()


def test_mixed_valid_and_invalid_document_hits_are_degraded(tmp_path: Path) -> None:
    retriever = _retriever(tmp_path, embedder=_Embedder())
    retriever.vector_store = _store()
    retriever.vector_store.search_doc.return_value = [(1, 0.1), (True, 0.2)]
    retriever._get_metadata = Mock(return_value=_metadata(1))

    response = retriever.search("semantic query", limit=2)

    assert response.status == "degraded"
    assert [item.knowledge_id for item in response.results] == [1]
    assert response.error_code is ErrorCode.RETRIEVAL_METADATA_INCONSISTENT


@pytest.mark.parametrize(
    "metadata",
    (None, [], {"knowledge_id": True}, {"knowledge_id": 2}),
)
def test_document_metadata_shape_and_identity_must_match_hit(
    tmp_path: Path,
    metadata,
) -> None:
    retriever = _retriever(tmp_path, embedder=_Embedder())
    retriever.vector_store = _store(doc_hits=[(1, 0.1)])
    retriever._get_metadata = Mock(return_value=metadata)

    response = retriever.search("semantic query")

    assert response.status == "error"
    assert response.error_code is ErrorCode.RETRIEVAL_METADATA_INCONSISTENT
    assert response.results == ()


def test_chunk_search_uses_same_response_contract(tmp_path: Path) -> None:
    retriever = _retriever(tmp_path, embedder=_Embedder())
    retriever.vector_store = _store(chunk_hits=[(1, 0, 0.05)])
    chunk_metadata = _metadata(1) | {
        "chunk_id": 10,
        "chunk_index": 0,
        "chunk_text": "deterministic chunk",
    }
    retriever._get_chunk_metadata = Mock(return_value=chunk_metadata)

    response = retriever.search_chunks("chunk", limit=1)

    assert isinstance(response, SearchResponse)
    assert response.status == "success"
    assert response.strategy == "vector_chunks"
    assert response.results[0].metadata["chunk_index"] == 0
    assert response.results[0].highlight == "deterministic chunk"


@pytest.mark.parametrize(
    "backend_value",
    (None, False, {}, (), ((item for item in ()),)),
)
def test_chunk_backend_only_exact_list_can_represent_hits_or_no_hits(
    tmp_path: Path,
    backend_value,
) -> None:
    retriever = _retriever(tmp_path, embedder=_Embedder())
    retriever.vector_store = _store()
    if type(backend_value) is tuple and len(backend_value) == 1:
        backend_value = backend_value[0]
    retriever.vector_store.search_chunk.return_value = backend_value

    response = retriever.search_chunks("semantic query")

    assert response.status == "error"
    assert response.error_code is ErrorCode.RETRIEVAL_METADATA_INCONSISTENT
    assert response.results == ()


@pytest.mark.parametrize(
    "raw_hit",
    (
        [1, 0, 0.1],
        (True, 0, 0.1),
        ("1", 0, 0.1),
        (1, True, 0.1),
        (1, -1, 0.1),
        (1, 0, True),
        (1, 0, float("nan")),
        (1, 0, 0.1, "extra"),
    ),
)
def test_chunk_hit_shape_is_exact_and_never_coerces_ids(
    tmp_path: Path,
    raw_hit,
) -> None:
    retriever = _retriever(tmp_path, embedder=_Embedder())
    retriever.vector_store = _store()
    retriever.vector_store.search_chunk.return_value = [raw_hit]
    retriever._get_chunk_metadata = Mock(
        side_effect=AssertionError("invalid hits must not reach metadata")
    )

    response = retriever.search_chunks("semantic query")

    assert response.status == "error"
    assert response.error_code is ErrorCode.RETRIEVAL_METADATA_INCONSISTENT
    retriever._get_chunk_metadata.assert_not_called()


@pytest.mark.parametrize(
    "metadata",
    (
        None,
        [],
        {"knowledge_id": 1, "chunk_id": 10, "chunk_index": 1},
        {"knowledge_id": 2, "chunk_id": 10, "chunk_index": 0},
        {"knowledge_id": 1, "chunk_id": True, "chunk_index": 0},
    ),
)
def test_chunk_metadata_shape_and_identity_must_match_hit(
    tmp_path: Path,
    metadata,
) -> None:
    retriever = _retriever(tmp_path, embedder=_Embedder())
    retriever.vector_store = _store(chunk_hits=[(1, 0, 0.1)])
    retriever._get_chunk_metadata = Mock(return_value=metadata)

    response = retriever.search_chunks("semantic query")

    assert response.status == "error"
    assert response.error_code is ErrorCode.RETRIEVAL_METADATA_INCONSISTENT
    assert response.results == ()


@pytest.mark.parametrize(
    "corruption",
    [
        "missing",
        "not_dict",
        "empty",
        "partial_missing",
        "extra_missing_index",
        "partial_malformed",
        "all_malformed",
    ],
)
def test_real_chunk_mapping_corruption_is_error_not_no_hits_or_success(
    tmp_path: Path,
    corruption: str,
) -> None:
    vector_dir = tmp_path / "vectors"
    store = VectorStore(vector_dir, dim=3)
    first = np.asarray([1.0, 0.0, 0.0], dtype=np.float32)
    second = np.asarray([0.0, 1.0, 0.0], dtype=np.float32)
    third = np.asarray([0.0, 0.0, 1.0], dtype=np.float32)
    store.add_chunk_vector(1, 0, first)
    store.add_chunk_vector(2, 0, second)
    store.add_chunk_vector(3, 0, third)

    metadata_path = vector_dir / "chunk_vectors_metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    mapping = metadata["id_mapping"]
    labels = sorted(mapping)
    if corruption == "missing":
        metadata.pop("id_mapping")
    elif corruption == "not_dict":
        metadata["id_mapping"] = []
    elif corruption == "empty":
        metadata["id_mapping"] = {}
    elif corruption == "partial_missing":
        # 删除与 query 最远的 active label；即使 limit=1 也必须检测到漂移。
        mapping.pop(labels[-1])
    elif corruption == "extra_missing_index":
        extra_label = VectorStore.encode_chunk_id(4, 0)
        mapping[str(extra_label)] = [4, 0]
    elif corruption == "partial_malformed":
        mapping[labels[0]] = ["not-an-integer", 0]
    else:
        metadata["id_mapping"] = {
            label: ["not-an-integer", 0] for label in labels
        }
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    embedder = _Embedder(first)
    retriever = VectorRetriever(
        tmp_path / "knowledge.db",
        vector_dir,
        embedder,
    )

    response = retriever.search_chunks(
        "chunk query",
        limit=1 if corruption in {"partial_missing", "extra_missing_index"} else 2,
    )

    assert response.status == "error"
    assert response.error_code is ErrorCode.RETRIEVAL_METADATA_INCONSISTENT
    assert response.error_message == "chunk 向量索引查询失败"
    assert response.issues[0].stage == "chunk_index_metadata"
    assert response.results == ()
    assert embedder.calls == ["chunk query"]


def test_versioned_fixture_drives_fixed_vector_hit_mapping(tmp_path: Path) -> None:
    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    corpus = {item["knowledge_id"]: item for item in fixture["corpus"]}
    hits = fixture["ranked_inputs"]["vector_hits"]
    retriever = _retriever(
        tmp_path,
        embedder=_Embedder(fixture["corpus"][0]["vector"]),
    )
    retriever.vector_store = _store(doc_hits=[tuple(hit) for hit in hits])
    retriever._get_metadata = Mock(
        side_effect=lambda knowledge_id: {
            **_metadata(knowledge_id),
            "title": corpus[knowledge_id]["title"],
        }
    )

    response = retriever.search("alpha beta", limit=2)

    assert response.status == "success"
    assert [item.knowledge_id for item in response.results] == [303, 202]
    assert [item.title for item in response.results] == [
        "Alpha Beta Bridge",
        "Beta Graphs",
    ]
