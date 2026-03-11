"""
证据聚合服务。

基于检索、关系解释和条目元数据构建文档级证据包。
当前版本先返回条目级证据，不依赖 chunk 文本落库。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from src.relations.models import CollectedEvidenceItem, CollectedEvidenceResult


class EvidenceCollectionService:
    """面向 Phase B 的最小证据聚合服务。"""

    def __init__(
        self,
        query_router: Any,
        sqlite_store: Any,
        markdown_store: Any,
        relation_query_service: Any,
    ) -> None:
        self.query_router = query_router
        self.sqlite_store = sqlite_store
        self.markdown_store = markdown_store
        self.relation_query_service = relation_query_service

    def collect_evidence(
        self,
        question: str,
        top_k: int = 5,
        relation_max_depth: int = 2,
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
        if not search_results:
            return CollectedEvidenceResult(
                question=question_clean,
                found=False,
                summary=f"未找到与问题「{question_clean}」直接相关的证据",
            )

        seed_result = search_results[0]
        seed_entry = self.sqlite_store.query_by_id(seed_result.knowledge_id) or {}
        seed_title = seed_entry.get("title", seed_result.title)

        evidence_items: list[CollectedEvidenceItem] = []
        for index, result in enumerate(search_results[:top_k], start=1):
            entry = self.sqlite_store.query_by_id(result.knowledge_id) or {}
            evidence_items.append(
                self._build_evidence_item(
                    result=result,
                    entry=entry,
                    retrieval_rank=index,
                    seed_knowledge_id=seed_result.knowledge_id,
                    relation_max_depth=relation_max_depth,
                )
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
        )

    def _build_evidence_item(
        self,
        result: Any,
        entry: dict[str, Any],
        retrieval_rank: int,
        seed_knowledge_id: int,
        relation_max_depth: int,
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
        return CollectedEvidenceItem(
            knowledge_id=knowledge_id,
            title=entry.get("title", result.title),
            abstract=entry.get("summary_one_sentence", "") or result.highlight,
            source_type=entry.get("source_type", result.metadata.get("source_type", "")),
            archived_at=entry.get("archived_at", result.metadata.get("archived_at", "")),
            tags=self._parse_tags(entry.get("tags", result.metadata.get("tags", ""))),
            source_url=entry.get("source_url", result.metadata.get("source_url", "")),
            file_path=file_path,
            content_preview=self._load_content_preview(file_path),
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
    def _parse_tags(raw_tags: Any) -> list[str]:
        if not raw_tags:
            return []
        if isinstance(raw_tags, list):
            return [str(tag).strip() for tag in raw_tags if str(tag).strip()]
        return [tag.strip() for tag in str(raw_tags).split(",") if tag.strip()]
