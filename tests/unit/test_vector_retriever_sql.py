"""VectorRetriever metadata, chunk mapping, and index safety contracts."""

import sys
from pathlib import Path

# Add project root to Python path
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

import pytest
import numpy as np
import src.storage.vector_store as vector_store_module
from src.storage.markdown_store import MarkdownStore, Entry
from src.storage.sqlite_store import SQLiteStore
from src.storage.vector_store import VectorStore
from src.utils.config import Config


@pytest.fixture(autouse=True)
def isolate_vector_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Config:
    """Keep VectorStore configuration and every runtime path in pytest temp data."""
    data_root = tmp_path / "runtime"
    runtime_paths = {
        "DATA_DIR": data_root,
        "DB_PATH": data_root / "db" / "knowledge_vault.db",
        "VAULT_DIR": data_root / "vault",
        "VECTOR_DIR": data_root / "vectors",
        "LOG_DIR": data_root / "logs",
        "TMP_DIR": data_root / "tmp",
    }
    for key, path in runtime_paths.items():
        monkeypatch.setenv(key, str(path))

    config = Config(str(project_root / "config" / "config.yaml"))
    monkeypatch.setattr(vector_store_module, "get_config", lambda: config)
    return config




def test_vector_retriever_get_metadata(
    isolate_vector_config: Config,
):
    """
    测试 VectorRetriever._get_metadata() 方法

    验证：
    1. 方法能够正确获取元数据
    2. 返回的字典包含所有必要字段
    """
    # 创建临时测试环境
    db_path = isolate_vector_config.db_path
    vault_dir = isolate_vector_config.vault_dir
    vector_dir = isolate_vector_config.vector_index_dir
    vault_dir.mkdir(parents=True)
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
    mock_embedder.dim = 1536

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

def test_vector_retriever_search_chunks_returns_chunk_metadata(
    isolate_vector_config: Config,
):
    """search_chunks should map vector hits back to chunk rows."""
    from unittest.mock import Mock
    from src.retrieval.vector_retriever import VectorRetriever

    db_path = isolate_vector_config.db_path
    vault_dir = isolate_vector_config.vault_dir
    vector_dir = isolate_vector_config.vector_index_dir
    vault_dir.mkdir(parents=True)
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

    vector_store = VectorStore(vector_dir, dim=1536)
    vector_store.add_chunk_vector(knowledge_id, 0, np.ones(1536, dtype="float32"))

    mock_embedder = Mock()
    mock_embedder.dim = 1536
    mock_embedder.embed_document.return_value = np.ones(1536, dtype="float32")

    retriever = VectorRetriever(db_path, vector_dir, mock_embedder)
    response = retriever.search_chunks("第一段", limit=3)

    assert response.status == "success"
    assert len(response.results) == 1
    assert response.results[0].knowledge_id == knowledge_id
    assert response.results[0].metadata["chunk_index"] == 0
    assert response.results[0].metadata["chunk_text"] == "第一段"
    assert response.results[0].highlight == "第一段"


def test_vector_store_rejects_chunk_index_overflow(tmp_path: Path):
    """chunk_index beyond encoding range should raise ValueError."""
    vector_store = VectorStore(tmp_path / "vectors", dim=1536)

    with pytest.raises(ValueError, match="chunk_index 超出编码范围"):
        vector_store.add_chunk_vector(
            knowledge_id=1,
            chunk_index=VectorStore.MAX_CHUNK_INDEX + 1,
            vector=np.zeros(1536, dtype="float32"),
        )


def test_vector_retriever_records_embedder_dimension_without_creating_read_index(tmp_path: Path):
    """只读检索器记录维度，但初始化不得创建空向量索引。"""
    from unittest.mock import Mock
    from src.retrieval.vector_retriever import VectorRetriever

    db_path = tmp_path / "test.db"
    vector_dir = tmp_path / "vectors"
    mock_embedder = Mock()
    mock_embedder.dim = 8

    retriever = VectorRetriever(db_path, vector_dir, mock_embedder)

    assert retriever._embedder_dim == 8
    assert retriever.vector_store is None


def test_vector_retriever_auto_mode_skips_empty_index_cold_start(tmp_path: Path):
    """auto 模式在空索引目录下不应在初始化阶段报错。"""
    from unittest.mock import Mock
    from src.retrieval.vector_retriever import VectorRetriever

    db_path = tmp_path / "test.db"
    vector_dir = tmp_path / "vectors"
    vector_dir.mkdir()

    mock_embedder = Mock()
    mock_embedder.dim = None

    retriever = VectorRetriever(db_path, vector_dir, mock_embedder)
    response = retriever.search("cold start query")

    assert retriever.vector_store is None
    assert response.status == "error"
    assert response.error_code.value == "retrieval_index_unavailable"
    assert response.results == ()
    mock_embedder.embed_document.assert_not_called()
