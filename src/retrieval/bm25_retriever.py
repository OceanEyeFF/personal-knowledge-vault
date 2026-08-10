"""
BM25 关键词检索器

基于 SQLite FTS5 全文索引的关键词检索
"""

from pathlib import Path
import sqlite3

from src.storage.sqlite_store import FTS_TABLE_NAME, SQLiteStore
from src.runtime.errors import ErrorCode
from src.utils.text_utils import TextProcessor
from src.utils.logger import get_logger
from src.retrieval.result import RetrievalIssue, SearchResponse, SearchResult

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

    def search(self, query: str, limit: int = 10) -> SearchResponse:
        """
        执行 BM25 关键词检索

        Args:
            query: 查询文本
            limit: 返回结果数量

        Returns:
            检索响应；通过 status 区分 success/no_hits/invalid/error
        """
        if not isinstance(query, str) or not query.strip():
            logger.debug("查询文本为空，拒绝检索")
            return SearchResponse.invalid("查询文本不能为空", strategy="bm25")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            logger.debug("检索 limit 非法: type=%s", type(limit).__name__)
            return SearchResponse.invalid(
                "limit 必须是正整数",
                strategy="bm25",
                stage="limit_validation",
            )

        try:
            # 构建 FTS5 查询
            match_query = self._build_match_query(query)
            if not match_query:
                logger.warning(
                    "查询分词后为空，无法执行 BM25 检索: query_length=%s",
                    len(query),
                )
                return SearchResponse.invalid(
                    "查询分词后没有可检索 token",
                    strategy="bm25",
                )

            with self.store.get_connection() as conn:
                results = self._execute_match_query(conn, match_query, limit, "strict")
                relaxed_query = self._build_relaxed_match_query(query)
                if not results and relaxed_query and relaxed_query != match_query:
                    logger.info(
                        "BM25 严格查询无结果，尝试放宽查询: "
                        "query_length=%s, strict_tokens=%s, relaxed_tokens=%s",
                        len(query),
                        len(match_query.split()),
                        len(relaxed_query.split()),
                    )
                    results = self._execute_match_query(conn, relaxed_query, limit, "relaxed_or")

            logger.info(
                "BM25 检索完成: query_length=%s, result_count=%s",
                len(query),
                len(results),
            )
            return SearchResponse.completed(results, strategy="bm25")

        except Exception as e:
            logger.error(
                "BM25 检索失败: error_type=%s",
                type(e).__name__,
            )
            metadata_failure = isinstance(e, (IndexError, KeyError))
            return SearchResponse.failed_response(
                RetrievalIssue.from_exception(
                    e,
                    fallback_code=(
                        ErrorCode.RETRIEVAL_METADATA_INCONSISTENT
                        if metadata_failure
                        else ErrorCode.RETRIEVAL_BACKEND_FAILED
                    ),
                    public_message=(
                        "BM25 检索结果元数据不一致"
                        if metadata_failure
                        else "BM25 检索后端不可用"
                    ),
                    stage="bm25_metadata" if metadata_failure else "bm25_backend",
                    recoverable=True,
                ),
                strategy="bm25",
            )

    def _build_match_query(self, query: str) -> str:
        """
        构建 FTS5 MATCH 查询字符串

        Args:
            query: 用户查询

        Returns:
            FTS5 查询字符串
        """
        tokens = self._query_tokens(query)

        if not tokens:
            return ""

        # FTS5 使用空格分隔的词，语义为严格匹配全部 token
        return " ".join(tokens)

    def _build_relaxed_match_query(self, query: str) -> str:
        """构建多词 OR fallback 查询字符串。"""
        tokens = self._query_tokens(query)
        if len(tokens) <= 1:
            return ""
        return " OR ".join(tokens)

    def _query_tokens(self, query: str) -> list[str]:
        """将用户查询转换为安全的 FTS token 列表。"""
        tokenized = self.text_processor.tokenize_chinese(query)
        tokens = [self._sanitize_token(token) for token in tokenized.split()]
        return [token for token in tokens if token]

    def _execute_match_query(
        self,
        conn: sqlite3.Connection,
        match_query: str,
        limit: int,
        match_mode: str,
    ) -> list[SearchResult]:
        """执行一次 FTS5 MATCH 查询并转换为统一结果。"""
        cursor = conn.execute(
            f"""
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
                bm25({FTS_TABLE_NAME}) as bm25_score,
                snippet({FTS_TABLE_NAME}, 0, '...', '...', '', 64) as snippet
            FROM knowledge_items ki
            JOIN {FTS_TABLE_NAME} ON ki.knowledge_id = {FTS_TABLE_NAME}.rowid
            WHERE {FTS_TABLE_NAME} MATCH ?
            ORDER BY bm25_score ASC
            LIMIT ?
            """,
            (match_query, limit),
        )

        results = []
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
                "bm25_match_query": match_query,
                "bm25_match_mode": match_mode,
            }

            result = SearchResult(
                knowledge_id=knowledge_id,
                title=title,
                score=normalized_score,
                highlight=snippet[:200],  # 限制长度
                metadata=metadata,
            )
            results.append(result)
        return results

    @staticmethod
    def _sanitize_token(token: str) -> str:
        """
        清理 token，移除 FTS5 特殊字符

        Args:
            token: 原始 token

        Returns:
            清理后的 token
        """
        # MATCH 的列过滤、括号、引号和布尔操作符都有独立语义。
        # 只保留 Unicode 字母/数字/下划线，确保用户文本永远只是 token。
        token = "".join(char for char in token.strip() if char.isalnum() or char == "_")
        if token.upper() in {"AND", "OR", "NOT", "NEAR"}:
            return ""
        return token

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
