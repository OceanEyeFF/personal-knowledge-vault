"""
VectorStore 安全性回归测试
"""

import sys
from pathlib import Path

project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

import numpy as np
import pytest

from src.storage.vector_store import VectorStore


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
