"""Vector retrieval with explicit failure and degradation states."""

from __future__ import annotations

from collections.abc import Callable
import math
from numbers import Integral
from pathlib import Path
from threading import Lock
from typing import Any

import numpy as np

from src.ai.embedder import Embedder
from src.ai.openai_client import project_float32_cosine_vector
from src.retrieval.result import RetrievalIssue, SearchResponse, SearchResult
from src.runtime.errors import ErrorCode, PKVRuntimeError
from src.storage.sqlite_store import SQLiteStore
from src.storage.vector_store import VectorStore
from src.utils.logger import get_logger

logger = get_logger(__name__)

EmbedderFactory = Callable[[], Embedder]


class VectorRetriever:
    """Search document or chunk indexes without hiding provider/index failures."""

    def __init__(
        self,
        db_path: Path,
        vector_index_dir: Path,
        embedder: Embedder | None = None,
        *,
        embedder_factory: EmbedderFactory | None = None,
        runtime_config: Any = None,
    ) -> None:
        if embedder is not None and embedder_factory is not None:
            raise ValueError("embedder 与 embedder_factory 只能提供一个")

        self.store = SQLiteStore(db_path)
        self.vector_index_dir = Path(vector_index_dir)
        self.embedder = embedder
        self._embedder_factory = embedder_factory
        self._runtime_config = runtime_config
        self._embedder_lock = Lock()
        self._embedder_dim = self._read_embedder_dim(embedder)

        # Read paths must not create an empty index.  Both index and provider
        # are therefore acquired only when semantic retrieval is requested.
        self.vector_store: VectorStore | None = None

    def search(self, query: str, limit: int = 10) -> SearchResponse:
        """Execute document-level semantic retrieval."""

        invalid = self._validate_request(query, limit)
        if invalid is not None:
            return invalid

        vector_store_response = self._load_vector_store(strategy="vector")
        if isinstance(vector_store_response, SearchResponse):
            return vector_store_response
        vector_store = vector_store_response

        query_vector_response = self._embed_query(
            query,
            vector_store,
            strategy="vector",
        )
        if isinstance(query_vector_response, SearchResponse):
            return query_vector_response
        query_vector = query_vector_response

        try:
            vector_hits = vector_store.search_doc(query_vector, k=limit)
        except Exception as exc:
            logger.error(
                "文档向量索引查询失败: error_type=%s",
                type(exc).__name__,
            )
            return self._failed_from_exception(
                exc,
                fallback_code=ErrorCode.RETRIEVAL_BACKEND_FAILED,
                public_message="向量索引查询失败",
                stage="vector_index_search",
                recoverable=True,
                strategy="vector",
            )

        if type(vector_hits) is not list:
            logger.error("文档向量索引返回了无效命中集合")
            return self._invalid_hit_collection(strategy="vector")
        if not vector_hits:
            logger.info("向量检索无结果")
            return SearchResponse.completed((), strategy="vector")
        return self._map_document_hits(vector_hits)

    def search_chunks(self, query: str, limit: int = 10) -> SearchResponse:
        """Execute chunk-level semantic retrieval using the same five states."""

        invalid = self._validate_request(query, limit, stage="chunk_query_validation")
        if invalid is not None:
            return invalid

        vector_store_response = self._load_vector_store(strategy="vector_chunks")
        if isinstance(vector_store_response, SearchResponse):
            return vector_store_response
        vector_store = vector_store_response

        query_vector_response = self._embed_query(
            query,
            vector_store,
            strategy="vector_chunks",
        )
        if isinstance(query_vector_response, SearchResponse):
            return query_vector_response
        query_vector = query_vector_response

        try:
            vector_hits = vector_store.search_chunk(query_vector, k=limit)
        except Exception as exc:
            logger.error(
                "chunk 向量索引查询失败: error_type=%s",
                type(exc).__name__,
            )
            return self._failed_from_exception(
                exc,
                fallback_code=ErrorCode.RETRIEVAL_BACKEND_FAILED,
                public_message="chunk 向量索引查询失败",
                stage="chunk_vector_index_search",
                recoverable=True,
                strategy="vector_chunks",
            )

        if type(vector_hits) is not list:
            logger.error("chunk 向量索引返回了无效命中集合")
            return self._invalid_hit_collection(strategy="vector_chunks")
        if not vector_hits:
            logger.info("chunk 向量检索无结果")
            return SearchResponse.completed((), strategy="vector_chunks")
        return self._map_chunk_hits(vector_hits)

    def _validate_request(
        self,
        query: str,
        limit: int,
        *,
        stage: str = "query_validation",
    ) -> SearchResponse | None:
        strategy = "vector_chunks" if stage.startswith("chunk_") else "vector"
        if not isinstance(query, str) or not query.strip():
            logger.debug("查询文本为空，拒绝向量检索")
            return SearchResponse.invalid(
                "查询文本不能为空",
                strategy=strategy,
                stage=stage,
            )
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            logger.debug("向量检索 limit 非法: %r", limit)
            return SearchResponse.invalid(
                "limit 必须是正整数",
                strategy=strategy,
                stage="chunk_limit_validation" if stage.startswith("chunk_") else "limit_validation",
            )
        return None

    def _load_vector_store(self, *, strategy: str) -> VectorStore | SearchResponse:
        if self.vector_store is not None:
            return self.vector_store

        try:
            has_artifacts = VectorStore.has_index_artifacts(self.vector_index_dir)
        except Exception as exc:
            logger.error(
                "检查向量索引失败: error_type=%s",
                type(exc).__name__,
            )
            return self._failed_from_exception(
                exc,
                fallback_code=ErrorCode.RETRIEVAL_INDEX_UNAVAILABLE,
                public_message="向量索引状态不可判定",
                stage="vector_index_probe",
                recoverable=True,
                strategy=strategy,
            )

        if not has_artifacts:
            logger.warning("向量索引不存在")
            return SearchResponse.failed_response(
                RetrievalIssue(
                    code=ErrorCode.RETRIEVAL_INDEX_UNAVAILABLE,
                    message="向量索引尚不可用",
                    stage="vector_index_load",
                    recoverable=True,
                ),
                strategy=strategy,
            )

        try:
            self.vector_store = VectorStore(
                self.vector_index_dir,
                dim=self._embedder_dim,
                runtime_config=self._runtime_config,
                allow_index_creation=False,
            )
            return self.vector_store
        except Exception as exc:
            logger.error(
                "加载向量索引失败: error_type=%s",
                type(exc).__name__,
            )
            return self._failed_from_exception(
                exc,
                fallback_code=ErrorCode.RETRIEVAL_INDEX_UNAVAILABLE,
                public_message="向量索引无法加载",
                stage="vector_index_load",
                recoverable=True,
                strategy=strategy,
            )

    def _get_embedder(self) -> Embedder:
        if self.embedder is not None:
            return self.embedder

        with self._embedder_lock:
            if self.embedder is not None:
                return self.embedder
            if self._embedder_factory is None:
                raise PKVRuntimeError(
                    ErrorCode.PROVIDER_CONFIG_INVALID,
                    "未配置 Embedding Provider",
                    stage="provider_configuration",
                    recoverable=True,
                )
            embedder = self._embedder_factory()
            if embedder is None or not callable(getattr(embedder, "embed_document", None)):
                raise PKVRuntimeError(
                    ErrorCode.PROVIDER_CONFIG_INVALID,
                    "Embedding Provider 接口无效",
                    stage="provider_configuration",
                    recoverable=True,
                )
            self.embedder = embedder
            self._embedder_dim = self._read_embedder_dim(embedder)
            return embedder

    def _embed_query(
        self,
        query: str,
        vector_store: VectorStore,
        *,
        strategy: str,
    ) -> Any | SearchResponse:
        try:
            embedder = self._get_embedder()
            provider_dim = self._read_embedder_dim(embedder)
            if provider_dim is not None and provider_dim != vector_store.dim:
                raise PKVRuntimeError(
                    ErrorCode.PROVIDER_PROTOCOL_FAILED,
                    "Embedding 维度与向量索引不一致",
                    stage="embedding_protocol",
                    recoverable=True,
                )
            query_vector = embedder.embed_document(query)
        except Exception as exc:
            logger.error(
                "查询向量生成失败: error_type=%s",
                type(exc).__name__,
            )
            public_message = "Embedding Provider 不可用"
            public_stage = "embedding_provider"
            if (
                isinstance(exc, PKVRuntimeError)
                and exc.code is ErrorCode.PROVIDER_PROTOCOL_FAILED
            ):
                public_message = "Embedding Provider 响应与向量索引不兼容"
                public_stage = "embedding_protocol"
            return self._failed_from_exception(
                exc,
                fallback_code=ErrorCode.PROVIDER_UNAVAILABLE,
                public_message=public_message,
                stage=public_stage,
                recoverable=True,
                strategy=strategy,
            )

        converted_vector: np.ndarray | None = None
        try:
            normalized_vector = np.asarray(query_vector)
            real_numeric = np.issubdtype(
                normalized_vector.dtype,
                np.integer,
            ) or np.issubdtype(normalized_vector.dtype, np.floating)
            vector_is_valid = (
                normalized_vector.ndim == 1
                and len(normalized_vector) == int(vector_store.dim)
                and real_numeric
                and bool(np.all(np.isfinite(normalized_vector)))
            )
            if vector_is_valid:
                projected_vector = project_float32_cosine_vector(
                    normalized_vector
                )
                # Freeze a private snapshot before handing the query to the
                # backend.  A custom Embedder may otherwise retain and mutate
                # the array after validation.
                converted_vector = np.array(
                    projected_vector,
                    dtype=np.float32,
                    order="C",
                    copy=True,
                    subok=False,
                )
                with np.errstate(over="ignore", under="ignore", invalid="ignore"):
                    norm_squared = np.sum(
                        converted_vector * converted_vector,
                        dtype=np.float32,
                    )
                    norm = np.sqrt(norm_squared)
                vector_is_valid = bool(
                    converted_vector.ndim == 1
                    and len(converted_vector) == int(vector_store.dim)
                    and converted_vector.dtype == np.float32
                    and np.all(np.isfinite(converted_vector))
                    and np.isfinite(norm_squared)
                    and norm_squared > np.float32(0.0)
                    and np.isfinite(norm)
                    and norm > np.float32(0.0)
                )
        except Exception:
            vector_is_valid = False
        if not vector_is_valid:
            logger.error("Embedding Provider 返回了无效向量")
            return SearchResponse.failed_response(
                RetrievalIssue(
                    code=ErrorCode.PROVIDER_PROTOCOL_FAILED,
                    message="Embedding Provider 返回无效响应",
                    stage="embedding_protocol",
                    recoverable=True,
                ),
                strategy=strategy,
            )
        assert converted_vector is not None
        return converted_vector

    def _map_document_hits(self, vector_hits: list[Any]) -> SearchResponse:
        results: list[SearchResult] = []
        issues: list[RetrievalIssue] = []

        for rank, raw_hit in enumerate(vector_hits, start=1):
            if (
                type(raw_hit) is not tuple
                or len(raw_hit) != 2
                or type(raw_hit[0]) is not int
                or raw_hit[0] <= 0
                or type(raw_hit[1]) not in {int, float}
                or not math.isfinite(raw_hit[1])
            ):
                logger.error("文档向量命中结构无效")
                issues.append(self._invalid_hit_issue(strategy="vector"))
                continue
            knowledge_id, distance = raw_hit

            try:
                metadata = self._get_metadata(knowledge_id)
            except Exception as exc:
                logger.error(
                    "读取文档向量命中元数据失败: error_type=%s",
                    type(exc).__name__,
                )
                issues.append(
                    RetrievalIssue.from_exception(
                        exc,
                        fallback_code=ErrorCode.RETRIEVAL_BACKEND_FAILED,
                        public_message="向量命中元数据读取失败",
                        stage="vector_metadata_read",
                        recoverable=True,
                    )
                )
                continue

            if (
                type(metadata) is not dict
                or type(metadata.get("knowledge_id")) is not int
                or metadata["knowledge_id"] != knowledge_id
            ):
                logger.warning("向量命中文档元数据结构或身份不一致")
                issues.append(
                    RetrievalIssue(
                        code=ErrorCode.RETRIEVAL_METADATA_INCONSISTENT,
                        message="向量命中与文档元数据不一致",
                        stage="vector_metadata_mapping",
                        recoverable=True,
                    )
                )
                continue

            try:
                enriched = dict(metadata)
                enriched["vector_distance"] = float(distance)
                enriched["vector_rank"] = rank
                results.append(
                    SearchResult(
                        knowledge_id=knowledge_id,
                        title=metadata.get("title") or f"条目 {knowledge_id}",
                        score=self._distance_to_score(distance),
                        highlight=(
                            metadata.get("summary_one_sentence", "")
                            or metadata.get("summary_100_words", "")[:200]
                        ),
                        metadata=enriched,
                    )
                )
            except Exception as exc:
                logger.error(
                    "构造文档向量命中失败: error_type=%s",
                    type(exc).__name__,
                )
                issues.append(
                    RetrievalIssue.from_exception(
                        exc,
                        fallback_code=ErrorCode.RETRIEVAL_METADATA_INCONSISTENT,
                        public_message="向量命中内容无效",
                        stage="vector_result_mapping",
                        recoverable=True,
                    )
                )

        return self._mapped_response(results, issues, strategy="vector")

    def _map_chunk_hits(self, vector_hits: list[Any]) -> SearchResponse:
        results: list[SearchResult] = []
        issues: list[RetrievalIssue] = []

        for rank, raw_hit in enumerate(vector_hits, start=1):
            if (
                type(raw_hit) is not tuple
                or len(raw_hit) != 3
                or type(raw_hit[0]) is not int
                or raw_hit[0] <= 0
                or type(raw_hit[1]) is not int
                or raw_hit[1] < 0
                or type(raw_hit[2]) not in {int, float}
                or not math.isfinite(raw_hit[2])
            ):
                logger.error("chunk 向量命中结构无效")
                issues.append(self._invalid_hit_issue(strategy="vector_chunks"))
                continue
            knowledge_id, chunk_index, distance = raw_hit

            try:
                metadata = self._get_chunk_metadata(
                    knowledge_id,
                    chunk_index,
                )
            except Exception as exc:
                logger.error(
                    "读取 chunk 向量命中元数据失败: error_type=%s",
                    type(exc).__name__,
                )
                issues.append(
                    RetrievalIssue.from_exception(
                        exc,
                        fallback_code=ErrorCode.RETRIEVAL_BACKEND_FAILED,
                        public_message="chunk 命中元数据读取失败",
                        stage="chunk_metadata_read",
                        recoverable=True,
                    )
                )
                continue

            if (
                type(metadata) is not dict
                or type(metadata.get("knowledge_id")) is not int
                or metadata["knowledge_id"] != knowledge_id
                or type(metadata.get("chunk_id")) is not int
                or metadata["chunk_id"] <= 0
                or type(metadata.get("chunk_index")) is not int
                or metadata["chunk_index"] != chunk_index
            ):
                logger.warning("chunk 向量命中元数据结构或身份不一致")
                issues.append(
                    RetrievalIssue(
                        code=ErrorCode.RETRIEVAL_METADATA_INCONSISTENT,
                        message="chunk 向量命中与元数据不一致",
                        stage="chunk_metadata_mapping",
                        recoverable=True,
                    )
                )
                continue

            try:
                enriched = dict(metadata)
                enriched["vector_distance"] = float(distance)
                enriched["vector_rank"] = rank
                results.append(
                    SearchResult(
                        knowledge_id=knowledge_id,
                        title=metadata.get("title") or f"条目 {knowledge_id}",
                        score=self._distance_to_score(distance),
                        highlight=metadata.get("chunk_text", "")[:200],
                        metadata=enriched,
                    )
                )
            except Exception as exc:
                logger.error(
                    "构造 chunk 向量命中失败: error_type=%s",
                    type(exc).__name__,
                )
                issues.append(
                    RetrievalIssue.from_exception(
                        exc,
                        fallback_code=ErrorCode.RETRIEVAL_METADATA_INCONSISTENT,
                        public_message="chunk 向量命中内容无效",
                        stage="chunk_result_mapping",
                        recoverable=True,
                    )
                )

        return self._mapped_response(results, issues, strategy="vector_chunks")

    @staticmethod
    def _mapped_response(
        results: list[SearchResult],
        issues: list[RetrievalIssue],
        *,
        strategy: str,
    ) -> SearchResponse:
        if issues and results:
            return SearchResponse.degraded_response(
                results,
                issues,
                strategy=strategy,
            )
        if issues:
            return SearchResponse(
                status="error",
                strategy=strategy,
                issues=tuple(issues),
            )
        return SearchResponse.completed(results, strategy=strategy)

    @staticmethod
    def _invalid_hit_issue(*, strategy: str) -> RetrievalIssue:
        is_chunk = strategy == "vector_chunks"
        return RetrievalIssue(
            code=ErrorCode.RETRIEVAL_METADATA_INCONSISTENT,
            message=(
                "chunk 向量索引返回无效命中结构"
                if is_chunk
                else "向量索引返回无效命中结构"
            ),
            stage="chunk_hit_mapping" if is_chunk else "vector_hit_mapping",
            recoverable=True,
        )

    @classmethod
    def _invalid_hit_collection(cls, *, strategy: str) -> SearchResponse:
        return SearchResponse.failed_response(
            cls._invalid_hit_issue(strategy=strategy),
            strategy=strategy,
        )

    @staticmethod
    def _read_embedder_dim(embedder: Embedder | None) -> int | None:
        embedder_dim = getattr(embedder, "dim", None)
        if not isinstance(embedder_dim, Integral) or isinstance(embedder_dim, bool):
            return None
        return int(embedder_dim)

    @staticmethod
    def _failed_from_exception(
        exc: BaseException,
        *,
        fallback_code: ErrorCode,
        public_message: str,
        stage: str,
        recoverable: bool,
        strategy: str,
    ) -> SearchResponse:
        return SearchResponse.failed_response(
            RetrievalIssue.from_exception(
                exc,
                fallback_code=fallback_code,
                public_message=public_message,
                stage=stage,
                recoverable=recoverable,
            ),
            strategy=strategy,
        )

    def _get_metadata(self, knowledge_id: int) -> dict[str, Any] | None:
        """Map one document-level vector id to SQLite metadata."""

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
                return None

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

    def _get_chunk_metadata(
        self,
        knowledge_id: int,
        chunk_index: int,
    ) -> dict[str, Any] | None:
        """Map one encoded chunk hit to SQLite metadata."""

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
                return None

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

    @staticmethod
    def _distance_to_score(distance: float) -> float:
        """Convert cosine distance to a bounded relevance score."""

        score = 1.0 - float(distance)
        return max(min(score, 1.0), 0.0)
