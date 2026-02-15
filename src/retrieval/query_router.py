"""
查询路由器

根据查询特征智能选择检索策略
"""

from typing import List
from pathlib import Path

from src.ai.embedder import Embedder
from src.utils.text_utils import TextProcessor
from src.utils.logger import get_logger
from src.retrieval.result import SearchResult
from src.retrieval.bm25_retriever import BM25Retriever
from src.retrieval.hybrid_retriever import HybridRetriever

logger = get_logger(__name__)


class QueryRouter:
    """
    查询路由器

    根据查询长度智能选择 BM25 或混合检索
    """

    def __init__(
        self,
        db_path: Path,
        vector_index_dir: Path,
        embedder: Embedder,
        token_threshold: int = 5,
    ):
        """
        初始化查询路由器

        Args:
            db_path: 数据库文件路径
            vector_index_dir: 向量索引目录
            embedder: 向量化工具
            token_threshold: 分词数阈值（< threshold 用 BM25，>= threshold 用混合）
        """
        self.bm25_retriever = BM25Retriever(db_path)
        self.hybrid_retriever = HybridRetriever(db_path, vector_index_dir, embedder)
        self.text_processor = TextProcessor()
        self.token_threshold = token_threshold

    def search(self, query: str, limit: int = 10) -> List[SearchResult]:
        """
        智能检索

        Args:
            query: 查询文本
            limit: 返回结果数量

        Returns:
            搜索结果列表
        """
        if not query or not query.strip():
            logger.debug("查询文本为空，返回空结果")
            return []

        try:
            # 分词
            tokenized = self.text_processor.tokenize_chinese(query)
            tokens = tokenized.split()
            token_count = len(tokens)

            # 路由决策
            if token_count < self.token_threshold:
                # 短查询：使用 BM25
                logger.info(
                    f"查询分词数={token_count} < {self.token_threshold}，使用 BM25 检索"
                )
                return self.bm25_retriever.search(query, limit)
            else:
                # 长查询：使用混合检索
                logger.info(
                    f"查询分词数={token_count} >= {self.token_threshold}，使用混合检索"
                )
                return self.hybrid_retriever.search(query, limit)

        except Exception as e:
            logger.error(f"查询路由失败: {e}", exc_info=True)
            return []
