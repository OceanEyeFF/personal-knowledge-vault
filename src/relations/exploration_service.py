"""
关系探索服务。

本模块实现 Phase B 第二优先级的受限版本能力：
- find_bridges: 基于显式关系子图与简单度数启发式发现桥接节点
- timeline_of: 基于结构化时间字段的弱时间线重建
- contrast: 基于检索候选表面字段的主题对比

注意：
- 这些能力当前都是 partial implementation
- 不依赖事件时间抽取、chunk 级证据落库或语义关系图
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
import re
from typing import Any

from src.relations.models import (
    BridgeCandidate,
    BridgeDiscoveryResult,
    ContrastCandidateItem,
    ContrastResult,
    TimelinePoint,
    TimelineResult,
)
from src.utils.text_utils import get_text_processor


class ExplorationService:
    """关系探索服务的受限实现。"""

    def __init__(
        self,
        query_router: Any,
        sqlite_store: Any,
        relation_query_service: Any,
    ) -> None:
        self.query_router = query_router
        self.sqlite_store = sqlite_store
        self.relation_query_service = relation_query_service
        self.text_processor = get_text_processor()

    def find_bridges(
        self,
        seed_knowledge_id: int,
        top_k: int = 5,
        max_depth: int = 2,
    ) -> BridgeDiscoveryResult:
        """发现 seed 周围子图中的桥接候选。

        当前实现只基于显式关系子图和简单邻接度启发式。
        它能帮助找出“位于多条边汇合处”的节点，但不能替代完整主题桥接发现。
        """
        if seed_knowledge_id <= 0:
            raise ValueError("seed_knowledge_id 必须为正整数")
        if top_k <= 0:
            raise ValueError("top_k 必须大于 0")
        if max_depth <= 0:
            raise ValueError("max_depth 必须大于 0")

        subgraph = self.relation_query_service.query_subgraph(
            seed_knowledge_id=seed_knowledge_id,
            depth=max_depth,
            per_node_limit=100,
            max_nodes=100,
            max_edges=300,
            group_by_relation_type=False,
        )
        node_depth_map = {
            node.knowledge_id: node.depth for node in subgraph.nodes
        }
        neighbors_by_node: dict[int, set[int]] = defaultdict(set)
        relation_types_by_node: dict[int, set[str]] = defaultdict(set)

        for edge in subgraph.edges:
            neighbors_by_node[edge.source_knowledge_id].add(edge.target_knowledge_id)
            neighbors_by_node[edge.target_knowledge_id].add(edge.source_knowledge_id)
            relation_types_by_node[edge.source_knowledge_id].add(
                edge.relation_type.value
            )
            relation_types_by_node[edge.target_knowledge_id].add(
                edge.relation_type.value
            )

        candidates: list[BridgeCandidate] = []
        seed_entry = self.sqlite_store.query_by_id(seed_knowledge_id) or {}
        for knowledge_id, neighbors in neighbors_by_node.items():
            if knowledge_id == seed_knowledge_id:
                continue
            if len(neighbors) < 2:
                continue

            depth = node_depth_map.get(knowledge_id, max_depth)
            entry = self.sqlite_store.query_by_id(knowledge_id) or {}
            structural_score = self._compute_structural_bridge_score(
                depth=depth,
                neighbor_count=len(neighbors),
                max_depth=max_depth,
            )
            semantic_score = self._compute_semantic_bridge_score(
                seed_entry=seed_entry,
                candidate_entry=entry,
                neighbor_ids=neighbors,
            )
            if semantic_score <= 0.0:
                continue

            bridge_score = round(structural_score * 0.65 + semantic_score * 0.35, 4)
            candidates.append(
                BridgeCandidate(
                    knowledge_id=knowledge_id,
                    title=entry.get("title", f"条目 {knowledge_id}"),
                    depth=depth,
                    bridge_score=bridge_score,
                    structural_bridge_score=round(structural_score, 4),
                    semantic_bridge_score=round(semantic_score, 4),
                    connected_knowledge_ids=sorted(neighbors),
                    relation_types=sorted(relation_types_by_node[knowledge_id]),
                    summary=(
                        f"当前把 {knowledge_id} 视为桥接候选，因为它在 {max_depth} 跳子图中"
                        f"连接了 {len(neighbors)} 个相邻节点，且与 seed/邻居存在可解释的标签或文本重合"
                    ),
                )
            )

        candidates.sort(
            key=lambda item: (
                -item.bridge_score,
                item.depth,
                item.knowledge_id,
            )
        )
        selected = candidates[:top_k]
        return BridgeDiscoveryResult(
            seed_knowledge_id=seed_knowledge_id,
            found=bool(selected),
            max_depth=max_depth,
            items=selected,
            summary=(
                f"围绕 seed={seed_knowledge_id} 共发现 {len(selected)} 个桥接候选"
                if selected
                else f"围绕 seed={seed_knowledge_id} 未发现满足条件的桥接候选"
            ),
            evidence_sources=[
                "relation_subgraph",
                "entry_tags",
                "entry_title_summary",
            ],
            limitation_notes=[
                "当前语义桥接评分只基于 tags、标题和摘要的重合信号，不代表完整语义桥接发现",
                "当前未引入 chunk 级桥接证据、跨主题中心性分析和语义关系边",
            ],
        )

    def timeline_of(
        self,
        topic: str,
        top_k: int = 8,
        sort_order: str = "asc",
    ) -> TimelineResult:
        """重建 topic 的弱时间线。

        当前只使用结构化时间字段，不代表正文中的完整真实事件时间。
        """
        topic_clean = topic.strip()
        if not topic_clean:
            raise ValueError("topic 不能为空")
        if top_k <= 0:
            raise ValueError("top_k 必须大于 0")
        if sort_order not in {"asc", "desc"}:
            raise ValueError("sort_order 仅支持 asc 或 desc")

        results = self.query_router.search(topic_clean, limit=top_k)
        time_source_priority = ["event_time", "published_at", "archived_at"]
        points: list[TimelinePoint] = []
        for result in results[:top_k]:
            entry = self.sqlite_store.query_by_id(result.knowledge_id) or {}
            resolved_times = self._resolve_time_fields(entry, result.metadata)
            time_value, time_source = self._select_time_value(
                resolved_times, time_source_priority
            )
            points.append(
                TimelinePoint(
                    knowledge_id=result.knowledge_id,
                    title=entry.get("title", result.title),
                    time_value=time_value,
                    event_time=resolved_times["event_time"],
                    published_at=resolved_times["published_at"],
                    archived_at=resolved_times["archived_at"],
                    time_source=time_source,
                    source_type=entry.get("source_type", result.metadata.get("source_type", "")),
                    abstract=entry.get("summary_one_sentence", "") or result.highlight,
                    tags=self._parse_tags(entry.get("tags", result.metadata.get("tags", ""))),
                    retrieval_score=round(float(result.score), 4),
                )
            )

        points.sort(
            key=lambda item: self._timeline_sort_key(item, sort_order),
        )
        inferred_time_field = points[0].time_source if points else "archived_at"
        return TimelineResult(
            topic=topic_clean,
            found=bool(points),
            inferred_time_field=inferred_time_field,
            time_source_priority=time_source_priority,
            items=points,
            summary=(
                f"围绕主题「{topic_clean}」按时间来源优先级重建了 {len(points)} 个时间点"
                if points
                else f"未找到可用于主题「{topic_clean}」时间线重建的候选条目"
            ),
            evidence_sources=[
                "query_results",
                "entry_metadata",
            ],
            limitation_notes=[
                "当前只在 entry/metadata 中按 event_time > published_at > archived_at 选择时间，不代表正文中的完整真实事件时间",
                "当前未接入 video_timestamps、正文事件抽取或时间语义解析",
            ],
        )

    def contrast(
        self,
        topic_a: str,
        topic_b: str,
        top_k: int = 5,
    ) -> ContrastResult:
        """对比两个主题的检索候选表面特征。

        当前版本只对比候选集、标签和重叠条目，不代表完整语义对比。
        """
        topic_a_clean = topic_a.strip()
        topic_b_clean = topic_b.strip()
        if not topic_a_clean:
            raise ValueError("topic_a 不能为空")
        if not topic_b_clean:
            raise ValueError("topic_b 不能为空")
        if top_k <= 0:
            raise ValueError("top_k 必须大于 0")

        results_a = self.query_router.search(topic_a_clean, limit=top_k)
        results_b = self.query_router.search(topic_b_clean, limit=top_k)

        candidates_a = [
            self._build_contrast_item(result)
            for result in results_a[:top_k]
        ]
        candidates_b = [
            self._build_contrast_item(result)
            for result in results_b[:top_k]
        ]

        tags_a = {tag for item in candidates_a for tag in item.tags}
        tags_b = {tag for item in candidates_b for tag in item.tags}
        overlap_knowledge_ids = sorted(
            {item.knowledge_id for item in candidates_a}
            & {item.knowledge_id for item in candidates_b}
        )
        shared_tags = sorted(tags_a & tags_b)
        only_a_tags = sorted(tags_a - tags_b)
        only_b_tags = sorted(tags_b - tags_a)
        comparison_dimensions = {
            "shared_tags_count": len(shared_tags),
            "topic_a_only_tags_count": len(only_a_tags),
            "topic_b_only_tags_count": len(only_b_tags),
            "overlap_knowledge_count": len(overlap_knowledge_ids),
            "candidate_count": {
                "topic_a": len(candidates_a),
                "topic_b": len(candidates_b),
            },
        }

        return ContrastResult(
            topic_a=topic_a_clean,
            topic_b=topic_b_clean,
            found=bool(candidates_a or candidates_b),
            topic_a_candidates=candidates_a,
            topic_b_candidates=candidates_b,
            comparison_dimensions=comparison_dimensions,
            shared_tags=shared_tags,
            only_a_tags=only_a_tags,
            only_b_tags=only_b_tags,
            overlap_knowledge_ids=overlap_knowledge_ids,
            summary=(
                f"围绕「{topic_a_clean}」与「{topic_b_clean}」共对比 "
                f"{len(candidates_a)} + {len(candidates_b)} 条候选，"
                f"共享标签 {len(shared_tags)} 个、重叠条目 {len(overlap_knowledge_ids)} 个"
            ),
            evidence_sources=[
                "query_results",
                "entry_tags",
                "entry_summary",
            ],
            limitation_notes=[
                "当前只基于检索候选、tags 和摘要做表层对比，不代表完整语义对比",
                "当前未引入 contrast 关系类型，也未建模争议/补充/因果等高级语义边",
            ],
        )

    def _build_contrast_item(self, result: Any) -> ContrastCandidateItem:
        entry = self.sqlite_store.query_by_id(result.knowledge_id) or {}
        return ContrastCandidateItem(
            knowledge_id=result.knowledge_id,
            title=entry.get("title", result.title),
            abstract=entry.get("summary_one_sentence", "") or result.highlight,
            archived_at=entry.get("archived_at", result.metadata.get("archived_at", "")),
            source_type=entry.get("source_type", result.metadata.get("source_type", "")),
            tags=self._parse_tags(entry.get("tags", result.metadata.get("tags", ""))),
            retrieval_score=round(float(result.score), 4),
        )

    @staticmethod
    def _parse_tags(raw_tags: Any) -> list[str]:
        if not raw_tags:
            return []
        if isinstance(raw_tags, list):
            return [str(tag).strip() for tag in raw_tags if str(tag).strip()]
        return [tag.strip() for tag in str(raw_tags).split(",") if tag.strip()]

    @staticmethod
    def _compute_structural_bridge_score(
        depth: int,
        neighbor_count: int,
        max_depth: int,
    ) -> float:
        depth_bonus = max(0.0, 1 - ((depth - 1) / max(max_depth, 1)))
        neighbor_score = min(neighbor_count / 4.0, 1.0)
        return min(max(0.7 * neighbor_score + 0.3 * depth_bonus, 0.0), 1.0)

    def _compute_semantic_bridge_score(
        self,
        seed_entry: dict[str, Any],
        candidate_entry: dict[str, Any],
        neighbor_ids: set[int],
    ) -> float:
        candidate_tokens = self._entry_tokens(candidate_entry)
        if not candidate_tokens:
            return 0.0

        comparison_scores: list[float] = []
        seed_tokens = self._entry_tokens(seed_entry)
        if seed_tokens:
            comparison_scores.append(self._token_overlap(candidate_tokens, seed_tokens))

        for neighbor_id in neighbor_ids:
            neighbor_entry = self.sqlite_store.query_by_id(neighbor_id) or {}
            neighbor_tokens = self._entry_tokens(neighbor_entry)
            if neighbor_tokens:
                comparison_scores.append(
                    self._token_overlap(candidate_tokens, neighbor_tokens)
                )

        if not comparison_scores:
            return 0.0
        return max(comparison_scores)

    @staticmethod
    def _token_overlap(tokens_a: set[str], tokens_b: set[str]) -> float:
        if not tokens_a or not tokens_b:
            return 0.0
        overlap = len(tokens_a & tokens_b)
        return overlap / max(min(len(tokens_a), len(tokens_b)), 1)

    def _entry_tokens(self, entry: dict[str, Any]) -> set[str]:
        parts = [
            str(entry.get("title", "") or ""),
            str(entry.get("summary_one_sentence", "") or ""),
            str(entry.get("summary_100_words", "") or ""),
            " ".join(self._parse_tags(entry.get("tags", ""))),
        ]
        text = " ".join(part for part in parts if part).strip().lower()
        if not text:
            return set()
        tokenized = self.text_processor.tokenize_chinese(text)
        tokens = {token.strip() for token in tokenized.split() if token.strip()}
        if tokens:
            return tokens
        return {match.group(0) for match in re.finditer(r"[\w\u4e00-\u9fff]+", text)}

    @staticmethod
    def _resolve_time_fields(
        entry: dict[str, Any],
        metadata: dict[str, Any],
    ) -> dict[str, str]:
        published_at = (
            entry.get("published_at")
            or metadata.get("published_at", "")
            or metadata.get("published_time", "")
            or metadata.get("publish_time", "")
            or ""
        )
        return {
            "event_time": str(entry.get("event_time") or metadata.get("event_time", "") or ""),
            "published_at": str(published_at),
            "archived_at": str(entry.get("archived_at") or metadata.get("archived_at", "") or ""),
        }

    @staticmethod
    def _select_time_value(
        time_fields: dict[str, str],
        priority: list[str],
    ) -> tuple[str, str]:
        for field in priority:
            value = time_fields.get(field, "")
            if value:
                return value, field
        return "", priority[-1]

    @staticmethod
    def _parse_time_sort_key(raw_value: str) -> tuple[int, int, float, str]:
        if not raw_value:
            return (1, 1, 0.0, "")
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                parsed = datetime.strptime(raw_value, fmt)
                return (0, 0, parsed.timestamp(), "")
            except ValueError:
                continue
        try:
            parsed = datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
            return (0, 0, parsed.timestamp(), "")
        except ValueError:
            pass
        return (0, 1, 0.0, raw_value)

    @classmethod
    def _timeline_sort_key(
        cls, item: TimelinePoint, sort_order: str
    ) -> tuple[int, Any]:
        missing_rank, parse_kind, parsed_ts, raw_value = cls._parse_time_sort_key(
            item.time_value
        )
        if sort_order == "desc":
            if missing_rank:
                return (1, 1, 0.0, "", item.knowledge_id)
            if parse_kind == 0:
                return (0, 0, -parsed_ts, "", item.knowledge_id)
            return (0, 1, 0.0, raw_value, item.knowledge_id)
        return (missing_rank, parse_kind, parsed_ts, raw_value, item.knowledge_id)
