"""
关系层核心数据模型。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Iterable, Optional


class RelationType(str, Enum):
    """第一版低歧义关系类型。"""

    REFERENCES = "references"
    RELATED_DOCUMENT = "related_document"
    PARENT_OF = "parent_of"
    VERSION_OF = "version_of"


class RelationSourceType(str, Enum):
    """关系来源类型。"""

    MARKDOWN_LINK = "markdown_link"
    FRONTMATTER_RELATED_DOCS = "frontmatter_related_docs"
    FRONTMATTER_FIELD = "frontmatter_field"
    MANUAL = "manual"
    BACKFILL = "backfill"


class RelationDirection(str, Enum):
    """关系方向语义。"""

    DIRECTED = "directed"
    BIDIRECTIONAL = "bidirectional"


class RelationQueryDirection(str, Enum):
    """查询时相对 seed 的方向。"""

    OUTGOING = "outgoing"
    INCOMING = "incoming"
    BOTH = "both"


LOW_AMBIGUITY_RELATION_TYPES: tuple[RelationType, ...] = (
    RelationType.REFERENCES,
    RelationType.RELATED_DOCUMENT,
    RelationType.PARENT_OF,
    RelationType.VERSION_OF,
)


def _normalize_enum(enum_cls: type[Enum], value: Enum | str) -> Enum:
    if isinstance(value, enum_cls):
        return value
    return enum_cls(value)


@dataclass
class RelationRecord:
    """关系记录。

    约定：
    - `source_knowledge_id -> target_knowledge_id` 表示存储层中的有向边
    - `direction=bidirectional` 仅表示该边在语义上可双向解释，不改变字段含义
    """

    source_knowledge_id: int
    target_knowledge_id: int
    relation_type: RelationType | str
    relation_source_type: RelationSourceType | str
    direction: RelationDirection | str = RelationDirection.DIRECTED
    weight: float = 1.0
    evidence_payload: Dict[str, Any] = field(default_factory=dict)
    relation_id: Optional[int] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    def __post_init__(self) -> None:
        self.relation_type = _normalize_enum(RelationType, self.relation_type)
        self.relation_source_type = _normalize_enum(
            RelationSourceType, self.relation_source_type
        )
        self.direction = _normalize_enum(RelationDirection, self.direction)

        if self.source_knowledge_id <= 0 or self.target_knowledge_id <= 0:
            raise ValueError("knowledge_id 必须为正整数")
        if self.source_knowledge_id == self.target_knowledge_id:
            raise ValueError("暂不支持自指关系")
        if self.weight <= 0:
            raise ValueError("weight 必须大于 0")
        if not isinstance(self.evidence_payload, dict):
            raise TypeError("evidence_payload 必须为字典")

    def to_dict(self) -> Dict[str, Any]:
        """转换为便于调试和序列化的字典。"""
        return {
            "relation_id": self.relation_id,
            "source_knowledge_id": self.source_knowledge_id,
            "target_knowledge_id": self.target_knowledge_id,
            "relation_type": self.relation_type.value,
            "relation_source_type": self.relation_source_type.value,
            "direction": self.direction.value,
            "weight": self.weight,
            "evidence_payload": dict(self.evidence_payload),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    def to_db_payload(self) -> str:
        """序列化证据字段。"""
        return json.dumps(self.evidence_payload, ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_row(cls, row: Dict[str, Any]) -> "RelationRecord":
        """从 SQLite 行对象恢复关系记录。"""
        evidence_raw = row.get("evidence_payload") or "{}"
        evidence_payload = json.loads(evidence_raw)
        return cls(
            relation_id=row.get("relation_id"),
            source_knowledge_id=row["source_knowledge_id"],
            target_knowledge_id=row["target_knowledge_id"],
            relation_type=row["relation_type"],
            relation_source_type=row["relation_source_type"],
            direction=row["direction"],
            weight=row["weight"],
            evidence_payload=evidence_payload,
            created_at=row.get("created_at"),
            updated_at=row.get("updated_at"),
        )


@dataclass
class RelationQueryResult:
    """关系查询统一返回结构。"""

    seed_knowledge_id: int
    query_direction: RelationQueryDirection | str
    items: list[RelationRecord]
    grouped_items: Dict[str, list[RelationRecord]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.query_direction = _normalize_enum(
            RelationQueryDirection, self.query_direction
        )
        if self.seed_knowledge_id <= 0:
            raise ValueError("seed_knowledge_id 必须为正整数")

    @property
    def total(self) -> int:
        return len(self.items)

    def to_dict(self) -> Dict[str, Any]:
        """转换为后续 query service 可直接复用的结构。"""
        return {
            "seed_knowledge_id": self.seed_knowledge_id,
            "query_direction": self.query_direction.value,
            "total": self.total,
            "items": [item.to_dict() for item in self.items],
            "grouped_items": {
                relation_type: [item.to_dict() for item in items]
                for relation_type, items in self.grouped_items.items()
            },
        }


@dataclass(frozen=True)
class RelationSubgraphNode:
    """关系子图中的节点。"""

    knowledge_id: int
    depth: int

    def __post_init__(self) -> None:
        if self.knowledge_id <= 0:
            raise ValueError("knowledge_id 必须为正整数")
        if self.depth < 0:
            raise ValueError("depth 不能为负数")

    def to_dict(self) -> Dict[str, int]:
        return {
            "knowledge_id": self.knowledge_id,
            "depth": self.depth,
        }


@dataclass
class RelationSubgraphResult:
    """多跳关系子图统一返回结构。"""

    seed_knowledge_id: int
    max_depth: int
    nodes: list[RelationSubgraphNode]
    edges: list[RelationRecord]
    grouped_edges: Dict[str, list[RelationRecord]] = field(default_factory=dict)
    truncated: bool = False

    def __post_init__(self) -> None:
        if self.seed_knowledge_id <= 0:
            raise ValueError("seed_knowledge_id 必须为正整数")
        if self.max_depth <= 0:
            raise ValueError("max_depth 必须大于 0")

    @property
    def total_nodes(self) -> int:
        return len(self.nodes)

    @property
    def total_edges(self) -> int:
        return len(self.edges)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "seed_knowledge_id": self.seed_knowledge_id,
            "max_depth": self.max_depth,
            "total_nodes": self.total_nodes,
            "total_edges": self.total_edges,
            "truncated": self.truncated,
            "nodes": [node.to_dict() for node in self.nodes],
            "edges": [edge.to_dict() for edge in self.edges],
            "grouped_edges": {
                relation_type: [edge.to_dict() for edge in edges]
                for relation_type, edges in self.grouped_edges.items()
            },
        }


@dataclass
class RelationExplanationResult:
    """关系解释统一返回结构。"""

    source_knowledge_id: int
    target_knowledge_id: int
    found: bool
    explanation_type: str
    hops: int
    path: list[RelationRecord] = field(default_factory=list)
    supporting_relations: list[RelationRecord] = field(default_factory=list)
    intermediate_knowledge_ids: list[int] = field(default_factory=list)
    summary: str = ""
    evidence_items: list[Dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.source_knowledge_id <= 0 or self.target_knowledge_id <= 0:
            raise ValueError("knowledge_id 必须为正整数")
        if self.source_knowledge_id == self.target_knowledge_id:
            raise ValueError("source_knowledge_id 与 target_knowledge_id 不能相同")
        if self.hops < 0:
            raise ValueError("hops 不能为负数")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source_knowledge_id": self.source_knowledge_id,
            "target_knowledge_id": self.target_knowledge_id,
            "found": self.found,
            "explanation_type": self.explanation_type,
            "hops": self.hops,
            "path": [record.to_dict() for record in self.path],
            "supporting_relations": [
                record.to_dict() for record in self.supporting_relations
            ],
            "intermediate_knowledge_ids": list(self.intermediate_knowledge_ids),
            "summary": self.summary,
            "evidence_items": [dict(item) for item in self.evidence_items],
        }


@dataclass
class CollectedEvidenceItem:
    """证据聚合中的单条证据项。"""

    knowledge_id: int
    title: str
    abstract: str = ""
    source_type: str = ""
    archived_at: str = ""
    tags: list[str] = field(default_factory=list)
    source_url: str = ""
    file_path: str = ""
    content_preview: str = ""
    retrieval_rank: int = 1
    retrieval_score: float = 0.0
    is_seed: bool = False
    relation_found: bool = False
    relation_explanation_type: str = ""
    relation_hops: int = 0
    relation_summary: str = ""
    relation_path: list[RelationRecord] = field(default_factory=list)
    relation_evidence_items: list[Dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.knowledge_id <= 0:
            raise ValueError("knowledge_id 必须为正整数")
        if self.retrieval_rank <= 0:
            raise ValueError("retrieval_rank 必须为正整数")
        if not (0.0 <= self.retrieval_score <= 1.0):
            raise ValueError("retrieval_score 必须在 [0.0, 1.0] 范围内")
        if self.relation_hops < 0:
            raise ValueError("relation_hops 不能为负数")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "knowledge_id": self.knowledge_id,
            "title": self.title,
            "abstract": self.abstract,
            "source_type": self.source_type,
            "archived_at": self.archived_at,
            "tags": list(self.tags),
            "source_url": self.source_url,
            "file_path": self.file_path,
            "content_preview": self.content_preview,
            "retrieval_rank": self.retrieval_rank,
            "retrieval_score": self.retrieval_score,
            "is_seed": self.is_seed,
            "relation_found": self.relation_found,
            "relation_explanation_type": self.relation_explanation_type,
            "relation_hops": self.relation_hops,
            "relation_summary": self.relation_summary,
            "relation_path": [record.to_dict() for record in self.relation_path],
            "relation_evidence_items": [
                dict(item) for item in self.relation_evidence_items
            ],
        }


@dataclass
class CollectedEvidenceResult:
    """证据聚合统一返回结构。"""

    question: str
    found: bool
    seed_knowledge_id: Optional[int] = None
    seed_title: str = ""
    evidence: list[CollectedEvidenceItem] = field(default_factory=list)
    summary: str = ""

    def __post_init__(self) -> None:
        if not self.question or not self.question.strip():
            raise ValueError("question 不能为空")
        if self.seed_knowledge_id is not None and self.seed_knowledge_id <= 0:
            raise ValueError("seed_knowledge_id 必须为正整数")

    @property
    def total_evidence(self) -> int:
        return len(self.evidence)

    @property
    def related_evidence_count(self) -> int:
        return sum(1 for item in self.evidence if item.relation_found)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "question": self.question,
            "found": self.found,
            "seed_knowledge_id": self.seed_knowledge_id,
            "seed_title": self.seed_title,
            "total_evidence": self.total_evidence,
            "related_evidence_count": self.related_evidence_count,
            "summary": self.summary,
            "evidence": [item.to_dict() for item in self.evidence],
        }


@dataclass
class BridgeCandidate:
    """桥接候选节点。"""

    knowledge_id: int
    title: str
    depth: int
    bridge_score: float
    connected_knowledge_ids: list[int] = field(default_factory=list)
    relation_types: list[str] = field(default_factory=list)
    summary: str = ""

    def __post_init__(self) -> None:
        if self.knowledge_id <= 0:
            raise ValueError("knowledge_id 必须为正整数")
        if self.depth < 0:
            raise ValueError("depth 不能为负数")
        if self.bridge_score < 0:
            raise ValueError("bridge_score 不能为负数")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "knowledge_id": self.knowledge_id,
            "title": self.title,
            "depth": self.depth,
            "bridge_score": self.bridge_score,
            "connected_knowledge_ids": list(self.connected_knowledge_ids),
            "relation_types": list(self.relation_types),
            "summary": self.summary,
        }


@dataclass
class BridgeDiscoveryResult:
    """桥接发现结果。"""

    seed_knowledge_id: int
    found: bool
    max_depth: int
    items: list[BridgeCandidate] = field(default_factory=list)
    summary: str = ""
    implementation_level: str = "partial"
    limitation_notes: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.seed_knowledge_id <= 0:
            raise ValueError("seed_knowledge_id 必须为正整数")
        if self.max_depth <= 0:
            raise ValueError("max_depth 必须大于 0")

    @property
    def total_bridges(self) -> int:
        return len(self.items)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "seed_knowledge_id": self.seed_knowledge_id,
            "found": self.found,
            "max_depth": self.max_depth,
            "total_bridges": self.total_bridges,
            "summary": self.summary,
            "implementation_level": self.implementation_level,
            "limitation_notes": list(self.limitation_notes),
            "items": [item.to_dict() for item in self.items],
        }


@dataclass
class TimelinePoint:
    """时间线条目。"""

    knowledge_id: int
    title: str
    archived_at: str = ""
    source_type: str = ""
    abstract: str = ""
    tags: list[str] = field(default_factory=list)
    retrieval_score: float = 0.0

    def __post_init__(self) -> None:
        if self.knowledge_id <= 0:
            raise ValueError("knowledge_id 必须为正整数")
        if not (0.0 <= self.retrieval_score <= 1.0):
            raise ValueError("retrieval_score 必须在 [0.0, 1.0] 范围内")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "knowledge_id": self.knowledge_id,
            "title": self.title,
            "archived_at": self.archived_at,
            "source_type": self.source_type,
            "abstract": self.abstract,
            "tags": list(self.tags),
            "retrieval_score": self.retrieval_score,
        }


@dataclass
class TimelineResult:
    """时间线重建结果。"""

    topic: str
    found: bool
    inferred_time_field: str = "archived_at"
    items: list[TimelinePoint] = field(default_factory=list)
    summary: str = ""
    implementation_level: str = "partial"
    limitation_notes: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.topic or not self.topic.strip():
            raise ValueError("topic 不能为空")

    @property
    def total_points(self) -> int:
        return len(self.items)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "topic": self.topic,
            "found": self.found,
            "inferred_time_field": self.inferred_time_field,
            "total_points": self.total_points,
            "summary": self.summary,
            "implementation_level": self.implementation_level,
            "limitation_notes": list(self.limitation_notes),
            "items": [item.to_dict() for item in self.items],
        }


@dataclass
class ContrastCandidateItem:
    """对比分析中的候选条目。"""

    knowledge_id: int
    title: str
    abstract: str = ""
    archived_at: str = ""
    source_type: str = ""
    tags: list[str] = field(default_factory=list)
    retrieval_score: float = 0.0

    def __post_init__(self) -> None:
        if self.knowledge_id <= 0:
            raise ValueError("knowledge_id 必须为正整数")
        if not (0.0 <= self.retrieval_score <= 1.0):
            raise ValueError("retrieval_score 必须在 [0.0, 1.0] 范围内")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "knowledge_id": self.knowledge_id,
            "title": self.title,
            "abstract": self.abstract,
            "archived_at": self.archived_at,
            "source_type": self.source_type,
            "tags": list(self.tags),
            "retrieval_score": self.retrieval_score,
        }


@dataclass
class ContrastResult:
    """主题对比结果。"""

    topic_a: str
    topic_b: str
    found: bool
    topic_a_candidates: list[ContrastCandidateItem] = field(default_factory=list)
    topic_b_candidates: list[ContrastCandidateItem] = field(default_factory=list)
    shared_tags: list[str] = field(default_factory=list)
    only_a_tags: list[str] = field(default_factory=list)
    only_b_tags: list[str] = field(default_factory=list)
    overlap_knowledge_ids: list[int] = field(default_factory=list)
    summary: str = ""
    implementation_level: str = "partial"
    limitation_notes: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.topic_a or not self.topic_a.strip():
            raise ValueError("topic_a 不能为空")
        if not self.topic_b or not self.topic_b.strip():
            raise ValueError("topic_b 不能为空")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "topic_a": self.topic_a,
            "topic_b": self.topic_b,
            "found": self.found,
            "summary": self.summary,
            "implementation_level": self.implementation_level,
            "limitation_notes": list(self.limitation_notes),
            "shared_tags": list(self.shared_tags),
            "only_a_tags": list(self.only_a_tags),
            "only_b_tags": list(self.only_b_tags),
            "overlap_knowledge_ids": list(self.overlap_knowledge_ids),
            "topic_a_candidates": [
                item.to_dict() for item in self.topic_a_candidates
            ],
            "topic_b_candidates": [
                item.to_dict() for item in self.topic_b_candidates
            ],
        }


def normalize_relation_types(
    relation_types: Optional[Iterable[RelationType | str]],
) -> list[str]:
    """将关系类型列表转换为字符串值。"""
    if relation_types is None:
        return []
    return [_normalize_enum(RelationType, item).value for item in relation_types]


def normalize_relation_source_types(
    relation_source_types: Optional[Iterable[RelationSourceType | str]],
) -> list[str]:
    """将关系来源类型列表转换为字符串值。"""
    if relation_source_types is None:
        return []
    return [
        _normalize_enum(RelationSourceType, item).value
        for item in relation_source_types
    ]
