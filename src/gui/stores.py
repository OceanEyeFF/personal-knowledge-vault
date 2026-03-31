"""GUI 层存储与检索单例管理。

提供延迟初始化的全局单例，遵循 src/mcp/server.py 的单例模式。
所有视图通过此模块获取存储实例，避免重复初始化。
"""

from __future__ import annotations

import logging
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from src.storage.sqlite_store import SQLiteStore
    from src.storage.markdown_store import MarkdownStore
    from src.storage.vector_store import VectorStore
    from src.retrieval.bm25_retriever import BM25Retriever

logger = logging.getLogger("pkv.gui.stores")

_sqlite_store: Optional["SQLiteStore"] = None
_markdown_store: Optional["MarkdownStore"] = None
_vector_store: Optional["VectorStore"] = None
_bm25_retriever: Optional["BM25Retriever"] = None


def get_sqlite_store() -> "SQLiteStore":
    """获取 SQLiteStore 单例（延迟初始化）。

    Returns:
        SQLiteStore 实例。
    """
    global _sqlite_store
    if _sqlite_store is None:
        from src.storage.sqlite_store import SQLiteStore
        from src.utils.config import get_config
        config = get_config()
        _sqlite_store = SQLiteStore(config.db_path)
        logger.info(f"SQLiteStore 初始化: {config.db_path}")
    return _sqlite_store


def get_markdown_store() -> "MarkdownStore":
    """获取 MarkdownStore 单例（延迟初始化）。

    Returns:
        MarkdownStore 实例。
    """
    global _markdown_store
    if _markdown_store is None:
        from src.storage.markdown_store import MarkdownStore
        from src.utils.config import get_config
        config = get_config()
        _markdown_store = MarkdownStore(config.vault_dir)
        logger.info(f"MarkdownStore 初始化: {config.vault_dir}")
    return _markdown_store


def get_vector_store() -> Optional["VectorStore"]:
    """获取 VectorStore 单例（延迟初始化，仅删除时需要）。

    Returns:
        VectorStore 实例。
    """
    global _vector_store
    if _vector_store is None:
        from src.storage.vector_store import VectorStore
        from src.utils.config import get_config
        config = get_config()
        if (
            not VectorStore.has_index_artifacts(config.vector_index_dir)
            and config.embedding_dim is None
        ):
            logger.info("VectorStore 延迟初始化: 索引不存在且 embedding 维度尚未解析")
            return None
        _vector_store = VectorStore(
            index_dir=config.vector_index_dir,
            dim=None,
        )
        logger.info(f"VectorStore 初始化: {config.vector_index_dir}")
    return _vector_store


def get_bm25_retriever() -> "BM25Retriever":
    """获取 BM25Retriever 单例（延迟初始化）。

    直接使用 BM25Retriever，不通过 QueryRouter，
    避免 QueryRouter 触发 VectorStore 加载（hnswlib 索引），
    防止 GUI 启动时的冷启动延迟。

    Returns:
        BM25Retriever 实例。
    """
    global _bm25_retriever
    if _bm25_retriever is None:
        from src.retrieval.bm25_retriever import BM25Retriever
        from src.utils.config import get_config
        config = get_config()
        _bm25_retriever = BM25Retriever(db_path=config.db_path)
        logger.info(f"BM25Retriever 初始化: {config.db_path}")
    return _bm25_retriever


def reset_stores() -> None:
    """重置所有单例（仅用于测试）。"""
    global _sqlite_store, _markdown_store, _vector_store, _bm25_retriever
    _sqlite_store = None
    _markdown_store = None
    _vector_store = None
    _bm25_retriever = None
