"""
证据聚合服务。

基于检索、关系解释和条目元数据构建最小证据包。
当前版本默认返回条目级证据，并在 `include_chunks=True` 时启用
chunk-aware 检索路径；若 chunk 路径不可用或检索异常，会显式退化回文档级证据。
"""

from __future__ import annotations

from datetime import datetime
from difflib import SequenceMatcher
import logging
from pathlib import Path
import re
from typing import Any, Optional

from src.relations.citations import build_chunk_locator, resolve_citation_source
from src.relations.models import CollectedEvidenceItem, CollectedEvidenceResult
from src.utils.text_utils import get_text_processor

logger = logging.getLogger(__name__)


class EvidenceCollectionService:
    """面向 Phase B 的最小证据聚合服务。"""

    EXACT_DUPLICATE_THRESHOLD = 0.95
    CHUNK_STATUS_NOT_REQUESTED = "not_requested"
    CHUNK_STATUS_SUCCESS = "success"
    CHUNK_STATUS_NO_HITS = "no_hits"
    CHUNK_STATUS_PATH_UNAVAILABLE = "path_unavailable"
    CHUNK_STATUS_SEARCH_ERROR = "search_error"
    CHUNK_DEGRADED_REASON_PATH_UNAVAILABLE = "path_unavailable"
    CHUNK_DEGRADED_REASON_SEARCH_ERROR = "search_error"

    def __init__(
        self,
        query_router: Any,
        sqlite_store: Any,
        markdown_store: Any,
        relation_query_service: Any,
        chunk_searcher: Optional[Any] = None,
    ) -> None:
        self.query_router = query_router
        self.sqlite_store = sqlite_store
        self.markdown_store = markdown_store
        self.relation_query_service = relation_query_service
        self.chunk_searcher = chunk_searcher
        self.text_processor = get_text_processor()

    def collect_evidence(
        self,
        question: str,
        top_k: int = 5,
        relation_max_depth: int = 2,
        include_chunks: bool = False,
    ) -> CollectedEvidenceResult:
        """围绕问题收集最小可解释证据包。"""
        question_clean = question.strip()
        if not question_clean:
            raise ValueError("question 不能为空")
        if top_k <= 0:
            raise ValueError("top_k 必须大于 0")
        if relation_max_depth <= 0:
            raise ValueError("relation_max_depth 必须大于 0")

        search_results = self.query_router.search(question_clean, limit=top_k)
        limitation_notes: list[str] = []
        chunk_retrieval_status = self.CHUNK_STATUS_NOT_REQUESTED
        if include_chunks:
            chunk_results, chunk_retrieval_status, chunk_limitation_note = (
                self._search_chunk_results(question_clean, limit=max(top_k * 2, top_k))
            )
            if chunk_limitation_note:
                limitation_notes.append(chunk_limitation_note)
        else:
            chunk_results = []

        if not search_results and not chunk_results:
            return CollectedEvidenceResult(
                question=question_clean,
                found=False,
                summary=f"未找到与问题「{question_clean}」直接相关的证据",
                limitation_notes=limitation_notes,
                chunk_retrieval_status=chunk_retrieval_status,
            )

        seed_result = search_results[0] if search_results else chunk_results[0]
        seed_entry = self.sqlite_store.query_by_id(seed_result.knowledge_id) or {}
        seed_title = seed_entry.get("title", seed_result.title)
        if not include_chunks:
            evidence_items = self._build_document_evidence_items(
                search_results=search_results,
                top_k=top_k,
                seed_knowledge_id=seed_result.knowledge_id,
                relation_max_depth=relation_max_depth,
            )
            related_count = sum(1 for item in evidence_items if item.relation_found)
            summary = (
                f"围绕问题「{question_clean}」共聚合 {len(evidence_items)} 条证据，"
                f"其中 {related_count} 条可回溯到与种子条目的关系解释"
            )
            return CollectedEvidenceResult(
                question=question_clean,
                found=True,
                seed_knowledge_id=seed_result.knowledge_id,
                seed_title=seed_title,
                evidence=evidence_items,
                summary=summary,
                limitation_notes=limitation_notes,
                chunk_retrieval_status=chunk_retrieval_status,
            )

        chunk_result_by_knowledge: dict[int, Any] = {}
        for result in chunk_results:
            existing = chunk_result_by_knowledge.get(result.knowledge_id)
            if existing is None or float(result.score) > float(existing.score):
                chunk_result_by_knowledge[result.knowledge_id] = result

        evidence_items: list[CollectedEvidenceItem] = []
        seen_keys: set[tuple[int, Optional[int]]] = set()

        seed_chunk_result = chunk_result_by_knowledge.get(seed_result.knowledge_id)
        if seed_chunk_result is not None:
            evidence_items.append(
                self._build_evidence_item(
                    result=seed_chunk_result,
                    entry=seed_entry,
                    retrieval_rank=1,
                    seed_knowledge_id=seed_result.knowledge_id,
                    relation_max_depth=relation_max_depth,
                    include_chunks=True,
                )
            )
            seen_keys.add(
                self._evidence_key(
                    seed_chunk_result.knowledge_id,
                    seed_chunk_result.metadata.get("chunk_index"),
                )
            )
        else:
            evidence_items.append(
                self._build_evidence_item(
                    result=seed_result,
                    entry=seed_entry,
                    retrieval_rank=1,
                    seed_knowledge_id=seed_result.knowledge_id,
                    relation_max_depth=relation_max_depth,
                    include_chunks=False,
                )
            )
            seen_keys.add(self._evidence_key(seed_result.knowledge_id, None))

        for result in chunk_results:
            if len(evidence_items) >= top_k:
                break
            key = self._evidence_key(result.knowledge_id, result.metadata.get("chunk_index"))
            if key in seen_keys:
                continue
            entry = self.sqlite_store.query_by_id(result.knowledge_id) or {}
            evidence_items.append(
                self._build_evidence_item(
                    result=result,
                    entry=entry,
                    retrieval_rank=len(evidence_items) + 1,
                    seed_knowledge_id=seed_result.knowledge_id,
                    relation_max_depth=relation_max_depth,
                    include_chunks=True,
                )
            )
            seen_keys.add(key)

        represented_knowledge_ids = {item.knowledge_id for item in evidence_items}
        for result in search_results:
            if result.knowledge_id in represented_knowledge_ids:
                continue
            entry = self.sqlite_store.query_by_id(result.knowledge_id) or {}
            evidence_items.append(
                self._build_evidence_item(
                    result=result,
                    entry=entry,
                    retrieval_rank=len(evidence_items) + 1,
                    seed_knowledge_id=seed_result.knowledge_id,
                    relation_max_depth=relation_max_depth,
                    include_chunks=False,
                )
            )
            represented_knowledge_ids.add(result.knowledge_id)

        evidence_items = self._deduplicate_evidence_items(evidence_items)
        evidence_items = self._rank_evidence_items(question_clean, evidence_items)
        evidence_items = self._trim_evidence_items(evidence_items, top_k)

        related_count = sum(1 for item in evidence_items if item.relation_found)
        summary = (
            f"围绕问题「{question_clean}」共聚合 {len(evidence_items)} 条证据，"
            f"其中 {related_count} 条可回溯到与种子条目的关系解释"
        )
        return CollectedEvidenceResult(
            question=question_clean,
            found=True,
            seed_knowledge_id=seed_result.knowledge_id,
            seed_title=seed_title,
            evidence=evidence_items,
            summary=summary,
            limitation_notes=limitation_notes,
            chunk_retrieval_status=chunk_retrieval_status,
        )

    def _build_evidence_item(
        self,
        result: Any,
        entry: dict[str, Any],
        retrieval_rank: int,
        seed_knowledge_id: int,
        relation_max_depth: int,
        include_chunks: bool,
    ) -> CollectedEvidenceItem:
        knowledge_id = int(result.knowledge_id)
        is_seed = knowledge_id == seed_knowledge_id
        relation_result = None
        if not is_seed:
            relation_result = self.relation_query_service.explain_relation(
                source_knowledge_id=seed_knowledge_id,
                target_knowledge_id=knowledge_id,
                max_depth=relation_max_depth,
                per_node_limit=100,
            )

        file_path = entry.get("file_path") or result.metadata.get("file_path", "")
        source_url = entry.get("source_url", result.metadata.get("source_url", ""))
        chunk_id = result.metadata.get("chunk_id") if include_chunks else None
        chunk_index = result.metadata.get("chunk_index") if include_chunks else None
        return CollectedEvidenceItem(
            knowledge_id=knowledge_id,
            title=entry.get("title", result.title),
            abstract=entry.get("summary_one_sentence", "") or result.highlight,
            source_type=entry.get("source_type", result.metadata.get("source_type", "")),
            archived_at=entry.get("archived_at", result.metadata.get("archived_at", "")),
            tags=self._parse_tags(entry.get("tags", result.metadata.get("tags", ""))),
            source_url=source_url,
            file_path=file_path,
            citation_source=resolve_citation_source(
                knowledge_id,
                source_url=source_url,
                file_path=file_path,
            ),
            citation_locator=build_chunk_locator(
                knowledge_id,
                chunk_id=chunk_id,
                chunk_index=chunk_index,
            ),
            content_preview=self._resolve_content_preview(result, file_path, include_chunks),
            chunk_id=chunk_id,
            chunk_index=chunk_index,
            chunk_text=result.metadata.get("chunk_text", "") if include_chunks else "",
            retrieval_rank=retrieval_rank,
            retrieval_score=round(float(result.score), 4),
            is_seed=is_seed,
            relation_found=bool(relation_result and relation_result.found),
            relation_explanation_type=(
                relation_result.explanation_type if relation_result and relation_result.found else ""
            ),
            relation_hops=(
                relation_result.hops if relation_result and relation_result.found else 0
            ),
            relation_summary=(
                relation_result.summary if relation_result and relation_result.found else ""
            ),
            relation_path=(
                relation_result.path if relation_result and relation_result.found else []
            ),
            relation_evidence_items=(
                relation_result.evidence_items
                if relation_result and relation_result.found
                else []
            ),
        )

    def _search_chunk_results(
        self, question: str, limit: int
    ) -> tuple[list[Any], str, Optional[str]]:
        chunk_searcher = self.chunk_searcher
        if chunk_searcher is None:
            hybrid_retriever = getattr(self.query_router, "hybrid_retriever", None)
            chunk_searcher = getattr(hybrid_retriever, "vector_retriever", None)

        if chunk_searcher is None or not hasattr(chunk_searcher, "search_chunks"):
            limitation_note = self._build_chunk_degradation_note(
                self.CHUNK_DEGRADED_REASON_PATH_UNAVAILABLE,
                "chunk 检索路径不可用，已降级为文档级证据",
            )
            logger.warning("%s", limitation_note)
            return (
                [],
                self.CHUNK_STATUS_PATH_UNAVAILABLE,
                limitation_note,
            )

        try:
            chunk_results = chunk_searcher.search_chunks(question, limit=limit)
        except Exception:
            limitation_note = self._build_chunk_degradation_note(
                self.CHUNK_DEGRADED_REASON_SEARCH_ERROR,
                "chunk 检索异常，已降级为文档级证据",
            )
            logger.exception("%s", limitation_note)
            return (
                [],
                self.CHUNK_STATUS_SEARCH_ERROR,
                limitation_note,
            )
        if not chunk_results:
            return ([], self.CHUNK_STATUS_NO_HITS, None)
        return (list(chunk_results), self.CHUNK_STATUS_SUCCESS, None)

    @staticmethod
    def _build_chunk_degradation_note(reason: str, message: str) -> str:
        """生成可观测的 chunk 降级信号（结构化原因码 + 可读描述）。"""
        return f"chunk_degraded[{reason}] {message}"

    def _deduplicate_evidence_items(
        self, evidence_items: list[CollectedEvidenceItem]
    ) -> list[CollectedEvidenceItem]:
        deduplicated: list[CollectedEvidenceItem] = []
        for item in evidence_items:
            duplicate_index = self._find_duplicate_index(deduplicated, item)
            if duplicate_index is None:
                deduplicated.append(item)
                continue

            existing = deduplicated[duplicate_index]
            if existing.is_seed:
                continue
            if item.is_seed or item.retrieval_score > existing.retrieval_score:
                deduplicated[duplicate_index] = item
        return deduplicated

    def _find_duplicate_index(
        self,
        deduplicated: list[CollectedEvidenceItem],
        candidate: CollectedEvidenceItem,
    ) -> Optional[int]:
        candidate_text = self._normalize_similarity_text(candidate)
        for index, existing in enumerate(deduplicated):
            if (
                candidate.knowledge_id == existing.knowledge_id
                and candidate.chunk_index == existing.chunk_index
            ):
                return index

            existing_text = self._normalize_similarity_text(existing)
            if candidate_text and existing_text and candidate_text == existing_text:
                return index

            if (
                candidate_text
                and existing_text
                and SequenceMatcher(None, candidate_text, existing_text).ratio()
                >= self.EXACT_DUPLICATE_THRESHOLD
            ):
                return index
        return None

    def _rank_evidence_items(
        self, question: str, evidence_items: list[CollectedEvidenceItem]
    ) -> list[CollectedEvidenceItem]:
        if not evidence_items:
            return []

        retrieval_scores = [item.retrieval_score for item in evidence_items]
        min_score = min(retrieval_scores)
        max_score = max(retrieval_scores)
        newest_timestamp = self._get_newest_timestamp(evidence_items)

        for item in evidence_items:
            retrieval_norm = self._normalize_score(
                item.retrieval_score, min_score, max_score
            )
            relation_score = self._compute_relation_score(item)
            freshness_score = self._compute_freshness_score(item, newest_timestamp)
            coverage_score = self._compute_coverage_score(question, item)
            ranking_score = (
                0.40 * retrieval_norm
                + 0.25 * relation_score
                + 0.15 * freshness_score
                + 0.20 * coverage_score
            )

            item.relation_score = round(relation_score, 4)
            item.freshness_score = round(freshness_score, 4)
            item.coverage_score = round(coverage_score, 4)
            item.ranking_score = round(min(max(ranking_score, 0.0), 1.0), 4)

        seed_items = [item for item in evidence_items if item.is_seed]
        non_seed_items = [item for item in evidence_items if not item.is_seed]
        non_seed_items.sort(
            key=lambda item: (item.ranking_score, item.retrieval_score),
            reverse=True,
        )
        return seed_items + non_seed_items

    @staticmethod
    def _trim_evidence_items(
        evidence_items: list[CollectedEvidenceItem], top_k: int
    ) -> list[CollectedEvidenceItem]:
        if len(evidence_items) <= top_k:
            return evidence_items
        seed_items = [item for item in evidence_items if item.is_seed]
        non_seed_items = [item for item in evidence_items if not item.is_seed]
        remaining = max(top_k - len(seed_items), 0)
        return seed_items + non_seed_items[:remaining]

    def _resolve_content_preview(
        self, result: Any, file_path: str, include_chunks: bool
    ) -> str:
        if not include_chunks:
            return self._load_content_preview(file_path)
        chunk_text = str(result.metadata.get("chunk_text", "") or "").strip()
        if chunk_text:
            return chunk_text
        return self._load_content_preview(file_path)

    def _build_document_evidence_items(
        self,
        search_results: list[Any],
        top_k: int,
        seed_knowledge_id: int,
        relation_max_depth: int,
    ) -> list[CollectedEvidenceItem]:
        evidence_items: list[CollectedEvidenceItem] = []
        for index, result in enumerate(search_results[:top_k], start=1):
            entry = self.sqlite_store.query_by_id(result.knowledge_id) or {}
            evidence_items.append(
                self._build_evidence_item(
                    result=result,
                    entry=entry,
                    retrieval_rank=index,
                    seed_knowledge_id=seed_knowledge_id,
                    relation_max_depth=relation_max_depth,
                    include_chunks=False,
                )
            )
        return evidence_items

    def _compute_coverage_score(
        self, question: str, item: CollectedEvidenceItem
    ) -> float:
        question_tokens = self._tokenize_text(question)
        if not question_tokens:
            return 0.0
        evidence_text = " ".join(
            part
            for part in [
                item.title,
                item.abstract,
                item.content_preview,
                item.chunk_text,
                " ".join(item.tags),
            ]
            if part
        )
        evidence_tokens = self._tokenize_text(evidence_text)
        if not evidence_tokens:
            return 0.0
        overlap = len(question_tokens & evidence_tokens)
        return overlap / len(question_tokens)

    @staticmethod
    def _compute_relation_score(item: CollectedEvidenceItem) -> float:
        if item.is_seed:
            return 1.0
        if not item.relation_found:
            return 0.0
        return 1.0 / (1 + item.relation_hops)

    @staticmethod
    def _compute_freshness_score(
        item: CollectedEvidenceItem, newest_timestamp: Optional[datetime]
    ) -> float:
        if newest_timestamp is None:
            return 0.0
        item_timestamp = EvidenceCollectionService._parse_timestamp(item.archived_at)
        if item_timestamp is None:
            return 0.0
        delta_days = max((newest_timestamp - item_timestamp).days, 0)
        return 1.0 / (1 + delta_days)

    @staticmethod
    def _parse_timestamp(raw_timestamp: str) -> Optional[datetime]:
        if not raw_timestamp:
            return None
        try:
            return datetime.strptime(raw_timestamp, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None

    @staticmethod
    def _get_newest_timestamp(
        evidence_items: list[CollectedEvidenceItem],
    ) -> Optional[datetime]:
        timestamps = [
            EvidenceCollectionService._parse_timestamp(item.archived_at)
            for item in evidence_items
        ]
        timestamps = [timestamp for timestamp in timestamps if timestamp is not None]
        if not timestamps:
            return None
        return max(timestamps)

    @staticmethod
    def _normalize_score(value: float, min_value: float, max_value: float) -> float:
        if max_value <= min_value:
            return min(max(value, 0.0), 1.0)
        return (value - min_value) / (max_value - min_value)

    def _tokenize_text(self, text: str) -> set[str]:
        if not text or not text.strip():
            return set()
        tokenized = self.text_processor.tokenize_chinese(text.lower())
        tokens = {token.strip() for token in tokenized.split() if token.strip()}
        if tokens:
            return tokens
        return {match.group(0) for match in re.finditer(r"[\w\u4e00-\u9fff]+", text.lower())}

    def _normalize_similarity_text(self, item: CollectedEvidenceItem) -> str:
        text = item.chunk_text or item.content_preview or item.abstract
        return re.sub(r"\s+", " ", text.strip().lower())

    def _load_content_preview(self, file_path: str, max_chars: int = 280) -> str:
        if not file_path:
            return ""

        try:
            entry = self.markdown_store.load(Path(file_path))
        except Exception:
            return ""

        normalized = " ".join(
            line.strip() for line in entry.content.splitlines() if line.strip()
        )
        return normalized[:max_chars]

    @staticmethod
    def _evidence_key(
        knowledge_id: int, chunk_index: Optional[int]
    ) -> tuple[int, Optional[int]]:
        return knowledge_id, chunk_index

    @staticmethod
    def _parse_tags(raw_tags: Any) -> list[str]:
        if not raw_tags:
            return []
        if isinstance(raw_tags, list):
            return [str(tag).strip() for tag in raw_tags if str(tag).strip()]
        return [tag.strip() for tag in str(raw_tags).split(",") if tag.strip()]
