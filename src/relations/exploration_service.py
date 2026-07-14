"""
关系探索服务。

本模块实现 Phase B 第二优先级的受限版本能力：
- find_bridges: 基于显式关系子图与局部桥接信号发现桥接节点
- timeline_of: 基于结构化真实时间字段优先级的弱时间线重建
- contrast: 基于检索候选与跨主题显式关系路径的主题对比

注意：
- 这些能力当前都是 partial implementation
- 不依赖事件时间抽取、chunk 级证据落库或语义关系图
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from itertools import combinations
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

        当前实现基于显式关系子图、局部桥接信号和轻量文本重合。
        它能帮助找出“连接多个局部路径或断开邻居对”的节点，但不能替代完整主题桥接发现。
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
        entry_cache: dict[int, dict[str, Any]] = {}
        seed_entry = self._get_entry(seed_knowledge_id, entry_cache)
        for knowledge_id, neighbors in neighbors_by_node.items():
            if knowledge_id == seed_knowledge_id:
                continue
            if len(neighbors) < 2:
                continue

            depth = node_depth_map.get(knowledge_id, max_depth)
            entry = self._get_entry(knowledge_id, entry_cache)
            structural_score = self._compute_structural_bridge_score(
                depth=depth,
                neighbor_count=len(neighbors),
                max_depth=max_depth,
            )
            graph_bridge_score = self._compute_graph_bridge_score(
                seed_knowledge_id=seed_knowledge_id,
                neighbors=neighbors,
                adjacency=neighbors_by_node,
                node_depth_map=node_depth_map,
                max_depth=max_depth,
            )
            semantic_score = self._compute_semantic_bridge_score(
                seed_entry=seed_entry,
                candidate_entry=entry,
                neighbor_ids=neighbors,
                entry_cache=entry_cache,
            )
            if semantic_score <= 0.0 and graph_bridge_score < 0.45:
                continue

            bridge_score = round(
                structural_score * 0.4
                + graph_bridge_score * 0.4
                + semantic_score * 0.2,
                4,
            )
            candidates.append(
                BridgeCandidate(
                    knowledge_id=knowledge_id,
                    title=entry.get("title", f"条目 {knowledge_id}"),
                    depth=depth,
                    bridge_score=bridge_score,
                    structural_bridge_score=round(structural_score, 4),
                    graph_bridge_score=round(graph_bridge_score, 4),
                    semantic_bridge_score=round(semantic_score, 4),
                    connected_knowledge_ids=sorted(neighbors),
                    relation_types=sorted(relation_types_by_node[knowledge_id]),
                    summary=(
                        f"当前把 {knowledge_id} 视为桥接候选，因为它在 {max_depth} 跳子图中"
                        f"连接了 {len(neighbors)} 个相邻节点，局部图桥接信号为 {graph_bridge_score:.2f}，"
                        + (
                            "且与 seed/邻居存在可解释的标签或文本重合"
                            if semantic_score > 0.0
                            else "即使缺少明显文本重合，仍通过局部图桥接信号保留"
                        )
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
                "graph_bridge_signal",
                "entry_tags",
                "entry_title_summary",
            ],
            limitation_notes=[
                "当前桥接结果已引入局部图桥接信号（断开邻居对、深度跨度），但底层仍只使用显式关系图与轻量文本重合",
                "当前未引入 chunk 级桥接证据、全局中心性分析和语义关系边",
            ],
        )

    def timeline_of(
        self,
        topic: str,
        top_k: int = 8,
        sort_order: str = "asc",
    ) -> TimelineResult:
        """重建 topic 的弱时间线。"""
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
        inferred_time_field = self._infer_timeline_source(
            points,
            time_source_priority,
        )
        real_time_count = sum(
            1 for item in points if item.time_source in {"event_time", "published_at"}
        )
        return TimelineResult(
            topic=topic_clean,
            found=bool(points),
            inferred_time_field=inferred_time_field,
            time_source_priority=time_source_priority,
            items=points,
            summary=(
                f"围绕主题「{topic_clean}」按时间来源优先级重建了 {len(points)} 个时间点，"
                f"其中 {real_time_count} 个命中了 event_time/published_at"
                if points
                else f"未找到可用于主题「{topic_clean}」时间线重建的候选条目"
            ),
            evidence_sources=[
                "query_results",
                "entry_metadata",
                "structured_time_fields",
            ],
            limitation_notes=[
                "当前优先使用 entry/metadata 中的 event_time/published_at 真实时间字段，缺失时才回退 archived_at，不代表正文中的完整真实事件时间",
                "当前未接入 video_timestamps、正文事件抽取或时间语义解析",
            ],
        )

    def contrast(
        self,
        topic_a: str,
        topic_b: str,
        top_k: int = 5,
    ) -> ContrastResult:
        """对比两个主题的检索候选表面特征与显式关系图信号。"""
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
        relation_signal = self._collect_contrast_relation_signals(
            candidates_a,
            candidates_b,
        )
        for item in candidates_a:
            candidate_signal = relation_signal["topic_a"].get(item.knowledge_id, {})
            item.relation_signal_score = candidate_signal.get("score", 0.0)
            item.relation_types = candidate_signal.get("relation_types", [])
        for item in candidates_b:
            candidate_signal = relation_signal["topic_b"].get(item.knowledge_id, {})
            item.relation_signal_score = candidate_signal.get("score", 0.0)
            item.relation_types = candidate_signal.get("relation_types", [])

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
            "relation_graph_signal": relation_signal["summary"],
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
                f"共享标签 {len(shared_tags)} 个、重叠条目 {len(overlap_knowledge_ids)} 个，"
                f"显式关系候选对 {relation_signal['summary']['connected_candidate_pairs_count']} 组"
            ),
            evidence_sources=[
                "query_results",
                "relation_graph",
                "entry_tags",
                "entry_summary",
            ],
            limitation_notes=[
                "当前已引入跨主题显式关系路径信号，但底层仍只依赖低歧义显式关系图与候选表层文本，不代表完整语义对比",
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

    def _get_entry(
        self,
        knowledge_id: int,
        cache: dict[int, dict[str, Any]],
    ) -> dict[str, Any]:
        if knowledge_id not in cache:
            cache[knowledge_id] = self.sqlite_store.query_by_id(knowledge_id) or {}
        return cache[knowledge_id]

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
        entry_cache: dict[int, dict[str, Any]],
    ) -> float:
        candidate_tokens = self._entry_tokens(candidate_entry)
        if not candidate_tokens:
            return 0.0

        comparison_scores: list[float] = []
        seed_tokens = self._entry_tokens(seed_entry)
        if seed_tokens:
            comparison_scores.append(self._token_overlap(candidate_tokens, seed_tokens))

        for neighbor_id in neighbor_ids:
            neighbor_entry = self._get_entry(neighbor_id, entry_cache)
            neighbor_tokens = self._entry_tokens(neighbor_entry)
            if neighbor_tokens:
                comparison_scores.append(
                    self._token_overlap(candidate_tokens, neighbor_tokens)
                )

        if not comparison_scores:
            return 0.0
        top_scores = sorted(comparison_scores, reverse=True)
        anchor_score = top_scores[0]
        support_score = sum(top_scores[: min(len(top_scores), 2)]) / min(
            len(top_scores),
            2,
        )
        coverage_score = sum(1 for score in top_scores if score >= 0.08) / len(
            top_scores
        )
        return min(max(anchor_score * 0.55 + support_score * 0.3 + coverage_score * 0.15, 0.0), 1.0)

    @staticmethod
    def _compute_graph_bridge_score(
        seed_knowledge_id: int,
        neighbors: set[int],
        adjacency: dict[int, set[int]],
        node_depth_map: dict[int, int],
        max_depth: int,
    ) -> float:
        neighbor_list = sorted(neighbors)
        if len(neighbor_list) < 2:
            return 0.0

        disconnected_pairs = 0
        total_pairs = 0
        for left_id, right_id in combinations(neighbor_list, 2):
            total_pairs += 1
            if right_id not in adjacency.get(left_id, set()):
                disconnected_pairs += 1

        pair_score = disconnected_pairs / max(total_pairs, 1)
        neighbor_depths = sorted(
            node_depth_map.get(neighbor_id, max_depth) for neighbor_id in neighbor_list
        )
        depth_span = neighbor_depths[-1] - neighbor_depths[0]
        depth_span_score = min(depth_span / max(max_depth, 1), 1.0)
        seed_frontier_score = 1.0 if (
            seed_knowledge_id in neighbors
            and any(
                node_depth_map.get(neighbor_id, max_depth) > 1
                for neighbor_id in neighbor_list
                if neighbor_id != seed_knowledge_id
            )
        ) else 0.0
        return min(
            max(
                pair_score * 0.55
                + depth_span_score * 0.25
                + seed_frontier_score * 0.2,
                0.0,
            ),
            1.0,
        )

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
    def _infer_timeline_source(
        points: list[TimelinePoint],
        priority: list[str],
    ) -> str:
        if not priority:
            return "archived_at"
        source_counts = {field: 0 for field in priority}
        parseable_counts = {field: 0 for field in priority}

        for item in points:
            if not item.time_value or item.time_source not in source_counts:
                continue
            source_counts[item.time_source] += 1
            missing_rank, parse_kind, _, _ = ExplorationService._parse_time_sort_key(
                item.time_value
            )
            if missing_rank == 0 and parse_kind == 0:
                parseable_counts[item.time_source] += 1

        # 优先看可解析时间，避免单条高优先级时间值把整体时间源判断得过于乐观。
        baseline_counts = (
            parseable_counts
            if any(count > 0 for count in parseable_counts.values())
            else source_counts
        )
        nonzero = {
            source: count for source, count in baseline_counts.items() if count > 0
        }
        if not nonzero:
            return priority[-1]

        max_count = max(nonzero.values())
        leaders = [source for source, count in nonzero.items() if count == max_count]
        if len(leaders) == 1:
            return leaders[0]
        return "mixed"

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
            # 不可解析时间值不带方向语义，保持中性且稳定的文本排序。
            return (0, 1, raw_value, item.knowledge_id)
        return (missing_rank, parse_kind, parsed_ts, raw_value, item.knowledge_id)

    def _collect_contrast_relation_signals(
        self,
        candidates_a: list[ContrastCandidateItem],
        candidates_b: list[ContrastCandidateItem],
    ) -> dict[str, Any]:
        topic_a_signals: dict[int, dict[str, Any]] = {}
        topic_b_signals: dict[int, dict[str, Any]] = {}
        connected_pairs = 0
        relation_types_seen: set[str] = set()
        max_relation_hops = 0

        for item_a in candidates_a:
            for item_b in candidates_b:
                if item_a.knowledge_id == item_b.knowledge_id:
                    continue

                explanation = self.relation_query_service.explain_relation(
                    item_a.knowledge_id,
                    item_b.knowledge_id,
                    max_depth=2,
                )
                if not explanation.found:
                    continue

                connected_pairs += 1
                pair_score = round(float(explanation.confidence), 4)
                relation_types = self._extract_relation_types(explanation)
                relation_types_seen.update(relation_types)
                max_relation_hops = max(max_relation_hops, explanation.hops)
                self._merge_candidate_relation_signal(
                    topic_a_signals,
                    item_a.knowledge_id,
                    pair_score,
                    relation_types,
                )
                self._merge_candidate_relation_signal(
                    topic_b_signals,
                    item_b.knowledge_id,
                    pair_score,
                    relation_types,
                )

        return {
            "topic_a": topic_a_signals,
            "topic_b": topic_b_signals,
            "summary": {
                "connected_candidate_pairs_count": connected_pairs,
                "topic_a_connected_candidate_count": len(topic_a_signals),
                "topic_b_connected_candidate_count": len(topic_b_signals),
                "shared_relation_types": sorted(relation_types_seen),
                "max_relation_hops": max_relation_hops,
            },
        }

    @staticmethod
    def _merge_candidate_relation_signal(
        signal_map: dict[int, dict[str, Any]],
        knowledge_id: int,
        pair_score: float,
        relation_types: list[str],
    ) -> None:
        existing = signal_map.get(knowledge_id)
        if existing is None:
            signal_map[knowledge_id] = {
                "score": pair_score,
                "relation_types": list(relation_types),
            }
            return

        existing["score"] = round(max(float(existing["score"]), pair_score), 4)
        existing["relation_types"] = sorted(
            set(existing["relation_types"]) | set(relation_types)
        )

    @staticmethod
    def _extract_relation_types(explanation: Any) -> list[str]:
        relation_types = {
            record.relation_type.value
            for record in explanation.path + explanation.supporting_relations
        }
        if relation_types:
            return sorted(relation_types)
        fallback_relation_types = {
            str(item.get("relation_type", "")).strip()
            for item in explanation.evidence_items
            if str(item.get("relation_type", "")).strip()
        }
        return sorted(fallback_relation_types)
