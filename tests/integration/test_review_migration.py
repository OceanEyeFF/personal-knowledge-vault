"""
迁移 005 的实际执行验证。

直接读取 SQL 文件执行，确保 SQL 语句本身的正确性。
不依赖 MigrationManager。
"""
import sqlite3
from pathlib import Path

import pytest

MIGRATION_SQL = Path(__file__).parents[2] / "scripts/migrations/005_add_review_system.sql"


def _apply_migration(db_path: Path) -> None:
    """执行迁移 SQL（完整内容，包含触发器）。"""
    sql = MIGRATION_SQL.read_text(encoding="utf-8")
    conn = sqlite3.connect(str(db_path))
    conn.executescript(sql)
    conn.close()


class TestMigration005Schema:
    def test_review_queue_table_exists(self, tmp_path):
        """迁移后 review_queue 表应存在。"""
        db = tmp_path / "test.db"
        _apply_migration(db)
        conn = sqlite3.connect(str(db))
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        conn.close()
        assert "review_queue" in tables

    def test_review_history_table_exists(self, tmp_path):
        """迁移后 review_history 表应存在。"""
        db = tmp_path / "test.db"
        _apply_migration(db)
        conn = sqlite3.connect(str(db))
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        conn.close()
        assert "review_history" in tables

    def test_review_queue_required_columns(self, tmp_path):
        """review_queue 应包含所有必需列。"""
        db = tmp_path / "test.db"
        _apply_migration(db)
        conn = sqlite3.connect(str(db))
        cols = {r[1] for r in conn.execute("PRAGMA table_info(review_queue)")}
        conn.close()
        required = {
            "review_id", "review_status", "ai_generated_summary",
            "ai_generated_tags", "user_summary", "user_tags",
            "user_comments", "regeneration_count", "regeneration_prompts",
            "created_at", "review_version",
        }
        assert required.issubset(cols)

    def test_review_history_required_columns(self, tmp_path):
        """review_history 应包含所有必需列。"""
        db = tmp_path / "test.db"
        _apply_migration(db)
        conn = sqlite3.connect(str(db))
        cols = {r[1] for r in conn.execute("PRAGMA table_info(review_history)")}
        conn.close()
        assert {"history_id", "review_id", "action", "details", "operator", "created_at"}.issubset(cols)

    def test_indexes_created(self, tmp_path):
        """迁移后应创建所有预期索引。"""
        db = tmp_path / "test.db"
        _apply_migration(db)
        conn = sqlite3.connect(str(db))
        indexes = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        conn.close()
        expected_indexes = {
            "idx_review_queue_status",
            "idx_review_queue_created_at",
            "idx_review_queue_knowledge_id",
            "idx_review_history_review_id",
        }
        assert expected_indexes.issubset(indexes)

    def test_migration_idempotent(self, tmp_path):
        """重复执行迁移不报错（IF NOT EXISTS）。"""
        db = tmp_path / "test.db"
        _apply_migration(db)
        _apply_migration(db)  # 第二次执行不应抛出异常
        # 验证表仍然可用
        conn = sqlite3.connect(str(db))
        conn.execute("INSERT INTO review_queue (ai_generated_summary, ai_generated_tags, source_type) VALUES ('s', 't', 'webpage')")
        conn.commit()
        conn.close()

    def test_trigger_exists(self, tmp_path):
        """迁移后 updated_at 触发器应存在。"""
        db = tmp_path / "test.db"
        _apply_migration(db)
        conn = sqlite3.connect(str(db))
        triggers = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
        conn.close()
        assert "trg_review_queue_updated_at" in triggers


class TestMigration005Constraints:
    def test_review_status_default_pending(self, tmp_path):
        """新建记录默认 review_status 为 'pending'。"""
        db = tmp_path / "test.db"
        _apply_migration(db)
        conn = sqlite3.connect(str(db))
        conn.execute(
            "INSERT INTO review_queue (ai_generated_summary, ai_generated_tags, source_type) "
            "VALUES ('s', 't', 'webpage')"
        )
        conn.commit()
        row = conn.execute("SELECT review_status FROM review_queue WHERE review_id = 1").fetchone()
        conn.close()
        assert row[0] == "pending"

    def test_regeneration_count_default_zero(self, tmp_path):
        """新建记录默认 regeneration_count 为 0。"""
        db = tmp_path / "test.db"
        _apply_migration(db)
        conn = sqlite3.connect(str(db))
        conn.execute(
            "INSERT INTO review_queue (ai_generated_summary, ai_generated_tags, source_type) "
            "VALUES ('s', 't', 'webpage')"
        )
        conn.commit()
        row = conn.execute("SELECT regeneration_count FROM review_queue WHERE review_id = 1").fetchone()
        conn.close()
        assert row[0] == 0

    def test_review_version_default_one(self, tmp_path):
        """新建记录默认 review_version 为 1。"""
        db = tmp_path / "test.db"
        _apply_migration(db)
        conn = sqlite3.connect(str(db))
        conn.execute(
            "INSERT INTO review_queue (ai_generated_summary, ai_generated_tags, source_type) "
            "VALUES ('s', 't', 'webpage')"
        )
        conn.commit()
        row = conn.execute("SELECT review_version FROM review_queue WHERE review_id = 1").fetchone()
        conn.close()
        assert row[0] == 1

    def test_review_status_check_constraint(self, tmp_path):
        """review_status 字段只接受合法值，非法值应报错。"""
        db = tmp_path / "test.db"
        _apply_migration(db)
        conn = sqlite3.connect(str(db))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO review_queue (ai_generated_summary, ai_generated_tags, source_type, review_status) "
                "VALUES ('s', 't', 'webpage', 'invalid_status')"
            )
            conn.commit()
        conn.close()

    def test_review_history_fk_cascade(self, tmp_path):
        """删除 review_queue 记录时级联删除 review_history。"""
        db = tmp_path / "test.db"
        _apply_migration(db)
        conn = sqlite3.connect(str(db))
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT INTO review_queue (ai_generated_summary, ai_generated_tags, source_type) "
            "VALUES ('s', 't', 'webpage')"
        )
        conn.execute(
            "INSERT INTO review_history (review_id, action) VALUES (1, 'init')"
        )
        conn.commit()
        conn.execute("DELETE FROM review_queue WHERE review_id = 1")
        conn.commit()
        count = conn.execute("SELECT COUNT(*) FROM review_history WHERE review_id = 1").fetchone()[0]
        conn.close()
        assert count == 0

    def test_regeneration_prompts_default_empty_array(self, tmp_path):
        """新建记录默认 regeneration_prompts 为 '[]'。"""
        db = tmp_path / "test.db"
        _apply_migration(db)
        conn = sqlite3.connect(str(db))
        conn.execute(
            "INSERT INTO review_queue (ai_generated_summary, ai_generated_tags, source_type) "
            "VALUES ('s', 't', 'webpage')"
        )
        conn.commit()
        row = conn.execute("SELECT regeneration_prompts FROM review_queue WHERE review_id = 1").fetchone()
        conn.close()
        assert row[0] == "[]"
