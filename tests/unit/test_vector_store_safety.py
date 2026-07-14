"""
VectorStore 安全性回归测试
"""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pytest

from src.storage.vector_store import VectorStore


def _fake_config(base_url: str, model: str, dim: int):
    return SimpleNamespace(
        embedding_dim=dim,
        embd_base_url=base_url,
        embd_model=model,
        embedding_index_fingerprint=lambda resolved_dim: {
            "base_url": base_url,
            "embedding_model": model,
            "embedding_dim": str(int(resolved_dim)),
        },
    )


def test_vector_store_dimension_mismatch_does_not_rebuild_existing_index(
    tmp_path: Path,
):
    """维度不匹配时不应删除既有索引数据。"""
    vector_dir = tmp_path / "vectors"
    original_store = VectorStore(vector_dir, dim=4)
    original_store.add_doc_vector(knowledge_id=1, vector=np.ones(4, dtype=np.float32))
    original_store.add_chunk_vector(
        knowledge_id=1,
        chunk_index=0,
        vector=np.arange(4, dtype=np.float32),
    )

    with pytest.raises(RuntimeError, match="索引维度不匹配"):
        VectorStore(vector_dir, dim=8)

    recovered_store = VectorStore(vector_dir, dim=4)
    stats = recovered_store.get_index_stats()

    assert stats["doc_count"] == 1
    assert stats["chunk_count"] == 1
    assert recovered_store.get_chunk_indices_for_entry(1) == [0]


def test_vector_store_defaults_to_existing_index_dimension(tmp_path: Path):
    """未显式传入维度时应沿用已有索引维度。"""
    vector_dir = tmp_path / "vectors"
    original_store = VectorStore(vector_dir, dim=4)
    original_store.add_doc_vector(knowledge_id=7, vector=np.ones(4, dtype=np.float32))

    reopened_store = VectorStore(vector_dir)

    assert reopened_store.dim == 4
    assert reopened_store.get_index_stats()["doc_count"] == 1


def test_vector_store_persists_embedding_fingerprint(tmp_path: Path):
    """新索引元数据应记录非敏感 Embedding 契约指纹。"""
    vector_dir = tmp_path / "vectors"
    config = _fake_config("https://embd.example.com/v1", "model-a", 4)

    with patch("src.storage.vector_store.get_config", return_value=config):
        store = VectorStore(vector_dir, dim=4)

    metadata = json.loads((vector_dir / "doc_vectors_metadata.json").read_text())

    assert metadata["embedding_fingerprint"] == {
        "base_url": "https://embd.example.com/v1",
        "embedding_model": "model-a",
        "embedding_dim": "4",
    }
    assert store.get_index_stats()["embedding_fingerprint"]["embedding_model"] == "model-a"


def test_vector_store_rejects_same_dim_embedding_model_drift(tmp_path: Path):
    """同维度换 Embedding 模型也不能复用旧索引。"""
    vector_dir = tmp_path / "vectors"

    with patch(
        "src.storage.vector_store.get_config",
        return_value=_fake_config("https://embd.example.com/v1", "model-a", 4),
    ):
        VectorStore(vector_dir, dim=4)

    with patch(
        "src.storage.vector_store.get_config",
        return_value=_fake_config("https://embd.example.com/v1", "model-b", 4),
    ):
        with pytest.raises(RuntimeError, match="Embedding 索引契约不匹配"):
            VectorStore(vector_dir, dim=4)


def test_vector_store_rejects_same_model_embedding_endpoint_drift(tmp_path: Path):
    """同模型同维度但切换 Embedding 端点也不能静默复用旧索引。"""
    vector_dir = tmp_path / "vectors"

    with patch(
        "src.storage.vector_store.get_config",
        return_value=_fake_config("https://embd-a.example.com/v1", "model-a", 4),
    ):
        VectorStore(vector_dir, dim=4)

    with patch(
        "src.storage.vector_store.get_config",
        return_value=_fake_config("https://embd-b.example.com/v1", "model-a", 4),
    ):
        with pytest.raises(RuntimeError, match="Embedding 索引契约不匹配"):
            VectorStore(vector_dir, dim=4)


def test_vector_store_loads_legacy_metadata_without_embedding_fingerprint(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
):
    """旧索引缺少契约指纹时兼容加载，但必须可观测。"""
    vector_dir = tmp_path / "vectors"

    with patch(
        "src.storage.vector_store.get_config",
        return_value=_fake_config("https://embd.example.com/v1", "model-a", 4),
    ):
        VectorStore(vector_dir, dim=4)

    for metadata_path in vector_dir.glob("*_metadata.json"):
        metadata = json.loads(metadata_path.read_text())
        metadata.pop("embedding_fingerprint")
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    with patch(
        "src.storage.vector_store.get_config",
        return_value=_fake_config("https://embd.example.com/v1", "model-b", 4),
    ):
        store = VectorStore(vector_dir, dim=4)

    assert store.dim == 4
    assert "缺少 Embedding 契约指纹" in caplog.text
