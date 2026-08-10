"""Hybrid BM25/vector retrieval with observable branch degradation."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, Optional

from src.ai.embedder import Embedder
from src.retrieval.bm25_retriever import BM25Retriever
from src.retrieval.result import (
    RetrievalIssue,
    SearchResponse,
    SearchResult,
    is_strict_search_response,
)
from src.retrieval.vector_retriever import VectorRetriever
from src.runtime.errors import ErrorCode
from src.utils.logger import get_logger

logger = get_logger(__name__)


class HybridRetriever:
    """Run BM25 and vector branches in parallel and fuse healthy results."""

    def __init__(
        self,
        db_path: Path,
        vector_index_dir: Path,
        embedder: Embedder | None = None,
        bm25_weight: float = 0.4,
        vector_weight: float = 0.6,
        rrf_k: int = 60,
        *,
        embedder_factory: Callable[[], Embedder] | None = None,
    ) -> None:
        self.bm25_retriever = BM25Retriever(db_path)
        self.vector_retriever = VectorRetriever(
            db_path,
            vector_index_dir,
            embedder,
            embedder_factory=embedder_factory,
        )
        self.bm25_weight = bm25_weight
        self.vector_weight = vector_weight
        self.rrf_k = rrf_k

    def search(self, query: str, limit: int = 10) -> SearchResponse:
        """Return success/no_hits/invalid/error/degraded without branch masking."""

        if not isinstance(query, str) or not query.strip():
            logger.debug("查询文本为空，拒绝混合检索")
            return SearchResponse.invalid("查询文本不能为空", strategy="hybrid")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            logger.debug("混合检索 limit 非法: %r", limit)
            return SearchResponse.invalid(
                "limit 必须是正整数",
                strategy="hybrid",
                stage="limit_validation",
            )

        candidate_k = max(limit * 2, 20)
        responses: dict[str, SearchResponse] = {}

        try:
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = {
                    executor.submit(self.bm25_retriever.search, query, candidate_k): "bm25",
                    executor.submit(self.vector_retriever.search, query, candidate_k): "vector",
                }
                for future in as_completed(futures):
                    branch = futures[future]
                    try:
                        response = future.result()
                    except Exception as exc:
                        issue = RetrievalIssue.from_exception(
                            exc,
                            fallback_code=ErrorCode.RETRIEVAL_BACKEND_FAILED,
                            public_message=f"{branch} 检索分支不可用",
                            stage=f"hybrid_{branch}_branch",
                            recoverable=True,
                        )
                        logger.error(
                            "混合检索分支抛出异常: branch=%s, error_type=%s",
                            branch,
                            issue.cause_type,
                        )
                        response = SearchResponse.failed_response(
                            issue,
                            strategy=branch,
                        )

                    contract_valid = is_strict_search_response(response)
                    strategy_matches = contract_valid and response.strategy == branch
                    if not strategy_matches:
                        logger.error(
                            "混合检索分支响应合同无效: branch=%s, "
                            "contract_valid=%s, strategy_match=%s",
                            branch,
                            contract_valid,
                            strategy_matches,
                        )
                        response = SearchResponse.failed_response(
                            RetrievalIssue(
                                code=ErrorCode.RETRIEVAL_BACKEND_FAILED,
                                message=f"{branch} 检索分支返回无效响应",
                                stage=f"hybrid_{branch}_protocol",
                                recoverable=True,
                                cause_type=(
                                    "SearchStrategyMismatch"
                                    if contract_valid
                                    else "InvalidSearchResponse"
                                ),
                            ),
                            strategy=branch,
                        )
                    responses[branch] = response
        except Exception as exc:
            issue = RetrievalIssue.from_exception(
                exc,
                fallback_code=ErrorCode.RETRIEVAL_BACKEND_FAILED,
                public_message="混合检索执行失败",
                stage="hybrid_executor",
                recoverable=True,
            )
            logger.error(
                "混合检索执行器失败: error_type=%s",
                issue.cause_type,
            )
            return SearchResponse.failed_response(
                issue,
                strategy="hybrid",
            )

        # Iterate in fixed branch order; completion timing must not reorder
        # public issues or change the aggregate state.
        ordered = (responses["bm25"], responses["vector"])
        unhealthy = tuple(
            response
            for response in ordered
            if response.status in {"invalid", "error", "degraded"}
        )
        issues = tuple(issue for response in unhealthy for issue in response.issues)

        if len(unhealthy) == 2:
            if all(response.status == "invalid" for response in unhealthy):
                return SearchResponse(
                    status="invalid",
                    strategy="hybrid",
                    issues=issues,
                )
            has_usable_results = any(response.results for response in ordered)
            has_trusted_completion = any(
                response.status in {"success", "no_hits"}
                for response in ordered
            )
            has_partial_trust = any(
                response.status == "degraded" for response in ordered
            )
            if (
                not has_usable_results
                and not has_trusted_completion
                and not has_partial_trust
            ):
                logger.error("BM25 与向量分支均未完整完成且无可用结果")
                return SearchResponse(
                    status="error",
                    strategy="hybrid",
                    issues=issues,
                )

        bm25_results = responses["bm25"].results
        vector_results = responses["vector"].results
        try:
            combined = self._combine_results(bm25_results, vector_results, limit)
        except Exception as exc:
            issue = RetrievalIssue.from_exception(
                exc,
                fallback_code=ErrorCode.RETRIEVAL_BACKEND_FAILED,
                public_message="混合检索结果融合失败",
                stage="hybrid_fusion",
                recoverable=True,
            )
            logger.error(
                "混合检索结果融合失败: error_type=%s",
                issue.cause_type,
            )
            return SearchResponse.failed_response(
                issue,
                strategy="hybrid",
            )

        if unhealthy:
            logger.warning(
                "混合检索降级: incomplete_strategies=%s, result_count=%s",
                ",".join(response.strategy for response in unhealthy),
                len(combined),
            )
            return SearchResponse.degraded_response(
                combined,
                issues,
                strategy="hybrid",
            )

        logger.info(
            "混合检索完成: BM25=%s, Vector=%s, 融合=%s",
            len(bm25_results),
            len(vector_results),
            len(combined),
        )
        return SearchResponse.completed(combined, strategy="hybrid")

    def _combine_results(
        self,
        bm25_results: Sequence[SearchResult],
        vector_results: Sequence[SearchResult],
        limit: int,
    ) -> tuple[SearchResult, ...]:
        if not bm25_results:
            return tuple(vector_results[:limit])
        if not vector_results:
            return tuple(bm25_results[:limit])
        return tuple(self._rrf_fuse(bm25_results, vector_results, top_k=limit))

    def _rrf_fuse(
        self,
        bm25_results: Sequence[SearchResult],
        vector_results: Sequence[SearchResult],
        top_k: int,
    ) -> list[SearchResult]:
        """Fuse two ranked lists; exact tie/merge hardening remains P1."""

        bm25_ranks = {r.knowledge_id: rank + 1 for rank, r in enumerate(bm25_results)}
        vector_ranks = {
            r.knowledge_id: rank + 1 for rank, r in enumerate(vector_results)
        }
        all_ids = set(bm25_ranks) | set(vector_ranks)

        rrf_scores: Dict[int, float] = {}
        for knowledge_id in all_ids:
            score = 0.0
            if knowledge_id in bm25_ranks:
                score += self.bm25_weight / (
                    self.rrf_k + bm25_ranks[knowledge_id]
                )
            if knowledge_id in vector_ranks:
                score += self.vector_weight / (
                    self.rrf_k + vector_ranks[knowledge_id]
                )
            rrf_scores[knowledge_id] = score

        sorted_ids = sorted(rrf_scores.items(), key=lambda item: item[1], reverse=True)
        id_to_bm25 = {r.knowledge_id: r for r in bm25_results}
        id_to_vector = {r.knowledge_id: r for r in vector_results}

        fused_results: list[SearchResult] = []
        for knowledge_id, rrf_score in sorted_ids[:top_k]:
            merged = self._merge_result(
                id_to_bm25.get(knowledge_id),
                id_to_vector.get(knowledge_id),
                rrf_score,
            )
            if merged is not None:
                fused_results.append(merged)
        return fused_results

    @staticmethod
    def _merge_result(
        bm25_result: Optional[SearchResult],
        vector_result: Optional[SearchResult],
        rrf_score: float,
    ) -> Optional[SearchResult]:
        if bm25_result is None and vector_result is None:
            return None

        primary = bm25_result if bm25_result is not None else vector_result
        secondary = vector_result if bm25_result is not None else bm25_result
        assert primary is not None

        merged_metadata = dict(primary.metadata)
        if secondary is not None:
            for key, value in secondary.metadata.items():
                if key not in merged_metadata or merged_metadata[key] in (None, "", []):
                    merged_metadata[key] = value
        merged_metadata["rrf_score"] = rrf_score

        return SearchResult(
            knowledge_id=primary.knowledge_id,
            title=primary.title or (secondary.title if secondary else ""),
            score=min(rrf_score, 1.0),
            highlight=primary.highlight or (secondary.highlight if secondary else ""),
            metadata=merged_metadata,
        )
