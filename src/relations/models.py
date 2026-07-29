"""
关系层核心数据模型。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Iterable, Optional


PHASE_B_SCHEMA_VERSION = "phase_b.v1"
PHASE_B_BASELINE_IMPLEMENTATION_LEVEL = "baseline"
CHUNK_RETRIEVAL_STATUSES = frozenset(
    {
        "not_requested",
        "success",
        "no_hits",
        "path_unavailable",
        "search_error",
    }
)
TIMELINE_INFERRED_TIME_MIXED = "mixed"


def _clamp_score(value: float) -> float:
    return round(min(max(float(value), 0.0), 1.0), 4)


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
    schema_version: str = PHASE_B_SCHEMA_VERSION
    implementation_level: str = PHASE_B_BASELINE_IMPLEMENTATION_LEVEL
    limitation_notes: list[str] = field(default_factory=list)

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

    @property
    def evidence_count(self) -> int:
        return self.total_edges

    @property
    def confidence(self) -> float:
        if self.total_edges <= 0:
            return 0.0
        base_score = 0.8
        if self.truncated:
            base_score -= 0.2
        return _clamp_score(base_score)

    @property
    def coverage(self) -> float:
        expected_depth_span = max(self.max_depth + 1, 1)
        return _clamp_score(self.total_nodes / expected_depth_span)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "implementation_level": self.implementation_level,
            "limitation_notes": list(self.limitation_notes),
            "seed_knowledge_id": self.seed_knowledge_id,
            "max_depth": self.max_depth,
            "total_nodes": self.total_nodes,
            "total_edges": self.total_edges,
            "evidence_count": self.evidence_count,
            "confidence": self.confidence,
            "coverage": self.coverage,
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
    schema_version: str = PHASE_B_SCHEMA_VERSION
    implementation_level: str = PHASE_B_BASELINE_IMPLEMENTATION_LEVEL
    limitation_notes: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.source_knowledge_id <= 0 or self.target_knowledge_id <= 0:
            raise ValueError("knowledge_id 必须为正整数")
        if self.source_knowledge_id == self.target_knowledge_id:
            raise ValueError("source_knowledge_id 与 target_knowledge_id 不能相同")
        if self.hops < 0:
            raise ValueError("hops 不能为负数")

    @property
    def evidence_count(self) -> int:
        return max(len(self.evidence_items), len(self.path), len(self.supporting_relations))

    @property
    def confidence(self) -> float:
        if not self.found:
            return 0.0
        if self.hops <= 1:
            return 0.9
        if self.hops == 2:
            return 0.75
        return 0.6

    @property
    def coverage(self) -> float:
        if not self.found:
            return 0.0
        if self.path:
            return 1.0
        if self.supporting_relations:
            return 0.75
        return 0.5

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "implementation_level": self.implementation_level,
            "limitation_notes": list(self.limitation_notes),
            "source_knowledge_id": self.source_knowledge_id,
            "target_knowledge_id": self.target_knowledge_id,
            "found": self.found,
            "explanation_type": self.explanation_type,
            "hops": self.hops,
            "evidence_count": self.evidence_count,
            "confidence": self.confidence,
            "coverage": self.coverage,
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
    chunk_id: Optional[int] = None
    chunk_index: Optional[int] = None
    chunk_text: str = ""
    retrieval_rank: int = 1
    retrieval_score: float = 0.0
    ranking_score: float = 0.0
    coverage_score: float = 0.0
    freshness_score: float = 0.0
    relation_score: float = 0.0
    is_seed: bool = False
    relation_found: bool = False
    relation_explanation_type: str = ""
    relation_hops: int = 0
    relation_summary: str = ""
    relation_path: list[RelationRecord] = field(default_factory=list)
    relation_evidence_items: list[Dict[str, Any]] = field(default_factory=list)
    citation_source: str = ""
    citation_locator: str = ""

    def __post_init__(self) -> None:
        if self.knowledge_id <= 0:
            raise ValueError("knowledge_id 必须为正整数")
        if self.retrieval_rank <= 0:
            raise ValueError("retrieval_rank 必须为正整数")
        if not (0.0 <= self.retrieval_score <= 1.0):
            raise ValueError("retrieval_score 必须在 [0.0, 1.0] 范围内")
        if not (0.0 <= self.ranking_score <= 1.0):
            raise ValueError("ranking_score 必须在 [0.0, 1.0] 范围内")
        if not (0.0 <= self.coverage_score <= 1.0):
            raise ValueError("coverage_score 必须在 [0.0, 1.0] 范围内")
        if not (0.0 <= self.freshness_score <= 1.0):
            raise ValueError("freshness_score 必须在 [0.0, 1.0] 范围内")
        if not (0.0 <= self.relation_score <= 1.0):
            raise ValueError("relation_score 必须在 [0.0, 1.0] 范围内")
        if self.relation_hops < 0:
            raise ValueError("relation_hops 不能为负数")

    def to_dict(self) -> Dict[str, Any]:
        from src.relations.citations import (
            resolve_citation_source,
            sanitize_public_evidence,
            sanitize_public_source_url,
            serialize_relation_evidence,
        )

        public_source_url = sanitize_public_source_url(self.source_url)
        return {
            "knowledge_id": self.knowledge_id,
            "title": self.title,
            "abstract": self.abstract,
            "source_type": self.source_type,
            "archived_at": self.archived_at,
            "tags": list(self.tags),
            "source_url": public_source_url,
            "citation_source": resolve_citation_source(
                self.knowledge_id,
                source_url=self.citation_source or public_source_url,
            ),
            "citation_locator": self.citation_locator,
            "content_preview": self.content_preview,
            "chunk_id": self.chunk_id,
            "chunk_index": self.chunk_index,
            "chunk_text": self.chunk_text,
            "retrieval_rank": self.retrieval_rank,
            "retrieval_score": self.retrieval_score,
            "ranking_score": self.ranking_score,
            "coverage_score": self.coverage_score,
            "freshness_score": self.freshness_score,
            "relation_score": self.relation_score,
            "is_seed": self.is_seed,
            "relation_found": self.relation_found,
            "relation_explanation_type": self.relation_explanation_type,
            "relation_hops": self.relation_hops,
            "relation_summary": self.relation_summary,
            "relation_path": [
                serialize_relation_evidence(record) for record in self.relation_path
            ],
            "relation_evidence_items": [
                sanitize_public_evidence(dict(item))
                for item in self.relation_evidence_items
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
    schema_version: str = PHASE_B_SCHEMA_VERSION
    implementation_level: str = PHASE_B_BASELINE_IMPLEMENTATION_LEVEL
    limitation_notes: list[str] = field(default_factory=list)
    chunk_retrieval_status: str = "not_requested"

    def __post_init__(self) -> None:
        if not self.question or not self.question.strip():
            raise ValueError("question 不能为空")
        if self.seed_knowledge_id is not None and self.seed_knowledge_id <= 0:
            raise ValueError("seed_knowledge_id 必须为正整数")
        if self.chunk_retrieval_status not in CHUNK_RETRIEVAL_STATUSES:
            raise ValueError(
                "chunk_retrieval_status 必须是 "
                + ", ".join(sorted(CHUNK_RETRIEVAL_STATUSES))
            )

    @property
    def total_evidence(self) -> int:
        return len(self.evidence)

    @property
    def related_evidence_count(self) -> int:
        return sum(1 for item in self.evidence if item.relation_found)

    @property
    def evidence_count(self) -> int:
        return self.total_evidence

    @property
    def confidence(self) -> float:
        if not self.evidence:
            return 0.0
        scores = [
            item.ranking_score if item.ranking_score > 0 else item.retrieval_score
            for item in self.evidence
        ]
        return _clamp_score(sum(scores) / len(scores))

    @property
    def coverage(self) -> float:
        if not self.evidence:
            return 0.0
        avg_coverage = sum(item.coverage_score for item in self.evidence) / len(self.evidence)
        relation_ratio = self.related_evidence_count / len(self.evidence)
        return _clamp_score(max(avg_coverage, relation_ratio))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "implementation_level": self.implementation_level,
            "limitation_notes": list(self.limitation_notes),
            "chunk_retrieval_status": self.chunk_retrieval_status,
            "question": self.question,
            "found": self.found,
            "seed_knowledge_id": self.seed_knowledge_id,
            "seed_title": self.seed_title,
            "total_evidence": self.total_evidence,
            "related_evidence_count": self.related_evidence_count,
            "evidence_count": self.evidence_count,
            "confidence": self.confidence,
            "coverage": self.coverage,
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
    structural_bridge_score: float = 0.0
    graph_bridge_score: float = 0.0
    semantic_bridge_score: float = 0.0
    connected_knowledge_ids: list[int] = field(default_factory=list)
    relation_types: list[str] = field(default_factory=list)
    summary: str = ""
    evidence_path: list[Dict[str, Any]] = field(default_factory=list)
    supporting_subgraph: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.knowledge_id <= 0:
            raise ValueError("knowledge_id 必须为正整数")
        if self.depth < 0:
            raise ValueError("depth 不能为负数")
        if self.bridge_score < 0:
            raise ValueError("bridge_score 不能为负数")
        if not (0.0 <= self.structural_bridge_score <= 1.0):
            raise ValueError("structural_bridge_score 必须在 [0.0, 1.0] 范围内")
        if not (0.0 <= self.graph_bridge_score <= 1.0):
            raise ValueError("graph_bridge_score 必须在 [0.0, 1.0] 范围内")
        if not (0.0 <= self.semantic_bridge_score <= 1.0):
            raise ValueError("semantic_bridge_score 必须在 [0.0, 1.0] 范围内")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "knowledge_id": self.knowledge_id,
            "title": self.title,
            "depth": self.depth,
            "bridge_score": self.bridge_score,
            "structural_bridge_score": self.structural_bridge_score,
            "graph_bridge_score": self.graph_bridge_score,
            "semantic_bridge_score": self.semantic_bridge_score,
            "connected_knowledge_ids": list(self.connected_knowledge_ids),
            "relation_types": list(self.relation_types),
            "evidence_path": [dict(item) for item in self.evidence_path],
            "supporting_subgraph": dict(self.supporting_subgraph),
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
    schema_version: str = PHASE_B_SCHEMA_VERSION
    implementation_level: str = "partial"
    evidence_sources: list[str] = field(default_factory=list)
    limitation_notes: list[str] = field(default_factory=list)
    subgraph_truncated: bool = False
    subgraph_max_nodes: int = 100
    subgraph_max_edges: int = 300
    subgraph_node_count: int = 0
    subgraph_edge_count: int = 0

    def __post_init__(self) -> None:
        if self.seed_knowledge_id <= 0:
            raise ValueError("seed_knowledge_id 必须为正整数")
        if self.max_depth <= 0:
            raise ValueError("max_depth 必须大于 0")

    @property
    def total_bridges(self) -> int:
        return len(self.items)

    @property
    def evidence_count(self) -> int:
        return self.total_bridges

    @property
    def confidence(self) -> float:
        if not self.items:
            return 0.0
        avg_score = sum(min(item.bridge_score, 3.0) / 3.0 for item in self.items) / len(self.items)
        return _clamp_score(avg_score * 0.8)

    @property
    def coverage(self) -> float:
        if not self.items:
            return 0.0
        return _clamp_score(self.total_bridges / max(self.max_depth, 1))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "seed_knowledge_id": self.seed_knowledge_id,
            "found": self.found,
            "max_depth": self.max_depth,
            "total_bridges": self.total_bridges,
            "evidence_count": self.evidence_count,
            "confidence": self.confidence,
            "coverage": self.coverage,
            "summary": self.summary,
            "implementation_level": self.implementation_level,
            "evidence_sources": list(self.evidence_sources),
            "limitation_notes": list(self.limitation_notes),
            "subgraph_truncated": self.subgraph_truncated,
            "subgraph_max_nodes": self.subgraph_max_nodes,
            "subgraph_max_edges": self.subgraph_max_edges,
            "subgraph_node_count": self.subgraph_node_count,
            "subgraph_edge_count": self.subgraph_edge_count,
            "items": [item.to_dict() for item in self.items],
        }


@dataclass
class TimelinePoint:
    """时间线条目。"""

    knowledge_id: int
    title: str
    time_value: str = ""
    event_time: str = ""
    published_at: str = ""
    archived_at: str = ""
    time_source: str = "unavailable"
    time_source_field: str = ""
    time_precision: str = "unavailable"
    source_type: str = ""
    abstract: str = ""
    tags: list[str] = field(default_factory=list)
    retrieval_score: float = 0.0
    source_url: str = ""
    file_path: str = ""
    source: str = ""
    citation_locator: str = ""

    def __post_init__(self) -> None:
        if self.knowledge_id <= 0:
            raise ValueError("knowledge_id 必须为正整数")
        if not (0.0 <= self.retrieval_score <= 1.0):
            raise ValueError("retrieval_score 必须在 [0.0, 1.0] 范围内")

    def to_dict(self) -> Dict[str, Any]:
        from src.relations.citations import (
            resolve_citation_source,
            sanitize_public_source_url,
        )

        public_source_url = sanitize_public_source_url(self.source_url)
        return {
            "knowledge_id": self.knowledge_id,
            "title": self.title,
            "time_value": self.time_value,
            "event_time": self.event_time,
            "published_at": self.published_at,
            "archived_at": self.archived_at,
            "time_source": self.time_source,
            "time_source_field": self.time_source_field,
            "time_precision": self.time_precision,
            "source_type": self.source_type,
            "source_url": public_source_url,
            "source": resolve_citation_source(
                self.knowledge_id,
                source_url=self.source or public_source_url,
            ),
            "citation_locator": self.citation_locator,
            "abstract": self.abstract,
            "tags": list(self.tags),
            "retrieval_score": self.retrieval_score,
        }


@dataclass
class TimelineResult:
    """时间线重建结果。

    `inferred_time_field` 表示当前时间线整体最可代表的时间来源。
    当多种时间源并列主导、无法安全归因为单一字段时，允许返回 `mixed`。
    """

    topic: str
    found: bool
    inferred_time_field: str = "unavailable"
    time_source_priority: list[str] = field(default_factory=list)
    items: list[TimelinePoint] = field(default_factory=list)
    summary: str = ""
    schema_version: str = PHASE_B_SCHEMA_VERSION
    implementation_level: str = "partial"
    evidence_sources: list[str] = field(default_factory=list)
    limitation_notes: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.topic or not self.topic.strip():
            raise ValueError("topic 不能为空")

    @property
    def total_points(self) -> int:
        return len(self.items)

    @property
    def evidence_count(self) -> int:
        return self.total_points

    @property
    def confidence(self) -> float:
        if not self.items:
            return 0.0
        avg_score = sum(item.retrieval_score for item in self.items) / len(self.items)
        return _clamp_score(avg_score * 0.85)

    @property
    def coverage(self) -> float:
        if not self.items:
            return 0.0
        return _clamp_score(self.total_points / 5.0)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "topic": self.topic,
            "found": self.found,
            "inferred_time_field": self.inferred_time_field,
            "time_source_priority": list(self.time_source_priority),
            "total_points": self.total_points,
            "evidence_count": self.evidence_count,
            "confidence": self.confidence,
            "coverage": self.coverage,
            "summary": self.summary,
            "implementation_level": self.implementation_level,
            "evidence_sources": list(self.evidence_sources),
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
    relation_signal_score: float = 0.0
    relation_types: list[str] = field(default_factory=list)
    source_url: str = ""
    file_path: str = ""
    source: str = ""
    citation_locator: str = ""

    def __post_init__(self) -> None:
        if self.knowledge_id <= 0:
            raise ValueError("knowledge_id 必须为正整数")
        if not (0.0 <= self.retrieval_score <= 1.0):
            raise ValueError("retrieval_score 必须在 [0.0, 1.0] 范围内")
        if not (0.0 <= self.relation_signal_score <= 1.0):
            raise ValueError("relation_signal_score 必须在 [0.0, 1.0] 范围内")

    def to_dict(self) -> Dict[str, Any]:
        from src.relations.citations import (
            resolve_citation_source,
            sanitize_public_source_url,
        )

        public_source_url = sanitize_public_source_url(self.source_url)
        return {
            "knowledge_id": self.knowledge_id,
            "title": self.title,
            "abstract": self.abstract,
            "archived_at": self.archived_at,
            "source_type": self.source_type,
            "source_url": public_source_url,
            "source": resolve_citation_source(
                self.knowledge_id,
                source_url=self.source or public_source_url,
            ),
            "citation_locator": self.citation_locator,
            "tags": list(self.tags),
            "retrieval_score": self.retrieval_score,
            "relation_signal_score": self.relation_signal_score,
            "relation_types": list(self.relation_types),
        }


@dataclass
class ContrastResult:
    """主题对比结果。"""

    topic_a: str
    topic_b: str
    found: bool
    topic_a_candidates: list[ContrastCandidateItem] = field(default_factory=list)
    topic_b_candidates: list[ContrastCandidateItem] = field(default_factory=list)
    comparison_dimensions: Dict[str, Any] = field(default_factory=dict)
    shared_tags: list[str] = field(default_factory=list)
    only_a_tags: list[str] = field(default_factory=list)
    only_b_tags: list[str] = field(default_factory=list)
    overlap_knowledge_ids: list[int] = field(default_factory=list)
    summary: str = ""
    schema_version: str = PHASE_B_SCHEMA_VERSION
    implementation_level: str = "partial"
    evidence_sources: list[str] = field(default_factory=list)
    limitation_notes: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.topic_a or not self.topic_a.strip():
            raise ValueError("topic_a 不能为空")
        if not self.topic_b or not self.topic_b.strip():
            raise ValueError("topic_b 不能为空")

    @property
    def evidence_count(self) -> int:
        return len(self.topic_a_candidates) + len(self.topic_b_candidates)

    @property
    def confidence(self) -> float:
        candidates = self.topic_a_candidates + self.topic_b_candidates
        if not candidates:
            return 0.0
        avg_score = sum(item.retrieval_score for item in candidates) / len(candidates)
        return _clamp_score(avg_score * 0.8)

    @property
    def coverage(self) -> float:
        signal_count = len(self.shared_tags) + len(self.only_a_tags) + len(self.only_b_tags)
        if signal_count <= 0:
            return 0.0
        return _clamp_score(signal_count / 4.0)

    def to_dict(self) -> Dict[str, Any]:
        from src.relations.citations import sanitize_public_evidence

        return {
            "schema_version": self.schema_version,
            "topic_a": self.topic_a,
            "topic_b": self.topic_b,
            "found": self.found,
            "evidence_count": self.evidence_count,
            "confidence": self.confidence,
            "coverage": self.coverage,
            "summary": self.summary,
            "implementation_level": self.implementation_level,
            "evidence_sources": list(self.evidence_sources),
            "limitation_notes": list(self.limitation_notes),
            "comparison_dimensions": sanitize_public_evidence(
                dict(self.comparison_dimensions)
            ),
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
