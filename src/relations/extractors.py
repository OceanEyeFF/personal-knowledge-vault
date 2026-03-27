"""
关系提取与回填工具。
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional
from urllib.parse import unquote, urlparse

import yaml

from src.relations.models import RelationRecord, RelationSourceType, RelationType
from src.storage.relation_store import RelationStore
from src.utils.logger import get_logger

logger = get_logger(__name__)

MARKDOWN_LINK_PATTERN = re.compile(r"(?<!!)\[([^\]]+)\]\(([^)]+)\)")
EXTRACTION_SOURCE_TYPES: tuple[RelationSourceType, ...] = (
    RelationSourceType.MARKDOWN_LINK,
    RelationSourceType.FRONTMATTER_RELATED_DOCS,
)


@dataclass(frozen=True)
class KnowledgeEntryRef:
    """用于回填的知识条目索引。"""

    knowledge_id: int
    file_path: Path


@dataclass(frozen=True)
class ExtractedReference:
    """提取出的原始引用。"""

    relation_type: RelationType
    relation_source_type: RelationSourceType
    raw_target: str
    evidence_payload: Dict[str, object]


@dataclass(frozen=True)
class ReferenceIssue:
    """未能形成有效关系的引用记录。"""

    relation_type: RelationType
    relation_source_type: RelationSourceType
    raw_target: str
    reason: str
    detail: Dict[str, object] = field(default_factory=dict)


@dataclass
class BackfillReport:
    """回填执行结果与质量统计。

    质量指标定义：
    - total_references：可识别的原始引用总数（包含有效与无效）。
    - resolved_references：成功解析到 knowledge_id 的引用数。
    - invalid_references：外链、锚点、空引用、自引用等无效引用数。
    - unresolved_references：格式合法但目标不存在的引用数。
    - conflicted_relations：与更高优先级来源冲突的解析关系数。
    - coverage_rate = resolved_references / total_references
    - noise_rate = (invalid_references + unresolved_references) / total_references
    - conflict_rate = conflicted_relations / resolved_references
    """

    scanned_entries: int = 0
    processed_entries: int = 0
    extracted_relations: int = 0
    applied_relations: int = 0
    deleted_relations: int = 0
    total_references: int = 0
    resolved_references: int = 0
    invalid_references: int = 0
    unresolved_references: int = 0
    conflicted_relations: int = 0
    missing_files: List[str] = field(default_factory=list)
    skipped_references: List[Dict[str, object]] = field(default_factory=list)
    limitation_notes: List[str] = field(default_factory=list)
    by_source_type: Dict[str, Dict[str, int]] = field(default_factory=dict)
    by_relation_type: Dict[str, Dict[str, int]] = field(default_factory=dict)
    extensions: Dict[str, object] = field(default_factory=dict)

    @property
    def schema_version(self) -> str:
        return "backfill_quality_report.v1"

    @property
    def coverage_rate(self) -> float:
        return _safe_rate(self.resolved_references, self.total_references)

    @property
    def noise_rate(self) -> float:
        return _safe_rate(
            self.invalid_references + self.unresolved_references,
            self.total_references,
        )

    @property
    def conflict_rate(self) -> float:
        return _safe_rate(self.conflicted_relations, self.resolved_references)

    def metric_definitions(self) -> Dict[str, str]:
        """返回指标口径，便于人和自动化理解。"""
        return {
            "total_references": "可识别的原始引用总数（包含有效与无效）。",
            "resolved_references": "成功解析到 knowledge_id 的引用数。",
            "invalid_references": "外链、锚点、空引用、自引用等无效引用数。",
            "unresolved_references": "格式合法但目标不存在的引用数。",
            "conflicted_relations": "与更高优先级来源冲突的解析关系数。",
            "coverage_rate": "resolved_references / total_references。",
            "noise_rate": "(invalid_references + unresolved_references) / total_references。",
            "conflict_rate": "conflicted_relations / resolved_references。",
        }

    def register_reference(
        self,
        relation_source_type: RelationSourceType,
        relation_type: RelationType,
        outcome: str,
    ) -> None:
        """记录单条引用结果。

        outcome 取值：resolved / invalid / unresolved
        """
        self.total_references += 1
        if outcome == "resolved":
            self.resolved_references += 1
        elif outcome == "invalid":
            self.invalid_references += 1
        elif outcome == "unresolved":
            self.unresolved_references += 1
        else:
            raise ValueError(f"未知 outcome: {outcome}")

        self._increment_bucket(
            self.by_source_type,
            relation_source_type.value,
            outcome,
        )
        self._increment_bucket(
            self.by_relation_type,
            relation_type.value,
            outcome,
        )

    @staticmethod
    def _increment_bucket(
        bucket: Dict[str, Dict[str, int]],
        key: str,
        outcome: str,
    ) -> None:
        if key not in bucket:
            bucket[key] = {
                "total": 0,
                "resolved": 0,
                "invalid": 0,
                "unresolved": 0,
            }
        bucket[key]["total"] += 1
        bucket[key][outcome] += 1

    def to_dict(self, include_definitions: bool = True) -> Dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "scanned_entries": self.scanned_entries,
            "processed_entries": self.processed_entries,
            "extracted_relations": self.extracted_relations,
            "applied_relations": self.applied_relations,
            "deleted_relations": self.deleted_relations,
            "total_references": self.total_references,
            "resolved_references": self.resolved_references,
            "invalid_references": self.invalid_references,
            "unresolved_references": self.unresolved_references,
            "conflicted_relations": self.conflicted_relations,
            "coverage_rate": self.coverage_rate,
            "noise_rate": self.noise_rate,
            "conflict_rate": self.conflict_rate,
            "missing_files": list(self.missing_files),
            "skipped_references": list(self.skipped_references),
            "limitation_notes": list(self.limitation_notes),
            "by_source_type": dict(self.by_source_type),
            "by_relation_type": dict(self.by_relation_type),
            "extensions": dict(self.extensions),
            "metric_definitions": self.metric_definitions() if include_definitions else {},
        }

    def to_markdown(self) -> str:
        """生成用于归档的 Markdown 质量报告。"""
        lines = [
            "# 关系回填质量报告",
            "",
            f"- schema_version: {self.schema_version}",
            f"- scanned_entries: {self.scanned_entries}",
            f"- processed_entries: {self.processed_entries}",
            f"- extracted_relations: {self.extracted_relations}",
            f"- applied_relations: {self.applied_relations}",
            f"- deleted_relations: {self.deleted_relations}",
            "",
            "## 质量指标",
            f"- total_references: {self.total_references}",
            f"- resolved_references: {self.resolved_references}",
            f"- invalid_references: {self.invalid_references}",
            f"- unresolved_references: {self.unresolved_references}",
            f"- conflicted_relations: {self.conflicted_relations}",
            f"- coverage_rate: {self.coverage_rate:.4f}",
            f"- noise_rate: {self.noise_rate:.4f}",
            f"- conflict_rate: {self.conflict_rate:.4f}",
        ]

        if self.by_source_type:
            lines.extend(["", "## 按来源统计"])
            for key, stats in self.by_source_type.items():
                lines.append(
                    f"- {key}: total={stats['total']}, "
                    f"resolved={stats['resolved']}, "
                    f"invalid={stats['invalid']}, "
                    f"unresolved={stats['unresolved']}"
                )

        if self.by_relation_type:
            lines.extend(["", "## 按关系类型统计"])
            for key, stats in self.by_relation_type.items():
                lines.append(
                    f"- {key}: total={stats['total']}, "
                    f"resolved={stats['resolved']}, "
                    f"invalid={stats['invalid']}, "
                    f"unresolved={stats['unresolved']}"
                )

        if self.limitation_notes:
            lines.extend(["", "## 限制说明"])
            for note in self.limitation_notes:
                lines.append(f"- {note}")

        lines.extend(["", "## 指标口径"])
        for key, desc in self.metric_definitions().items():
            lines.append(f"- {key}: {desc}")

        return "\n".join(lines)


def parse_front_matter(markdown_text: str) -> tuple[Dict[str, object], str]:
    """解析 YAML Front Matter。

    仅处理仓库当前使用的标准 `---` front matter 形式。
    """
    if not markdown_text.startswith("---"):
        return {}, markdown_text

    lines = markdown_text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, markdown_text

    end_index = None
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            end_index = idx
            break

    if end_index is None:
        return {}, markdown_text

    front_matter_raw = "\n".join(lines[1:end_index])
    body = "\n".join(lines[end_index + 1 :])
    metadata = yaml.safe_load(front_matter_raw) or {}
    if not isinstance(metadata, dict):
        return {}, body
    return metadata, body


def extract_markdown_link_references(
    markdown_text: str,
) -> tuple[List[ExtractedReference], List[ReferenceIssue]]:
    """提取正文中的 Markdown 显式链接。"""
    _, body = parse_front_matter(markdown_text)
    extracted: List[ExtractedReference] = []
    issues: List[ReferenceIssue] = []

    for anchor_text, raw_target in MARKDOWN_LINK_PATTERN.findall(body):
        cleaned_target, reason = _normalize_link_target(raw_target)
        if cleaned_target is None:
            issues.append(
                ReferenceIssue(
                    relation_type=RelationType.REFERENCES,
                    relation_source_type=RelationSourceType.MARKDOWN_LINK,
                    raw_target=raw_target,
                    reason=reason or "invalid_target",
                    detail={
                        "anchor_text": anchor_text.strip(),
                    },
                )
            )
            continue

        extracted.append(
            ExtractedReference(
                relation_type=RelationType.REFERENCES,
                relation_source_type=RelationSourceType.MARKDOWN_LINK,
                raw_target=cleaned_target,
                evidence_payload={
                    "raw_target": raw_target.strip(),
                    "normalized_target": cleaned_target,
                    "anchor_text": anchor_text.strip(),
                },
            )
        )

    return extracted, issues


def extract_frontmatter_related_docs(
    markdown_text: str,
) -> tuple[List[ExtractedReference], List[ReferenceIssue]]:
    """提取 front matter 中的 related_docs。"""
    metadata, _ = parse_front_matter(markdown_text)
    related_docs = metadata.get("related_docs") or []
    if not isinstance(related_docs, list):
        return [], []

    extracted: List[ExtractedReference] = []
    issues: List[ReferenceIssue] = []
    for raw_target in related_docs:
        if not isinstance(raw_target, str) or not raw_target.strip():
            issues.append(
                ReferenceIssue(
                    relation_type=RelationType.RELATED_DOCUMENT,
                    relation_source_type=RelationSourceType.FRONTMATTER_RELATED_DOCS,
                    raw_target=str(raw_target),
                    reason="invalid_target",
                )
            )
            continue
        extracted.append(
            ExtractedReference(
                relation_type=RelationType.RELATED_DOCUMENT,
                relation_source_type=RelationSourceType.FRONTMATTER_RELATED_DOCS,
                raw_target=raw_target.strip(),
                evidence_payload={
                    "field": "related_docs",
                    "normalized_target": raw_target.strip(),
                },
            )
        )
    return extracted, issues


class RelationBackfillService:
    """基于 Markdown + SQLite 的第一版关系回填服务。"""

    def __init__(self, db_path: Path, vault_dir: Path):
        self.db_path = Path(db_path)
        self.vault_dir = Path(vault_dir)
        self.relation_store = RelationStore(self.db_path)

    def backfill(
        self,
        knowledge_ids: Optional[Iterable[int]] = None,
        apply: bool = False,
    ) -> BackfillReport:
        """执行关系回填。

        默认 dry-run；仅当 `apply=True` 时写入关系表。
        """
        if apply and not self.relation_store.table_exists():
            raise RuntimeError(
                "knowledge_relations 表不存在，请先显式执行关系 migration 再回填。"
            )

        all_entries = self._load_entries()
        entries = self._filter_entries(all_entries, knowledge_ids=knowledge_ids)
        entry_maps = self._build_entry_path_maps(all_entries)
        report = BackfillReport(scanned_entries=len(entries))
        can_check_conflicts = self.relation_store.table_exists()
        if not can_check_conflicts:
            report.limitation_notes.append("关系表不存在，未执行冲突检测。")

        for entry in entries:
            if not entry.file_path.exists():
                report.missing_files.append(str(entry.file_path))
                continue

            markdown_text = entry.file_path.read_text(encoding="utf-8")
            markdown_refs, markdown_issues = extract_markdown_link_references(
                markdown_text
            )
            frontmatter_refs, frontmatter_issues = extract_frontmatter_related_docs(
                markdown_text
            )
            raw_refs = markdown_refs + frontmatter_refs
            all_issues = markdown_issues + frontmatter_issues
            report.processed_entries += 1

            relations: List[RelationRecord] = []
            for issue in all_issues:
                report.register_reference(
                    issue.relation_source_type, issue.relation_type, "invalid"
                )
                report.skipped_references.append(
                    {
                        "source_knowledge_id": entry.knowledge_id,
                        "source_file_path": str(entry.file_path),
                        "raw_target": issue.raw_target,
                        "relation_type": issue.relation_type.value,
                        "reason": issue.reason,
                    }
                )

            for raw_ref in raw_refs:
                target_entry = self._resolve_target_entry(
                    raw_ref.raw_target,
                    source_file_path=entry.file_path,
                    entry_maps=entry_maps,
                )
                if target_entry is None:
                    report.register_reference(
                        raw_ref.relation_source_type,
                        raw_ref.relation_type,
                        "unresolved",
                    )
                    report.skipped_references.append(
                        {
                            "source_knowledge_id": entry.knowledge_id,
                            "source_file_path": str(entry.file_path),
                            "raw_target": raw_ref.raw_target,
                            "relation_type": raw_ref.relation_type.value,
                            "reason": "target_not_found",
                        }
                    )
                    continue
                if target_entry.knowledge_id == entry.knowledge_id:
                    report.register_reference(
                        raw_ref.relation_source_type,
                        raw_ref.relation_type,
                        "invalid",
                    )
                    report.skipped_references.append(
                        {
                            "source_knowledge_id": entry.knowledge_id,
                            "source_file_path": str(entry.file_path),
                            "raw_target": raw_ref.raw_target,
                            "relation_type": raw_ref.relation_type.value,
                            "reason": "self_reference",
                        }
                    )
                    continue

                report.register_reference(
                    raw_ref.relation_source_type,
                    raw_ref.relation_type,
                    "resolved",
                )

                if can_check_conflicts and self._is_conflicted_relation(
                    entry.knowledge_id,
                    target_entry.knowledge_id,
                    raw_ref.relation_source_type,
                ):
                    report.conflicted_relations += 1

                relations.append(
                    RelationRecord(
                        source_knowledge_id=entry.knowledge_id,
                        target_knowledge_id=target_entry.knowledge_id,
                        relation_type=raw_ref.relation_type,
                        relation_source_type=raw_ref.relation_source_type,
                        evidence_payload={
                            **raw_ref.evidence_payload,
                            "target_file_path": str(target_entry.file_path),
                        },
                    )
                )

            report.extracted_relations += len(relations)

            if apply:
                report.deleted_relations += self.relation_store.delete_outgoing_relations_for_knowledge(
                    entry.knowledge_id,
                    relation_source_types=[
                        source_type.value for source_type in EXTRACTION_SOURCE_TYPES
                    ],
                )
                for relation in relations:
                    self.relation_store.upsert_relation(relation)
                report.applied_relations += len(relations)

        return report

    def _load_entries(
        self, knowledge_ids: Optional[Iterable[int]] = None
    ) -> List[KnowledgeEntryRef]:
        query = "SELECT knowledge_id, file_path FROM knowledge_items"
        params: list[object] = []

        normalized_ids = [int(item) for item in knowledge_ids or []]
        if normalized_ids:
            placeholders = ", ".join("?" for _ in normalized_ids)
            query += f" WHERE knowledge_id IN ({placeholders})"
            params.extend(normalized_ids)

        query += " ORDER BY knowledge_id ASC"

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(query, tuple(params)).fetchall()
        conn.close()

        return [
            KnowledgeEntryRef(
                knowledge_id=int(row["knowledge_id"]),
                file_path=self._normalize_entry_file_path(row["file_path"]),
            )
            for row in rows
        ]

    @staticmethod
    def _filter_entries(
        entries: Iterable[KnowledgeEntryRef],
        knowledge_ids: Optional[Iterable[int]] = None,
    ) -> List[KnowledgeEntryRef]:
        normalized_ids = {int(item) for item in knowledge_ids or []}
        if not normalized_ids:
            return list(entries)
        return [entry for entry in entries if entry.knowledge_id in normalized_ids]

    def _normalize_entry_file_path(self, raw_path: str) -> Path:
        path = Path(raw_path)
        if path.is_absolute():
            return path.resolve()

        vault_candidate = (self.vault_dir / path).resolve()
        if vault_candidate.exists():
            return vault_candidate
        return path.resolve()

    def _build_entry_path_maps(
        self, entries: Iterable[KnowledgeEntryRef]
    ) -> Dict[str, Dict[str, KnowledgeEntryRef]]:
        absolute_map: Dict[str, KnowledgeEntryRef] = {}
        vault_relative_map: Dict[str, KnowledgeEntryRef] = {}
        filename_map: Dict[str, KnowledgeEntryRef] = {}

        for entry in entries:
            normalized_abs = self._normalized_key(entry.file_path)
            absolute_map[normalized_abs] = entry

            try:
                rel_path = entry.file_path.relative_to(self.vault_dir)
                vault_relative_map[self._normalized_key(rel_path)] = entry
            except ValueError:
                pass

            filename_map.setdefault(entry.file_path.name.lower(), entry)

        return {
            "absolute": absolute_map,
            "vault_relative": vault_relative_map,
            "filename": filename_map,
        }

    def _resolve_target_entry(
        self,
        raw_target: str,
        source_file_path: Path,
        entry_maps: Dict[str, Dict[str, KnowledgeEntryRef]],
    ) -> Optional[KnowledgeEntryRef]:
        for candidate in self._candidate_target_paths(raw_target, source_file_path):
            candidate_key = self._normalized_key(candidate)

            if candidate.is_absolute():
                absolute_match = entry_maps["absolute"].get(candidate_key)
                if absolute_match is not None:
                    return absolute_match

                try:
                    rel_candidate = candidate.relative_to(self.vault_dir)
                    rel_match = entry_maps["vault_relative"].get(
                        self._normalized_key(rel_candidate)
                    )
                    if rel_match is not None:
                        return rel_match
                except ValueError:
                    pass
            else:
                rel_match = entry_maps["vault_relative"].get(candidate_key)
                if rel_match is not None:
                    return rel_match

            filename_match = entry_maps["filename"].get(candidate.name.lower())
            if filename_match is not None:
                return filename_match

        return None

    def _candidate_target_paths(
        self, raw_target: str, source_file_path: Path
    ) -> List[Path]:
        target = raw_target.strip()
        if not target:
            return []

        target_path = Path(target)
        candidates: List[Path] = []
        if target_path.is_absolute():
            candidates.append(target_path.resolve())
        else:
            candidates.append((source_file_path.parent / target_path).resolve())
            candidates.append((self.vault_dir / target_path).resolve())
            candidates.append(target_path)

        if target_path.suffix == "":
            candidates.extend(
                [Path(f"{str(candidate)}.md") for candidate in candidates if str(candidate)]
            )

        deduped: List[Path] = []
        seen = set()
        for candidate in candidates:
            key = self._normalized_key(candidate)
            if key in seen:
                continue
            deduped.append(candidate)
            seen.add(key)
        return deduped

    @staticmethod
    def _normalized_key(path: Path) -> str:
        return str(path).replace("\\", "/").lower()

    def _is_conflicted_relation(
        self,
        source_knowledge_id: int,
        target_knowledge_id: int,
        relation_source_type: RelationSourceType,
    ) -> bool:
        """判断是否与更高优先级来源冲突。"""
        priority = {
            RelationSourceType.MANUAL: 1,
            RelationSourceType.FRONTMATTER_FIELD: 2,
            RelationSourceType.MARKDOWN_LINK: 3,
            RelationSourceType.FRONTMATTER_RELATED_DOCS: 3,
            RelationSourceType.BACKFILL: 4,
        }
        incoming_priority = priority.get(relation_source_type, 99)
        existing = self.relation_store.list_relations_between(
            source_knowledge_id, target_knowledge_id
        )
        if not existing:
            return False
        best_existing = min(
            priority.get(item.relation_source_type, 99) for item in existing
        )
        return best_existing < incoming_priority


def _safe_rate(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return numerator / denominator


def _normalize_link_target(raw_target: str) -> tuple[Optional[str], Optional[str]]:
    target = raw_target.strip()
    if not target:
        return None, "invalid_target"

    if target.startswith("<") and ">" in target:
        target = target[1 : target.index(">")].strip()
    else:
        target = re.split(r"\s+", target, maxsplit=1)[0].strip()

    parsed = urlparse(target)
    if parsed.scheme in {"http", "https", "mailto"}:
        return None, "external_link"
    if target.startswith("#"):
        return None, "anchor_link"

    cleaned = unquote(parsed.path or target)
    if not cleaned or cleaned.startswith("#"):
        return None, "invalid_target"
    return cleaned, None
