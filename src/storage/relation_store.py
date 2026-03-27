"""
关系存储层。

本模块只负责关系数据的低层读写，不隐式创建或升级真实数据库 Schema。
调用前应先显式执行对应 migration。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, List, Optional

from src.relations.models import (
    RelationQueryDirection,
    RelationRecord,
    normalize_relation_source_types,
    normalize_relation_types,
)
from src.utils.logger import get_logger

logger = get_logger(__name__)


class RelationStore:
    """关系表读写封装。"""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def get_connection(self):
        """获取数据库连接。"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        except Exception as e:
            conn.rollback()
            logger.error(f"关系数据库操作失败: {e}")
            raise
        finally:
            conn.close()

    def table_exists(self) -> bool:
        """检查关系表是否存在。"""
        with self.get_connection() as conn:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='knowledge_relations'"
            ).fetchone()
            return row is not None

    def upsert_relation(self, relation: RelationRecord) -> int:
        """插入或更新关系记录。"""
        with self.get_connection() as conn:
            conn.execute(
                """
                INSERT INTO knowledge_relations (
                    source_knowledge_id,
                    target_knowledge_id,
                    relation_type,
                    relation_source_type,
                    direction,
                    weight,
                    evidence_payload
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (
                    source_knowledge_id,
                    target_knowledge_id,
                    relation_type,
                    relation_source_type
                ) DO UPDATE SET
                    direction = excluded.direction,
                    weight = excluded.weight,
                    evidence_payload = excluded.evidence_payload,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    relation.source_knowledge_id,
                    relation.target_knowledge_id,
                    relation.relation_type.value,
                    relation.relation_source_type.value,
                    relation.direction.value,
                    relation.weight,
                    relation.to_db_payload(),
                ),
            )

            row = conn.execute(
                """
                SELECT relation_id
                FROM knowledge_relations
                WHERE source_knowledge_id = ?
                  AND target_knowledge_id = ?
                  AND relation_type = ?
                  AND relation_source_type = ?
                """,
                (
                    relation.source_knowledge_id,
                    relation.target_knowledge_id,
                    relation.relation_type.value,
                    relation.relation_source_type.value,
                ),
            ).fetchone()

            if row is None:
                raise RuntimeError("关系 upsert 后未找到 relation_id")
            return int(row["relation_id"])

    def get_relation(self, relation_id: int) -> Optional[RelationRecord]:
        """按 ID 获取关系记录。"""
        with self.get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM knowledge_relations WHERE relation_id = ?",
                (relation_id,),
            ).fetchone()
            if row is None:
                return None
            return RelationRecord.from_row(dict(row))

    def list_relations_for_knowledge(
        self,
        knowledge_id: int,
        direction: RelationQueryDirection | str = RelationQueryDirection.BOTH,
        relation_types: Optional[Iterable[str]] = None,
        relation_source_types: Optional[Iterable[str]] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[RelationRecord]:
        """按 knowledge_id 查询关系。"""
        if knowledge_id <= 0:
            raise ValueError("knowledge_id 必须为正整数")

        query_direction = RelationQueryDirection(direction)
        type_values = normalize_relation_types(relation_types)
        source_type_values = normalize_relation_source_types(relation_source_types)

        query = "SELECT * FROM knowledge_relations"
        conditions = []
        params: list[object] = []

        if query_direction == RelationQueryDirection.OUTGOING:
            conditions.append("source_knowledge_id = ?")
            params.append(knowledge_id)
        elif query_direction == RelationQueryDirection.INCOMING:
            conditions.append("target_knowledge_id = ?")
            params.append(knowledge_id)
        else:
            conditions.append("(source_knowledge_id = ? OR target_knowledge_id = ?)")
            params.extend([knowledge_id, knowledge_id])

        if type_values:
            placeholders = ", ".join("?" for _ in type_values)
            conditions.append(f"relation_type IN ({placeholders})")
            params.extend(type_values)

        if source_type_values:
            placeholders = ", ".join("?" for _ in source_type_values)
            conditions.append(f"relation_source_type IN ({placeholders})")
            params.extend(source_type_values)

        query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY updated_at DESC, relation_id DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        with self.get_connection() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()
            return [RelationRecord.from_row(dict(row)) for row in rows]

    def list_relations_between(
        self,
        source_knowledge_id: int,
        target_knowledge_id: int,
    ) -> List[RelationRecord]:
        """按 source/target 精确查询关系，用于冲突检测。"""
        if source_knowledge_id <= 0 or target_knowledge_id <= 0:
            raise ValueError("knowledge_id 必须为正整数")

        query = """
            SELECT *
            FROM knowledge_relations
            WHERE source_knowledge_id = ?
              AND target_knowledge_id = ?
        """
        with self.get_connection() as conn:
            rows = conn.execute(
                query,
                (source_knowledge_id, target_knowledge_id),
            ).fetchall()
            return [RelationRecord.from_row(dict(row)) for row in rows]

    def delete_relations_by_source_type(self, relation_source_type: str) -> int:
        """按关系来源类型清理关系记录。"""
        source_type_value = normalize_relation_source_types([relation_source_type])[0]

        with self.get_connection() as conn:
            cursor = conn.execute(
                "DELETE FROM knowledge_relations WHERE relation_source_type = ?",
                (source_type_value,),
            )
            return cursor.rowcount

    def delete_outgoing_relations_for_knowledge(
        self,
        knowledge_id: int,
        relation_source_types: Optional[Iterable[str]] = None,
    ) -> int:
        """删除指定条目导出的关系，用于安全重跑 backfill。"""
        if knowledge_id <= 0:
            raise ValueError("knowledge_id 必须为正整数")

        query = "DELETE FROM knowledge_relations WHERE source_knowledge_id = ?"
        params: list[object] = [knowledge_id]

        source_type_values = normalize_relation_source_types(relation_source_types)
        if source_type_values:
            placeholders = ", ".join("?" for _ in source_type_values)
            query += f" AND relation_source_type IN ({placeholders})"
            params.extend(source_type_values)

        with self.get_connection() as conn:
            cursor = conn.execute(query, tuple(params))
            return cursor.rowcount
