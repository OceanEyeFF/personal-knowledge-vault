"""
ReviewManager 单元测试

使用 pytest + tmp_path fixture（临时 SQLite 数据库），不需要任何 mock。
覆盖 ReviewManager 的全部公开方法及边界条件。
"""

import json
import sqlite3
from pathlib import Path

import pytest

from src.runtime.errors import ErrorCode, PKVRuntimeError
from src.storage.review_manager import ReviewItem, ReviewManager


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------

def _apply_migration(db_path: Path) -> None:
    """对临时数据库执行 005 迁移 SQL（兜底：使用内联 DDL）。"""
    migration_file = Path(__file__).parents[2] / "scripts/migrations/005_add_review_system.sql"
    conn = sqlite3.connect(db_path)
    try:
        if migration_file.exists():
            sql = migration_file.read_text(encoding="utf-8")
            conn.executescript(sql)
        else:
            # 内联兜底
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS review_queue (
                    review_id               INTEGER PRIMARY KEY AUTOINCREMENT,
                    ai_generated_summary    TEXT NOT NULL,
                    ai_generated_tags       TEXT NOT NULL DEFAULT '',
                    source_type             TEXT NOT NULL DEFAULT 'unknown',
                    ai_cleaned_content      TEXT NOT NULL DEFAULT '',
                    ai_generation_model     TEXT NOT NULL DEFAULT 'deepseek-chat',
                    original_content_preview TEXT NOT NULL DEFAULT '',
                    source_url              TEXT,
                    knowledge_id            INTEGER,
                    user_summary            TEXT,
                    user_tags               TEXT,
                    user_comments           TEXT,
                    regeneration_count      INTEGER NOT NULL DEFAULT 0,
                    regeneration_prompts    TEXT NOT NULL DEFAULT '[]',
                    review_status           TEXT NOT NULL DEFAULT 'pending'
                                            CHECK(review_status IN ('pending','approved','rejected','draft')),
                    review_version          INTEGER NOT NULL DEFAULT 1,
                    created_at              TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at              TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS review_history (
                    history_id  INTEGER PRIMARY KEY AUTOINCREMENT,
                    review_id   INTEGER NOT NULL,
                    action      TEXT NOT NULL,
                    details     TEXT NOT NULL DEFAULT '',
                    operator    TEXT NOT NULL DEFAULT 'user',
                    created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (review_id) REFERENCES review_queue(review_id) ON DELETE CASCADE
                );
            """)
        conn.commit()
    finally:
        conn.close()


def _make_manager(tmp_path: Path) -> ReviewManager:
    """创建使用临时数据库的 ReviewManager。"""
    db_path = tmp_path / "test_review.db"
    _apply_migration(db_path)
    return ReviewManager(db_path=db_path)


def _make_item(**kwargs) -> ReviewItem:
    """创建带有默认值的 ReviewItem（方便覆盖特定字段）。"""
    defaults = {
        "ai_generated_summary": "这是 AI 生成的摘要内容，介绍了某篇文章的主要观点。",
        "ai_generated_tags": "AI,知识管理,Python",
        "source_type": "webpage",
        "original_content_preview": "正文前 500 字的预览内容...",
        "source_url": "https://example.com/article",
    }
    defaults.update(kwargs)
    return ReviewItem(**defaults)


# ---------------------------------------------------------------------------
# create_review 测试
# ---------------------------------------------------------------------------

class TestCreateReview:
    def test_create_basic(self, tmp_path):
        """基本创建，返回正整数 review_id。"""
        manager = _make_manager(tmp_path)
        item = _make_item()
        review_id = manager.create_review(item)
        assert isinstance(review_id, int)
        assert review_id > 0

    def test_create_multiple(self, tmp_path):
        """多次创建，review_id 递增。"""
        manager = _make_manager(tmp_path)
        id1 = manager.create_review(_make_item())
        id2 = manager.create_review(_make_item())
        assert id2 > id1

    def test_create_without_optional_fields(self, tmp_path):
        """仅必填字段创建成功。"""
        manager = _make_manager(tmp_path)
        item = ReviewItem(
            ai_generated_summary="最小化摘要",
            ai_generated_tags="tag1",
            source_type="text",
        )
        review_id = manager.create_review(item)
        assert review_id > 0

    def test_create_empty_summary_raises(self, tmp_path):
        """空摘要应抛出 ValueError。"""
        manager = _make_manager(tmp_path)
        item = _make_item(ai_generated_summary="")
        with pytest.raises(ValueError, match="ai_generated_summary"):
            manager.create_review(item)

    def test_create_records_history(self, tmp_path):
        """创建后应自动写入 create 历史记录。"""
        manager = _make_manager(tmp_path)
        review_id = manager.create_review(_make_item())
        history = manager.get_history(review_id)
        assert len(history) >= 1
        assert history[0]["action"] == "create"
        assert history[0]["operator"] == "system"


# ---------------------------------------------------------------------------
# get_review 测试
# ---------------------------------------------------------------------------

class TestGetReview:
    def test_get_existing(self, tmp_path):
        """查询存在的条目应返回正确的 ReviewItem。"""
        manager = _make_manager(tmp_path)
        item = _make_item()
        review_id = manager.create_review(item)
        fetched = manager.get_review(review_id)
        assert fetched is not None
        assert fetched.review_id == review_id
        assert fetched.ai_generated_summary == item.ai_generated_summary
        assert fetched.ai_generated_tags == item.ai_generated_tags
        assert fetched.source_type == item.source_type

    def test_get_nonexistent(self, tmp_path):
        """查询不存在的 review_id 应返回 None。"""
        manager = _make_manager(tmp_path)
        result = manager.get_review(99999)
        assert result is None

    def test_get_default_status(self, tmp_path):
        """新建条目默认状态为 pending。"""
        manager = _make_manager(tmp_path)
        review_id = manager.create_review(_make_item())
        fetched = manager.get_review(review_id)
        assert fetched.review_status == "pending"

    def test_get_default_version(self, tmp_path):
        """新建条目默认版本为 1。"""
        manager = _make_manager(tmp_path)
        review_id = manager.create_review(_make_item())
        fetched = manager.get_review(review_id)
        assert fetched.review_version == 1


# ---------------------------------------------------------------------------
# update_user_summary 测试
# ---------------------------------------------------------------------------

class TestUpdateUserSummary:
    def test_update_summary_success(self, tmp_path):
        """正常更新用户摘要。"""
        manager = _make_manager(tmp_path)
        review_id = manager.create_review(_make_item())
        ok = manager.update_user_summary(review_id, "用户修改后的摘要")
        assert ok is True
        fetched = manager.get_review(review_id)
        assert fetched.user_summary == "用户修改后的摘要"

    def test_update_summary_version_increments(self, tmp_path):
        """更新摘要后版本号应递增。"""
        manager = _make_manager(tmp_path)
        review_id = manager.create_review(_make_item())
        original_version = manager.get_review(review_id).review_version
        manager.update_user_summary(review_id, "新摘要")
        assert manager.get_review(review_id).review_version == original_version + 1

    def test_update_summary_nonexistent(self, tmp_path):
        """不存在的 review_id 应返回 False。"""
        manager = _make_manager(tmp_path)
        ok = manager.update_user_summary(99999, "摘要")
        assert ok is False

    def test_update_summary_empty_string(self, tmp_path):
        """空字符串摘要也可更新（由调用方决定是否有意义）。"""
        manager = _make_manager(tmp_path)
        review_id = manager.create_review(_make_item())
        ok = manager.update_user_summary(review_id, "")
        assert ok is True

    def test_update_summary_records_history(self, tmp_path):
        """更新摘要后历史记录应包含 edit_summary 操作。"""
        manager = _make_manager(tmp_path)
        review_id = manager.create_review(_make_item())
        manager.update_user_summary(review_id, "新摘要")
        history = manager.get_history(review_id)
        actions = [h["action"] for h in history]
        assert "edit_summary" in actions

    def test_get_effective_summary_user_priority(self, tmp_path):
        """get_effective_summary 应返回用户版本。"""
        manager = _make_manager(tmp_path)
        review_id = manager.create_review(_make_item())
        manager.update_user_summary(review_id, "用户优先摘要")
        fetched = manager.get_review(review_id)
        assert fetched.get_effective_summary() == "用户优先摘要"


# ---------------------------------------------------------------------------
# update_user_tags 测试
# ---------------------------------------------------------------------------

class TestUpdateUserTags:
    def test_update_tags_success(self, tmp_path):
        """正常更新用户标签。"""
        manager = _make_manager(tmp_path)
        review_id = manager.create_review(_make_item())
        ok = manager.update_user_tags(review_id, ["Python", "测试", "自动化"])
        assert ok is True
        fetched = manager.get_review(review_id)
        assert fetched.user_tags == "Python,测试,自动化"

    def test_update_tags_empty_list(self, tmp_path):
        """空标签列表转换为空字符串。"""
        manager = _make_manager(tmp_path)
        review_id = manager.create_review(_make_item())
        manager.update_user_tags(review_id, [])
        fetched = manager.get_review(review_id)
        assert fetched.user_tags == ""

    def test_update_tags_nonexistent(self, tmp_path):
        """不存在的 review_id 应返回 False。"""
        manager = _make_manager(tmp_path)
        ok = manager.update_user_tags(99999, ["tag"])
        assert ok is False

    def test_get_effective_tags(self, tmp_path):
        """get_effective_tags 应返回用户标签列表。"""
        manager = _make_manager(tmp_path)
        review_id = manager.create_review(_make_item())
        manager.update_user_tags(review_id, ["用户标签1", "用户标签2"])
        fetched = manager.get_review(review_id)
        assert fetched.get_effective_tags() == ["用户标签1", "用户标签2"]

    def test_get_effective_tags_fallback_to_ai(self, tmp_path):
        """未修改时 get_effective_tags 应使用 AI 标签。"""
        manager = _make_manager(tmp_path)
        item = _make_item(ai_generated_tags="AI,知识管理,Python")
        review_id = manager.create_review(item)
        fetched = manager.get_review(review_id)
        assert fetched.get_effective_tags() == ["AI", "知识管理", "Python"]

    def test_update_tags_strips_whitespace(self, tmp_path):
        """标签应自动去除首尾空格。"""
        manager = _make_manager(tmp_path)
        review_id = manager.create_review(_make_item())
        manager.update_user_tags(review_id, ["  tag1  ", " tag2"])
        fetched = manager.get_review(review_id)
        assert fetched.get_effective_tags() == ["tag1", "tag2"]


# ---------------------------------------------------------------------------
# add_user_comment 测试
# ---------------------------------------------------------------------------

class TestAddUserComment:
    def test_add_comment_success(self, tmp_path):
        """正常添加评论。"""
        manager = _make_manager(tmp_path)
        review_id = manager.create_review(_make_item())
        ok = manager.add_user_comment(review_id, "这篇文章很有价值。")
        assert ok is True
        fetched = manager.get_review(review_id)
        assert "这篇文章很有价值。" in fetched.user_comments

    def test_add_comment_appends(self, tmp_path):
        """多次添加评论应追加而不是覆盖。"""
        manager = _make_manager(tmp_path)
        review_id = manager.create_review(_make_item())
        manager.add_user_comment(review_id, "第一条评论")
        manager.add_user_comment(review_id, "第二条评论")
        fetched = manager.get_review(review_id)
        assert "第一条评论" in fetched.user_comments
        assert "第二条评论" in fetched.user_comments

    def test_add_comment_nonexistent(self, tmp_path):
        """不存在的 review_id 应返回 False。"""
        manager = _make_manager(tmp_path)
        ok = manager.add_user_comment(99999, "评论")
        assert ok is False

    def test_add_comment_records_history(self, tmp_path):
        """添加评论后历史记录应包含 add_comment 操作。"""
        manager = _make_manager(tmp_path)
        review_id = manager.create_review(_make_item())
        manager.add_user_comment(review_id, "测试评论")
        history = manager.get_history(review_id)
        actions = [h["action"] for h in history]
        assert "add_comment" in actions


# ---------------------------------------------------------------------------
# record_regeneration 测试
# ---------------------------------------------------------------------------

class TestRecordRegeneration:
    def test_record_regen_success(self, tmp_path):
        """正常记录重新生成。"""
        manager = _make_manager(tmp_path)
        review_id = manager.create_review(_make_item())
        ok = manager.record_regeneration(
            review_id,
            prompt="请更简洁",
            new_summary="更简洁的摘要",
            new_tags=["简洁", "优化"],
        )
        assert ok is True
        fetched = manager.get_review(review_id)
        assert fetched.ai_generated_summary == "更简洁的摘要"
        assert "简洁" in fetched.ai_generated_tags
        assert fetched.regeneration_count == 1

    def test_record_regen_increments_count(self, tmp_path):
        """多次重生成应递增计数。"""
        manager = _make_manager(tmp_path)
        review_id = manager.create_review(_make_item())
        manager.record_regeneration(review_id, "指导1", "摘要1", ["t1"])
        manager.record_regeneration(review_id, "指导2", "摘要2", ["t2"])
        fetched = manager.get_review(review_id)
        assert fetched.regeneration_count == 2

    def test_record_regen_prompts_stored(self, tmp_path):
        """prompt 应记录到 regeneration_prompts JSON 数组。"""
        manager = _make_manager(tmp_path)
        review_id = manager.create_review(_make_item())
        manager.record_regeneration(review_id, "我的指导", "新摘要", ["新标签"])
        fetched = manager.get_review(review_id)
        prompts = json.loads(fetched.regeneration_prompts)
        assert len(prompts) == 1
        assert prompts[0]["prompt"] == "我的指导"

    def test_record_regen_nonexistent(self, tmp_path):
        """不存在的 review_id 应返回 False。"""
        manager = _make_manager(tmp_path)
        ok = manager.record_regeneration(99999, "prompt", "summary", ["tag"])
        assert ok is False

    def test_record_regen_records_history(self, tmp_path):
        """重生成后历史记录应包含 regenerate 操作。"""
        manager = _make_manager(tmp_path)
        review_id = manager.create_review(_make_item())
        manager.record_regeneration(review_id, "prompt", "summary", ["tag"])
        history = manager.get_history(review_id)
        actions = [h["action"] for h in history]
        assert "regenerate" in actions


# ---------------------------------------------------------------------------
# approve_review / reject_review 测试
# ---------------------------------------------------------------------------

class TestApproveRejectReview:
    def test_approve_success(self, tmp_path):
        """正常通过审核。"""
        manager = _make_manager(tmp_path)
        review_id = manager.create_review(_make_item())
        ok = manager.approve_review(review_id)
        assert ok is True
        fetched = manager.get_review(review_id)
        assert fetched.review_status == "approved"

    def test_approve_nonexistent(self, tmp_path):
        """不存在的 review_id 应返回 False。"""
        manager = _make_manager(tmp_path)
        ok = manager.approve_review(99999)
        assert ok is False

    def test_approve_records_history(self, tmp_path):
        """通过审核后历史记录应包含 approve 操作。"""
        manager = _make_manager(tmp_path)
        review_id = manager.create_review(_make_item())
        manager.approve_review(review_id)
        history = manager.get_history(review_id)
        actions = [h["action"] for h in history]
        assert "approve" in actions

    def test_reject_moves_to_draft(self, tmp_path):
        """拒绝后状态应变为 draft（草稿区）。"""
        manager = _make_manager(tmp_path)
        review_id = manager.create_review(_make_item())
        ok = manager.reject_review(review_id)
        assert ok is True
        fetched = manager.get_review(review_id)
        assert fetched.review_status == "draft"

    def test_reject_nonexistent(self, tmp_path):
        """不存在的 review_id 应返回 False。"""
        manager = _make_manager(tmp_path)
        ok = manager.reject_review(99999)
        assert ok is False

    def test_reject_records_history(self, tmp_path):
        """拒绝后历史记录应包含 reject 操作。"""
        manager = _make_manager(tmp_path)
        review_id = manager.create_review(_make_item())
        manager.reject_review(review_id)
        history = manager.get_history(review_id)
        actions = [h["action"] for h in history]
        assert "reject" in actions


# ---------------------------------------------------------------------------
# get_history 测试
# ---------------------------------------------------------------------------

class TestGetHistory:
    def test_get_history_order(self, tmp_path):
        """历史记录应按 history_id 正序排列。"""
        manager = _make_manager(tmp_path)
        review_id = manager.create_review(_make_item())
        manager.update_user_summary(review_id, "修改一")
        manager.update_user_tags(review_id, ["tag"])
        history = manager.get_history(review_id)
        ids = [h["history_id"] for h in history]
        assert ids == sorted(ids)

    def test_get_history_nonexistent(self, tmp_path):
        """不存在的 review_id 应返回空列表。"""
        manager = _make_manager(tmp_path)
        history = manager.get_history(99999)
        assert history == []

    def test_get_history_details_parsed(self, tmp_path):
        """历史记录的 details 字段应解析为字典（非字符串）。"""
        manager = _make_manager(tmp_path)
        review_id = manager.create_review(_make_item())
        history = manager.get_history(review_id)
        for record in history:
            assert isinstance(record["details"], dict)

    def test_get_history_full_workflow(self, tmp_path):
        """完整流程：create → edit_summary → edit_tags → approve。"""
        manager = _make_manager(tmp_path)
        review_id = manager.create_review(_make_item())
        manager.update_user_summary(review_id, "修改摘要")
        manager.update_user_tags(review_id, ["新标签"])
        manager.approve_review(review_id)
        history = manager.get_history(review_id)
        actions = [h["action"] for h in history]
        assert "create" in actions
        assert "edit_summary" in actions
        assert "edit_tags" in actions
        assert "approve" in actions


# ---------------------------------------------------------------------------
# list_drafts / restore_draft 测试
# ---------------------------------------------------------------------------

class TestDraftManagement:
    def test_list_drafts_empty(self, tmp_path):
        """无草稿时应返回空列表。"""
        manager = _make_manager(tmp_path)
        assert manager.list_drafts() == []

    def test_list_drafts_after_reject(self, tmp_path):
        """拒绝后条目应出现在草稿列表中。"""
        manager = _make_manager(tmp_path)
        review_id = manager.create_review(_make_item())
        manager.reject_review(review_id)
        drafts = manager.list_drafts()
        assert len(drafts) == 1
        assert drafts[0].review_id == review_id

    def test_list_drafts_excludes_approved(self, tmp_path):
        """已通过的条目不应出现在草稿列表。"""
        manager = _make_manager(tmp_path)
        review_id = manager.create_review(_make_item())
        manager.approve_review(review_id)
        assert manager.list_drafts() == []

    def test_restore_draft_success(self, tmp_path):
        """正常从草稿区恢复为待审核状态。"""
        manager = _make_manager(tmp_path)
        review_id = manager.create_review(_make_item())
        manager.reject_review(review_id)
        ok = manager.restore_draft(review_id)
        assert ok is True
        fetched = manager.get_review(review_id)
        assert fetched.review_status == "pending"

    def test_restore_draft_nonexistent(self, tmp_path):
        """不存在的 review_id 应返回 False。"""
        manager = _make_manager(tmp_path)
        ok = manager.restore_draft(99999)
        assert ok is False

    def test_restore_draft_non_draft_status(self, tmp_path):
        """非 draft 状态的条目恢复应返回 False。"""
        manager = _make_manager(tmp_path)
        review_id = manager.create_review(_make_item())
        # 状态是 pending，不是 draft
        ok = manager.restore_draft(review_id)
        assert ok is False

    def test_restore_draft_records_history(self, tmp_path):
        """恢复草稿后历史记录应包含 restore 操作。"""
        manager = _make_manager(tmp_path)
        review_id = manager.create_review(_make_item())
        manager.reject_review(review_id)
        manager.restore_draft(review_id)
        history = manager.get_history(review_id)
        actions = [h["action"] for h in history]
        assert "restore" in actions

    def test_restore_draft_removed_from_drafts_list(self, tmp_path):
        """恢复后条目不应再出现在草稿列表中。"""
        manager = _make_manager(tmp_path)
        review_id = manager.create_review(_make_item())
        manager.reject_review(review_id)
        manager.restore_draft(review_id)
        drafts = manager.list_drafts()
        draft_ids = [d.review_id for d in drafts]
        assert review_id not in draft_ids


# ---------------------------------------------------------------------------
# ReviewItem 数据类方法测试
# ---------------------------------------------------------------------------

class TestReviewItemMethods:
    def test_get_effective_summary_no_user(self):
        """无用户摘要时使用 AI 摘要。"""
        item = _make_item()
        assert item.get_effective_summary() == item.ai_generated_summary

    def test_get_effective_summary_with_user(self):
        """有用户摘要时优先使用用户摘要。"""
        item = _make_item(user_summary="用户版摘要")
        assert item.get_effective_summary() == "用户版摘要"

    def test_get_effective_tags_no_user(self):
        """无用户标签时使用 AI 标签。"""
        item = _make_item(ai_generated_tags="AI,知识管理,Python")
        assert item.get_effective_tags() == ["AI", "知识管理", "Python"]

    def test_get_effective_tags_with_user(self):
        """有用户标签时优先使用用户标签。"""
        item = _make_item(user_tags="用户标签1,用户标签2")
        tags = item.get_effective_tags()
        assert tags == ["用户标签1", "用户标签2"]

    def test_get_effective_tags_empty_ai_tags(self):
        """AI 标签为空时返回空列表。"""
        item = _make_item(ai_generated_tags="")
        assert item.get_effective_tags() == []

    def test_get_effective_tags_strips_whitespace(self):
        """标签应自动去除首尾空格。"""
        item = _make_item(ai_generated_tags="  AI  ,  Python  ")
        tags = item.get_effective_tags()
        assert tags == ["AI", "Python"]


# ---------------------------------------------------------------------------
# Task 1 补充：_update_field 边界行为
# ---------------------------------------------------------------------------

class TestUpdateField:
    def test_update_field_returns_false_on_missing_record(self, tmp_path):
        """对不存在的 review_id 执行 _update_field 返回 False。"""
        manager = _make_manager(tmp_path)
        ok = manager._update_field(
            review_id=99999,
            sql="UPDATE review_queue SET user_summary = ? WHERE review_id = ?",
            params=("test", 99999),
            action="edit_summary",
            details="{}",
            warn_msg="test warn",
        )
        assert ok is False

    def test_update_field_records_history(self, tmp_path):
        """_update_field 成功后 history 表有记录。"""
        manager = _make_manager(tmp_path)
        review_id = manager.create_review(_make_item())
        manager._update_field(
            review_id=review_id,
            sql="UPDATE review_queue SET user_summary = ?, review_version = review_version + 1 WHERE review_id = ?",
            params=("新摘要", review_id),
            action="edit_summary",
            details='{"summary_length": 3}',
            warn_msg="not found",
        )
        history = manager.get_history(review_id)
        actions = [h["action"] for h in history]
        assert "edit_summary" in actions


# ---------------------------------------------------------------------------
# W1：审核表只能由 migration 创建
# ---------------------------------------------------------------------------

class TestMigrationOwnedTables:
    def test_missing_database_is_rejected_without_implicit_creation(self, tmp_path):
        """业务管理器不得把缺库误判成 fresh install 并自行建表。"""
        db_path = tmp_path / "new_db.db"

        with pytest.raises(PKVRuntimeError) as error:
            ReviewManager(db_path=db_path)

        assert error.value.code is ErrorCode.DATABASE_MISSING
        assert not db_path.exists()

    def test_schema_verification_is_idempotent(self, tmp_path):
        """已迁移 schema 可重复验证，验证过程不执行 DDL。"""
        manager = _make_manager(tmp_path)
        manager._verify_tables()
        manager._verify_tables()
        review_id = manager.create_review(_make_item())
        assert review_id > 0


# ---------------------------------------------------------------------------
# Task 1 补充：approve 后不在草稿区
# ---------------------------------------------------------------------------

class TestApproveRemovesFromDrafts:
    def test_approved_not_in_drafts(self, tmp_path):
        """审核通过的条目不出现在 list_drafts 中。"""
        manager = _make_manager(tmp_path)
        review_id = manager.create_review(_make_item())
        manager.approve_review(review_id)
        drafts = manager.list_drafts()
        assert all(d.review_id != review_id for d in drafts)

    def test_pending_not_in_drafts(self, tmp_path):
        """pending 状态的条目不出现在 list_drafts 中。"""
        manager = _make_manager(tmp_path)
        review_id = manager.create_review(_make_item())
        drafts = manager.list_drafts()
        assert all(d.review_id != review_id for d in drafts)


# ---------------------------------------------------------------------------
# Task 1 补充：record_regeneration 多次叠加
# ---------------------------------------------------------------------------

class TestMultipleRegeneration:
    def test_multiple_regen_prompts_accumulate(self, tmp_path):
        """多次 record_regeneration 后 regeneration_prompts 是完整列表。"""
        manager = _make_manager(tmp_path)
        review_id = manager.create_review(_make_item())
        manager.record_regeneration(review_id, "指导1", "摘要1", ["t1"])
        manager.record_regeneration(review_id, "指导2", "摘要2", ["t2"])
        manager.record_regeneration(review_id, "指导3", "摘要3", ["t3"])
        fetched = manager.get_review(review_id)
        prompts = json.loads(fetched.regeneration_prompts)
        assert len(prompts) == 3
        assert prompts[0]["prompt"] == "指导1"
        assert prompts[1]["prompt"] == "指导2"
        assert prompts[2]["prompt"] == "指导3"

    def test_regen_count_increments_correctly(self, tmp_path):
        """连续 3 次 record_regeneration 后 regeneration_count == 3。"""
        manager = _make_manager(tmp_path)
        review_id = manager.create_review(_make_item())
        for i in range(3):
            manager.record_regeneration(review_id, f"prompt{i}", f"摘要{i}", [f"tag{i}"])
        fetched = manager.get_review(review_id)
        assert fetched.regeneration_count == 3

    def test_regen_updates_ai_summary_to_latest(self, tmp_path):
        """每次重生成后 ai_generated_summary 应为最新内容。"""
        manager = _make_manager(tmp_path)
        review_id = manager.create_review(_make_item())
        manager.record_regeneration(review_id, "prompt1", "第一次摘要", ["t1"])
        manager.record_regeneration(review_id, "prompt2", "第二次摘要", ["t2"])
        fetched = manager.get_review(review_id)
        assert fetched.ai_generated_summary == "第二次摘要"


# ---------------------------------------------------------------------------
# Task 1 补充：add_user_comment 多次追加
# ---------------------------------------------------------------------------

class TestCommentAppend:
    def test_comment_appends_newline(self, tmp_path):
        """第二次 add_user_comment 内容追加（以换行符分隔）。"""
        manager = _make_manager(tmp_path)
        review_id = manager.create_review(_make_item())
        manager.add_user_comment(review_id, "第一条评论")
        manager.add_user_comment(review_id, "第二条评论")
        fetched = manager.get_review(review_id)
        assert "第一条评论" in fetched.user_comments
        assert "第二条评论" in fetched.user_comments
        # 两条评论应通过换行分隔
        assert "\n" in fetched.user_comments

    def test_comment_three_times(self, tmp_path):
        """三次追加评论，全部保留。"""
        manager = _make_manager(tmp_path)
        review_id = manager.create_review(_make_item())
        for i in range(1, 4):
            manager.add_user_comment(review_id, f"评论{i}")
        fetched = manager.get_review(review_id)
        for i in range(1, 4):
            assert f"评论{i}" in fetched.user_comments

    def test_first_comment_no_leading_newline(self, tmp_path):
        """首次添加评论，不应有前导换行符。"""
        manager = _make_manager(tmp_path)
        review_id = manager.create_review(_make_item())
        manager.add_user_comment(review_id, "唯一评论")
        fetched = manager.get_review(review_id)
        assert not fetched.user_comments.startswith("\n")


# ---------------------------------------------------------------------------
# W1：缺表时 fail closed，不保留内联 DDL 兜底
# ---------------------------------------------------------------------------

class TestMissingReviewSchema:
    def test_partial_review_schema_is_rejected_without_repairing_it(self, tmp_path):
        db_path = tmp_path / "inline.db"
        with sqlite3.connect(str(db_path)) as conn:
            conn.execute("CREATE TABLE review_queue (review_id INTEGER PRIMARY KEY)")

        with pytest.raises(PKVRuntimeError) as error:
            ReviewManager(db_path=db_path)

        assert error.value.code is ErrorCode.DATABASE_SCHEMA_DRIFT
        with sqlite3.connect(str(db_path)) as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        assert "review_queue" in tables
        assert "review_history" not in tables


# ---------------------------------------------------------------------------
# 连接初始化失败 fail-closed：row_factory/PRAGMA 任一步失败也恰好关闭一次
# ---------------------------------------------------------------------------


def _install_init_failure_tracking(
    monkeypatch: pytest.MonkeyPatch,
    *,
    fail_row_factory: bool = False,
    fail_pragma: bool = False,
):
    """Fault-inject sqlite3.connect with a tracking connection whose
    row_factory assignment / PRAGMA init step can fail."""
    real_connect = sqlite3.connect
    opened: list[sqlite3.Connection] = []
    close_counts: dict[sqlite3.Connection, int] = {}

    class TrackingConnection(sqlite3.Connection):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._tracked_row_factory = None

        @property
        def row_factory(self):
            return self._tracked_row_factory

        @row_factory.setter
        def row_factory(self, value):
            if fail_row_factory:
                raise RuntimeError("simulated row_factory failure")
            self._tracked_row_factory = value

        def execute(self, sql, parameters=()):
            if (
                fail_pragma
                and isinstance(sql, str)
                and sql.upper().startswith("PRAGMA FOREIGN_KEYS")
            ):
                raise sqlite3.OperationalError("simulated PRAGMA failure")
            return super().execute(sql, parameters)

        def close(self):
            close_counts[self] = close_counts.get(self, 0) + 1
            super().close()

    def tracked_connect(database, *args, **kwargs):
        conn = real_connect(database, *args, factory=TrackingConnection, **kwargs)
        opened.append(conn)
        return conn

    monkeypatch.setattr(sqlite3, "connect", tracked_connect)
    return opened, close_counts


@pytest.mark.parametrize("failure_mode", ["row_factory", "pragma"])
def test_get_connection_closes_once_when_init_step_fails(
    tmp_path, monkeypatch, failure_mode
):
    """row_factory/PRAGMA 初始化任一步失败时连接也必须恰好关闭一次。"""
    manager = _make_manager(tmp_path)
    opened, close_counts = _install_init_failure_tracking(
        monkeypatch,
        fail_row_factory=(failure_mode == "row_factory"),
        fail_pragma=(failure_mode == "pragma"),
    )

    with pytest.raises((RuntimeError, sqlite3.OperationalError)):
        with manager._get_connection() as conn:
            pass

    assert len(opened) == 1
    assert close_counts.get(opened[0], 0) == 1
