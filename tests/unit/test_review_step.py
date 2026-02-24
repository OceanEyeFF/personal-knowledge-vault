"""
ReviewStep 单元测试

使用 Mock 替代 ReviewManager 和 DeepSeekClient，测试 ReviewStep 的异步逻辑。
覆盖：跳过条件、批准流程、拒绝流程、entry 字段更新。
"""

import asyncio
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.storage.markdown_store import Entry
from src.storage.review_manager import ReviewItem
from src.workflow.models import WorkflowContext
from src.workflow.steps import ReviewStep


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------

def _make_context(**kwargs) -> WorkflowContext:
    """创建带初始状态的 WorkflowContext。"""
    return WorkflowContext(initial_state=kwargs)


def _make_entry(**kwargs) -> Entry:
    """创建带默认字段的 Entry。"""
    defaults = {
        "title": "测试文章",
        "content": "这是文章正文内容，用于审核测试。",
        "summary_100_words": "AI 生成的摘要文字。",
        "summary_one_sentence": "一句话摘要。",
        "tags": ["AI", "测试"],
        "source_type": "webpage",
        "source_url": "https://example.com/test",
    }
    defaults.update(kwargs)
    entry = Entry.__new__(Entry)
    for k, v in defaults.items():
        setattr(entry, k, v)
    return entry


def _make_review_item(review_id: int = 1, **kwargs) -> ReviewItem:
    """创建用于 mock 返回的 ReviewItem。"""
    defaults = {
        "review_id": review_id,
        "ai_generated_summary": "AI 生成的摘要文字。",
        "ai_generated_tags": "AI,测试",
        "source_type": "webpage",
        "review_status": "pending",
    }
    defaults.update(kwargs)
    return ReviewItem(**defaults)


def _make_manager_mock(
    review_id: int = 1,
    get_item: Optional[ReviewItem] = None,
) -> MagicMock:
    """创建 ReviewManager 的 Mock 对象。"""
    mock = MagicMock()
    mock.create_review.return_value = review_id
    mock.get_review.return_value = get_item or _make_review_item(review_id=review_id)
    mock.approve_review.return_value = True
    mock.reject_review.return_value = True
    return mock


def _make_step(
    config: Optional[Dict[str, Any]] = None,
    manager_mock: Optional[MagicMock] = None,
) -> ReviewStep:
    """创建配置好的 ReviewStep 实例。"""
    step_config = config or {"required": True, "timeout": 600}
    return ReviewStep(
        step_id="review_entry",
        config=step_config,
        review_manager=manager_mock,
    )


# ---------------------------------------------------------------------------
# 跳过条件测试
# ---------------------------------------------------------------------------

class TestReviewStepSkipConditions:
    def test_skip_when_entry_is_none(self):
        """context 中无 entry 时应返回 errors 并跳过。"""
        step = _make_step()
        ctx = _make_context()  # 无 entry
        result = asyncio.run(step.execute(ctx))
        assert "errors" in result
        assert "缺少 Entry" in result["errors"][0]

    def test_skip_when_skip_review_is_true(self):
        """context.state.skip_review=True 时跳过审核。"""
        manager_mock = _make_manager_mock()
        step = _make_step(manager_mock=manager_mock)
        entry = _make_entry()
        ctx = _make_context(entry=entry, skip_review=True)
        result = asyncio.run(step.execute(ctx))
        # 应直接跳过，不调用 create_review
        manager_mock.create_review.assert_not_called()
        assert "review_id" not in result

    def test_skip_when_not_required_and_skip_sharpen(self):
        """required=False 且 skip_sharpen=True 时跳过。"""
        manager_mock = _make_manager_mock()
        step = _make_step(
            config={"required": False, "timeout": 600},
            manager_mock=manager_mock,
        )
        entry = _make_entry()
        ctx = _make_context(entry=entry, skip_sharpen=True)
        result = asyncio.run(step.execute(ctx))
        manager_mock.create_review.assert_not_called()
        assert "review_id" not in result

    def test_not_skip_when_required_even_with_skip_sharpen(self):
        """required=True 时即使 skip_sharpen=True 也不跳过。"""
        manager_mock = _make_manager_mock()

        # 需要 patch _interactive_review 以避免实际 CLI 输入
        async def fake_interactive(*args, **kwargs):
            return "approve"

        step = _make_step(
            config={"required": True, "timeout": 600},
            manager_mock=manager_mock,
        )
        entry = _make_entry()
        ctx = _make_context(entry=entry, skip_sharpen=True)

        with patch.object(step, "_interactive_review", side_effect=fake_interactive):
            result = asyncio.run(step.execute(ctx))

        manager_mock.create_review.assert_called_once()


# ---------------------------------------------------------------------------
# 批准流程测试
# ---------------------------------------------------------------------------

class TestReviewStepApprove:
    def _run_with_approve(self, extra_fields: Optional[Dict] = None) -> tuple:
        """辅助：运行批准流程，返回 (result, manager_mock, entry, ctx)。"""
        get_item = _make_review_item(
            review_id=1,
            user_summary="用户修改后的摘要",
            user_tags="用户标签1,用户标签2",
            user_comments="用户评论内容",
        )
        manager_mock = _make_manager_mock(review_id=1, get_item=get_item)

        async def fake_interactive(*args, **kwargs):
            return "approve"

        step = _make_step(manager_mock=manager_mock)
        entry = _make_entry(**(extra_fields or {}))
        ctx = _make_context(entry=entry)

        with patch.object(step, "_interactive_review", side_effect=fake_interactive):
            result = asyncio.run(step.execute(ctx))

        return result, manager_mock, entry, ctx

    def test_approve_result_fields(self):
        """批准流程应返回包含 review_id、review_status、review_rejected 的结果。"""
        result, _, _, _ = self._run_with_approve()
        assert result["review_id"] == 1
        assert result["review_status"] == "approved"
        assert result["review_rejected"] is False

    def test_approve_calls_approve_review(self):
        """批准流程应调用 manager.approve_review。"""
        _, manager_mock, _, _ = self._run_with_approve()
        manager_mock.approve_review.assert_called_once_with(1)

    def test_approve_updates_entry_summary(self):
        """批准后 entry.summary_100_words 应更新为 get_effective_summary()。"""
        result, _, entry, _ = self._run_with_approve()
        assert entry.summary_100_words == "用户修改后的摘要"

    def test_approve_updates_entry_tags(self):
        """批准后 entry.tags 应更新为 get_effective_tags()。"""
        result, _, entry, _ = self._run_with_approve()
        assert entry.tags == ["用户标签1", "用户标签2"]

    def test_approve_updates_entry_notes_with_comments(self):
        """批准后用户评论应追加到 entry.notes。"""
        result, _, entry, _ = self._run_with_approve()
        assert "用户评论内容" in (entry.notes or "")

    def test_approve_sets_entry_in_context(self):
        """批准后 context.state 中的 entry 应被更新。"""
        result, _, _, ctx = self._run_with_approve()
        assert ctx.state.get("entry") is not None

    def test_approve_timeout_auto_approves(self):
        """超时后应自动通过审核。"""
        manager_mock = _make_manager_mock(review_id=1)

        async def timeout_interactive(*args, **kwargs):
            raise asyncio.TimeoutError()

        step = _make_step(
            config={"required": True, "timeout": 1},
            manager_mock=manager_mock,
        )
        entry = _make_entry()
        ctx = _make_context(entry=entry)

        with patch.object(step, "_interactive_review", side_effect=timeout_interactive):
            # 在 steps 模块内打补丁，模拟 asyncio.wait_for 抛出超时
            async def mock_wait_for(coro, timeout):
                # 关闭协程避免 RuntimeWarning，然后抛超时
                try:
                    coro.close()
                except Exception:
                    pass
                raise asyncio.TimeoutError()

            with patch("src.workflow.steps.asyncio.wait_for", side_effect=mock_wait_for):
                result = asyncio.run(step.execute(ctx))

        assert result["review_status"] == "approved"
        manager_mock.approve_review.assert_called_once()


# ---------------------------------------------------------------------------
# 拒绝流程测试
# ---------------------------------------------------------------------------

class TestReviewStepReject:
    def _run_with_reject(self) -> tuple:
        """辅助：运行拒绝流程，返回 (result, manager_mock, entry, ctx)。"""
        manager_mock = _make_manager_mock(review_id=2)

        async def fake_interactive(*args, **kwargs):
            return "reject"

        step = _make_step(manager_mock=manager_mock)
        entry = _make_entry()
        ctx = _make_context(entry=entry)

        with patch.object(step, "_interactive_review", side_effect=fake_interactive):
            result = asyncio.run(step.execute(ctx))

        return result, manager_mock, entry, ctx

    def test_reject_result_fields(self):
        """拒绝流程应返回包含 review_rejected=True 的结果。"""
        result, _, _, _ = self._run_with_reject()
        assert result["review_id"] == 2
        assert result["review_status"] == "rejected"
        assert result["review_rejected"] is True

    def test_reject_calls_reject_review(self):
        """拒绝流程应调用 manager.reject_review。"""
        _, manager_mock, _, _ = self._run_with_reject()
        manager_mock.reject_review.assert_called_once_with(2)

    def test_reject_not_calls_approve(self):
        """拒绝流程不应调用 approve_review。"""
        _, manager_mock, _, _ = self._run_with_reject()
        manager_mock.approve_review.assert_not_called()

    def test_reject_sets_review_rejected_in_context(self):
        """拒绝后 context.state.review_rejected 应为 True。"""
        _, _, _, ctx = self._run_with_reject()
        assert ctx.state.get("review_rejected") is True

    def test_store_step_skipped_after_reject(self):
        """review_rejected=True 时，StoreStep 应跳过存储。"""
        from src.workflow.steps import StoreStep

        ctx = _make_context(review_rejected=True)
        store_step = StoreStep(
            step_id="store_entry",
            config={"targets": ["markdown"]},
        )
        result = asyncio.run(store_step.execute(ctx))
        assert result.get("review_rejected") is True
        assert "errors" in result


# ---------------------------------------------------------------------------
# create_review 调用测试
# ---------------------------------------------------------------------------

class TestReviewStepCreateReview:
    def test_create_review_called_with_correct_fields(self):
        """execute 应使用 entry 的数据调用 create_review。"""
        manager_mock = _make_manager_mock(review_id=3)

        async def fake_interactive(*args, **kwargs):
            return "approve"

        step = _make_step(manager_mock=manager_mock)
        entry = _make_entry(
            summary_100_words="专属摘要",
            tags=["标签A", "标签B"],
            source_type="webpage",
        )
        ctx = _make_context(entry=entry)

        with patch.object(step, "_interactive_review", side_effect=fake_interactive):
            asyncio.run(step.execute(ctx))

        call_args = manager_mock.create_review.call_args
        assert call_args is not None
        item = call_args[0][0]  # 第一个位置参数即 ReviewItem
        assert item.ai_generated_summary == "专属摘要"
        assert "标签A" in item.ai_generated_tags
        assert "标签B" in item.ai_generated_tags
        assert item.source_type == "webpage"
