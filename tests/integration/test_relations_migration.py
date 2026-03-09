"""
关系层基础迁移验证。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

BASE_SQL = Path(__file__).parents[2] / "scripts/migrations/001_initial_schema.sql"
RELATION_SQL = (
    Path(__file__).parents[2] / "scripts/migrations/006_add_relations_foundation.sql"
)


def _apply_sql(db_path: Path, sql_path: Path) -> None:
    sql = sql_path.read_text(encoding="utf-8")
    conn = sqlite3.connect(str(db_path))
    conn.executescript(sql)
    conn.close()


def _apply_base_and_relation_migrations(db_path: Path) -> None:
    _apply_sql(db_path, BASE_SQL)
    _apply_sql(db_path, RELATION_SQL)


class TestMigration006Schema:
    def test_knowledge_relations_table_exists(self, tmp_path):
        db = tmp_path / "test.db"
        _apply_base_and_relation_migrations(db)

        conn = sqlite3.connect(str(db))
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        conn.close()

        assert "knowledge_relations" in tables

    def test_knowledge_relations_required_columns(self, tmp_path):
        db = tmp_path / "test.db"
        _apply_base_and_relation_migrations(db)

        conn = sqlite3.connect(str(db))
        cols = {r[1] for r in conn.execute("PRAGMA table_info(knowledge_relations)")}
        conn.close()

        required = {
            "relation_id",
            "source_knowledge_id",
            "target_knowledge_id",
            "relation_type",
            "relation_source_type",
            "direction",
            "weight",
            "evidence_payload",
            "created_at",
            "updated_at",
        }
        assert required.issubset(cols)

    def test_indexes_and_trigger_created(self, tmp_path):
        db = tmp_path / "test.db"
        _apply_base_and_relation_migrations(db)

        conn = sqlite3.connect(str(db))
        indexes = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        triggers = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
        conn.close()

        assert {
            "idx_relations_source_knowledge_id",
            "idx_relations_target_knowledge_id",
            "idx_relations_type",
            "idx_relations_source_type",
        }.issubset(indexes)
        assert "trg_knowledge_relations_updated_at" in triggers

    def test_migration_idempotent(self, tmp_path):
        db = tmp_path / "test.db"
        _apply_base_and_relation_migrations(db)
        _apply_sql(db, RELATION_SQL)

        conn = sqlite3.connect(str(db))
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        conn.close()

        assert "knowledge_relations" in tables

    def test_schema_version_record_written(self, tmp_path):
        db = tmp_path / "test.db"
        _apply_base_and_relation_migrations(db)

        conn = sqlite3.connect(str(db))
        row = conn.execute(
            "SELECT version FROM schema_version WHERE version = '1.2.0'"
        ).fetchone()
        conn.close()

        assert row is not None


class TestMigration006Constraints:
    def test_unique_constraint_blocks_duplicate_relation(self, tmp_path):
        db = tmp_path / "test.db"
        _apply_base_and_relation_migrations(db)

        conn = sqlite3.connect(str(db))
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            """
            INSERT INTO knowledge_items (
                title, source_type, file_path
            ) VALUES ('A', 'generic', 'a.md')
            """
        )
        conn.execute(
            """
            INSERT INTO knowledge_items (
                title, source_type, file_path
            ) VALUES ('B', 'generic', 'b.md')
            """
        )
        conn.execute(
            """
            INSERT INTO knowledge_relations (
                source_knowledge_id,
                target_knowledge_id,
                relation_type,
                relation_source_type
            ) VALUES (1, 2, 'references', 'markdown_link')
            """
        )

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO knowledge_relations (
                    source_knowledge_id,
                    target_knowledge_id,
                    relation_type,
                    relation_source_type
                ) VALUES (1, 2, 'references', 'markdown_link')
                """
            )
            conn.commit()

        conn.close()

    def test_self_relation_rejected(self, tmp_path):
        db = tmp_path / "test.db"
        _apply_base_and_relation_migrations(db)

        conn = sqlite3.connect(str(db))
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            """
            INSERT INTO knowledge_items (
                title, source_type, file_path
            ) VALUES ('A', 'generic', 'a.md')
            """
        )

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO knowledge_relations (
                    source_knowledge_id,
                    target_knowledge_id,
                    relation_type,
                    relation_source_type
                ) VALUES (1, 1, 'references', 'markdown_link')
                """
            )
            conn.commit()

        conn.close()
