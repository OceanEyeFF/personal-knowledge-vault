"""
关系查询服务。

本模块提供内部关系查询底座，包括一跳查询、受限子图遍历和最小关系解释。
当前仍不承担 MCP 参数适配或完整证据聚合职责。
"""

from __future__ import annotations

from collections import OrderedDict, defaultdict, deque
from datetime import datetime
from typing import Iterable, Optional

from src.relations.models import (
    RelationExplanationResult,
    RelationQueryDirection,
    RelationQueryResult,
    RelationRecord,
    RelationSubgraphNode,
    RelationSubgraphResult,
    RelationType,
)
from src.storage.relation_store import RelationStore


class RelationQueryService:
    """关系查询与最小解释服务。"""

    def __init__(self, relation_store: RelationStore):
        self.relation_store = relation_store
        self._relation_type_order = {
            relation_type.value: index
            for index, relation_type in enumerate(RelationType)
        }

    def list_relations(
        self,
        seed_knowledge_id: int,
        direction: RelationQueryDirection | str = RelationQueryDirection.BOTH,
        relation_types: Optional[Iterable[RelationType | str]] = None,
        relation_source_types: Optional[Iterable[str]] = None,
        limit: int = 100,
        offset: int = 0,
        group_by_relation_type: bool = True,
    ) -> RelationQueryResult:
        """列出与 seed 相邻的一跳关系。"""
        records = self.relation_store.list_relations_for_knowledge(
            knowledge_id=seed_knowledge_id,
            direction=direction,
            relation_types=relation_types,
            relation_source_types=relation_source_types,
            limit=limit,
            offset=offset,
        )
        return self._build_result(
            seed_knowledge_id=seed_knowledge_id,
            query_direction=direction,
            records=records,
            group_by_relation_type=group_by_relation_type,
        )

    def get_neighbors(
        self,
        seed_knowledge_id: int,
        relation_types: Optional[Iterable[RelationType | str]] = None,
        relation_source_types: Optional[Iterable[str]] = None,
        limit: int = 100,
        group_by_relation_type: bool = True,
    ) -> RelationQueryResult:
        """获取与 seed 直接相邻的一跳关系。"""
        return self.list_relations(
            seed_knowledge_id=seed_knowledge_id,
            direction=RelationQueryDirection.BOTH,
            relation_types=relation_types,
            relation_source_types=relation_source_types,
            limit=limit,
            offset=0,
            group_by_relation_type=group_by_relation_type,
        )

    def get_relations_between(
        self,
        knowledge_id_a: int,
        knowledge_id_b: int,
        relation_types: Optional[Iterable[RelationType | str]] = None,
        relation_source_types: Optional[Iterable[str]] = None,
        limit: int = 100,
        group_by_relation_type: bool = True,
    ) -> RelationQueryResult:
        """查询两个条目之间的一跳关系。"""
        if knowledge_id_a <= 0 or knowledge_id_b <= 0:
            raise ValueError("knowledge_id 必须为正整数")
        if knowledge_id_a == knowledge_id_b:
            raise ValueError("暂不支持同一条目之间的关系查询")

        candidate_records = self.relation_store.list_relations_for_knowledge(
            knowledge_id=knowledge_id_a,
            direction=RelationQueryDirection.BOTH,
            relation_types=relation_types,
            relation_source_types=relation_source_types,
            limit=max(limit * 2, limit),
            offset=0,
        )

        matched_records = [
            record
            for record in candidate_records
            if {record.source_knowledge_id, record.target_knowledge_id}
            == {knowledge_id_a, knowledge_id_b}
        ][:limit]

        return self._build_result(
            seed_knowledge_id=knowledge_id_a,
            query_direction=RelationQueryDirection.BOTH,
            records=matched_records,
            group_by_relation_type=group_by_relation_type,
        )

    def query_subgraph(
        self,
        seed_knowledge_id: int,
        depth: int = 2,
        relation_types: Optional[Iterable[RelationType | str]] = None,
        relation_source_types: Optional[Iterable[str]] = None,
        per_node_limit: int = 100,
        max_nodes: int = 200,
        max_edges: int = 500,
        group_by_relation_type: bool = True,
    ) -> RelationSubgraphResult:
        """基于一跳查询服务做受限多跳子图遍历。"""
        if seed_knowledge_id <= 0:
            raise ValueError("seed_knowledge_id 必须为正整数")
        if depth <= 0:
            raise ValueError("depth 必须大于 0")
        if per_node_limit <= 0:
            raise ValueError("per_node_limit 必须大于 0")
        if max_nodes <= 0:
            raise ValueError("max_nodes 必须大于 0")
        if max_edges <= 0:
            raise ValueError("max_edges 必须大于 0")

        pending_nodes = deque([(seed_knowledge_id, 0)])
        node_depths = {seed_knowledge_id: 0}
        collected_records: dict[tuple[object, ...], RelationRecord] = {}
        truncated = False

        while pending_nodes:
            current_knowledge_id, current_depth = pending_nodes.popleft()
            if current_depth >= depth:
                continue

            hop_result = self.list_relations(
                seed_knowledge_id=current_knowledge_id,
                direction=RelationQueryDirection.BOTH,
                relation_types=relation_types,
                relation_source_types=relation_source_types,
                limit=per_node_limit,
                offset=0,
                group_by_relation_type=False,
            )

            for record in hop_result.items:
                neighbor_knowledge_id = self._resolve_neighbor_knowledge_id(
                    current_knowledge_id,
                    record,
                )
                if neighbor_knowledge_id is None:
                    continue

                if (
                    neighbor_knowledge_id not in node_depths
                    and len(node_depths) >= max_nodes
                ):
                    truncated = True
                    continue

                record_key = self._relation_record_key(record)
                if record_key not in collected_records:
                    if len(collected_records) >= max_edges:
                        truncated = True
                        continue
                    collected_records[record_key] = record

                if neighbor_knowledge_id in node_depths:
                    continue

                next_depth = current_depth + 1
                node_depths[neighbor_knowledge_id] = next_depth
                if next_depth < depth:
                    pending_nodes.append((neighbor_knowledge_id, next_depth))

        records = list(collected_records.values())
        grouped_edges = self._group_records(records) if group_by_relation_type else {}
        ordered_edges = (
            self._flatten_grouped_items(grouped_edges)
            if grouped_edges
            else self._sort_group(records)
        )
        ordered_nodes = [
            RelationSubgraphNode(knowledge_id=knowledge_id, depth=node_depths[knowledge_id])
            for knowledge_id in sorted(
                node_depths,
                key=lambda knowledge_id: (node_depths[knowledge_id], knowledge_id),
            )
        ]
        return RelationSubgraphResult(
            seed_knowledge_id=seed_knowledge_id,
            max_depth=depth,
            nodes=ordered_nodes,
            edges=ordered_edges,
            grouped_edges=grouped_edges,
            truncated=truncated,
        )

    def explain_relation(
        self,
        source_knowledge_id: int,
        target_knowledge_id: int,
        relation_types: Optional[Iterable[RelationType | str]] = None,
        relation_source_types: Optional[Iterable[str]] = None,
        max_depth: int = 2,
        per_node_limit: int = 100,
    ) -> RelationExplanationResult:
        """解释两个条目之间为何相关。"""
        if source_knowledge_id <= 0 or target_knowledge_id <= 0:
            raise ValueError("knowledge_id 必须为正整数")
        if source_knowledge_id == target_knowledge_id:
            raise ValueError("暂不支持同一条目之间的关系解释")
        if max_depth <= 0:
            raise ValueError("max_depth 必须大于 0")
        if per_node_limit <= 0:
            raise ValueError("per_node_limit 必须大于 0")

        direct_result = self.get_relations_between(
            source_knowledge_id,
            target_knowledge_id,
            relation_types=relation_types,
            relation_source_types=relation_source_types,
            limit=per_node_limit,
            group_by_relation_type=False,
        )
        if direct_result.total > 0:
            primary_relation = direct_result.items[0]
            return RelationExplanationResult(
                source_knowledge_id=source_knowledge_id,
                target_knowledge_id=target_knowledge_id,
                found=True,
                explanation_type="direct",
                hops=1,
                path=[primary_relation],
                supporting_relations=direct_result.items,
                summary=self._build_path_summary(
                    source_knowledge_id,
                    [primary_relation],
                ),
                evidence_items=[
                    self._build_evidence_item(record, step_index=index)
                    for index, record in enumerate(direct_result.items)
                ],
            )

        path = self._find_shortest_path(
            source_knowledge_id=source_knowledge_id,
            target_knowledge_id=target_knowledge_id,
            relation_types=relation_types,
            relation_source_types=relation_source_types,
            max_depth=max_depth,
            per_node_limit=per_node_limit,
        )
        if path:
            node_path = self._build_path_node_ids(source_knowledge_id, path)
            return RelationExplanationResult(
                source_knowledge_id=source_knowledge_id,
                target_knowledge_id=target_knowledge_id,
                found=True,
                explanation_type="path",
                hops=len(path),
                path=path,
                supporting_relations=path,
                intermediate_knowledge_ids=node_path[1:-1],
                summary=self._build_path_summary(source_knowledge_id, path),
                evidence_items=[
                    self._build_evidence_item(record, step_index=index)
                    for index, record in enumerate(path)
                ],
            )

        return RelationExplanationResult(
            source_knowledge_id=source_knowledge_id,
            target_knowledge_id=target_knowledge_id,
            found=False,
            explanation_type="not_found",
            hops=0,
            summary=(
                f"未找到 {source_knowledge_id} 与 {target_knowledge_id} "
                f"在 {max_depth} 跳内的关系解释"
            ),
        )

    def _build_result(
        self,
        seed_knowledge_id: int,
        query_direction: RelationQueryDirection | str,
        records: list[RelationRecord],
        group_by_relation_type: bool,
    ) -> RelationQueryResult:
        grouped_items = self._group_records(records) if group_by_relation_type else {}
        ordered_items = self._flatten_grouped_items(grouped_items) if grouped_items else self._sort_group(records)
        return RelationQueryResult(
            seed_knowledge_id=seed_knowledge_id,
            query_direction=query_direction,
            items=ordered_items,
            grouped_items=grouped_items,
        )

    def _group_records(
        self,
        records: list[RelationRecord],
    ) -> dict[str, list[RelationRecord]]:
        grouped_raw: dict[str, list[RelationRecord]] = defaultdict(list)
        for record in records:
            grouped_raw[record.relation_type.value].append(record)

        ordered_groups: "OrderedDict[str, list[RelationRecord]]" = OrderedDict()
        for relation_type in sorted(grouped_raw, key=self._relation_type_sort_key):
            ordered_groups[relation_type] = self._sort_group(grouped_raw[relation_type])

        return dict(ordered_groups)

    def _flatten_grouped_items(
        self,
        grouped_items: dict[str, list[RelationRecord]],
    ) -> list[RelationRecord]:
        flattened: list[RelationRecord] = []
        for items in grouped_items.values():
            flattened.extend(items)
        return flattened

    def _sort_group(self, records: list[RelationRecord]) -> list[RelationRecord]:
        return sorted(
            records,
            key=lambda record: (
                -record.weight,
                -self._timestamp_to_rank(record.updated_at or record.created_at),
                record.relation_id if record.relation_id is not None else 2**31 - 1,
            ),
        )

    def _relation_type_sort_key(self, relation_type_value: str) -> tuple[int, str]:
        return (
            self._relation_type_order.get(relation_type_value, len(self._relation_type_order)),
            relation_type_value,
        )

    def _find_shortest_path(
        self,
        source_knowledge_id: int,
        target_knowledge_id: int,
        relation_types: Optional[Iterable[RelationType | str]],
        relation_source_types: Optional[Iterable[str]],
        max_depth: int,
        per_node_limit: int,
    ) -> Optional[list[RelationRecord]]:
        pending_nodes = deque([(source_knowledge_id, [])])
        best_depth_by_node = {source_knowledge_id: 0}

        while pending_nodes:
            current_knowledge_id, current_path = pending_nodes.popleft()
            current_depth = len(current_path)
            if current_depth >= max_depth:
                continue

            hop_result = self.list_relations(
                seed_knowledge_id=current_knowledge_id,
                direction=RelationQueryDirection.BOTH,
                relation_types=relation_types,
                relation_source_types=relation_source_types,
                limit=per_node_limit,
                offset=0,
                group_by_relation_type=False,
            )

            for record in hop_result.items:
                neighbor_knowledge_id = self._resolve_neighbor_knowledge_id(
                    current_knowledge_id,
                    record,
                )
                if neighbor_knowledge_id is None:
                    continue

                next_path = [*current_path, record]
                if neighbor_knowledge_id == target_knowledge_id:
                    return next_path

                next_depth = len(next_path)
                if next_depth >= max_depth:
                    continue
                if best_depth_by_node.get(neighbor_knowledge_id, next_depth + 1) <= next_depth:
                    continue

                best_depth_by_node[neighbor_knowledge_id] = next_depth
                pending_nodes.append((neighbor_knowledge_id, next_path))

        return None

    def _build_path_summary(
        self,
        source_knowledge_id: int,
        path: list[RelationRecord],
    ) -> str:
        if not path:
            return ""

        current_knowledge_id = source_knowledge_id
        parts = [str(source_knowledge_id)]
        for record in path:
            next_knowledge_id = self._resolve_neighbor_knowledge_id(
                current_knowledge_id,
                record,
            )
            if next_knowledge_id is None:
                break

            if record.source_knowledge_id == current_knowledge_id:
                connector = f"-[{record.relation_type.value}]->"
            else:
                connector = f"<-[{record.relation_type.value}]-"

            parts.append(connector)
            parts.append(str(next_knowledge_id))
            current_knowledge_id = next_knowledge_id

        return " ".join(parts)

    def _build_path_node_ids(
        self,
        source_knowledge_id: int,
        path: list[RelationRecord],
    ) -> list[int]:
        node_ids = [source_knowledge_id]
        current_knowledge_id = source_knowledge_id

        for record in path:
            next_knowledge_id = self._resolve_neighbor_knowledge_id(
                current_knowledge_id,
                record,
            )
            if next_knowledge_id is None:
                break
            node_ids.append(next_knowledge_id)
            current_knowledge_id = next_knowledge_id

        return node_ids

    @staticmethod
    def _build_evidence_item(
        record: RelationRecord,
        step_index: int,
    ) -> dict[str, object]:
        return {
            "step_index": step_index,
            "relation_type": record.relation_type.value,
            "relation_source_type": record.relation_source_type.value,
            "direction": record.direction.value,
            "weight": record.weight,
            "source_knowledge_id": record.source_knowledge_id,
            "target_knowledge_id": record.target_knowledge_id,
            "evidence_payload": dict(record.evidence_payload),
        }

    @staticmethod
    def _resolve_neighbor_knowledge_id(
        seed_knowledge_id: int,
        record: RelationRecord,
    ) -> Optional[int]:
        if record.source_knowledge_id == seed_knowledge_id:
            return record.target_knowledge_id
        if record.target_knowledge_id == seed_knowledge_id:
            return record.source_knowledge_id
        return None

    @staticmethod
    def _relation_record_key(record: RelationRecord) -> tuple[object, ...]:
        if record.relation_id is not None:
            return ("relation_id", record.relation_id)
        return (
            "relation_fields",
            record.source_knowledge_id,
            record.target_knowledge_id,
            record.relation_type.value,
            record.relation_source_type.value,
            record.direction.value,
        )

    @staticmethod
    def _timestamp_to_rank(raw_timestamp: Optional[str]) -> float:
        if not raw_timestamp:
            return 0.0
        try:
            return datetime.fromisoformat(raw_timestamp.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return 0.0
