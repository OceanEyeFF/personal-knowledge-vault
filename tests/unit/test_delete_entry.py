"""
Unit tests for delete_entry functionality across storage layers.

覆盖:
- SQLiteStore.delete_entry() 及级联删除、标签计数、FTS5 清理
- VectorStore.delete_vectors_for_entry() 及 hnswlib mark_deleted
"""

import sys
from pathlib import Path

# Add project root to Python path
PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import numpy as np
import pytest

from src.storage.markdown_store import Entry
from src.storage.sqlite_store import SQLiteStore
from src.storage.vector_store import VectorStore


# ============================================================
# Fixtures
# ============================================================

@pytest.fixture
def store(tmp_path: Path) -> SQLiteStore:
    """Create a fresh SQLiteStore for testing."""
    db_path = tmp_path / "test.db"
    s = SQLiteStore(db_path)
    s.initialize()
    return s


@pytest.fixture
def store_with_entry(store: SQLiteStore) -> tuple:
    """创建包含一个测试条目的 store，返回 (store, knowledge_id)。"""
    entry = Entry(
        title="测试删除条目",
        source_type="wechat",
        source_url="https://example.com/delete-test",
        tags=["AI", "测试"],
        keywords=["test"],
        content="# 测试内容\n\n待删除的内容",
        word_count=10,
    )
    kid = store.insert_entry(entry, "wechat/2026/02/20260219-test.md")
    return store, kid


@pytest.fixture
def vector_store(tmp_path: Path) -> VectorStore:
    """Create a fresh VectorStore with dim=4 for testing."""
    return VectorStore(index_dir=tmp_path / "vectors", dim=4)


# ============================================================
# SQLiteStore 删除测试
# ============================================================

class TestSQLiteDeleteEntry:
    """SQLiteStore.delete_entry 测试组。"""

    def test_delete_existing_entry(self, store_with_entry):
        """删除后 query_by_id 返回 None。"""
        store, kid = store_with_entry
        result = store.delete_entry(kid)
        assert result is True
        assert store.query_by_id(kid) is None

    def test_delete_nonexistent_entry(self, store: SQLiteStore):
        """删除不存在的条目返回 False。"""
        result = store.delete_entry(99999)
        assert result is False

    def test_delete_cascades_content_chunks(self, store_with_entry):
        """删除后 content_chunks 也应被清理（CASCADE）。"""
        store, kid = store_with_entry
        # 手动插入 chunk
        with store.get_connection() as conn:
            conn.execute(
                "INSERT INTO content_chunks (knowledge_id, chunk_index, chunk_text) "
                "VALUES (?, ?, ?)",
                (kid, 0, "chunk text"),
            )
        store.delete_entry(kid)
        with store.get_connection() as conn:
            cursor = conn.execute(
                "SELECT COUNT(*) FROM content_chunks WHERE knowledge_id = ?",
                (kid,),
            )
            assert cursor.fetchone()[0] == 0

    def test_delete_cleans_fts5(self, store_with_entry):
        """删除后 FTS5 虚拟表也应被清理。"""
        store, kid = store_with_entry
        store.delete_entry(kid)
        with store.get_connection() as conn:
            cursor = conn.execute(
                "SELECT COUNT(*) FROM knowledge_items_fts WHERE rowid = ?",
                (kid,),
            )
            assert cursor.fetchone()[0] == 0

    def test_delete_decrements_tag_count(self, store_with_entry):
        """删除条目后共享标签计数应递减。"""
        store, kid = store_with_entry
        # 插入第二个也使用 "AI" 标签的条目
        entry2 = Entry(
            title="另一条目",
            source_type="generic",
            source_url="https://example.com/other",
            tags=["AI"],
            keywords=[],
            content="other content",
        )
        store.insert_entry(entry2, "generic/2026/02/20260219-other.md")

        # 删除第一个条目
        store.delete_entry(kid)

        # AI 标签 count 应从 2 变为 1
        tags = store.get_all_tags_with_count()
        tag_map = {t["name"]: t["count"] for t in tags}
        assert tag_map.get("AI") == 1

    def test_delete_removes_orphan_tags(self, store_with_entry):
        """标签仅被一个条目使用时，删除条目后标签也应被删除。"""
        store, kid = store_with_entry
        store.delete_entry(kid)

        tags = store.get_all_tags_with_count()
        tag_names = [t["name"] for t in tags]
        # "测试" 标签仅被第一个条目使用，删除后应不存在
        assert "测试" not in tag_names
        # "AI" 也仅被第一个条目使用
        assert "AI" not in tag_names

    def test_count_decreases_after_delete(self, store_with_entry):
        """count_entries 在删除后正确递减。"""
        store, kid = store_with_entry
        assert store.count_entries() == 1
        store.delete_entry(kid)
        assert store.count_entries() == 0


# ============================================================
# VectorStore 删除测试
# ============================================================

class TestVectorStoreDelete:
    """VectorStore.delete_vectors_for_entry 测试组。"""

    def test_delete_doc_vector(self, vector_store: VectorStore):
        """删除文档向量后 doc_deleted 为 True。"""
        vec = np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)
        vector_store.add_doc_vector(1, vec)
        stats = vector_store.delete_vectors_for_entry(1)
        assert stats["doc_deleted"] is True

    def test_delete_chunk_vectors(self, vector_store: VectorStore):
        """删除分块向量后 chunks_deleted 计数正确。"""
        for i in range(3):
            vec = np.random.rand(4).astype(np.float32)
            vector_store.add_chunk_vector(1, i, vec)
        stats = vector_store.delete_vectors_for_entry(1)
        assert stats["chunks_deleted"] == 3
        metadata = vector_store._load_metadata("chunk_vectors")
        assert metadata["id_mapping"] == {}

    def test_delete_nonexistent_vector(self, vector_store: VectorStore):
        """删除不存在的条目向量不报错。"""
        stats = vector_store.delete_vectors_for_entry(99999)
        assert stats["doc_deleted"] is False
        assert stats["chunks_deleted"] == 0

    def test_deleted_vectors_not_in_search(self, vector_store: VectorStore):
        """被删除的向量不应出现在搜索结果中。"""
        vec1 = np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)
        vec2 = np.array([0.0, 1.0, 0.0, 0.0], dtype=np.float32)
        vec3 = np.array([0.0, 0.0, 1.0, 0.0], dtype=np.float32)
        vector_store.add_doc_vector(1, vec1)
        vector_store.add_doc_vector(2, vec2)
        vector_store.add_doc_vector(3, vec3)

        vector_store.delete_vectors_for_entry(1)

        # 搜索与 vec1 最相似的（k=2，有效元素有 2 个）
        results = vector_store.search_doc(vec1, k=2)
        result_ids = [r[0] for r in results]
        assert 1 not in result_ids
        assert 2 in result_ids or 3 in result_ids
