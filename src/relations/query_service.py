"""
关系查询服务。

本模块只负责一跳关系查询、稳定排序和按关系类型分组。
当前不承担多跳遍历、推理或 MCP 参数适配职责。
"""

from __future__ import annotations

from collections import OrderedDict, defaultdict
from datetime import datetime
from typing import Iterable, Optional

from src.relations.models import (
    RelationQueryDirection,
    RelationQueryResult,
    RelationRecord,
    RelationType,
)
from src.storage.relation_store import RelationStore


class RelationQueryService:
    """一跳关系查询服务。"""

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

    @staticmethod
    def _timestamp_to_rank(raw_timestamp: Optional[str]) -> float:
        if not raw_timestamp:
            return 0.0
        try:
            return datetime.fromisoformat(raw_timestamp.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return 0.0
