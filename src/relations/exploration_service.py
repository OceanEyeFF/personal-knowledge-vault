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
from pathlib import Path
import re
from typing import Any

from src.relations.citations import (
    build_entry_metadata_locator,
    build_entry_locator,
    build_metadata_locator,
    build_relation_locator,
    read_persisted_metadata_field,
    resolve_citation_source,
    resolve_vault_file_path,
    serialize_relation_evidence,
)
from src.relations.models import (
    BridgeCandidate,
    BridgeDiscoveryResult,
    ContrastCandidateItem,
    ContrastResult,
    TimelinePoint,
    TimelineResult,
)
from src.retrieval.result import is_strict_search_response
from src.runtime.errors import ErrorCode, PKVRuntimeError
from src.utils.text_utils import get_text_processor


class ExplorationService:
    """关系探索服务的受限实现。"""

    BRIDGE_MAX_NODES = 100
    BRIDGE_MAX_EDGES = 300

    def __init__(
        self,
        query_router: Any,
        sqlite_store: Any,
        relation_query_service: Any,
        vault_dir: Path | None = None,
    ) -> None:
        self.query_router = query_router
        self.sqlite_store = sqlite_store
        self.relation_query_service = relation_query_service
        self.vault_dir = Path(vault_dir) if vault_dir is not None else None
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

        entry_cache: dict[int, dict[str, Any]] = {}
        seed_entry = self._get_entry(seed_knowledge_id, entry_cache)
        if not self._entry_file_is_safe(seed_entry):
            return BridgeDiscoveryResult(
                seed_knowledge_id=seed_knowledge_id,
                found=False,
                max_depth=max_depth,
                summary=(
                    f"围绕 seed={seed_knowledge_id} 未发现可公开的桥接候选，"
                    "因为种子条目未通过 vault 文件边界校验"
                ),
                evidence_sources=[
                    "relation_subgraph",
                    "graph_bridge_signal",
                    "entry_tags",
                    "entry_title_summary",
                ],
                limitation_notes=[
                    "种子条目未通过 vault 文件边界校验，未查询或公开其关系证据"
                ],
                subgraph_max_nodes=self.BRIDGE_MAX_NODES,
                subgraph_max_edges=self.BRIDGE_MAX_EDGES,
            )

        subgraph = self.relation_query_service.query_subgraph(
            seed_knowledge_id=seed_knowledge_id,
            depth=max_depth,
            per_node_limit=100,
            max_nodes=self.BRIDGE_MAX_NODES,
            max_edges=self.BRIDGE_MAX_EDGES,
            group_by_relation_type=False,
        )
        node_depth_map = {
            node.knowledge_id: node.depth
            for node in subgraph.nodes
            if self._entry_file_is_safe(
                self._get_entry(node.knowledge_id, entry_cache)
            )
        }
        public_edges, excluded_unsafe_edge_count = (
            self._filter_relation_records_by_vault(subgraph.edges, entry_cache)
        )
        excluded_unsafe_node_count = len(subgraph.nodes) - len(node_depth_map)
        neighbors_by_node: dict[int, set[int]] = defaultdict(set)
        relation_types_by_node: dict[int, set[str]] = defaultdict(set)

        for edge in public_edges:
            neighbors_by_node[edge.source_knowledge_id].add(edge.target_knowledge_id)
            neighbors_by_node[edge.target_knowledge_id].add(edge.source_knowledge_id)
            relation_types_by_node[edge.source_knowledge_id].add(
                edge.relation_type.value
            )
            relation_types_by_node[edge.target_knowledge_id].add(
                edge.relation_type.value
            )

        candidates: list[BridgeCandidate] = []
        excluded_unsafe_explanation_count = 0
        excluded_unexplainable_candidate_count = 0
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
            semantic_score_inputs = self._build_semantic_bridge_score_inputs(
                seed_entry=seed_entry,
                candidate_entry=entry,
                neighbor_ids=neighbors,
                entry_cache=entry_cache,
            )
            semantic_score = float(semantic_score_inputs["semantic_score"])
            if semantic_score <= 0.0 and graph_bridge_score < 0.45:
                continue

            bridge_score = round(
                structural_score * 0.4
                + graph_bridge_score * 0.4
                + semantic_score * 0.2,
                4,
            )
            relation_explanation = self.relation_query_service.explain_relation(
                seed_knowledge_id,
                knowledge_id,
                max_depth=max_depth,
            )
            if not relation_explanation.found:
                excluded_unexplainable_candidate_count += 1
                continue
            if not self._relation_records_are_vault_safe(
                relation_explanation.path + relation_explanation.supporting_relations,
                entry_cache,
            ):
                excluded_unsafe_explanation_count += 1
                continue
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
                    evidence_path=self._build_bridge_evidence_path(
                        seed_knowledge_id=seed_knowledge_id,
                        candidate_knowledge_id=knowledge_id,
                        explanation=relation_explanation,
                        subgraph_edges=public_edges,
                    ),
                    supporting_subgraph=self._build_bridge_supporting_subgraph(
                        seed_knowledge_id=seed_knowledge_id,
                        candidate_knowledge_id=knowledge_id,
                        neighbors=neighbors,
                        adjacency=neighbors_by_node,
                        node_depth_map=node_depth_map,
                        subgraph_edges=public_edges,
                        max_depth=max_depth,
                        semantic_score_inputs=semantic_score_inputs,
                    ),
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
        limitation_notes = [
            (
                "当前桥接结果已引入局部图桥接信号（断开邻居对、深度跨度），"
                "但底层仍只使用显式关系图与轻量文本重合"
            ),
            "当前未引入 chunk 级桥接证据、全局中心性分析和语义关系边",
            (
                f"查询范围固定为 seed 的 {max_depth} 跳显式关系子图，"
                f"最多 {self.BRIDGE_MAX_NODES} 个节点、"
                f"{self.BRIDGE_MAX_EDGES} 条边"
            ),
        ]
        if subgraph.truncated:
            limitation_notes.append(
                "本次关系子图已截断；候选集合和未发现结论均不完整，"
                "不得解释为全图范围的桥接结论"
            )
        if excluded_unsafe_node_count or excluded_unsafe_edge_count:
            limitation_notes.append(
                f"{excluded_unsafe_node_count} 个节点、{excluded_unsafe_edge_count} 条关系边"
                "未通过 vault 文件边界校验，已从公开桥接范围排除"
            )
        if excluded_unsafe_explanation_count:
            limitation_notes.append(
                f"{excluded_unsafe_explanation_count} 条候选关系路径未通过 vault "
                "文件边界校验，未作为桥接证据公开"
            )
        if excluded_unexplainable_candidate_count:
            limitation_notes.append(
                f"{excluded_unexplainable_candidate_count} 个局部图候选缺少可验证的 "
                "seed 路径，未作为桥接证据公开"
            )

        public_scope_truncated = bool(
            subgraph.truncated
            or excluded_unsafe_node_count
            or excluded_unsafe_edge_count
            or excluded_unsafe_explanation_count
            or excluded_unexplainable_candidate_count
        )

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
            limitation_notes=limitation_notes,
            subgraph_truncated=public_scope_truncated,
            subgraph_max_nodes=self.BRIDGE_MAX_NODES,
            subgraph_max_edges=self.BRIDGE_MAX_EDGES,
            subgraph_node_count=len(node_depth_map),
            subgraph_edge_count=len(public_edges),
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

        response = self.query_router.search(topic_clean, limit=top_k)
        results, retrieval_limitations = self._consume_retrieval_response(
            response,
            operation="timeline",
        )
        time_source_priority = ["event_time", "published_at", "archived_at"]
        points: list[TimelinePoint] = []
        excluded_entry_count = 0
        for result in results[:top_k]:
            entry = self.sqlite_store.query_by_id(result.knowledge_id) or {}
            if not self._entry_file_is_safe(entry):
                excluded_entry_count += 1
                continue
            resolved_times, physical_source_fields = self._resolve_time_fields(
                entry,
                result.metadata,
                self.vault_dir,
            )
            time_value, time_source = self._select_time_value(
                resolved_times, time_source_priority
            )
            time_source_field = physical_source_fields.get(
                time_source,
                "",
            )
            has_persisted_time = bool(time_value and time_source_field)
            citation_locator = (
                build_metadata_locator(
                    result.knowledge_id,
                    time_source_field,
                )
                if has_persisted_time
                else build_entry_locator(result.knowledge_id)
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
                    time_source_field=time_source_field,
                    time_precision=(
                        "structured_field" if has_persisted_time else "unavailable"
                    ),
                    source_type=entry.get("source_type", result.metadata.get("source_type", "")),
                    source_url=str(
                        entry.get("source_url")
                        or result.metadata.get("source_url", "")
                        or ""
                    ),
                    file_path=str(
                        entry.get("file_path")
                        or result.metadata.get("file_path", "")
                        or ""
                    ),
                    source=resolve_citation_source(
                        result.knowledge_id,
                        source_url=str(
                            entry.get("source_url")
                            or result.metadata.get("source_url", "")
                            or ""
                        ),
                        file_path=str(
                            entry.get("file_path")
                            or result.metadata.get("file_path", "")
                            or ""
                        ),
                    ),
                    citation_locator=citation_locator,
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
        unavailable_time_count = sum(
            1 for item in points if item.time_source == "unavailable"
        )
        limitation_notes = retrieval_limitations + [
            "当前优先使用 entry/metadata 中的 event_time/published_at 真实时间字段，缺失时才回退 archived_at，不代表正文中的完整真实事件时间",
            "仅对 SQLite 或 Markdown frontmatter 中可持久读取的时间字段生成精确 locator；临时检索 metadata 或损坏 frontmatter 不作为可引用时间来源",
            "当前未接入 video_timestamps、正文事件抽取或时间语义解析",
        ]
        if unavailable_time_count:
            limitation_notes.append(
                f"{unavailable_time_count} 个候选缺少可持久读取的时间字段；"
                "这些 item 标记 time_source/time_precision=unavailable，"
                "仅引用 entry Resource，不作为精确时间点"
            )
        if excluded_entry_count:
            limitation_notes.append(
                f"{excluded_entry_count} 个候选未通过 vault 文件边界校验，"
                "已从时间线中排除"
            )
        return TimelineResult(
            topic=topic_clean,
            found=bool(points),
            inferred_time_field=inferred_time_field,
            time_source_priority=time_source_priority,
            items=points,
            summary=(
                f"围绕主题「{topic_clean}」按时间来源优先级重建了 {len(points)} 个时间点，"
                f"其中 {real_time_count} 个命中了 event_time/published_at，"
                f"{unavailable_time_count} 个缺少可持久读取的时间字段"
                if points
                else f"未找到可用于主题「{topic_clean}」时间线重建的候选条目"
            ),
            evidence_sources=[
                "query_results",
                "entry_metadata",
                "structured_time_fields",
            ],
            limitation_notes=limitation_notes,
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

        response_a = self.query_router.search(topic_a_clean, limit=top_k)
        response_b = self.query_router.search(topic_b_clean, limit=top_k)
        raw_results_a, retrieval_limitations_a = self._consume_retrieval_response(
            response_a,
            operation="contrast_topic_a",
        )
        raw_results_b, retrieval_limitations_b = self._consume_retrieval_response(
            response_b,
            operation="contrast_topic_b",
        )
        results_a, excluded_a = self._filter_results_by_vault(raw_results_a)
        results_b, excluded_b = self._filter_results_by_vault(raw_results_b)

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
            "provenance": self._build_contrast_provenance(
                candidates_a=candidates_a,
                candidates_b=candidates_b,
                shared_tags=shared_tags,
                only_a_tags=only_a_tags,
                only_b_tags=only_b_tags,
                overlap_knowledge_ids=overlap_knowledge_ids,
                relation_pairs=relation_signal["pairs"],
            ),
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
            limitation_notes=retrieval_limitations_a
            + retrieval_limitations_b
            + [
                "当前已引入跨主题显式关系路径信号，但底层仍只依赖低歧义显式关系图与候选表层文本，不代表完整语义对比",
                "当前未引入 contrast 关系类型，也未建模争议/补充/因果等高级语义边",
            ]
            + (
                [
                    f"{excluded_a + excluded_b} 个候选未通过 vault "
                    "文件边界校验，已从对比中排除"
                ]
                if excluded_a or excluded_b
                else []
            )
            + (
                [
                    f"{relation_signal['excluded_unsafe_relation_path_count']} 条"
                    "候选关系路径未通过 vault 文件边界校验，未作为关系图信号"
                ]
                if relation_signal["excluded_unsafe_relation_path_count"]
                else []
            ),
        )

    def _build_contrast_item(self, result: Any) -> ContrastCandidateItem:
        entry = self.sqlite_store.query_by_id(result.knowledge_id) or {}
        source_url = str(
            entry.get("source_url") or result.metadata.get("source_url", "") or ""
        )
        file_path = str(
            entry.get("file_path") or result.metadata.get("file_path", "") or ""
        )
        return ContrastCandidateItem(
            knowledge_id=result.knowledge_id,
            title=entry.get("title", result.title),
            abstract=entry.get("summary_one_sentence", "") or result.highlight,
            archived_at=entry.get("archived_at", result.metadata.get("archived_at", "")),
            source_type=entry.get("source_type", result.metadata.get("source_type", "")),
            source_url=source_url,
            file_path=file_path,
            source=resolve_citation_source(
                result.knowledge_id,
                source_url=source_url,
                file_path=file_path,
            ),
            citation_locator=build_entry_locator(result.knowledge_id),
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

    def _entry_file_is_safe(self, entry: dict[str, Any]) -> bool:
        if self.vault_dir is None:
            return True
        try:
            resolve_vault_file_path(entry.get("file_path"), self.vault_dir)
        except Exception:
            return False
        return True

    def _filter_results_by_vault(
        self,
        results: list[Any],
    ) -> tuple[list[Any], int]:
        safe_results: list[Any] = []
        for result in results:
            entry = self.sqlite_store.query_by_id(result.knowledge_id) or {}
            if self._entry_file_is_safe(entry):
                safe_results.append(result)
        return safe_results, len(results) - len(safe_results)

    @staticmethod
    def _consume_retrieval_response(
        response: Any,
        *,
        operation: str,
    ) -> tuple[list[Any], list[str]]:
        """Consume the strict five-state retrieval contract without erasing outages."""
        stage = f"{operation}_retrieval"
        if not is_strict_search_response(response):
            raise PKVRuntimeError(
                ErrorCode.RETRIEVAL_BACKEND_FAILED,
                "检索服务返回了无效响应",
                stage=stage,
                recoverable=False,
            )

        if response.status in {"success", "no_hits"}:
            return list(response.results), []

        if response.status == "degraded":
            issue_codes = ",".join(
                sorted({issue.code.value for issue in response.issues})
            )
            return list(response.results), [
                f"{operation}_retrieval_degraded[{issue_codes}]："
                "部分检索能力不可用，结果可能不完整"
            ]

        issue = response.issues[0]
        if response.status == "invalid":
            public_message = "检索请求无效"
        else:
            public_message = "检索服务暂不可用"
        raise PKVRuntimeError(
            issue.code,
            public_message,
            stage=stage,
            recoverable=issue.recoverable,
        )

    def _relation_endpoints_are_vault_safe(
        self,
        record: Any,
        entry_cache: dict[int, dict[str, Any]],
    ) -> bool:
        """A public relation locator is usable only when both entries are usable."""
        return self._entry_file_is_safe(
            self._get_entry(record.source_knowledge_id, entry_cache)
        ) and self._entry_file_is_safe(
            self._get_entry(record.target_knowledge_id, entry_cache)
        )

    def _relation_records_are_vault_safe(
        self,
        records: list[Any],
        entry_cache: dict[int, dict[str, Any]],
    ) -> bool:
        return all(
            self._relation_endpoints_are_vault_safe(record, entry_cache)
            for record in records
        )

    def _filter_relation_records_by_vault(
        self,
        records: list[Any],
        entry_cache: dict[int, dict[str, Any]],
    ) -> tuple[list[Any], int]:
        public_records = [
            record
            for record in records
            if self._relation_endpoints_are_vault_safe(record, entry_cache)
        ]
        return public_records, len(records) - len(public_records)

    @staticmethod
    def _relation_key(record: Any) -> tuple[Any, ...]:
        if record.relation_id is not None:
            return ("id", record.relation_id)
        return (
            "edge",
            record.source_knowledge_id,
            record.target_knowledge_id,
            record.relation_type.value,
            record.relation_source_type.value,
        )

    @staticmethod
    def _relation_citation_locator(record: Any) -> str:
        return build_relation_locator(
            relation_id=record.relation_id,
            source_knowledge_id=record.source_knowledge_id,
            target_knowledge_id=record.target_knowledge_id,
            relation_type=record.relation_type.value,
            relation_source_type=record.relation_source_type.value,
        )

    @classmethod
    def _serialize_relation_edge(
        cls,
        record: Any,
        *,
        evidence_roles: list[str],
        from_knowledge_id: int | None = None,
        to_knowledge_id: int | None = None,
        traversal_direction: str = "",
        hop_index: int | None = None,
    ) -> dict[str, Any]:
        item = serialize_relation_evidence(record)
        item.update(
            {
                "evidence_roles": list(evidence_roles),
                "citation_locator": cls._relation_citation_locator(record),
            }
        )
        if from_knowledge_id is not None:
            item["from_knowledge_id"] = from_knowledge_id
        if to_knowledge_id is not None:
            item["to_knowledge_id"] = to_knowledge_id
        if traversal_direction:
            item["traversal_direction"] = traversal_direction
        if hop_index is not None:
            item["hop_index"] = hop_index
        return item

    @classmethod
    def _build_bridge_evidence_path(
        cls,
        *,
        seed_knowledge_id: int,
        candidate_knowledge_id: int,
        explanation: Any,
        subgraph_edges: list[Any],
    ) -> list[dict[str, Any]]:
        """Cover both seed reachability and every candidate-adjacent edge."""
        evidence_by_key: dict[tuple[Any, ...], dict[str, Any]] = {}
        ordered_keys: list[tuple[Any, ...]] = []

        current_knowledge_id = seed_knowledge_id
        if explanation and explanation.found:
            for hop_index, record in enumerate(explanation.path, start=1):
                if record.source_knowledge_id == current_knowledge_id:
                    next_knowledge_id = record.target_knowledge_id
                    traversal_direction = "forward"
                elif record.target_knowledge_id == current_knowledge_id:
                    next_knowledge_id = record.source_knowledge_id
                    traversal_direction = "reverse"
                else:
                    break

                key = cls._relation_key(record)
                evidence_by_key[key] = cls._serialize_relation_edge(
                    record,
                    evidence_roles=["seed_path"],
                    from_knowledge_id=current_knowledge_id,
                    to_knowledge_id=next_knowledge_id,
                    traversal_direction=traversal_direction,
                    hop_index=hop_index,
                )
                ordered_keys.append(key)
                current_knowledge_id = next_knowledge_id

        incident_edges = sorted(
            (
                edge
                for edge in subgraph_edges
                if candidate_knowledge_id
                in {edge.source_knowledge_id, edge.target_knowledge_id}
            ),
            key=lambda edge: (
                min(edge.source_knowledge_id, edge.target_knowledge_id),
                max(edge.source_knowledge_id, edge.target_knowledge_id),
                edge.relation_type.value,
                edge.relation_source_type.value,
                edge.relation_id or 0,
            ),
        )
        for record in incident_edges:
            if record.source_knowledge_id == candidate_knowledge_id:
                neighbor_id = record.target_knowledge_id
                traversal_direction = "forward"
            else:
                neighbor_id = record.source_knowledge_id
                traversal_direction = "reverse"
            key = cls._relation_key(record)
            if key in evidence_by_key:
                roles = evidence_by_key[key]["evidence_roles"]
                if "candidate_adjacency" not in roles:
                    roles.append("candidate_adjacency")
                continue
            evidence_by_key[key] = cls._serialize_relation_edge(
                record,
                evidence_roles=["candidate_adjacency"],
                from_knowledge_id=candidate_knowledge_id,
                to_knowledge_id=neighbor_id,
                traversal_direction=traversal_direction,
            )
            ordered_keys.append(key)

        return [evidence_by_key[key] for key in ordered_keys]

    @classmethod
    def _build_bridge_supporting_subgraph(
        cls,
        *,
        seed_knowledge_id: int,
        candidate_knowledge_id: int,
        neighbors: set[int],
        adjacency: dict[int, set[int]],
        node_depth_map: dict[int, int],
        subgraph_edges: list[Any],
        max_depth: int,
        semantic_score_inputs: dict[str, Any],
    ) -> dict[str, Any]:
        """Expose the complete bounded subgraph needed to recompute graph scores."""
        neighbor_list = sorted(neighbors)
        neighbor_pairs = []
        disconnected_pairs = []
        for left_id, right_id in combinations(neighbor_list, 2):
            connected = right_id in adjacency.get(left_id, set())
            pair = {
                "left_knowledge_id": left_id,
                "right_knowledge_id": right_id,
                "connected_within_scope": connected,
            }
            neighbor_pairs.append(pair)
            if not connected:
                disconnected_pairs.append(dict(pair))

        neighbor_depths = {
            str(neighbor_id): node_depth_map.get(neighbor_id, max_depth)
            for neighbor_id in neighbor_list
        }
        depth_values = sorted(neighbor_depths.values())
        depth_span = depth_values[-1] - depth_values[0] if depth_values else 0
        seed_frontier = (
            seed_knowledge_id in neighbors
            and any(
                node_depth_map.get(neighbor_id, max_depth) > 1
                for neighbor_id in neighbor_list
                if neighbor_id != seed_knowledge_id
            )
        )
        pair_ratio = len(disconnected_pairs) / max(len(neighbor_pairs), 1)

        scoped_edges = [
            cls._serialize_relation_edge(
                record,
                evidence_roles=["bounded_subgraph_edge"],
            )
            for record in subgraph_edges
        ]
        return {
            "scope": {
                "seed_knowledge_id": seed_knowledge_id,
                "candidate_knowledge_id": candidate_knowledge_id,
                "max_depth": max_depth,
                "node_depths": {
                    str(knowledge_id): depth
                    for knowledge_id, depth in sorted(node_depth_map.items())
                },
                "edge_completeness": "complete_unless_result_subgraph_truncated",
            },
            "edges": scoped_edges,
            "candidate_connected_knowledge_ids": neighbor_list,
            "neighbor_pairs": neighbor_pairs,
            "disconnected_neighbor_pairs": disconnected_pairs,
            "structural_score_inputs": {
                "candidate_depth": node_depth_map.get(
                    candidate_knowledge_id,
                    max_depth,
                ),
                "neighbor_count": len(neighbor_list),
                "max_depth": max_depth,
            },
            "graph_score_inputs": {
                "disconnected_pair_count": len(disconnected_pairs),
                "neighbor_pair_count": len(neighbor_pairs),
                "disconnected_pair_ratio": round(pair_ratio, 6),
                "neighbor_depths": neighbor_depths,
                "depth_span": depth_span,
                "seed_frontier": seed_frontier,
            },
            "semantic_score_inputs": semantic_score_inputs,
        }

    @staticmethod
    def _candidate_provenance(
        item: ContrastCandidateItem,
        topic_side: str,
    ) -> dict[str, Any]:
        return {
            "topic_side": topic_side,
            "knowledge_id": item.knowledge_id,
            "source": item.source,
            "source_url": item.source_url,
            "citation_locator": item.citation_locator,
        }

    @classmethod
    def _build_contrast_provenance(
        cls,
        *,
        candidates_a: list[ContrastCandidateItem],
        candidates_b: list[ContrastCandidateItem],
        shared_tags: list[str],
        only_a_tags: list[str],
        only_b_tags: list[str],
        overlap_knowledge_ids: list[int],
        relation_pairs: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Map every public comparison signal back to candidates and sources."""
        tagged_a = {
            tag: [
                cls._candidate_provenance(item, "topic_a")
                for item in candidates_a
                if tag in item.tags
            ]
            for tag in sorted({tag for item in candidates_a for tag in item.tags})
        }
        tagged_b = {
            tag: [
                cls._candidate_provenance(item, "topic_b")
                for item in candidates_b
                if tag in item.tags
            ]
            for tag in sorted({tag for item in candidates_b for tag in item.tags})
        }
        candidates_by_id_a = {item.knowledge_id: item for item in candidates_a}
        candidates_by_id_b = {item.knowledge_id: item for item in candidates_b}
        return {
            "shared_tags": {
                tag: {
                    "topic_a": tagged_a.get(tag, []),
                    "topic_b": tagged_b.get(tag, []),
                }
                for tag in shared_tags
            },
            "only_a_tags": {tag: tagged_a.get(tag, []) for tag in only_a_tags},
            "only_b_tags": {tag: tagged_b.get(tag, []) for tag in only_b_tags},
            "overlap_knowledge_ids": {
                str(knowledge_id): {
                    "topic_a": cls._candidate_provenance(
                        candidates_by_id_a[knowledge_id],
                        "topic_a",
                    ),
                    "topic_b": cls._candidate_provenance(
                        candidates_by_id_b[knowledge_id],
                        "topic_b",
                    ),
                }
                for knowledge_id in overlap_knowledge_ids
            },
            "relation_graph_signal": list(relation_pairs),
        }

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

    def _build_semantic_bridge_score_inputs(
        self,
        seed_entry: dict[str, Any],
        candidate_entry: dict[str, Any],
        neighbor_ids: set[int],
        entry_cache: dict[int, dict[str, Any]],
    ) -> dict[str, Any]:
        candidate_tokens = self._entry_tokens(candidate_entry)
        candidate_knowledge_id = candidate_entry.get("knowledge_id")
        candidate_locator = (
            build_entry_locator(int(candidate_knowledge_id))
            if candidate_knowledge_id
            else ""
        )
        if not candidate_tokens:
            return {
                "fields_used": [
                    "title",
                    "summary_one_sentence",
                    "summary_100_words",
                    "tags",
                ],
                "candidate": {
                    "knowledge_id": candidate_knowledge_id,
                    "citation_locator": candidate_locator,
                    "metadata_locator": (
                        build_entry_metadata_locator(
                            int(candidate_knowledge_id)
                        )
                        if candidate_knowledge_id
                        else ""
                    ),
                    "token_count": 0,
                },
                "comparisons": [],
                "anchor_score": 0.0,
                "support_score": 0.0,
                "coverage_score": 0.0,
                "semantic_score": 0.0,
            }

        comparison_scores: list[float] = []
        comparisons: list[dict[str, Any]] = []
        seed_tokens = self._entry_tokens(seed_entry)
        if seed_tokens:
            seed_knowledge_id = seed_entry.get("knowledge_id")
            overlap_count = len(candidate_tokens & seed_tokens)
            overlap_score = self._token_overlap(candidate_tokens, seed_tokens)
            comparison_scores.append(overlap_score)
            comparisons.append(
                {
                    "comparison_role": "seed",
                    "knowledge_id": seed_knowledge_id,
                    "citation_locator": (
                        build_entry_locator(int(seed_knowledge_id))
                        if seed_knowledge_id
                        else ""
                    ),
                    "metadata_locator": (
                        build_entry_metadata_locator(int(seed_knowledge_id))
                        if seed_knowledge_id
                        else ""
                    ),
                    "candidate_token_count": len(candidate_tokens),
                    "comparison_token_count": len(seed_tokens),
                    "overlap_token_count": overlap_count,
                    "overlap_score": round(overlap_score, 6),
                }
            )

        for neighbor_id in neighbor_ids:
            neighbor_entry = self._get_entry(neighbor_id, entry_cache)
            neighbor_tokens = self._entry_tokens(neighbor_entry)
            if neighbor_tokens:
                overlap_count = len(candidate_tokens & neighbor_tokens)
                overlap_score = self._token_overlap(
                    candidate_tokens,
                    neighbor_tokens,
                )
                comparison_scores.append(overlap_score)
                comparisons.append(
                    {
                        "comparison_role": "candidate_neighbor",
                        "knowledge_id": neighbor_id,
                        "citation_locator": build_entry_locator(neighbor_id),
                        "metadata_locator": build_entry_metadata_locator(
                            neighbor_id
                        ),
                        "candidate_token_count": len(candidate_tokens),
                        "comparison_token_count": len(neighbor_tokens),
                        "overlap_token_count": overlap_count,
                        "overlap_score": round(overlap_score, 6),
                    }
                )

        if not comparison_scores:
            anchor_score = 0.0
            support_score = 0.0
            coverage_score = 0.0
            semantic_score = 0.0
        else:
            top_scores = sorted(comparison_scores, reverse=True)
            anchor_score = top_scores[0]
            support_count = min(len(top_scores), 2)
            support_score = sum(top_scores[:support_count]) / support_count
            coverage_score = sum(
                1 for score in top_scores if score >= 0.08
            ) / len(top_scores)
            semantic_score = min(
                max(
                    anchor_score * 0.55
                    + support_score * 0.3
                    + coverage_score * 0.15,
                    0.0,
                ),
                1.0,
            )
        return {
            "fields_used": [
                "title",
                "summary_one_sentence",
                "summary_100_words",
                "tags",
            ],
            "candidate": {
                "knowledge_id": candidate_knowledge_id,
                "citation_locator": candidate_locator,
                "metadata_locator": (
                    build_entry_metadata_locator(int(candidate_knowledge_id))
                    if candidate_knowledge_id
                    else ""
                ),
                "token_count": len(candidate_tokens),
            },
            "comparisons": comparisons,
            "anchor_score": round(anchor_score, 6),
            "support_score": round(support_score, 6),
            "coverage_score": round(coverage_score, 6),
            "semantic_score": round(semantic_score, 6),
        }

    def _compute_semantic_bridge_score(
        self,
        seed_entry: dict[str, Any],
        candidate_entry: dict[str, Any],
        neighbor_ids: set[int],
        entry_cache: dict[int, dict[str, Any]],
    ) -> float:
        """Compatibility wrapper for callers that only need the score."""
        if (
            not self._entry_tokens(candidate_entry)
            or not candidate_entry.get("knowledge_id")
        ):
            return 0.0
        inputs = self._build_semantic_bridge_score_inputs(
            seed_entry=seed_entry,
            candidate_entry=candidate_entry,
            neighbor_ids=neighbor_ids,
            entry_cache=entry_cache,
        )
        return float(inputs["semantic_score"])

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
        vault_dir: Path | None = None,
    ) -> tuple[dict[str, str], dict[str, str]]:
        """Resolve semantic time categories and retain their physical field names."""
        resolved: dict[str, str] = {}
        physical_fields: dict[str, str] = {}
        candidates = {
            "event_time": (
                ("event_time", entry.get("event_time")),
                ("event_time", metadata.get("event_time")),
            ),
            "published_at": (
                ("published_at", entry.get("published_at")),
                ("published_at", metadata.get("published_at")),
                ("published_time", metadata.get("published_time")),
                ("publish_time", metadata.get("publish_time")),
            ),
            "archived_at": (
                ("archived_at", entry.get("archived_at")),
                ("archived_at", metadata.get("archived_at")),
            ),
        }
        for semantic_field, field_candidates in candidates.items():
            resolved[semantic_field] = ""
            physical_fields[semantic_field] = semantic_field
            for physical_field, raw_value in field_candidates:
                if raw_value in (None, ""):
                    continue
                found, persisted_value, _ = read_persisted_metadata_field(
                    entry,
                    physical_field,
                    vault_dir=vault_dir,
                )
                if found and str(persisted_value) == str(raw_value):
                    resolved[semantic_field] = str(persisted_value)
                    physical_fields[semantic_field] = physical_field
                    break
        return resolved, physical_fields

    @staticmethod
    def _select_time_value(
        time_fields: dict[str, str],
        priority: list[str],
    ) -> tuple[str, str]:
        for field in priority:
            value = time_fields.get(field, "")
            if value:
                return value, field
        return "", "unavailable"

    @staticmethod
    def _infer_timeline_source(
        points: list[TimelinePoint],
        priority: list[str],
    ) -> str:
        if not priority:
            return "unavailable"
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
            return "unavailable"

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
        pair_provenance: list[dict[str, Any]] = []
        relation_types_seen: set[str] = set()
        max_relation_hops = 0
        entry_cache: dict[int, dict[str, Any]] = {}
        excluded_unsafe_relation_path_count = 0

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
                if not self._relation_records_are_vault_safe(
                    explanation.path + explanation.supporting_relations,
                    entry_cache,
                ):
                    excluded_unsafe_relation_path_count += 1
                    continue

                connected_pairs += 1
                pair_score = round(float(explanation.confidence), 4)
                relation_types = self._extract_relation_types(explanation)
                relation_types_seen.update(relation_types)
                max_relation_hops = max(max_relation_hops, explanation.hops)
                pair_provenance.append(
                    {
                        "topic_a_knowledge_id": item_a.knowledge_id,
                        "topic_b_knowledge_id": item_b.knowledge_id,
                        "topic_a_source": item_a.source,
                        "topic_b_source": item_b.source,
                        "topic_a_citation_locator": item_a.citation_locator,
                        "topic_b_citation_locator": item_b.citation_locator,
                        "confidence": pair_score,
                        "relation_types": relation_types,
                        "evidence_path": self._build_bridge_evidence_path(
                            seed_knowledge_id=item_a.knowledge_id,
                            candidate_knowledge_id=item_b.knowledge_id,
                            explanation=explanation,
                            subgraph_edges=explanation.path,
                        ),
                    }
                )
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
            "pairs": pair_provenance,
            "excluded_unsafe_relation_path_count": (
                excluded_unsafe_relation_path_count
            ),
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
