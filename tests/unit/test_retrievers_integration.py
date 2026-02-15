"""
检索器集成测试（无 API 依赖）

测试检索器之间的集成工作，不依赖真实的 OpenAI API
"""

import sys
from pathlib import Path

# Add project root to Python path
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

import pytest
from unittest.mock import Mock, MagicMock
from src.storage.markdown_store import MarkdownStore, Entry
from src.storage.sqlite_store import SQLiteStore
from src.storage.vector_store import VectorStore
from src.retrieval.bm25_retriever import BM25Retriever
from src.retrieval.vector_retriever import VectorRetriever
from src.retrieval.hybrid_retriever import HybridRetriever
from src.retrieval.query_router import QueryRouter


@pytest.fixture
def test_env(tmp_path):
    """创建测试环境"""
    db_path = tmp_path / "test.db"
    vault_dir = tmp_path / "vault"
    vector_dir = tmp_path / "vectors"

    vault_dir.mkdir()
    vector_dir.mkdir()

    # 初始化存储
    md_store = MarkdownStore(vault_dir)
    sql_store = SQLiteStore(db_path)
    sql_store.initialize()

    # 创建测试数据
    entries = [
        Entry(
            title="Python 基础教程",
            content="# Python 基础教程\n\nPython 是一门简单易学的编程语言。适合初学者入门。",
            abstract="Python 入门",
            summary_one_sentence="Python 编程基础知识",
            summary_100_words="Python 是一门解释型、面向对象的编程语言，语法简洁清晰。",
            tags=["Python", "编程", "入门"],
            keywords="Python,编程,教程,入门",
            source_type="generic",
            source_url="https://example.com/python-basics",
        ),
        Entry(
            title="深度学习入门",
            content="# 深度学习入门\n\n深度学习是机器学习的一个分支，使用神经网络。",
            abstract="深度学习简介",
            summary_one_sentence="深度学习基础知识",
            summary_100_words="深度学习通过多层神经网络学习数据的表示，在图像识别等领域表现优异。",
            tags=["深度学习", "机器学习", "AI"],
            keywords="深度学习,神经网络,AI",
            source_type="generic",
            source_url="https://example.com/dl-intro",
        ),
        Entry(
            title="机器学习算法",
            content="# 机器学习算法\n\n常见的机器学习算法包括决策树、随机森林等。",
            abstract="机器学习算法概述",
            summary_one_sentence="常见机器学习算法介绍",
            summary_100_words="机器学习算法分为监督学习、无监督学习和强化学习三大类。",
            tags=["机器学习", "算法"],
            keywords="机器学习,算法,决策树",
            source_type="generic",
            source_url="https://example.com/ml-algorithms",
        ),
    ]

    # 保存数据
    knowledge_ids = []
    for entry in entries:
        file_path = md_store.save(entry)
        kid = sql_store.insert_entry(entry, str(file_path))
        knowledge_ids.append(kid)

    return {
        "db_path": db_path,
        "vault_dir": vault_dir,
        "vector_dir": vector_dir,
        "md_store": md_store,
        "sql_store": sql_store,
        "knowledge_ids": knowledge_ids,
    }


def test_bm25_retriever_basic(test_env):
    """测试 BM25 检索器基本功能"""
    retriever = BM25Retriever(test_env["db_path"])

    # 测试 1: 单词查询
    results = retriever.search("Python", limit=5)
    assert len(results) >= 1, "应该能召回 Python 相关内容"
    assert results[0].title == "Python 基础教程", "最相关的应该是 Python 基础教程"

    # 测试 2: 多词查询
    results = retriever.search("学习", limit=5)
    assert len(results) >= 2, "应该能召回至少 2 条包含'学习'的内容"

    # 测试 3: 不存在的词
    results = retriever.search("区块链", limit=5)
    assert len(results) == 0, "不存在的词应该返回空结果"


def test_vector_retriever_with_mock(test_env):
    """测试 VectorRetriever（使用 Mock Embedder）"""
    # Mock Embedder
    mock_embedder = Mock()
    mock_embedder.embed_document.return_value = [0.1] * 1536  # Mock 向量

    retriever = VectorRetriever(
        test_env["db_path"], test_env["vector_dir"], mock_embedder
    )

    # 测试元数据获取（不执行真实的向量检索）
    kid = test_env["knowledge_ids"][0]
    metadata = retriever._get_metadata(kid)

    assert metadata is not None, "应该能获取到元数据"
    assert metadata["knowledge_id"] == kid, "knowledge_id 应该匹配"
    assert "Python" in metadata["title"], "标题应该包含 Python"


def test_hybrid_retriever_with_mock(test_env):
    """测试 HybridRetriever（使用 Mock Embedder）"""
    # Mock Embedder
    mock_embedder = Mock()

    # 不执行真实的向量检索，只测试 BM25 部分
    retriever = HybridRetriever(
        test_env["db_path"], test_env["vector_dir"], mock_embedder
    )

    # 测试检索器初始化
    assert retriever.bm25_retriever is not None, "BM25 检索器应该初始化成功"
    assert retriever.vector_retriever is not None, "向量检索器应该初始化成功"
    assert retriever.bm25_weight == 0.4, "BM25 权重应该是 0.4"
    assert retriever.vector_weight == 0.6, "向量权重应该是 0.6"


def test_query_router_short_query(test_env):
    """测试 QueryRouter 短查询路由"""
    # Mock Embedder
    mock_embedder = Mock()

    router = QueryRouter(
        test_env["db_path"], test_env["vector_dir"], mock_embedder, token_threshold=5
    )

    # 短查询（< 5 tokens）应该使用 BM25
    results = router.search("Python", limit=5)
    assert len(results) >= 1, "应该能召回结果"

    # 验证使用了 BM25（通过检查结果中的元数据）
    if results:
        assert "bm25_score" in results[0].metadata, "短查询应该使用 BM25"


def test_query_router_token_threshold(test_env):
    """测试 QueryRouter 分词阈值"""
    # Mock Embedder
    mock_embedder = Mock()

    router = QueryRouter(
        test_env["db_path"], test_env["vector_dir"], mock_embedder, token_threshold=3
    )

    # 测试分词逻辑（不实际执行检索）
    from src.utils.text_utils import TextProcessor

    tp = TextProcessor()

    # 短查询
    short_query = "Python"
    tokens = tp.tokenize_chinese(short_query).split()
    assert len(tokens) < 3, "短查询分词数应该 < 3"

    # 长查询
    long_query = "如何学习 Python 编程语言"
    tokens = tp.tokenize_chinese(long_query).split()
    assert len(tokens) >= 3, "长查询分词数应该 >= 3"


def test_all_retrievers_column_names(test_env):
    """
    测试所有检索器的列名一致性

    验证所有检索器都使用正确的列名 'id' 而非 'knowledge_id'
    """
    # 测试 BM25Retriever
    bm25 = BM25Retriever(test_env["db_path"])
    results = bm25.search("Python", limit=1)
    assert len(results) >= 1, "BM25 应该能召回结果"

    # 测试 VectorRetriever._get_metadata
    mock_embedder = Mock()
    vector = VectorRetriever(test_env["db_path"], test_env["vector_dir"], mock_embedder)
    kid = test_env["knowledge_ids"][0]
    metadata = vector._get_metadata(kid)
    assert metadata is not None, "VectorRetriever 应该能获取元数据"
    assert metadata["knowledge_id"] == kid, "knowledge_id 应该正确"


def test_search_result_score_range(test_env):
    """测试 SearchResult 分数范围"""
    retriever = BM25Retriever(test_env["db_path"])
    results = retriever.search("Python", limit=5)

    if results:
        for result in results:
            assert 0.0 <= result.score <= 1.0, f"分数应该在 [0.0, 1.0] 范围内，当前: {result.score}"


def test_empty_query_handling(test_env):
    """测试空查询处理"""
    retriever = BM25Retriever(test_env["db_path"])

    # 空字符串
    results = retriever.search("", limit=5)
    assert len(results) == 0, "空查询应该返回空结果"

    # 仅空格
    results = retriever.search("   ", limit=5)
    assert len(results) == 0, "仅空格查询应该返回空结果"
