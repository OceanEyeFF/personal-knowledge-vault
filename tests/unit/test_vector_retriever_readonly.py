"""R4 regression: semantic retrieval must use the strict zero-write opener."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np

from src.retrieval.vector_retriever import VectorRetriever
from src.runtime.errors import ErrorCode
from src.storage.vector_store import VectorStore
from src.utils.config import endpoint_contract_sha256


def _config(dim: int = 3) -> SimpleNamespace:
    endpoint = "https://embedding-r4.example.test/v1"
    return SimpleNamespace(
        embedding_dim=dim,
        embd_base_url=endpoint,
        embd_model="embedding-r4",
        embedding_index_fingerprint=lambda resolved_dim: {
            "base_url_sha256": endpoint_contract_sha256(endpoint),
            "embedding_model": "embedding-r4",
            "embedding_dim": str(int(resolved_dim)),
        },
    )


def _snapshot(vector_dir: Path) -> tuple[tuple[int, int], dict[str, tuple[bytes, int]]]:
    directory = vector_dir.stat()
    return (
        (directory.st_mtime_ns, directory.st_ctime_ns),
        {
            path.name: (path.read_bytes(), path.stat().st_mtime_ns)
            for path in sorted(vector_dir.iterdir())
            if path.is_file()
        },
    )


def _remove_writer_locks(vector_dir: Path) -> None:
    for lock_path in vector_dir.glob(".*.lock"):
        lock_path.unlink()


def test_vector_retriever_loads_published_index_without_creating_writer_sidecars(
    tmp_path: Path,
) -> None:
    config = _config()
    vector_dir = tmp_path / "vectors"
    writer = VectorStore(vector_dir, dim=3, runtime_config=config)
    writer.add_doc_vector(1, np.asarray([1.0, 0.0, 0.0], dtype=np.float32))
    writer.add_chunk_vector(1, 0, np.asarray([1.0, 0.0, 0.0], dtype=np.float32))
    _remove_writer_locks(vector_dir)
    before = _snapshot(vector_dir)
    factory = Mock(side_effect=AssertionError("a strict index open must stay provider-lazy"))
    retriever = VectorRetriever(
        tmp_path / "knowledge.db",
        vector_dir,
        embedder_factory=factory,
        runtime_config=config,
    )

    loaded = retriever._load_vector_store(strategy="vector")

    assert isinstance(loaded, VectorStore)
    assert loaded._read_only is True
    assert loaded.search_chunk(
        np.asarray([1.0, 0.0, 0.0], dtype=np.float32),
        k=1,
    ) == [(1, 0, 0.0)]
    assert _snapshot(vector_dir) == before
    assert list(vector_dir.glob(".*.lock")) == []
    assert list(vector_dir.glob(".*.pair-transaction.json")) == []
    factory.assert_not_called()


def test_vector_retriever_rejects_legacy_metadata_without_migrating_or_creating_locks(
    tmp_path: Path,
) -> None:
    config = _config()
    vector_dir = tmp_path / "vectors"
    writer = VectorStore(vector_dir, dim=3, runtime_config=config)
    writer.add_doc_vector(1, np.asarray([1.0, 0.0, 0.0], dtype=np.float32))
    writer.add_chunk_vector(1, 0, np.asarray([1.0, 0.0, 0.0], dtype=np.float32))
    for name in VectorStore.PAIR_NAMES:
        metadata_path = vector_dir / f"{name}_metadata.json"
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata.pop("schema_version")
        metadata.pop(VectorStore.EMBEDDING_FINGERPRINT_V2_KEY)
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    _remove_writer_locks(vector_dir)
    before = _snapshot(vector_dir)
    factory = Mock(side_effect=AssertionError("legacy metadata must fail before provider creation"))
    retriever = VectorRetriever(
        tmp_path / "knowledge.db",
        vector_dir,
        embedder_factory=factory,
        runtime_config=config,
    )

    response = retriever.search("semantic query")

    assert response.status == "error"
    assert response.error_code is ErrorCode.RETRIEVAL_METADATA_INCONSISTENT
    assert response.issues[0].stage == "vector_index_metadata"
    assert _snapshot(vector_dir) == before
    assert list(vector_dir.glob(".*.lock")) == []
    factory.assert_not_called()
