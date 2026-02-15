"""
BM25 关键词检索器

基于 SQLite FTS5 全文索引的关键词检索
"""

from typing import List
from pathlib import Path

from src.storage.sqlite_store import SQLiteStore
from src.utils.text_utils import TextProcessor
from src.utils.logger import get_logger
from src.retrieval.result import SearchResult

logger = get_logger(__name__)


class BM25Retriever:
    """
    BM25 关键词检索器

    使用 SQLite FTS5 进行全文检索，适合精确关键词匹配
    """

    def __init__(self, db_path: Path):
        """
        初始化 BM25 检索器

        Args:
            db_path: 数据库文件路径
        """
        self.store = SQLiteStore(db_path)
        self.text_processor = TextProcessor()

    def search(self, query: str, limit: int = 10) -> List[SearchResult]:
        """
        执行 BM25 关键词检索

        Args:
            query: 查询文本
            limit: 返回结果数量

        Returns:
            搜索结果列表，按相关性降序排列
        """
        if not query or not query.strip():
            logger.debug("查询文本为空，返回空结果")
            return []

        try:
            # 构建 FTS5 查询
            match_query = self._build_match_query(query)
            if not match_query:
                logger.warning(f"查询 '{query}' 分词后为空，无法执行 BM25 检索")
                return []

            # 执行 FTS5 查询
            results = []
            with self.store.get_connection() as conn:
                cursor = conn.execute(
                    """
                    SELECT
                        ki.knowledge_id,
                        ki.title,
                        ki.summary_one_sentence,
                        ki.summary_100_words,
                        ki.source_type,
                        ki.source_url,
                        ki.tags,
                        ki.keywords,
                        ki.file_path,
                        ki.archived_at,
                        ki.updated_at,
                        bm25(knowledge_items_fts) as bm25_score,
                        snippet(knowledge_items_fts, 0, '...', '...', '', 64) as snippet
                    FROM knowledge_items ki
                    JOIN knowledge_items_fts ON ki.knowledge_id = knowledge_items_fts.rowid
                    WHERE knowledge_items_fts MATCH ?
                    ORDER BY bm25_score ASC
                    LIMIT ?
                    """,
                    (match_query, limit),
                )

                for rank, row in enumerate(cursor.fetchall(), start=1):
                    knowledge_id = row[0]
                    title = row[1] or f"条目 {knowledge_id}"
                    summary_one = row[2] or ""
                    summary_100 = row[3] or ""
                    raw_score = row[11]  # bm25_score
                    snippet = row[12] or summary_one or summary_100

                    # 归一化分数到 [0.0, 1.0]
                    normalized_score = self._normalize_score(raw_score, rank)

                    # 构建元数据
                    metadata = {
                        "source_type": row[4],
                        "source_url": row[5],
                        "tags": row[6],
                        "keywords": row[7],
                        "file_path": row[8],
                        "archived_at": row[9],
                        "updated_at": row[10],
                        "bm25_score": raw_score,
                        "bm25_rank": rank,
                    }

                    result = SearchResult(
                        knowledge_id=knowledge_id,
                        title=title,
                        score=normalized_score,
                        highlight=snippet[:200],  # 限制长度
                        metadata=metadata,
                    )
                    results.append(result)

            logger.info(f"BM25 检索完成: 查询='{query}', 结果数={len(results)}")
            return results

        except Exception as e:
            logger.error(f"BM25 检索失败: {e}", exc_info=True)
            return []

    def _build_match_query(self, query: str) -> str:
        """
        构建 FTS5 MATCH 查询字符串

        Args:
            query: 用户查询

        Returns:
            FTS5 查询字符串
        """
        # 使用 jieba 分词
        tokenized = self.text_processor.tokenize_chinese(query)
        tokens = [self._sanitize_token(token) for token in tokenized.split()]
        tokens = [token for token in tokens if token]

        if not tokens:
            return ""

        # FTS5 使用空格分隔的词
        return " ".join(tokens)

    @staticmethod
    def _sanitize_token(token: str) -> str:
        """
        清理 token，移除 FTS5 特殊字符

        Args:
            token: 原始 token

        Returns:
            清理后的 token
        """
        # 移除 FTS5 特殊字符
        special_chars = '"*'
        for char in special_chars:
            token = token.replace(char, "")
        return token.strip()

    @staticmethod
    def _normalize_score(raw_score: float, rank: int) -> float:
        """
        将 BM25 原始分数归一化到 [0.0, 1.0]

        FTS5 的 bm25() 函数返回负数，绝对值越大越相关

        Args:
            raw_score: FTS5 返回的原始分数（负数）
            rank: 结果排名（从 1 开始）

        Returns:
            归一化后的分数
        """
        # FTS5 BM25 分数通常为负数，绝对值越大越相关
        if raw_score <= 0:
            # 使用绝对值并限制范围
            abs_score = abs(raw_score)
            # 限制最大值为 50（经验值）
            return min(abs_score / 50.0, 1.0)
        else:
            # 处理异常情况（正值）
            return min(raw_score / 10.0, 1.0)
