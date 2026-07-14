"""
向量语义检索器

基于 hnswlib 的向量相似度检索
"""

from typing import List
from pathlib import Path
from numbers import Integral

from src.storage.sqlite_store import SQLiteStore
from src.storage.vector_store import VectorStore
from src.ai.embedder import Embedder
from src.utils.logger import get_logger
from src.retrieval.result import SearchResult

logger = get_logger(__name__)


class VectorRetriever:
    """
    向量语义检索器

    使用向量相似度进行语义检索，适合自然语言查询
    """

    def __init__(self, db_path: Path, vector_index_dir: Path, embedder: Embedder):
        """
        初始化向量检索器

        Args:
            db_path: 数据库文件路径
            vector_index_dir: 向量索引目录
            embedder: 向量化工具
        """
        self.store = SQLiteStore(db_path)
        self.vector_index_dir = Path(vector_index_dir)
        self.embedder = embedder
        embedder_dim = getattr(embedder, "dim", None)
        if not isinstance(embedder_dim, Integral):
            embedder_dim = None
        self._embedder_dim = int(embedder_dim) if embedder_dim is not None else None
        self.vector_store = None
        if self._embedder_dim is not None or VectorStore.has_index_artifacts(
            self.vector_index_dir
        ):
            self.vector_store = VectorStore(
                self.vector_index_dir,
                dim=self._embedder_dim,
            )

    def search(self, query: str, limit: int = 10) -> List[SearchResult]:
        """
        执行向量语义检索

        Args:
            query: 查询文本
            limit: 返回结果数量

        Returns:
            搜索结果列表，按相似度降序排列
        """
        if not query or not query.strip():
            logger.debug("查询文本为空，返回空结果")
            return []

        try:
            vector_store = self._get_vector_store()
            if vector_store is None:
                logger.info("向量索引不存在，返回空结果")
                return []

            # 向量化查询
            query_vector = self.embedder.embed_document(query)
            if query_vector is None:
                logger.error("查询文本向量化失败")
                return []

            # 执行向量检索
            vector_results = vector_store.search_doc(query_vector, k=limit)
            if not vector_results:
                logger.info("向量检索无结果")
                return []

            # 获取元数据并构建结果
            results = []
            for rank, (knowledge_id, distance) in enumerate(vector_results, start=1):
                # 从数据库获取元数据
                metadata_dict = self._get_metadata(knowledge_id)
                if not metadata_dict:
                    logger.warning(f"向量检索结果缺少元数据: knowledge_id={knowledge_id}")
                    continue

                # 转换距离为相似度分数
                score = self._distance_to_score(distance)

                # 构建元数据
                metadata = dict(metadata_dict)
                metadata["vector_distance"] = float(distance)
                metadata["vector_rank"] = rank

                result = SearchResult(
                    knowledge_id=knowledge_id,
                    title=metadata_dict.get("title", f"条目 {knowledge_id}"),
                    score=score,
                    highlight=metadata_dict.get("summary_one_sentence", "")
                    or metadata_dict.get("summary_100_words", "")[:200],
                    metadata=metadata,
                )
                results.append(result)

            logger.info(f"向量检索完成: 查询='{query[:50]}...', 结果数={len(results)}")
            return results

        except Exception as e:
            logger.error(f"向量检索失败: {e}", exc_info=True)
            return []

    def search_chunks(self, query: str, limit: int = 10) -> List[SearchResult]:
        """
        执行 chunk 级向量检索。

        Args:
            query: 查询文本
            limit: 返回结果数量

        Returns:
            chunk 级搜索结果列表，元数据中包含 chunk_index/chunk_text 等字段
        """
        if not query or not query.strip():
            logger.debug("查询文本为空，返回空 chunk 结果")
            return []

        try:
            vector_store = self._get_vector_store()
            if vector_store is None:
                logger.info("chunk 向量索引不存在，返回空结果")
                return []

            query_vector = self.embedder.embed_document(query)
            if query_vector is None:
                logger.error("查询文本向量化失败")
                return []

            vector_results = vector_store.search_chunk(query_vector, k=limit)
            if not vector_results:
                logger.info("chunk 向量检索无结果")
                return []

            results = []
            for rank, (knowledge_id, chunk_index, distance) in enumerate(
                vector_results, start=1
            ):
                metadata_dict = self._get_chunk_metadata(knowledge_id, chunk_index)
                if not metadata_dict:
                    logger.warning(
                        "chunk 向量检索结果缺少元数据: "
                        f"knowledge_id={knowledge_id}, chunk_index={chunk_index}"
                    )
                    continue

                score = self._distance_to_score(distance)
                metadata = dict(metadata_dict)
                metadata["vector_distance"] = float(distance)
                metadata["vector_rank"] = rank

                results.append(
                    SearchResult(
                        knowledge_id=knowledge_id,
                        title=metadata_dict.get("title", f"条目 {knowledge_id}"),
                        score=score,
                        highlight=metadata_dict.get("chunk_text", "")[:200],
                        metadata=metadata,
                    )
                )

            logger.info(f"chunk 向量检索完成: 查询='{query[:50]}...', 结果数={len(results)}")
            return results

        except Exception as e:
            logger.error(f"chunk 向量检索失败: {e}", exc_info=True)
            return []

    def _get_vector_store(self) -> VectorStore | None:
        if self.vector_store is not None:
            return self.vector_store

        if not VectorStore.has_index_artifacts(self.vector_index_dir):
            return None

        self.vector_store = VectorStore(
            self.vector_index_dir,
            dim=self._embedder_dim,
        )
        return self.vector_store

    def _get_metadata(self, knowledge_id: int) -> dict:
        """
        从数据库获取知识条目元数据

        Args:
            knowledge_id: 知识条目 ID

        Returns:
            元数据字典
        """
        try:
            with self.store.get_connection() as conn:
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
                if not row:
                    return {}

                return {
                    "knowledge_id": row[0],
                    "title": row[1],
                    "summary_one_sentence": row[2],
                    "summary_100_words": row[3],
                    "source_type": row[4],
                    "source_url": row[5],
                    "tags": row[6],
                    "keywords": row[7],
                    "file_path": row[8],
                    "archived_at": row[9],
                    "updated_at": row[10],
                }
        except Exception as e:
            logger.error(f"获取元数据失败: knowledge_id={knowledge_id}, {e}")
            return {}

    def _get_chunk_metadata(self, knowledge_id: int, chunk_index: int) -> dict:
        """
        从数据库获取 chunk 元数据。

        Args:
            knowledge_id: 知识条目 ID
            chunk_index: 分块序号

        Returns:
            元数据字典
        """
        try:
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
                        cc.chunk_id,
                        cc.chunk_index,
                        cc.chunk_text
                    FROM knowledge_items ki
                    JOIN content_chunks cc
                      ON ki.knowledge_id = cc.knowledge_id
                    WHERE ki.knowledge_id = ? AND cc.chunk_index = ?
                    """,
                    (knowledge_id, chunk_index),
                )
                row = cursor.fetchone()
                if not row:
                    return {}

                return {
                    "knowledge_id": row["knowledge_id"],
                    "title": row["title"],
                    "summary_one_sentence": row["summary_one_sentence"],
                    "summary_100_words": row["summary_100_words"],
                    "source_type": row["source_type"],
                    "source_url": row["source_url"],
                    "tags": row["tags"],
                    "keywords": row["keywords"],
                    "file_path": row["file_path"],
                    "archived_at": row["archived_at"],
                    "updated_at": row["updated_at"],
                    "chunk_id": row["chunk_id"],
                    "chunk_index": row["chunk_index"],
                    "chunk_text": row["chunk_text"],
                }
        except Exception as e:
            logger.error(
                "获取 chunk 元数据失败: "
                f"knowledge_id={knowledge_id}, chunk_index={chunk_index}, {e}"
            )
            return {}

    @staticmethod
    def _distance_to_score(distance: float) -> float:
        """
        将向量距离转换为相似度分数

        hnswlib 使用余弦距离，范围 [0, 2]，越小越相似

        Args:
            distance: 向量距离

        Returns:
            相似度分数 [0.0, 1.0]
        """
        # 余弦距离转换为相似度
        # distance = 1 - cosine_similarity
        # similarity = 1 - distance
        score = 1.0 - float(distance)

        # 限制范围到 [0.0, 1.0]
        return max(min(score, 1.0), 0.0)
