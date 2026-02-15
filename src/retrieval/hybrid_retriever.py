"""
混合检索器

结合 BM25 和向量检索，使用 RRF 融合算法
"""

from typing import List, Dict, Optional
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

from src.ai.embedder import Embedder
from src.utils.logger import get_logger
from src.retrieval.result import SearchResult
from src.retrieval.bm25_retriever import BM25Retriever
from src.retrieval.vector_retriever import VectorRetriever

logger = get_logger(__name__)


class HybridRetriever:
    """
    混合检索器

    并行执行 BM25 和向量检索，使用 RRF 算法融合结果
    """

    def __init__(
        self,
        db_path: Path,
        vector_index_dir: Path,
        embedder: Embedder,
        bm25_weight: float = 0.4,
        vector_weight: float = 0.6,
        rrf_k: int = 60,
    ):
        """
        初始化混合检索器

        Args:
            db_path: 数据库文件路径
            vector_index_dir: 向量索引目录
            embedder: 向量化工具
            bm25_weight: BM25 权重
            vector_weight: 向量权重
            rrf_k: RRF 算法常数
        """
        self.bm25_retriever = BM25Retriever(db_path)
        self.vector_retriever = VectorRetriever(db_path, vector_index_dir, embedder)
        self.bm25_weight = bm25_weight
        self.vector_weight = vector_weight
        self.rrf_k = rrf_k

    def search(self, query: str, limit: int = 10) -> List[SearchResult]:
        """
        执行混合检索

        Args:
            query: 查询文本
            limit: 返回结果数量

        Returns:
            搜索结果列表，按融合分数降序排列
        """
        if not query or not query.strip():
            logger.debug("查询文本为空，返回空结果")
            return []

        try:
            # 并行执行两种检索
            candidate_k = max(limit * 2, 20)  # 获取更多候选结果

            bm25_results = []
            vector_results = []

            with ThreadPoolExecutor(max_workers=2) as executor:
                # 提交并行任务
                future_bm25 = executor.submit(
                    self.bm25_retriever.search, query, candidate_k
                )
                future_vector = executor.submit(
                    self.vector_retriever.search, query, candidate_k
                )

                # 等待完成
                for future in as_completed([future_bm25, future_vector]):
                    if future == future_bm25:
                        bm25_results = future.result()
                    elif future == future_vector:
                        vector_results = future.result()

            # 处理边界情况
            if not bm25_results and not vector_results:
                logger.info("BM25 和向量检索均无结果")
                return []
            if not bm25_results:
                logger.info("BM25 无结果，使用向量检索结果")
                return vector_results[:limit]
            if not vector_results:
                logger.info("向量检索无结果，使用 BM25 结果")
                return bm25_results[:limit]

            # RRF 融合
            fused_results = self._rrf_fuse(
                bm25_results, vector_results, top_k=limit
            )

            logger.info(
                f"混合检索完成: BM25={len(bm25_results)}, "
                f"Vector={len(vector_results)}, 融合={len(fused_results)}"
            )
            return fused_results

        except Exception as e:
            logger.error(f"混合检索失败: {e}", exc_info=True)
            return []

    def _rrf_fuse(
        self,
        bm25_results: List[SearchResult],
        vector_results: List[SearchResult],
        top_k: int,
    ) -> List[SearchResult]:
        """
        使用 RRF (Reciprocal Rank Fusion) 融合两个结果列表

        RRF 公式: score = sum(weight_i / (k + rank_i))

        Args:
            bm25_results: BM25 检索结果
            vector_results: 向量检索结果
            top_k: 返回结果数量

        Returns:
            融合后的结果列表
        """
        # 构建排名映射
        bm25_ranks = {r.knowledge_id: (rank + 1) for rank, r in enumerate(bm25_results)}
        vector_ranks = {
            r.knowledge_id: (rank + 1) for rank, r in enumerate(vector_results)
        }

        # 收集所有候选 ID
        all_ids = set(bm25_ranks.keys()) | set(vector_ranks.keys())

        # 计算 RRF 分数
        rrf_scores: Dict[int, float] = {}
        for knowledge_id in all_ids:
            score = 0.0

            # BM25 贡献
            if knowledge_id in bm25_ranks:
                rank = bm25_ranks[knowledge_id]
                score += self.bm25_weight / (self.rrf_k + rank)

            # 向量贡献
            if knowledge_id in vector_ranks:
                rank = vector_ranks[knowledge_id]
                score += self.vector_weight / (self.rrf_k + rank)

            rrf_scores[knowledge_id] = score

        # 按 RRF 分数排序
        sorted_ids = sorted(rrf_scores.items(), key=lambda x: x[1], reverse=True)

        # 构建融合结果
        # 使用字典快速查找
        id_to_bm25 = {r.knowledge_id: r for r in bm25_results}
        id_to_vector = {r.knowledge_id: r for r in vector_results}

        fused_results = []
        for knowledge_id, rrf_score in sorted_ids[:top_k]:
            # 合并结果（优先使用 BM25 的元数据）
            bm25_result = id_to_bm25.get(knowledge_id)
            vector_result = id_to_vector.get(knowledge_id)

            merged_result = self._merge_result(bm25_result, vector_result, rrf_score)
            if merged_result:
                fused_results.append(merged_result)

        return fused_results

    @staticmethod
    def _merge_result(
        bm25_result: Optional[SearchResult],
        vector_result: Optional[SearchResult],
        rrf_score: float,
    ) -> Optional[SearchResult]:
        """
        合并两个检索结果

        Args:
            bm25_result: BM25 结果（可能为 None）
            vector_result: 向量结果（可能为 None）
            rrf_score: RRF 融合分数

        Returns:
            合并后的结果，创建新的 SearchResult 对象
        """
        # 至少有一个结果
        if bm25_result is None and vector_result is None:
            return None

        # 选择主结果（优先 BM25）
        primary = bm25_result if bm25_result is not None else vector_result
        secondary = vector_result if bm25_result is not None else bm25_result

        # 合并元数据（创建新字典）
        merged_metadata = dict(primary.metadata)
        if secondary is not None:
            for key, value in secondary.metadata.items():
                if key not in merged_metadata or merged_metadata[key] in (None, "", []):
                    merged_metadata[key] = value

        # 添加 RRF 分数
        merged_metadata["rrf_score"] = rrf_score

        # 创建新的 SearchResult（不修改原对象）
        return SearchResult(
            knowledge_id=primary.knowledge_id,
            title=primary.title or (secondary.title if secondary else ""),
            score=min(rrf_score, 1.0),  # 确保在 [0.0, 1.0] 范围内
            highlight=primary.highlight or (secondary.highlight if secondary else ""),
            metadata=merged_metadata,
        )
