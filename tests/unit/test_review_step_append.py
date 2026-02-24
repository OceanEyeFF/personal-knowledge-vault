"""
ReviewStep 单元测试 - Task 2 补充

覆盖 _interactive_review 交互式菜单各分支：
修改摘要、修改标签、添加评论、AI 重新生成、查看历史、_open_editor。
"""

import asyncio
import subprocess
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.storage.markdown_store import Entry
from src.storage.review_manager import ReviewItem
from src.workflow.models import WorkflowContext
from src.workflow.steps import ReviewStep


# ---------------------------------------------------------------------------
# 复用辅助函数（与 test_review_step.py 保持一致）
# ---------------------------------------------------------------------------

def _make_context(**kwargs) -> WorkflowContext:
    return WorkflowContext(initial_state=kwargs)


def _make_entry(**kwargs) -> Entry:
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
    defaults = {
        "review_id": review_id,
        "ai_generated_summary": "AI 生成的摘要文字。",
        "ai_generated_tags": "AI,测试",
        "source_type": "webpage",
        "review_status": "pending",
        "regeneration_count": 0,
    }
    defaults.update(kwargs)
    return ReviewItem(**defaults)


def _make_manager_mock(review_id: int = 1, get_item: Optional[ReviewItem] = None) -> MagicMock:
    mock = MagicMock()
    mock.create_review.return_value = review_id
    mock.get_review.return_value = get_item or _make_review_item(review_id=review_id)
    mock.approve_review.return_value = True
    mock.reject_review.return_value = True
    mock.update_user_summary.return_value = True
    mock.update_user_tags.return_value = True
    mock.add_user_comment.return_value = True
    mock.record_regeneration.return_value = True
    mock.get_history.return_value = []
    return mock


def _make_step(
    config: Optional[Dict[str, Any]] = None,
    manager_mock: Optional[MagicMock] = None,
) -> ReviewStep:
    step_config = config or {"required": True, "timeout": 600, "max_regenerations": 3}
    return ReviewStep(
        step_id="review_entry",
        config=step_config,
        review_manager=manager_mock,
    )


def _run_menu(step, manager_mock, ctx, answers_list):
    """
    辅助：运行 execute，Prompt.ask 依序返回 answers_list 中的值。
    asyncio.to_thread 直接同步调用 fn(*args)。
    """
    answer_iter = iter(answers_list)

    async def fake_to_thread(fn, *args, **kwargs):
        if callable(fn):
            return fn(*args, **kwargs)

    async def fake_wait_for(coro, timeout):
        return await coro

    def fake_prompt(*args, **kwargs):
        try:
            return next(answer_iter)
        except StopIteration:
            return "a"

    with (
        patch("src.workflow.steps.asyncio.wait_for", side_effect=fake_wait_for),
        patch("src.workflow.steps.asyncio.to_thread", side_effect=fake_to_thread),
        patch("rich.prompt.Prompt.ask", side_effect=fake_prompt),
    ):
        return asyncio.run(step.execute(ctx))


# ---------------------------------------------------------------------------
# Task 2 补充：修改摘要分支（choice == "m"）
# ---------------------------------------------------------------------------

class TestReviewStepEditSummary:
    def test_edit_summary_via_console_input(self):
        """菜单选 m → 输入方式 i → 新摘要 → update_user_summary 被调用。"""
        manager_mock = _make_manager_mock(review_id=1)
        step = _make_step(manager_mock=manager_mock)
        ctx = _make_context(entry=_make_entry())
        # Prompt.ask 调用顺序：选操作、选编辑方式、输入摘要、再选操作
        result = _run_menu(step, manager_mock, ctx, ["m", "i", "全新摘要内容", "a"])
        manager_mock.update_user_summary.assert_called_once_with(1, "全新摘要内容")
        assert result["review_status"] == "approved"

    def test_edit_summary_empty_input_keeps_original(self):
        """输入空字符串时不调用 update_user_summary。"""
        manager_mock = _make_manager_mock(review_id=1)
        step = _make_step(manager_mock=manager_mock)
        ctx = _make_context(entry=_make_entry())
        _run_menu(step, manager_mock, ctx, ["m", "i", "   ", "a"])
        manager_mock.update_user_summary.assert_not_called()


# ---------------------------------------------------------------------------
# Task 2 补充：修改标签分支（choice == "t"）
# ---------------------------------------------------------------------------

class TestReviewStepEditTags:
    def test_edit_tags_comma_separated(self):
        """输入 'AI, Python, 工程' → update_user_tags(['AI', 'Python', '工程'])。"""
        manager_mock = _make_manager_mock(review_id=1)
        step = _make_step(manager_mock=manager_mock)
        ctx = _make_context(entry=_make_entry())
        _run_menu(step, manager_mock, ctx, ["t", "AI, Python, 工程", "a"])
        manager_mock.update_user_tags.assert_called_once_with(1, ["AI", "Python", "工程"])

    def test_edit_tags_empty_input_keeps_original(self):
        """输入空字符串时不调用 update_user_tags。"""
        manager_mock = _make_manager_mock(review_id=1)
        step = _make_step(manager_mock=manager_mock)
        ctx = _make_context(entry=_make_entry())
        _run_menu(step, manager_mock, ctx, ["t", "", "a"])
        manager_mock.update_user_tags.assert_not_called()


# ---------------------------------------------------------------------------
# Task 2 补充：添加评论分支（choice == "c"）
# ---------------------------------------------------------------------------

class TestReviewStepAddComment:
    def test_add_comment_calls_manager(self):
        """菜单选 c → 输入评论 → add_user_comment 被调用。"""
        manager_mock = _make_manager_mock(review_id=1)
        step = _make_step(manager_mock=manager_mock)
        ctx = _make_context(entry=_make_entry())
        _run_menu(step, manager_mock, ctx, ["c", "这是测试评论", "a"])
        manager_mock.add_user_comment.assert_called_once_with(1, "这是测试评论")

    def test_add_comment_empty_skips(self):
        """输入空评论时不调用 add_user_comment。"""
        manager_mock = _make_manager_mock(review_id=1)
        step = _make_step(manager_mock=manager_mock)
        ctx = _make_context(entry=_make_entry())
        _run_menu(step, manager_mock, ctx, ["c", "   ", "a"])
        manager_mock.add_user_comment.assert_not_called()


# ---------------------------------------------------------------------------
# Task 2 补充：AI 重新生成分支（choice == "r"）
# ---------------------------------------------------------------------------

class TestReviewStepAiRegen:
    def test_ai_regen_calls_record_regeneration(self):
        """菜单选 r → AI 成功 → record_regeneration 被调用。"""
        manager_mock = _make_manager_mock(review_id=1)
        step = _make_step(
            config={"required": True, "timeout": 600, "max_regenerations": 3},
            manager_mock=manager_mock,
        )

        async def fake_ai_regen(*args, **kwargs):
            return "新的精简摘要", ["标签X", "标签Y"]

        answer_iter = iter(["r", "请更简洁一些", "a"])

        async def fake_to_thread(fn, *args, **kwargs):
            if callable(fn):
                return fn(*args, **kwargs)

        async def fake_wait_for(coro, timeout):
            return await coro

        ctx = _make_context(entry=_make_entry())

        with (
            patch("src.workflow.steps.asyncio.wait_for", side_effect=fake_wait_for),
            patch("src.workflow.steps.asyncio.to_thread", side_effect=fake_to_thread),
            patch("rich.prompt.Prompt.ask", side_effect=lambda *a, **kw: next(answer_iter)),
            patch.object(step, "_call_ai_regenerate", side_effect=fake_ai_regen),
        ):
            asyncio.run(step.execute(ctx))

        manager_mock.record_regeneration.assert_called_once_with(
            1, "请更简洁一些", "新的精简摘要", ["标签X", "标签Y"]
        )

    def test_ai_regen_failure_falls_back_gracefully(self):
        """AI 调用失败时不崩溃，继续菜单循环，最终通过。"""
        manager_mock = _make_manager_mock(review_id=1)
        step = _make_step(
            config={"required": True, "timeout": 600, "max_regenerations": 3},
            manager_mock=manager_mock,
        )

        async def failing_ai(*args, **kwargs):
            raise RuntimeError("API 超时")

        answer_iter = iter(["r", "指导文字", "a"])

        async def fake_to_thread(fn, *args, **kwargs):
            if callable(fn):
                return fn(*args, **kwargs)

        async def fake_wait_for(coro, timeout):
            return await coro

        ctx = _make_context(entry=_make_entry())

        with (
            patch("src.workflow.steps.asyncio.wait_for", side_effect=fake_wait_for),
            patch("src.workflow.steps.asyncio.to_thread", side_effect=fake_to_thread),
            patch("rich.prompt.Prompt.ask", side_effect=lambda *a, **kw: next(answer_iter)),
            patch.object(step, "_call_ai_regenerate", side_effect=failing_ai),
        ):
            result = asyncio.run(step.execute(ctx))

        assert result["review_status"] == "approved"
        manager_mock.record_regeneration.assert_not_called()

    def test_ai_regen_exceeds_max_regenerations(self):
        """已达到 max_regenerations 时不调用 AI，继续循环。"""
        saturated = _make_review_item(review_id=1, regeneration_count=3)
        manager_mock = _make_manager_mock(review_id=1, get_item=saturated)
        step = _make_step(
            config={"required": True, "timeout": 600, "max_regenerations": 3},
            manager_mock=manager_mock,
        )
        ctx = _make_context(entry=_make_entry())
        # 选 r（达到上限，应提示不可用） → 选 a
        result = _run_menu(step, manager_mock, ctx, ["r", "a"])
        manager_mock.record_regeneration.assert_not_called()
        assert result["review_status"] == "approved"

    def test_ai_regen_empty_prompt_skips(self):
        """输入空 prompt 时不调用 AI。"""
        manager_mock = _make_manager_mock(review_id=1)
        step = _make_step(
            config={"required": True, "timeout": 600, "max_regenerations": 3},
            manager_mock=manager_mock,
        )
        ctx = _make_context(entry=_make_entry())
        # 选 r → 输入空 prompt（应跳过 AI 调用） → 选 a
        result = _run_menu(step, manager_mock, ctx, ["r", "   ", "a"])
        manager_mock.record_regeneration.assert_not_called()
        assert result["review_status"] == "approved"


# ---------------------------------------------------------------------------
# Task 2 补充：查看历史分支（choice == "h"）
# ---------------------------------------------------------------------------

class TestReviewStepViewHistory:
    def test_view_history_calls_get_history(self):
        """菜单选 h → get_history 被调用。"""
        manager_mock = _make_manager_mock(review_id=1)
        step = _make_step(manager_mock=manager_mock)
        ctx = _make_context(entry=_make_entry())
        _run_menu(step, manager_mock, ctx, ["h", "a"])
        manager_mock.get_history.assert_called_with(1)

    def test_view_history_with_records_no_crash(self):
        """查看历史有记录时不抛出异常。"""
        manager_mock = _make_manager_mock(review_id=1)
        manager_mock.get_history.return_value = [
            {"history_id": 1, "action": "create", "operator": "system",
             "created_at": "2026-01-01", "details": {"note": "init"}}
        ]
        step = _make_step(manager_mock=manager_mock)
        ctx = _make_context(entry=_make_entry())
        result = _run_menu(step, manager_mock, ctx, ["h", "a"])
        assert result["review_status"] == "approved"


# ---------------------------------------------------------------------------
# Task 2 补充：_open_editor 静态方法
# ---------------------------------------------------------------------------

class TestOpenEditor:
    def test_open_editor_success_returns_content(self, tmp_path):
        """编辑器成功返回时，返回修改后的文件内容。"""
        edited_content = "编辑器修改后的内容"

        def fake_subprocess_call(cmd):
            path = cmd[1]
            with open(path, "w", encoding="utf-8") as f:
                f.write(edited_content)
            return 0

        with patch("subprocess.call", side_effect=fake_subprocess_call):
            result = ReviewStep._open_editor("初始内容")

        assert result == edited_content

    def test_open_editor_failure_returns_none(self):
        """编辑器返回非零退出码时返回 None。"""
        with patch("subprocess.call", return_value=1):
            result = ReviewStep._open_editor("初始内容")
        assert result is None

    def test_open_editor_exception_returns_none(self):
        """编辑器抛出异常（如找不到可执行文件）时返回 None。"""
        with patch("subprocess.call", side_effect=OSError("editor not found")):
            result = ReviewStep._open_editor("初始内容")
        assert result is None
