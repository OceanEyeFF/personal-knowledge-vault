"""Query router that preserves the selected retriever's explicit outcome."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from src.ai.embedder import Embedder
from src.retrieval.bm25_retriever import BM25Retriever
from src.retrieval.hybrid_retriever import HybridRetriever
from src.retrieval.result import (
    RetrievalIssue,
    SearchResponse,
    is_strict_search_response,
)
from src.runtime.errors import ErrorCode
from src.utils.logger import get_logger
from src.utils.text_utils import TextProcessor

logger = get_logger(__name__)


class QueryRouter:
    """Route short queries to BM25 and longer queries to hybrid retrieval."""

    def __init__(
        self,
        db_path: Path,
        vector_index_dir: Path,
        embedder: Embedder | None = None,
        token_threshold: int = 5,
        *,
        embedder_factory: Callable[[], Embedder] | None = None,
        runtime_config: Any = None,
        hybrid_retriever_factory: Callable[[], Any] | None = None,
    ) -> None:
        if isinstance(token_threshold, bool) or not isinstance(token_threshold, int):
            raise ValueError("token_threshold 必须是正整数")
        if token_threshold <= 0:
            raise ValueError("token_threshold 必须是正整数")

        # The router, its BM25 retriever and FTS store must all use one captured
        # Config snapshot.  Falling back to TextProcessor()->get_config() here
        # would let an explicit Config B graph observe global Config A.
        self.text_processor = TextProcessor(runtime_config=runtime_config)
        self.bm25_retriever = BM25Retriever(
            db_path,
            runtime_config=runtime_config,
            text_processor=self.text_processor,
        )
        self._hybrid_retriever_factory = hybrid_retriever_factory
        self.hybrid_retriever = (
            None
            if hybrid_retriever_factory is not None
            else HybridRetriever(
                db_path,
                vector_index_dir,
                embedder,
                embedder_factory=embedder_factory,
                runtime_config=runtime_config,
            )
        )
        self.token_threshold = token_threshold

    def search(self, query: str, limit: int = 10) -> SearchResponse:
        """Return the chosen strategy's five-state response unchanged."""

        if not isinstance(query, str) or not query.strip():
            logger.debug("查询文本为空，拒绝路由")
            return SearchResponse.invalid("查询文本不能为空", strategy="router")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            logger.debug("查询路由 limit 非法: %r", limit)
            return SearchResponse.invalid(
                "limit 必须是正整数",
                strategy="router",
                stage="limit_validation",
            )

        try:
            tokenized = self.text_processor.tokenize_chinese(query)
            tokens = tokenized.split()
        except Exception as exc:
            issue = RetrievalIssue.from_exception(
                exc,
                fallback_code=ErrorCode.RETRIEVAL_BACKEND_FAILED,
                public_message="查询路由暂时不可用",
                stage="query_router_tokenize",
                recoverable=True,
            )
            logger.error(
                "查询路由分词失败: error_type=%s",
                issue.cause_type,
            )
            return SearchResponse.failed_response(
                issue,
                strategy="router",
            )

        if not tokens:
            return SearchResponse.invalid(
                "查询分词后没有可检索 token",
                strategy="router",
            )

        if len(tokens) < self.token_threshold:
            selected = "bm25"
            retriever = self.bm25_retriever
        else:
            selected = "hybrid"
            hybrid_retriever_factory = getattr(self, "_hybrid_retriever_factory", None)
            retriever = (
                hybrid_retriever_factory()
                if hybrid_retriever_factory is not None
                else self.hybrid_retriever
            )

        logger.info(
            "查询分词数=%s, threshold=%s, strategy=%s",
            len(tokens),
            self.token_threshold,
            selected,
        )
        try:
            response = retriever.search(query, limit)
        except Exception as exc:
            issue = RetrievalIssue.from_exception(
                exc,
                fallback_code=ErrorCode.RETRIEVAL_BACKEND_FAILED,
                public_message=f"{selected} 检索暂时不可用",
                stage=f"query_router_{selected}",
                recoverable=True,
            )
            logger.error(
                "查询路由下游异常: strategy=%s, error_type=%s",
                selected,
                issue.cause_type,
            )
            return SearchResponse.failed_response(
                issue,
                strategy=selected,
            )

        contract_valid = is_strict_search_response(response)
        strategy_matches = contract_valid and response.strategy == selected
        if strategy_matches:
            return response

        logger.error(
            "查询路由下游响应合同无效: strategy=%s, contract_valid=%s, "
            "strategy_match=%s",
            selected,
            contract_valid,
            strategy_matches,
        )
        return SearchResponse.failed_response(
            RetrievalIssue(
                code=ErrorCode.RETRIEVAL_BACKEND_FAILED,
                message=f"{selected} 检索返回无效响应",
                stage=f"query_router_{selected}_protocol",
                recoverable=True,
                cause_type=(
                    "SearchStrategyMismatch"
                    if contract_valid
                    else "InvalidSearchResponse"
                ),
            ),
            strategy=selected,
        )
