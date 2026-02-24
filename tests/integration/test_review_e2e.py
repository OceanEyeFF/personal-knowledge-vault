"""
ReviewManager 完整生命周期的端到端测试。

使用真实 SQLite（tmp_path），不 mock 任何方法。
场景覆盖：批准工作流、拒绝/恢复工作流、重生成工作流、并发多条目。
"""
import json
import sqlite3
from pathlib import Path

import pytest

from src.storage.review_manager import ReviewItem, ReviewManager

MIGRATION_SQL = Path(__file__).parents[2] / "scripts/migrations/005_add_review_system.sql"


def _make_manager(tmp_path: Path) -> ReviewManager:
    db = tmp_path / "e2e.db"
    sql = MIGRATION_SQL.read_text(encoding="utf-8")
    conn = sqlite3.connect(str(db))
    conn.executescript(sql)
    conn.close()
    return ReviewManager(db_path=db)


def _make_item(**kwargs) -> ReviewItem:
    defaults = dict(
        ai_generated_summary="AI 摘要：本文介绍了知识管理的核心实践。",
        ai_generated_tags="AI,知识管理,实践",
        source_type="webpage",
        source_url="https://example.com",
        original_content_preview="这是原始内容的前 500 字...",
    )
    defaults.update(kwargs)
    return ReviewItem(**defaults)


class TestFullApproveWorkflow:
    """场景：创建 → 修改摘要 → 修改标签 → 添加评论 → 审核通过"""

    def test_approved_item_has_correct_effective_values(self, tmp_path):
        """通过审核后 get_effective_summary/tags 返回用户版本。"""
        mgr = _make_manager(tmp_path)
        rid = mgr.create_review(_make_item())

        mgr.update_user_summary(rid, "用户修改后的摘要")
        mgr.update_user_tags(rid, ["Python", "工程", "测试"])
        mgr.add_user_comment(rid, "这是一篇不错的文章")
        mgr.approve_review(rid)

        item = mgr.get_review(rid)
        assert item.review_status == "approved"
        assert item.get_effective_summary() == "用户修改后的摘要"
        assert "Python" in item.get_effective_tags()
        assert item.user_comments == "这是一篇不错的文章"

    def test_approved_history_has_all_actions(self, tmp_path):
        """完整审核流程后历史记录应包含所有操作。"""
        mgr = _make_manager(tmp_path)
        rid = mgr.create_review(_make_item())
        mgr.update_user_summary(rid, "新摘要")
        mgr.update_user_tags(rid, ["tag1"])
        mgr.add_user_comment(rid, "评论")
        mgr.approve_review(rid)

        history = mgr.get_history(rid)
        actions = [h["action"] for h in history]
        assert "create" in actions
        assert "edit_summary" in actions
        assert "edit_tags" in actions
        assert "add_comment" in actions
        assert "approve" in actions

    def test_version_increments_on_each_edit(self, tmp_path):
        """每次修改都应使 review_version 递增。"""
        mgr = _make_manager(tmp_path)
        rid = mgr.create_review(_make_item())

        v1 = mgr.get_review(rid).review_version
        mgr.update_user_summary(rid, "第一次修改")
        v2 = mgr.get_review(rid).review_version
        mgr.update_user_summary(rid, "第二次修改")
        v3 = mgr.get_review(rid).review_version

        assert v1 == 1
        assert v2 == 2
        assert v3 == 3

    def test_approved_item_not_in_pending(self, tmp_path):
        """通过审核的条目状态不再是 pending。"""
        mgr = _make_manager(tmp_path)
        rid = mgr.create_review(_make_item())
        mgr.approve_review(rid)
        item = mgr.get_review(rid)
        assert item.review_status != "pending"

    def test_approve_twice_succeeds(self, tmp_path):
        """对同一条目二次 approve 不应崩溃（幂等性）。"""
        mgr = _make_manager(tmp_path)
        rid = mgr.create_review(_make_item())
        assert mgr.approve_review(rid) is True
        # 第二次同样成功（SQLite UPDATE 影响 1 行）
        assert mgr.approve_review(rid) is True


class TestFullRejectAndRestoreWorkflow:
    """场景：创建 → 拒绝 → 出现在草稿区 → 恢复 → 再次通过"""

    def test_reject_appears_in_drafts(self, tmp_path):
        """拒绝后条目应出现在草稿列表中。"""
        mgr = _make_manager(tmp_path)
        rid = mgr.create_review(_make_item())
        mgr.reject_review(rid)

        drafts = mgr.list_drafts()
        assert any(d.review_id == rid for d in drafts)

    def test_restore_removes_from_drafts(self, tmp_path):
        """恢复后条目不应再出现在草稿列表中。"""
        mgr = _make_manager(tmp_path)
        rid = mgr.create_review(_make_item())
        mgr.reject_review(rid)
        mgr.restore_draft(rid)

        drafts = mgr.list_drafts()
        assert not any(d.review_id == rid for d in drafts)

    def test_restored_item_can_be_approved(self, tmp_path):
        """恢复草稿后可以成功通过审核。"""
        mgr = _make_manager(tmp_path)
        rid = mgr.create_review(_make_item())
        mgr.reject_review(rid)
        mgr.restore_draft(rid)
        mgr.approve_review(rid)

        item = mgr.get_review(rid)
        assert item.review_status == "approved"

    def test_reject_history_recorded(self, tmp_path):
        """拒绝后历史记录应包含 reject 操作。"""
        mgr = _make_manager(tmp_path)
        rid = mgr.create_review(_make_item())
        mgr.reject_review(rid)

        history = mgr.get_history(rid)
        actions = [h["action"] for h in history]
        assert "reject" in actions

    def test_restore_history_recorded(self, tmp_path):
        """恢复后历史记录应包含 restore 操作。"""
        mgr = _make_manager(tmp_path)
        rid = mgr.create_review(_make_item())
        mgr.reject_review(rid)
        mgr.restore_draft(rid)

        history = mgr.get_history(rid)
        actions = [h["action"] for h in history]
        assert "restore" in actions

    def test_restore_non_draft_returns_false(self, tmp_path):
        """对非草稿状态的条目调用 restore_draft 应返回 False。"""
        mgr = _make_manager(tmp_path)
        rid = mgr.create_review(_make_item())
        # 状态是 pending，不是 draft
        result = mgr.restore_draft(rid)
        assert result is False


class TestRegenerationWorkflow:
    """场景：AI 重新生成多次，prompts 正确累积"""

    def test_three_regenerations_stored(self, tmp_path):
        """三次重生成后 prompts 列表包含所有指导内容。"""
        mgr = _make_manager(tmp_path)
        rid = mgr.create_review(_make_item())

        mgr.record_regeneration(rid, "标签太多，请精简", "精简后的摘要", ["标签1", "标签2"])
        mgr.record_regeneration(rid, "摘要太长", "更短的摘要", ["标签1"])
        mgr.record_regeneration(rid, "再精简一次", "最终摘要", ["标签"])

        item = mgr.get_review(rid)
        assert item.regeneration_count == 3

        prompts = json.loads(item.regeneration_prompts)
        assert len(prompts) == 3
        assert prompts[0]["prompt"] == "标签太多，请精简"
        assert prompts[2]["prompt"] == "再精简一次"

    def test_latest_regen_updates_ai_fields(self, tmp_path):
        """最新一次重生成后 AI 字段应更新为最新内容。"""
        mgr = _make_manager(tmp_path)
        rid = mgr.create_review(_make_item())
        mgr.record_regeneration(rid, "优化", "最新摘要", ["新标签"])

        item = mgr.get_review(rid)
        assert item.ai_generated_summary == "最新摘要"
        assert "新标签" in item.ai_generated_tags

    def test_regen_history_entries(self, tmp_path):
        """每次重生成都应写入历史记录。"""
        mgr = _make_manager(tmp_path)
        rid = mgr.create_review(_make_item())
        mgr.record_regeneration(rid, "p1", "s1", ["t1"])
        mgr.record_regeneration(rid, "p2", "s2", ["t2"])

        history = mgr.get_history(rid)
        regen_records = [h for h in history if h["action"] == "regenerate"]
        assert len(regen_records) == 2

    def test_regen_incremental_index(self, tmp_path):
        """重生成 prompts 列表中的 index 应从 1 开始递增。"""
        mgr = _make_manager(tmp_path)
        rid = mgr.create_review(_make_item())
        mgr.record_regeneration(rid, "第一次", "摘要1", ["t1"])
        mgr.record_regeneration(rid, "第二次", "摘要2", ["t2"])

        item = mgr.get_review(rid)
        prompts = json.loads(item.regeneration_prompts)
        assert prompts[0]["index"] == 1
        assert prompts[1]["index"] == 2


class TestMultipleItemsWorkflow:
    """场景：多个条目并存，互不干扰"""

    def test_multiple_items_independent(self, tmp_path):
        """多个条目的审核状态互不影响。"""
        mgr = _make_manager(tmp_path)
        rid1 = mgr.create_review(_make_item(ai_generated_summary="摘要1"))
        rid2 = mgr.create_review(_make_item(ai_generated_summary="摘要2"))
        rid3 = mgr.create_review(_make_item(ai_generated_summary="摘要3"))

        mgr.approve_review(rid1)
        mgr.reject_review(rid2)
        # rid3 保持 pending

        assert mgr.get_review(rid1).review_status == "approved"
        assert mgr.get_review(rid2).review_status == "draft"
        assert mgr.get_review(rid3).review_status == "pending"

    def test_drafts_list_only_contains_rejected(self, tmp_path):
        """list_drafts 只返回状态为 draft 的条目。"""
        mgr = _make_manager(tmp_path)
        rid1 = mgr.create_review(_make_item())
        rid2 = mgr.create_review(_make_item())
        rid3 = mgr.create_review(_make_item())

        mgr.reject_review(rid1)
        mgr.approve_review(rid2)
        # rid3 保持 pending

        drafts = mgr.list_drafts()
        draft_ids = {d.review_id for d in drafts}
        assert rid1 in draft_ids
        assert rid2 not in draft_ids
        assert rid3 not in draft_ids

    def test_each_item_has_own_history(self, tmp_path):
        """不同条目的历史记录互相隔离。"""
        mgr = _make_manager(tmp_path)
        rid1 = mgr.create_review(_make_item())
        rid2 = mgr.create_review(_make_item())

        mgr.update_user_summary(rid1, "仅修改条目1的摘要")

        h1 = mgr.get_history(rid1)
        h2 = mgr.get_history(rid2)

        actions1 = [h["action"] for h in h1]
        actions2 = [h["action"] for h in h2]

        assert "edit_summary" in actions1
        assert "edit_summary" not in actions2
