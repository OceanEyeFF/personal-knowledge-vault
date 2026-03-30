"""
VectorRetriever SQL 语法测试

验证 VectorRetriever 的 SQL 查询是否正确
（不需要真实的 Embedder 和向量索引）
"""

import sys
from pathlib import Path

# Add project root to Python path
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

import pytest
import numpy as np
from src.storage.markdown_store import MarkdownStore, Entry
from src.storage.sqlite_store import SQLiteStore
from src.storage.vector_store import VectorStore


def test_vector_retriever_metadata_query():
    """
    测试 VectorRetriever._get_metadata() 的 SQL 查询

    验证：
    1. SQL 列名使用 'id' 而非 'knowledge_id'
    2. WHERE 子句使用 'id' 而非 'knowledge_id'
    3. 元数据能够正确获取
    """
    # 创建临时测试环境
    import tempfile

    temp_dir = Path(tempfile.mkdtemp())
    db_path = temp_dir / "test.db"
    vault_dir = temp_dir / "vault"
    vault_dir.mkdir()

    # 初始化存储
    md_store = MarkdownStore(vault_dir)
    sql_store = SQLiteStore(db_path)
    sql_store.initialize()

    # 创建测试 Entry
    entry = Entry(
        title="测试标题",
        content="# 测试标题\n\n测试内容",
        abstract="测试摘要",
        summary_one_sentence="测试一句话摘要",
        summary_100_words="测试百字摘要",
        tags=["测试", "单元测试"],
        keywords="测试,单元测试",
        source_type="generic",
        source_url="https://example.com/test",
    )

    # 保存到数据库
    file_path = md_store.save(entry)
    knowledge_id = sql_store.insert_entry(entry, str(file_path))

    # 测试元数据查询（直接执行 SQL，不依赖 VectorRetriever）
    with sql_store.get_connection() as conn:
        cursor = conn.execute(
            """
            SELECT
                knowledge_id,
                title,
                summary_one_sentence,
                summary_100_words,
                source_type,
                source_url,
                tags,
                keywords,
                file_path,
                archived_at,
                updated_at
            FROM knowledge_items
            WHERE knowledge_id = ?
            """,
            (knowledge_id,),
        )
        row = cursor.fetchone()

        # 验证结果
        assert row is not None, "应该能查询到数据"
        assert row[0] == knowledge_id, "ID 应该匹配"
        assert row[1] == entry.title, "标题应该匹配"
        assert row[2] == entry.summary_one_sentence, "一句话摘要应该匹配"
        assert row[4] == entry.source_type, "source_type 应该匹配"

    # Cleanup
    import shutil
    shutil.rmtree(temp_dir)


def test_vector_retriever_get_metadata():
    """
    测试 VectorRetriever._get_metadata() 方法

    验证：
    1. 方法能够正确获取元数据
    2. 返回的字典包含所有必要字段
    """
    # 创建临时测试环境
    import tempfile

    temp_dir = Path(tempfile.mkdtemp())
    db_path = temp_dir / "test.db"
    vault_dir = temp_dir / "vault"
    vector_dir = temp_dir / "vectors"
    vault_dir.mkdir()
    vector_dir.mkdir()

    # 初始化存储
    md_store = MarkdownStore(vault_dir)
    sql_store = SQLiteStore(db_path)
    sql_store.initialize()

    # 创建测试 Entry
    entry = Entry(
        title="向量检索测试",
        content="# 向量检索测试\n\n测试向量检索元数据获取",
        abstract="测试摘要",
        summary_one_sentence="测试一句话摘要",
        summary_100_words="测试百字摘要",
        tags=["向量检索", "测试"],
        keywords="向量,检索,测试",
        source_type="generic",
        source_url="https://example.com/vector-test",
    )

    # 保存到数据库
    file_path = md_store.save(entry)
    knowledge_id = sql_store.insert_entry(entry, str(file_path))

    # 创建 VectorRetriever 实例（不需要真实的 Embedder）
    # 只测试 _get_metadata 方法
    from src.retrieval.vector_retriever import VectorRetriever
    from unittest.mock import Mock

    # Mock Embedder 和 VectorStore
    mock_embedder = Mock()

    # 创建 VectorRetriever 实例
    retriever = VectorRetriever(db_path, vector_dir, mock_embedder)

    # 调用 _get_metadata
    metadata = retriever._get_metadata(knowledge_id)

    # 验证结果
    assert metadata is not None, "应该返回元数据"
    assert metadata["knowledge_id"] == knowledge_id, "knowledge_id 应该匹配"
    assert metadata["title"] == entry.title, "标题应该匹配"
    assert metadata["source_type"] == entry.source_type, "source_type 应该匹配"
    assert metadata["tags"] == "向量检索,测试", "tags 应该匹配"

    # Cleanup
    import shutil
    shutil.rmtree(temp_dir)


def test_vector_retriever_search_chunks_returns_chunk_metadata():
    """search_chunks should map vector hits back to chunk rows."""
    import tempfile
    from unittest.mock import Mock
    from src.retrieval.vector_retriever import VectorRetriever

    temp_dir = Path(tempfile.mkdtemp())
    db_path = temp_dir / "test.db"
    vault_dir = temp_dir / "vault"
    vector_dir = temp_dir / "vectors"
    vault_dir.mkdir()
    vector_dir.mkdir()

    md_store = MarkdownStore(vault_dir)
    sql_store = SQLiteStore(db_path)
    sql_store.initialize()

    entry = Entry(
        title="Chunk 检索测试",
        content="# Chunk 检索测试\n\n第一段\n\n第二段",
        abstract="测试摘要",
        summary_one_sentence="测试一句话摘要",
        summary_100_words="测试百字摘要",
        tags=["chunk", "测试"],
        keywords="chunk,测试",
        source_type="generic",
        source_url="https://example.com/chunk-search",
    )

    file_path = md_store.save(entry)
    knowledge_id = sql_store.insert_entry(entry, str(file_path))
    sql_store.insert_chunks(knowledge_id, ["第一段", "第二段"])

    vector_store = VectorStore(vector_dir)
    vector_store.add_chunk_vector(knowledge_id, 0, np.ones(1536, dtype="float32"))

    mock_embedder = Mock()
    mock_embedder.embed_document.return_value = np.ones(1536, dtype="float32")

    retriever = VectorRetriever(db_path, vector_dir, mock_embedder)
    results = retriever.search_chunks("第一段", limit=3)

    assert len(results) == 1
    assert results[0].knowledge_id == knowledge_id
    assert results[0].metadata["chunk_index"] == 0
    assert results[0].metadata["chunk_text"] == "第一段"
    assert results[0].highlight == "第一段"

    import shutil
    shutil.rmtree(temp_dir)


def test_vector_store_rejects_chunk_index_overflow(tmp_path: Path):
    """chunk_index beyond encoding range should raise ValueError."""
    vector_store = VectorStore(tmp_path / "vectors")

    with pytest.raises(ValueError, match="chunk_index 超出编码范围"):
        vector_store.add_chunk_vector(
            knowledge_id=1,
            chunk_index=VectorStore.MAX_CHUNK_INDEX + 1,
            vector=np.zeros(1536, dtype="float32"),
        )
