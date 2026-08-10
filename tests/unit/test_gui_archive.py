"""ArchiveView pytest-qt 单元测试。

覆盖 M11 验收标准:
- ArchiveView UI 结构（双标签页、输入控件、按钮、进度区、结果区）
- ArchiveViewModel 验证逻辑和信号
- View + ViewModel 协作集成

测试策略：Mock ArchiveViewModel（及其依赖的 WorkflowEngine / TextFallbackProcessor），
验证 UI 组件结构和交互逻辑。
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from unittest.mock import patch, MagicMock, AsyncMock

import pytest
from PySide6.QtWidgets import QTabWidget, QLineEdit, QPlainTextEdit, QPushButton

# 确保项目根目录在 sys.path 中
_PROJECT_ROOT = Path(__file__).parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


# ============================================================
# Mock 数据
# ============================================================

MOCK_STATS = {
    "total_entries": 10,
    "by_source_type": [("wechat", 5), ("zhihu", 3), ("text", 2)],
    "top_tags": [{"name": "AI", "count": 5}, {"name": "Python", "count": 3}],
}

_MISSING_TERMINAL = object()


def _completed_archive_data(
    knowledge_id: int,
    *,
    title: str = "archived",
    status: str = "ready",
    **overrides,
):
    """Build the exact W1 storage projection consumed by GUI completion."""

    data = {
        "knowledge_id": knowledge_id,
        "title": title,
        "file_path": f"C:/synthetic-vault/{knowledge_id}.md",
        "status": status,
        "operation_id": f"{knowledge_id:032x}",
        "core_committed": True,
        "do_not_retry": True,
        "repair_actions": (
            ["rebuild_vectors_for_entry"] if status == "degraded" else []
        ),
    }
    data.update(overrides)
    return data


# ============================================================
# Fixtures
# ============================================================

@pytest.fixture
def mock_store():
    """创建 Mock SQLiteStore。"""
    store = MagicMock()
    store.get_statistics.return_value = MOCK_STATS
    store.get_all_tags_with_count.return_value = []
    store.list_entries.return_value = []
    store.count_entries.return_value = 0
    return store


@pytest.fixture
def archive_view(qtbot, mock_store):
    """创建带有 Mock 依赖的 ArchiveView。

    Mock ArchiveViewModel 的依赖（validate_url_security_result / validate_text_length），
    使用 yield 确保 mock 上下文在整个测试期间保持活跃。
    """
    with patch("src.gui.stores.get_sqlite_store", return_value=mock_store), \
         patch("src.gui.stores.get_bm25_retriever", return_value=MagicMock()), \
         patch("src.gui.stores.get_markdown_store", return_value=MagicMock()):
        from src.gui.views.archive_view import ArchiveView
        view = ArchiveView()
        qtbot.addWidget(view)
        yield view


@pytest.fixture
def archive_vm():
    """创建独立的 ArchiveViewModel 实例（不绑定 View）。"""
    with patch("src.gui.viewmodels.archive_viewmodel.validate_url_security_result", return_value=None), \
         patch(
             "src.gui.viewmodels.archive_viewmodel.validate_text_length",
             return_value=(True, ""),
         ):
        from src.gui.viewmodels.archive_viewmodel import ArchiveViewModel
        vm = ArchiveViewModel()
        yield vm


# ============================================================
# UI 结构验证
# ============================================================

class TestArchiveViewStructure:
    """验证 ArchiveView UI 结构。"""

    def test_has_tab_widget(self, archive_view):
        """包含 QTabWidget 且有 2 个标签页。"""
        tab = archive_view._tab_widget
        assert isinstance(tab, QTabWidget)
        assert tab.count() == 2

    def test_has_url_input(self, archive_view):
        """包含 URL 输入框。"""
        assert archive_view._url_input is not None
        assert isinstance(archive_view._url_input, QLineEdit)

    def test_has_text_input(self, archive_view):
        """包含文本输入区域。"""
        assert archive_view._text_input is not None
        assert isinstance(archive_view._text_input, QPlainTextEdit)

    def test_has_archive_buttons(self, archive_view):
        """包含 URL 归档和文本归档两个按钮。"""
        assert archive_view._archive_url_btn is not None
        assert isinstance(archive_view._archive_url_btn, QPushButton)
        assert archive_view._archive_text_btn is not None
        assert isinstance(archive_view._archive_text_btn, QPushButton)

    def test_progress_initially_hidden(self, archive_view):
        """进度区域初始状态为隐藏。"""
        assert not archive_view._progress_area.isVisible()

    def test_result_initially_hidden(self, archive_view):
        """结果区域初始状态为隐藏。"""
        assert not archive_view._result_area.isVisible()

    def test_url_input_has_placeholder(self, archive_view):
        """URL 输入框有占位提示文本。"""
        placeholder = archive_view._url_input.placeholderText()
        assert len(placeholder) > 0

    def test_text_input_has_placeholder(self, archive_view):
        """文本输入区域有占位提示文本。"""
        placeholder = archive_view._text_input.placeholderText()
        assert len(placeholder) > 0

    def test_has_title_input(self, archive_view):
        """包含标题输入框（文本归档模式）。"""
        assert archive_view._title_input is not None
        assert isinstance(archive_view._title_input, QLineEdit)

    def test_has_navigate_button(self, archive_view):
        """包含"前往浏览"按钮。"""
        assert archive_view._navigate_btn is not None
        assert isinstance(archive_view._navigate_btn, QPushButton)


# ============================================================
# ArchiveViewModel 验证逻辑和信号
# ============================================================

class TestArchiveViewModel:
    """验证 ArchiveViewModel 验证逻辑和信号。"""

    def test_empty_url_emits_error(self, archive_vm, qtbot):
        """空 URL 字符串触发 error_occurred 信号。"""
        with qtbot.waitSignal(archive_vm.error_occurred, timeout=1000) as blocker:
            archive_vm.archive_url("")
        assert "空" in blocker.args[0] or "URL" in blocker.args[0]

    def test_empty_text_emits_error(self, archive_vm, qtbot):
        """空文本字符串触发 error_occurred 信号。"""
        with qtbot.waitSignal(archive_vm.error_occurred, timeout=1000) as blocker:
            archive_vm.archive_text("")
        assert "空" in blocker.args[0] or "文本" in blocker.args[0]

    def test_whitespace_only_url_emits_error(self, archive_vm, qtbot):
        """纯空白 URL 触发 error_occurred 信号。"""
        with qtbot.waitSignal(archive_vm.error_occurred, timeout=1000) as blocker:
            archive_vm.archive_url("   ")
        assert "空" in blocker.args[0] or "URL" in blocker.args[0]

    def test_whitespace_only_text_emits_error(self, archive_vm, qtbot):
        """纯空白文本触发 error_occurred 信号。"""
        with qtbot.waitSignal(archive_vm.error_occurred, timeout=1000) as blocker:
            archive_vm.archive_text("   ")
        assert "空" in blocker.args[0] or "文本" in blocker.args[0]

    def test_state_changes_on_url_archive(self, archive_vm, qtbot):
        """URL 归档触发 state_changed 到 "running"。"""
        with patch.object(archive_vm, "_start_worker") as mock_start:
            archive_vm.archive_url("https://example.com")
            mock_start.assert_called_once_with("url", {"url": "https://example.com"})

    def test_state_changes_on_text_archive(self, archive_vm, qtbot):
        """文本归档触发 state_changed 到 "running"。"""
        with patch.object(archive_vm, "_start_worker") as mock_start:
            archive_vm.archive_text("测试文本内容", "测试标题")
            mock_start.assert_called_once_with("text", {"text": "测试文本内容", "title": "测试标题"})


# ============================================================
# View + ViewModel 协作
# ============================================================

class TestArchiveViewIntegration:
    """验证 View + ViewModel 协作。"""

    def test_url_archive_disables_buttons_during_run(self, archive_view):
        """URL 归档运行时按钮被禁用。"""
        # 模拟状态变更到 running
        archive_view._on_state_changed("running")
        assert not archive_view._archive_url_btn.isEnabled()
        assert not archive_view._archive_text_btn.isEnabled()
        # 使用 isVisibleTo(parent) 替代 isVisible()，因为视图未 show()
        assert not archive_view._progress_area.isHidden()

    def test_state_success_enables_buttons(self, archive_view):
        """归档成功后按钮恢复可用。"""
        archive_view._on_state_changed("running")
        archive_view._on_state_changed("success")
        assert archive_view._archive_url_btn.isEnabled()
        assert archive_view._archive_text_btn.isEnabled()
        assert not archive_view._progress_area.isVisible()

    def test_state_degraded_enables_buttons(self, archive_view):
        """降级是完成终态，不应让归档按钮保持锁定。"""
        archive_view._on_state_changed("running")
        archive_view._on_state_changed("degraded")
        assert archive_view._archive_url_btn.isEnabled()
        assert archive_view._archive_text_btn.isEnabled()
        assert not archive_view._progress_area.isVisible()

    def test_state_error_enables_buttons(self, archive_view):
        """归档失败后按钮恢复可用。"""
        archive_view._on_state_changed("running")
        archive_view._on_state_changed("error")
        assert archive_view._archive_url_btn.isEnabled()
        assert archive_view._archive_text_btn.isEnabled()
        assert not archive_view._progress_area.isVisible()

    def test_text_archive_shows_progress(self, archive_view):
        """文本归档启动时显示进度区域。"""
        archive_view._on_state_changed("running")
        assert not archive_view._progress_area.isHidden()
        assert archive_view._result_area.isHidden()

    def test_result_ready_shows_result_area(self, archive_view):
        """归档结果就绪时显示结果区域。"""
        data = {
            "title": "测试文章",
            "knowledge_id": "kid-001",
            "file_path": "text/2026/02/test.md",
        }
        archive_view._on_result_ready(data)
        assert not archive_view._result_area.isHidden()
        assert "测试文章" in archive_view._result_title_label.text()
        assert "kid-001" in archive_view._result_kid_label.text()

    def test_result_log_does_not_include_title_crlf_secret_canary(
        self, archive_view, caplog
    ):
        canary = "Remote title\r\napi_key=SECRET-CANARY"
        caplog.set_level(logging.INFO, logger="pkv.gui.archive")

        archive_view._on_result_ready({
            "title": canary,
            "knowledge_id": 77,
            "file_path": "text/ok.md",
        })

        assert "SECRET-CANARY" not in caplog.text
        assert "api_key" not in caplog.text
        assert "Remote title" not in caplog.text
        assert "knowledge_id=77" in caplog.text

    def test_result_ready_degraded_warns_visibly(self, archive_view):
        """DEGRADED 仍是核心成功，但 GUI 必须可见地警告辅助索引需要修复。"""
        data = {
            "title": "降级条目",
            "knowledge_id": 5,
            "file_path": "text/x.md",
            "status": "degraded",
            "repair_actions": ["rebuild_vectors_for_entry"],
            "core_committed": True,
            "do_not_retry": True,
        }
        with patch("PySide6.QtWidgets.QMessageBox.warning") as warning_mock:
            archive_view._on_result_ready(data)
        assert not archive_view._result_area.isHidden()
        assert "降级" in archive_view._result_title_label.text()
        warning_mock.assert_called_once()
        message = warning_mock.call_args[0][2]
        assert "辅助索引需要修复" in message
        assert "rebuild_vectors_for_entry" in message
        assert "请勿盲目重试" in message
        assert archive_view._result_warning_label.isVisibleTo(archive_view)

    def test_workflow_degraded_warning_is_visible_and_redacted(self, archive_view):
        """非存储步骤降级也必须呈现，但不得回显底层异常详情。"""
        data = {
            "title": "可用条目",
            "knowledge_id": 9,
            "file_path": "text/ok.md",
            "status": "ready",
            "workflow_terminal": "degraded",
            "workflow_issues": [{
                "code": "provider_unavailable",
                "message": "api_key=secret C:/private/config.yaml",
                "severity": "warning",
                "recoverable": True,
                "stage": "ai_analyze",
            }],
        }
        with patch("PySide6.QtWidgets.QMessageBox.warning") as warning_mock:
            archive_view._on_result_ready(data)

        warning = archive_view._result_warning_label.text()
        assert "降级" in archive_view._result_title_label.text()
        assert "部分可选工作流步骤未完成" in warning
        assert "provider_unavailable" in warning
        assert "secret" not in warning
        assert "private" not in warning
        warning_mock.assert_called_once()

    def test_error_shows_result_area_with_failure(self, archive_view):
        """归档错误时结果区域显示失败信息。"""
        with patch("PySide6.QtWidgets.QMessageBox.warning"):
            archive_view._on_error("网络连接失败")
        assert not archive_view._result_area.isHidden()
        assert "失败" in archive_view._result_title_label.text()

    def test_structured_fatal_keeps_safe_diagnostics_without_raw_message(
        self, archive_view
    ):
        failure = {
            "terminal": "error",
            "code": "storage_repair_required",
            "stage": "index_commit",
            "recoverable": False,
            "safe_message": "核心存储已提交，需要修复。请勿盲目重试！",
            "issues": [{"message": "token=secret C:/private/db.sqlite"}],
            "operation_id": "0123456789abcdef0123456789abcdef",
            "repair_actions": ["repair_operation_journal"],
            "do_not_retry": True,
        }
        with patch("PySide6.QtWidgets.QMessageBox.warning") as warning_mock:
            archive_view._on_error(failure)

        public_text = " ".join([
            archive_view._result_kid_label.text(),
            archive_view._result_path_label.text(),
            archive_view._result_warning_label.text(),
            warning_mock.call_args[0][2],
        ])
        assert "storage_repair_required" in public_text
        assert "index_commit" in public_text
        assert "0123456789abcdef0123456789abcdef" in public_text
        assert "repair_operation_journal" in public_text
        assert "请勿盲目重试" in public_text
        assert "token=secret" not in public_text
        assert "private" not in public_text

    def test_empty_url_shows_warning(self, archive_view):
        """空 URL 触发警告对话框。"""
        archive_view._url_input.clear()
        with patch("PySide6.QtWidgets.QMessageBox.warning") as mock_warn:
            archive_view._on_archive_url()
            mock_warn.assert_called_once()

    def test_empty_text_shows_warning(self, archive_view):
        """空文本触发警告对话框。"""
        archive_view._text_input.clear()
        with patch("PySide6.QtWidgets.QMessageBox.warning") as mock_warn:
            archive_view._on_archive_text()
            mock_warn.assert_called_once()

    def test_navigate_to_browser_signal(self, archive_view, qtbot):
        """点击"前往浏览"按钮发射 navigate_to_browser 信号。"""
        with qtbot.waitSignal(archive_view.navigate_to_browser, timeout=1000):
            archive_view._navigate_btn.click()


# ============================================================
# ArchiveWorker 崩溃协议终态 (committed-needs-repair)
# ============================================================


class TestArchiveWorkerCommittedRepair:
    """已提交但需修复的归档失败必须携带 do-not-retry 警告。"""

    def test_committed_failure_emits_do_not_retry_warning(self, qtbot, monkeypatch):
        """核心存储已提交但终态日志失败的归档失败携带显式 do-not-retry。"""
        from src.gui.viewmodels import archive_viewmodel as avm
        from src.gui.viewmodels.archive_viewmodel import ArchiveWorker

        monkeypatch.setattr(avm, "validate_url_security_result", lambda url: None)
        mock_result = MagicMock()
        mock_result.success = False
        mock_result.terminal = "error"
        mock_result.errors = ["storage_repair_required: 核心存储已提交但操作日志更新失败"]
        mock_result.data = {
            "status": "repair_required",
            "operation_id": "0123456789abcdef0123456789abcdef",
            "core_committed": True,
            "do_not_retry": True,
            "repair_actions": ["repair_operation_journal"],
        }
        engine = MagicMock()
        engine.execute_async = AsyncMock(return_value=mock_result)

        worker = ArchiveWorker("url", {"url": "https://example.com/x"})
        with patch("src.workflow.engine.WorkflowEngine", return_value=engine):
            with qtbot.waitSignal(worker.finished_err, timeout=2000) as blocker:
                asyncio.run(worker._execute_url())

        message = blocker.args[0]
        assert "请勿盲目重试" in message
        assert "0123456789abcdef0123456789abcdef" in message
        assert "repair_operation_journal" in message


class TestArchiveWorkerW2Contract:
    """GUI 非交互入口必须冻结 skip seam 并传播工作流三终态。"""

    @pytest.mark.parametrize("mode", ["url", "text"])
    @pytest.mark.parametrize(
        ("case", "terminal", "success", "data_kind", "expected_stage"),
        [
            ("success-false", "success", False, "valid", "workflow_terminal"),
            ("error-true", "error", True, "valid", "workflow_terminal"),
            ("success-int", "success", 1, "valid", "workflow_terminal"),
            ("non-dict-data", "success", True, "non-dict", "workflow_result"),
            ("missing-kid", "success", True, "missing", "workflow_result"),
            ("zero-kid", "success", True, "zero", "workflow_result"),
            ("bool-kid", "success", True, "bool", "workflow_result"),
        ],
    )
    def test_completed_result_contract_violation_never_emits_success(
        self,
        qtbot,
        monkeypatch,
        mode,
        case,
        terminal,
        success,
        data_kind,
        expected_stage,
    ):
        from src.gui.viewmodels import archive_viewmodel as avm
        from src.gui.viewmodels.archive_viewmodel import ArchiveWorker

        canary = f"{mode}-{case}-CANARY\r\napi_key=secret C:/private/vault.db"
        if data_kind == "valid":
            data = _completed_archive_data(993, title=canary)
        elif data_kind == "non-dict":
            data = canary
        elif data_kind == "missing":
            data = _completed_archive_data(993, title=canary)
            data.pop("knowledge_id")
        elif data_kind == "zero":
            data = _completed_archive_data(993, title=canary)
            data["knowledge_id"] = 0
        else:
            data = _completed_archive_data(993, title=canary)
            data["knowledge_id"] = True
        result = SimpleNamespace(
            terminal=terminal,
            success=success,
            data=data,
            warnings=[],
            errors=[],
            issues=[],
        )
        engine = MagicMock()
        engine.execute_async = AsyncMock(return_value=result)
        successes = []
        progress = []

        if mode == "url":
            monkeypatch.setattr(avm, "validate_url_security_result", lambda url: None)
            worker = ArchiveWorker("url", {"url": "https://example.com/x"})
            worker.finished_ok.connect(successes.append)
            worker.progress_text.connect(progress.append)
            with patch("src.workflow.engine.WorkflowEngine", return_value=engine):
                with qtbot.waitSignal(
                    worker.finished_failure,
                    timeout=2000,
                ) as blocker:
                    asyncio.run(worker._execute_url())
        else:
            monkeypatch.setattr(avm, "validate_text_length", lambda text: (True, ""))
            entry = SimpleNamespace(title="parsed", content="body")
            processor = MagicMock()
            processor.process_text = AsyncMock(return_value=entry)
            worker = ArchiveWorker("text", {"text": "body", "title": "given"})
            worker.finished_ok.connect(successes.append)
            worker.progress_text.connect(progress.append)
            with patch(
                "src.processors.text_fallback_processor.TextFallbackProcessor",
                return_value=processor,
            ), patch("src.workflow.engine.WorkflowEngine", return_value=engine):
                with qtbot.waitSignal(
                    worker.finished_failure,
                    timeout=2000,
                ) as blocker:
                    asyncio.run(worker._execute_text())

        payload = blocker.args[0]
        assert successes == []
        assert not any(message.startswith("归档完成") for message in progress)
        assert payload["terminal"] == "error"
        assert payload["code"] == "workflow_step_failed"
        assert payload["stage"] == expected_stage
        assert payload["recoverable"] is False
        assert "CANARY" not in repr(payload)
        assert "secret" not in repr(payload)
        assert "private" not in repr(payload)

    @pytest.mark.parametrize("mode", ["url", "text"])
    @pytest.mark.parametrize("status", ["repair_required", "rejected"])
    def test_completed_terminal_cannot_hide_fatal_storage_status(
        self, qtbot, monkeypatch, mode, status
    ):
        from src.gui.viewmodels import archive_viewmodel as avm
        from src.gui.viewmodels.archive_viewmodel import ArchiveWorker
        from src.workflow.models import WorkflowResult

        canary = f"{mode}-{status}-CANARY\r\napi_key=secret C:/private/db"
        result = WorkflowResult(
            success=True,
            terminal="success",
            data={
                "knowledge_id": 994,
                "title": canary,
                "status": status,
                "operation_id": "0123456789abcdef0123456789abcdef",
                "core_committed": status == "repair_required",
                "repair_actions": ["repair_operation_journal"],
            },
        )
        engine = MagicMock()
        engine.execute_async = AsyncMock(return_value=result)
        successes = []

        if mode == "url":
            monkeypatch.setattr(avm, "validate_url_security_result", lambda url: None)
            worker = ArchiveWorker("url", {"url": "https://example.com/x"})
            worker.finished_ok.connect(successes.append)
            with patch("src.workflow.engine.WorkflowEngine", return_value=engine):
                with qtbot.waitSignal(
                    worker.finished_failure,
                    timeout=2000,
                ) as blocker:
                    asyncio.run(worker._execute_url())
        else:
            monkeypatch.setattr(avm, "validate_text_length", lambda text: (True, ""))
            entry = SimpleNamespace(title="parsed", content="body")
            processor = MagicMock()
            processor.process_text = AsyncMock(return_value=entry)
            worker = ArchiveWorker("text", {"text": "body", "title": "given"})
            worker.finished_ok.connect(successes.append)
            with patch(
                "src.processors.text_fallback_processor.TextFallbackProcessor",
                return_value=processor,
            ), patch("src.workflow.engine.WorkflowEngine", return_value=engine):
                with qtbot.waitSignal(
                    worker.finished_failure,
                    timeout=2000,
                ) as blocker:
                    asyncio.run(worker._execute_text())

        payload = blocker.args[0]
        assert successes == []
        assert payload["terminal"] == "error"
        assert payload["status"] == status
        if status == "repair_required":
            assert payload["code"] == "storage_repair_required"
            assert payload["do_not_retry"] is True
            assert "请勿盲目重试" in payload["safe_message"]
        else:
            assert payload["code"] == "workflow_step_failed"
        assert "CANARY" not in repr(payload)
        assert "secret" not in repr(payload)
        assert "private" not in repr(payload)

    @pytest.mark.parametrize("mode", ["url", "text"])
    @pytest.mark.parametrize("diagnostic", ["warning", "error-issue"])
    def test_success_terminal_with_diagnostics_fails_closed(
        self, qtbot, monkeypatch, mode, diagnostic
    ):
        from src.gui.viewmodels import archive_viewmodel as avm
        from src.gui.viewmodels.archive_viewmodel import ArchiveWorker

        canary = f"{diagnostic}-CANARY\r\nAuthorization: Bearer secret"
        warnings = [canary] if diagnostic == "warning" else []
        issues = (
            [{
                "code": "workflow_step_failed",
                "message": canary,
                "severity": "error",
                "recoverable": False,
            }]
            if diagnostic == "error-issue"
            else []
        )
        result = SimpleNamespace(
            success=True,
            terminal="success",
            data=_completed_archive_data(995, title=canary),
            errors=[],
            warnings=warnings,
            issues=issues,
        )
        engine = MagicMock()
        engine.execute_async = AsyncMock(return_value=result)
        successes = []

        if mode == "url":
            monkeypatch.setattr(avm, "validate_url_security_result", lambda url: None)
            worker = ArchiveWorker("url", {"url": "https://example.com/x"})
            worker.finished_ok.connect(successes.append)
            with patch("src.workflow.engine.WorkflowEngine", return_value=engine):
                with qtbot.waitSignal(
                    worker.finished_failure,
                    timeout=2000,
                ) as blocker:
                    asyncio.run(worker._execute_url())
        else:
            monkeypatch.setattr(avm, "validate_text_length", lambda text: (True, ""))
            entry = SimpleNamespace(title="parsed", content="body")
            processor = MagicMock()
            processor.process_text = AsyncMock(return_value=entry)
            worker = ArchiveWorker("text", {"text": "body", "title": "given"})
            worker.finished_ok.connect(successes.append)
            with patch(
                "src.processors.text_fallback_processor.TextFallbackProcessor",
                return_value=processor,
            ), patch("src.workflow.engine.WorkflowEngine", return_value=engine):
                with qtbot.waitSignal(
                    worker.finished_failure,
                    timeout=2000,
                ) as blocker:
                    asyncio.run(worker._execute_text())

        payload = blocker.args[0]
        assert successes == []
        assert payload["code"] == "workflow_step_failed"
        assert payload["stage"] == "workflow_terminal"
        assert "CANARY" not in repr(payload)
        assert "secret" not in repr(payload)

    @pytest.mark.parametrize("mode", ["url", "text"])
    @pytest.mark.parametrize(
        ("case", "terminal", "status"),
        [
            ("mapping-data", "success", "ready"),
            ("dict-subclass-data", "success", "ready"),
            ("non-string-key", "success", "ready"),
            ("missing-title", "success", "ready"),
            ("title-duck", "success", "ready"),
            ("title-str-subclass", "success", "ready"),
            ("missing-file-path", "success", "ready"),
            ("file-path-object", "success", "ready"),
            ("file-path-str-subclass", "success", "ready"),
            ("empty-file-path", "success", "ready"),
            ("missing-operation-id", "success", "ready"),
            ("invalid-operation-id", "success", "ready"),
            ("operation-id-str-subclass", "success", "ready"),
            ("missing-core-committed", "success", "ready"),
            ("false-core-committed", "success", "ready"),
            ("truthy-core-committed", "success", "ready"),
            ("missing-do-not-retry", "success", "ready"),
            ("false-do-not-retry", "success", "ready"),
            ("truthy-do-not-retry", "success", "ready"),
            ("tuple-repair-actions", "success", "ready"),
            ("list-subclass-repair-actions", "success", "ready"),
            ("unknown-repair-action", "success", "ready"),
            ("repair-action-str-subclass", "degraded", "degraded"),
            ("duplicate-repair-action", "degraded", "degraded"),
            ("ready-with-repair", "success", "ready"),
            ("degraded-without-repair", "degraded", "degraded"),
            ("success-storage-mismatch", "success", "degraded"),
        ],
    )
    def test_completed_projection_malformed_fails_before_completion_publish(
        self,
        qtbot,
        monkeypatch,
        caplog,
        mode,
        case,
        terminal,
        status,
    ):
        from src.gui.viewmodels import archive_viewmodel as avm
        from src.gui.viewmodels.archive_viewmodel import ArchiveWorker

        canary = f"{case}-CANARY\r\napi_key=secret C:/private/vault.db"

        class SecretDuck:
            def __str__(self):
                return canary

        class SecretStr(str):
            pass

        class DictSubclass(dict):
            pass

        class ListSubclass(list):
            pass

        data = _completed_archive_data(997, title="safe", status=status)
        data["internal_secret"] = canary
        if case == "mapping-data":
            data = MappingProxyType(data)
        elif case == "dict-subclass-data":
            data = DictSubclass(data)
        elif case == "non-string-key":
            data[SecretDuck()] = canary
        elif case == "missing-title":
            data.pop("title")
        elif case == "title-duck":
            data["title"] = SecretDuck()
        elif case == "title-str-subclass":
            data["title"] = SecretStr(canary)
        elif case == "missing-file-path":
            data.pop("file_path")
        elif case == "file-path-object":
            data["file_path"] = Path("C:/synthetic-vault/997.md")
        elif case == "file-path-str-subclass":
            data["file_path"] = SecretStr(canary)
        elif case == "empty-file-path":
            data["file_path"] = ""
        elif case == "missing-operation-id":
            data.pop("operation_id")
        elif case == "invalid-operation-id":
            data["operation_id"] = canary
        elif case == "operation-id-str-subclass":
            data["operation_id"] = SecretStr("0" * 32)
        elif case == "missing-core-committed":
            data.pop("core_committed")
        elif case == "false-core-committed":
            data["core_committed"] = False
        elif case == "truthy-core-committed":
            data["core_committed"] = 1
        elif case == "missing-do-not-retry":
            data.pop("do_not_retry")
        elif case == "false-do-not-retry":
            data["do_not_retry"] = False
        elif case == "truthy-do-not-retry":
            data["do_not_retry"] = "true"
        elif case == "tuple-repair-actions":
            data["repair_actions"] = ()
        elif case == "list-subclass-repair-actions":
            data["repair_actions"] = ListSubclass()
        elif case == "unknown-repair-action":
            data["repair_actions"] = [canary]
        elif case == "repair-action-str-subclass":
            data["repair_actions"] = [SecretStr("rebuild_vectors_for_entry")]
        elif case == "duplicate-repair-action":
            data["repair_actions"] = [
                "rebuild_vectors_for_entry",
                "rebuild_vectors_for_entry",
            ]
        elif case == "ready-with-repair":
            data["repair_actions"] = ["rebuild_vectors_for_entry"]
        elif case == "degraded-without-repair":
            data["repair_actions"] = []

        warnings = [canary] if terminal == "degraded" else []
        issues = (
            [{
                "code": "provider_unavailable",
                "message": canary,
                "severity": "warning",
                "recoverable": True,
                "stage": "ai_analyze",
            }]
            if terminal == "degraded"
            else []
        )
        result = SimpleNamespace(
            success=True,
            terminal=terminal,
            data=data,
            errors=[],
            warnings=warnings,
            issues=issues,
        )
        engine = MagicMock()
        engine.execute_async = AsyncMock(return_value=result)
        successes = []
        progress = []
        worker = ArchiveWorker(
            mode,
            {"url": "https://example.com/x"}
            if mode == "url"
            else {"text": "body", "title": "given"},
        )
        worker.finished_ok.connect(successes.append)
        worker.progress_text.connect(progress.append)
        caplog.set_level(logging.INFO, logger="pkv.gui.viewmodels.archive")

        if mode == "url":
            monkeypatch.setattr(avm, "validate_url_security_result", lambda url: None)
            context = patch("src.workflow.engine.WorkflowEngine", return_value=engine)
            with context:
                with qtbot.waitSignal(
                    worker.finished_failure,
                    timeout=2000,
                ) as blocker:
                    asyncio.run(worker._execute_url())
        else:
            monkeypatch.setattr(avm, "validate_text_length", lambda text: (True, ""))
            entry = SimpleNamespace(title="parsed", content="body")
            processor = MagicMock()
            processor.process_text = AsyncMock(return_value=entry)
            with patch(
                "src.processors.text_fallback_processor.TextFallbackProcessor",
                return_value=processor,
            ), patch("src.workflow.engine.WorkflowEngine", return_value=engine):
                with qtbot.waitSignal(
                    worker.finished_failure,
                    timeout=2000,
                ) as blocker:
                    asyncio.run(worker._execute_text())

        payload = blocker.args[0]
        assert successes == []
        assert not any(message.startswith("归档完成") for message in progress)
        assert payload["terminal"] == "error"
        assert payload["code"] == "workflow_step_failed"
        assert payload["stage"] == "workflow_result"
        assert canary not in repr(payload)
        assert canary not in caplog.text

    @pytest.mark.parametrize("mode", ["url", "text"])
    def test_completed_projection_whitelists_public_fields(
        self, qtbot, monkeypatch, caplog, mode
    ):
        from src.gui.viewmodels import archive_viewmodel as avm
        from src.gui.viewmodels.archive_viewmodel import ArchiveWorker
        from src.workflow.models import WorkflowResult

        canary = "RAW-STATE-CANARY\r\napi_key=secret C:/private/input.txt"
        data = _completed_archive_data(998, title="safe title")
        data.update({
            "url": canary,
            "content": canary,
            "entry": {"secret": canary},
            "storage_errors": [{"message": canary}],
        })
        result = WorkflowResult(success=True, terminal="success", data=data)
        engine = MagicMock()
        engine.execute_async = AsyncMock(return_value=result)
        worker = ArchiveWorker(
            mode,
            {"url": "https://example.com/x"}
            if mode == "url"
            else {"text": "body", "title": "given"},
        )
        caplog.set_level(logging.INFO, logger="pkv.gui.viewmodels.archive")

        if mode == "url":
            monkeypatch.setattr(avm, "validate_url_security_result", lambda url: None)
            with patch("src.workflow.engine.WorkflowEngine", return_value=engine):
                with qtbot.waitSignal(worker.finished_ok, timeout=2000) as blocker:
                    asyncio.run(worker._execute_url())
        else:
            monkeypatch.setattr(avm, "validate_text_length", lambda text: (True, ""))
            entry = SimpleNamespace(title="parsed", content="body")
            processor = MagicMock()
            processor.process_text = AsyncMock(return_value=entry)
            with patch(
                "src.processors.text_fallback_processor.TextFallbackProcessor",
                return_value=processor,
            ), patch("src.workflow.engine.WorkflowEngine", return_value=engine):
                with qtbot.waitSignal(worker.finished_ok, timeout=2000) as blocker:
                    asyncio.run(worker._execute_text())

        payload = blocker.args[0]
        assert set(payload) == {
            "knowledge_id",
            "title",
            "file_path",
            "status",
            "operation_id",
            "core_committed",
            "do_not_retry",
            "repair_actions",
            "workflow_terminal",
            "workflow_warnings",
            "workflow_issues",
        }
        assert payload["workflow_terminal"] == "success"
        assert payload["core_committed"] is True
        assert payload["do_not_retry"] is True
        assert type(payload["repair_actions"]) is list
        assert canary not in repr(payload)
        assert canary not in caplog.text

    def test_completion_diagnostics_changed_after_terminal_check_fail_closed(
        self, qtbot, monkeypatch, caplog
    ):
        from src.gui.viewmodels import archive_viewmodel as avm
        from src.gui.viewmodels.archive_viewmodel import ArchiveWorker

        canary = "CHANGED-DIAGNOSTIC-CANARY\r\napi_key=secret"

        class ChangingResult:
            terminal = "success"
            success = True
            errors = []
            warnings = []
            data = _completed_archive_data(999, title="safe")

            def __init__(self):
                self.issue_reads = 0

            @property
            def issues(self):
                self.issue_reads += 1
                if self.issue_reads == 1:
                    return []
                return [{
                    "code": "workflow_step_failed",
                    "message": canary,
                    "severity": "warning",
                    "recoverable": True,
                }]

        engine = MagicMock()
        engine.execute_async = AsyncMock(return_value=ChangingResult())
        monkeypatch.setattr(avm, "validate_url_security_result", lambda url: None)
        worker = ArchiveWorker("url", {"url": "https://example.com/x"})
        successes = []
        progress = []
        worker.finished_ok.connect(successes.append)
        worker.progress_text.connect(progress.append)
        caplog.set_level(logging.INFO, logger="pkv.gui.viewmodels.archive")

        with patch("src.workflow.engine.WorkflowEngine", return_value=engine):
            with qtbot.waitSignal(worker.finished_failure, timeout=2000) as blocker:
                asyncio.run(worker._execute_url())

        payload = blocker.args[0]
        assert successes == []
        assert not any(message.startswith("归档完成") for message in progress)
        assert payload["stage"] == "workflow_result"
        assert canary not in repr(payload)
        assert canary not in caplog.text

    def test_text_archive_treats_existing_file_path_as_literal_text(
        self, qtbot, monkeypatch, tmp_path, caplog
    ):
        from src.gui.viewmodels import archive_viewmodel as avm
        from src.gui.viewmodels.archive_viewmodel import ArchiveWorker
        from src.workflow.models import WorkflowResult

        secret = "FILE-CONTENT-SECRET-CANARY api_key=never-read"
        secret_file = tmp_path / "external-secret.txt"
        secret_file.write_text(secret, encoding="utf-8")
        literal_text = str(secret_file)
        entry = SimpleNamespace(title="literal path", content=literal_text)
        processor = MagicMock()
        processor.process_text = AsyncMock(return_value=entry)
        processor.process = AsyncMock(side_effect=AssertionError("legacy process called"))
        processor.process_file = AsyncMock(
            side_effect=AssertionError("file import called")
        )
        engine = MagicMock()
        engine.execute_async = AsyncMock(return_value=WorkflowResult(
            success=True,
            terminal="success",
            data=_completed_archive_data(996, title="literal path"),
        ))
        monkeypatch.setattr(avm, "validate_text_length", lambda text: (True, ""))
        worker = ArchiveWorker("text", {"text": literal_text, "title": "given"})
        caplog.set_level(logging.INFO, logger="pkv.gui.viewmodels.archive")

        with patch(
            "src.processors.text_fallback_processor.TextFallbackProcessor",
            return_value=processor,
        ), patch("src.workflow.engine.WorkflowEngine", return_value=engine), patch.object(
            Path,
            "exists",
            side_effect=AssertionError("Path.exists called"),
        ) as exists, patch.object(
            Path,
            "read_text",
            side_effect=AssertionError("Path.read_text called"),
        ) as read_text:
            with qtbot.waitSignal(worker.finished_ok, timeout=2000) as blocker:
                asyncio.run(worker._execute_text())

        processor.process_text.assert_awaited_once_with(literal_text)
        processor.process.assert_not_called()
        processor.process_file.assert_not_called()
        exists.assert_not_called()
        read_text.assert_not_called()
        workflow_input = engine.execute_async.await_args.args[1]
        assert workflow_input["text"] == literal_text
        assert workflow_input["content"] == literal_text
        assert secret not in repr(workflow_input)
        assert secret not in repr(blocker.args[0])
        assert secret not in caplog.text

    @pytest.mark.parametrize(
        "terminal",
        [_MISSING_TERMINAL, None, "unexpected", 7],
        ids=["missing", "none", "unknown", "non-string"],
    )
    def test_url_invalid_terminal_fails_closed_without_success_signal(
        self, qtbot, monkeypatch, terminal
    ):
        from src.gui.viewmodels import archive_viewmodel as avm
        from src.gui.viewmodels.archive_viewmodel import ArchiveWorker

        canary = "URL-CANARY\r\nAuthorization: Bearer secret"
        result_fields = {
            "success": True,
            "data": {"knowledge_id": 991, "title": canary},
            "warnings": [canary],
            "errors": [],
            "issues": [{"message": canary}],
        }
        if terminal is not _MISSING_TERMINAL:
            result_fields["terminal"] = terminal
        result = SimpleNamespace(**result_fields)
        engine = MagicMock()
        engine.execute_async = AsyncMock(return_value=result)
        monkeypatch.setattr(avm, "validate_url_security_result", lambda url: None)
        worker = ArchiveWorker("url", {"url": "https://example.com/x"})
        successes = []
        progress = []
        legacy_failures = []
        worker.finished_ok.connect(successes.append)
        worker.progress_text.connect(progress.append)
        worker.finished_err.connect(legacy_failures.append)

        with patch("src.workflow.engine.WorkflowEngine", return_value=engine):
            with qtbot.waitSignal(worker.finished_failure, timeout=2000) as blocker:
                asyncio.run(worker._execute_url())

        payload = blocker.args[0]
        assert successes == []
        assert not any(message.startswith("归档完成") for message in progress)
        assert payload["terminal"] == "error"
        assert payload["code"] == "workflow_step_failed"
        assert payload["stage"] == "workflow_terminal"
        assert payload["recoverable"] is False
        assert legacy_failures == [payload["safe_message"]]
        assert "CANARY" not in repr(payload)
        assert "secret" not in repr(payload)

    @pytest.mark.parametrize(
        "terminal",
        [_MISSING_TERMINAL, None, "unexpected", 7],
        ids=["missing", "none", "unknown", "non-string"],
    )
    def test_text_invalid_terminal_fails_closed_without_success_signal(
        self, qtbot, monkeypatch, terminal
    ):
        from src.gui.viewmodels import archive_viewmodel as avm
        from src.gui.viewmodels.archive_viewmodel import ArchiveWorker

        canary = "TEXT-CANARY\r\napi_key=secret"
        result_fields = {
            "success": True,
            "data": {"knowledge_id": 992, "title": canary},
            "warnings": [canary],
            "errors": [],
            "issues": [{"message": canary}],
        }
        if terminal is not _MISSING_TERMINAL:
            result_fields["terminal"] = terminal
        result = SimpleNamespace(**result_fields)
        entry = SimpleNamespace(title="parsed", content="body")
        processor = MagicMock()
        processor.process_text = AsyncMock(return_value=entry)
        engine = MagicMock()
        engine.execute_async = AsyncMock(return_value=result)
        monkeypatch.setattr(avm, "validate_text_length", lambda text: (True, ""))
        worker = ArchiveWorker("text", {"text": "body", "title": "given"})
        successes = []
        progress = []
        legacy_failures = []
        worker.finished_ok.connect(successes.append)
        worker.progress_text.connect(progress.append)
        worker.finished_err.connect(legacy_failures.append)

        with patch(
            "src.processors.text_fallback_processor.TextFallbackProcessor",
            return_value=processor,
        ), patch("src.workflow.engine.WorkflowEngine", return_value=engine):
            with qtbot.waitSignal(worker.finished_failure, timeout=2000) as blocker:
                asyncio.run(worker._execute_text())

        payload = blocker.args[0]
        assert successes == []
        assert not any(message.startswith("归档完成") for message in progress)
        assert payload["terminal"] == "error"
        assert payload["code"] == "workflow_step_failed"
        assert payload["stage"] == "workflow_terminal"
        assert payload["recoverable"] is False
        assert legacy_failures == [payload["safe_message"]]
        assert "CANARY" not in repr(payload)
        assert "secret" not in repr(payload)

    def test_repair_required_prefers_fatal_storage_issue_over_earlier_warning(self):
        from src.gui.viewmodels.archive_viewmodel import (
            _failure_payload_from_result,
            sanitize_archive_failure,
        )
        from src.workflow.models import WorkflowResult

        result = WorkflowResult(
            success=False,
            terminal="error",
            data={
                "status": "repair_required",
                "operation_id": "0123456789abcdef0123456789abcdef",
                "repair_actions": ["repair_operation_journal"],
            },
            issues=[
                {
                    "code": "workflow_step_failed",
                    "message": "optional step warning",
                    "severity": "warning",
                    "recoverable": True,
                    "stage": "ai_analyze",
                },
                {
                    "code": "storage_repair_required",
                    "message": "primary committed, journal update failed",
                    "severity": "error",
                    "recoverable": False,
                    "stage": "operation_journal",
                },
            ],
        )

        payload = sanitize_archive_failure(_failure_payload_from_result(result))

        assert payload["code"] == "storage_repair_required"
        assert payload["stage"] == "operation_journal"
        assert payload["do_not_retry"] is True
        assert payload["recoverable"] is False
        assert "请勿盲目重试" in payload["safe_message"]

    @pytest.mark.parametrize(
        ("url", "expected_code"),
        [
            ("ftp://example.com/file", "url_invalid"),
            ("http://127.0.0.1/private", "ssrf_target_forbidden"),
            ("http://localhost/private", "ssrf_target_forbidden"),
        ],
    )
    def test_url_preflight_preserves_stable_security_code(
        self, qtbot, url, expected_code
    ):
        from src.gui.viewmodels.archive_viewmodel import ArchiveWorker

        worker = ArchiveWorker("url", {"url": url})
        with qtbot.waitSignal(worker.finished_failure, timeout=2000) as blocker:
            asyncio.run(worker._execute_url())

        payload = blocker.args[0]
        assert payload["code"] == expected_code
        assert payload["stage"] in {"network_policy", "url_preflight"}
        assert url not in repr(payload)

    def test_url_passes_noninteractive_flags_and_success_terminal(
        self, qtbot, monkeypatch
    ):
        from src.gui.viewmodels import archive_viewmodel as avm
        from src.gui.viewmodels.archive_viewmodel import ArchiveWorker
        from src.workflow.models import WorkflowResult

        monkeypatch.setattr(avm, "validate_url_security_result", lambda url: None)
        engine = MagicMock()
        engine.execute_async = AsyncMock(return_value=WorkflowResult(
            success=True,
            terminal="success",
            data=_completed_archive_data(1, title="ok"),
        ))
        worker = ArchiveWorker("url", {"url": "https://example.com/x"})

        with patch("src.workflow.engine.WorkflowEngine", return_value=engine):
            with qtbot.waitSignal(worker.finished_ok, timeout=2000) as blocker:
                asyncio.run(worker._execute_url())

        engine.execute_async.assert_awaited_once_with(
            "archive-url",
            {
                "url": "https://example.com/x",
                "skip_sharpen": True,
                "skip_review": True,
            },
        )
        assert blocker.args[0]["workflow_terminal"] == "success"

    def test_text_passes_noninteractive_flags_and_degraded_terminal(
        self, qtbot, monkeypatch
    ):
        from src.gui.viewmodels import archive_viewmodel as avm
        from src.gui.viewmodels.archive_viewmodel import ArchiveWorker
        from src.workflow.models import WorkflowResult

        monkeypatch.setattr(avm, "validate_text_length", lambda text: (True, ""))
        entry = MagicMock()
        entry.title = "parsed"
        entry.content = "body"
        processor = MagicMock()
        processor.process_text = AsyncMock(return_value=entry)
        engine = MagicMock()
        engine.execute_async = AsyncMock(return_value=WorkflowResult(
            success=True,
            terminal="degraded",
            data=_completed_archive_data(2, title="given", status="ready"),
            warnings=["provider raw warning"],
            issues=[{
                "code": "provider_unavailable",
                "message": "raw provider detail",
                "severity": "warning",
                "recoverable": True,
                "stage": "ai_analyze",
            }],
        ))
        worker = ArchiveWorker("text", {"text": "body", "title": "given"})

        with patch(
            "src.processors.text_fallback_processor.TextFallbackProcessor",
            return_value=processor,
        ), patch("src.workflow.engine.WorkflowEngine", return_value=engine):
            with qtbot.waitSignal(worker.finished_ok, timeout=2000) as blocker:
                asyncio.run(worker._execute_text())

        args = engine.execute_async.await_args.args
        assert args[0] == "archive-text"
        processor.process_text.assert_awaited_once_with("body")
        assert args[1]["skip_sharpen"] is True
        assert args[1]["skip_review"] is True
        assert blocker.args[0]["workflow_terminal"] == "degraded"
        assert blocker.args[0]["workflow_issues"][0]["code"] == "provider_unavailable"
        assert blocker.args[0]["workflow_issues"][0]["message"] == "归档步骤降级"
        assert blocker.args[0]["workflow_warnings"] == ["工作流存在降级警告"]
        assert "raw" not in repr(blocker.args[0])

    def test_fatal_uses_structured_signal_and_legacy_safe_string(
        self, qtbot, monkeypatch
    ):
        from src.gui.viewmodels import archive_viewmodel as avm
        from src.gui.viewmodels.archive_viewmodel import ArchiveWorker
        from src.workflow.models import WorkflowResult

        monkeypatch.setattr(avm, "validate_url_security_result", lambda url: None)
        engine = MagicMock()
        engine.execute_async = AsyncMock(return_value=WorkflowResult(
            success=False,
            terminal="error",
            data={"status": "error"},
            errors=["api_key=secret C:/private/db.sqlite"],
            issues=[{
                "code": "workflow_step_failed",
                "message": "api_key=secret C:/private/db.sqlite",
                "severity": "error",
                "recoverable": False,
                "stage": "store_entry",
            }],
        ))
        worker = ArchiveWorker("url", {"url": "https://example.com/x"})

        legacy_messages: list[str] = []
        worker.finished_err.connect(legacy_messages.append)
        with patch("src.workflow.engine.WorkflowEngine", return_value=engine):
            with qtbot.waitSignal(worker.finished_failure, timeout=2000) as blocker:
                asyncio.run(worker._execute_url())

        payload = blocker.args[0]
        assert payload["code"] == "workflow_step_failed"
        assert payload["stage"] == "store_entry"
        assert payload["issues"][0]["message"] == "归档步骤未能完成"
        assert "secret" not in repr(payload)
        assert "private" not in repr(payload)
        assert legacy_messages
        assert "secret" not in legacy_messages[0]
        assert "private" not in legacy_messages[0]

    def test_dns_resolution_code_survives_workflow_failure(
        self, qtbot, monkeypatch
    ):
        from src.gui.viewmodels import archive_viewmodel as avm
        from src.gui.viewmodels.archive_viewmodel import ArchiveWorker
        from src.workflow.models import WorkflowResult

        monkeypatch.setattr(avm, "validate_url_security_result", lambda url: None)
        engine = MagicMock()
        engine.execute_async = AsyncMock(return_value=WorkflowResult(
            success=False,
            terminal="error",
            data={"status": "error"},
            issues=[{
                "code": "ssrf_resolution_failed",
                "message": "secret-host.example could not resolve",
                "severity": "error",
                "recoverable": True,
                "stage": "network_policy",
            }],
        ))
        worker = ArchiveWorker("url", {"url": "https://example.com/x"})

        with patch("src.workflow.engine.WorkflowEngine", return_value=engine):
            with qtbot.waitSignal(worker.finished_failure, timeout=2000) as blocker:
                asyncio.run(worker._execute_url())

        payload = blocker.args[0]
        assert payload["code"] == "ssrf_resolution_failed"
        assert payload["stage"] == "network_policy"
        assert payload["recoverable"] is True
        assert "secret-host" not in repr(payload)

    def test_exception_and_legacy_failure_signals_remove_canary(self, qtbot):
        from src.gui.viewmodels.archive_viewmodel import (
            ArchiveViewModel,
            _failure_payload_from_exception,
            sanitize_archive_failure,
        )

        canary = "api_key=CANARY-DO-NOT-LEAK C:/private/local.yaml"
        exception_payload = _failure_payload_from_exception(
            RuntimeError(canary),
            stage="archive_url",
        )
        assert "CANARY" not in repr(exception_payload)
        assert "private" not in repr(exception_payload)

        vm = ArchiveViewModel()
        with qtbot.waitSignal(vm.failure_ready, timeout=1000) as blocker:
            vm._on_worker_err(canary)
        assert "CANARY" not in repr(blocker.args[0])
        assert "private" not in repr(blocker.args[0])

        repair_required = sanitize_archive_failure({
            "code": "storage_repair_required",
            "stage": "operation_journal",
            "status": "repair_required",
            "recoverable": True,
        })
        assert repair_required["do_not_retry"] is True
        assert repair_required["recoverable"] is False
        assert "请勿盲目重试" in repair_required["safe_message"]

        identifier_canaries = sanitize_archive_failure({
            "code": "storage_repair_required",
            "stage": "operation_journal",
            "status": "repair_required",
            "operation_id": "sk-SECRET-CANARY",
            "repair_actions": ["api_key_CANARY", "repair_operation_journal"],
            "do_not_retry": True,
        })
        assert identifier_canaries["operation_id"] == ""
        assert identifier_canaries["repair_actions"] == ["repair_operation_journal"]
        assert "SECRET-CANARY" not in repr(identifier_canaries)
        assert "api_key_CANARY" not in repr(identifier_canaries)

        with qtbot.waitSignal(vm.failure_ready, timeout=1000) as blocker:
            vm._publish_failure({
                "code": canary,
                "stage": canary,
                "safe_message": canary,
                "issues": [{"message": canary, "details": canary}],
                "operation_id": canary,
                "repair_actions": [canary],
            })
        assert "CANARY" not in repr(blocker.args[0])
        assert "private" not in repr(blocker.args[0])

    def test_identifier_shaped_secrets_and_truthy_values_fail_closed(
        self,
        qtbot,
        caplog,
    ):
        """Only fixed archive vocabularies and exact booleans cross Qt."""
        from src.gui.viewmodels.archive_viewmodel import ArchiveViewModel

        canary = "api_key_SECRET_CANARY"
        vm = ArchiveViewModel()
        legacy_messages: list[str] = []
        vm.error_occurred.connect(legacy_messages.append)

        with caplog.at_level(
            logging.INFO,
            logger="pkv.gui.viewmodels.archive",
        ):
            with qtbot.waitSignal(vm.failure_ready, timeout=1000) as blocker:
                vm._publish_failure({
                    "code": "workflow_step_failed",
                    "stage": canary,
                    "recoverable": 1,
                    "core_committed": "true",
                    "do_not_retry": [True],
                    "status": "error",
                    "safe_message": canary,
                    "issues": [{
                        "code": "workflow_step_failed",
                        "message": canary,
                        "severity": "error",
                        "recoverable": "true",
                        "stage": canary,
                        "step_id": canary,
                        "cause_type": canary,
                    }],
                })

        payload = blocker.args[0]
        assert payload["stage"] == "workflow"
        assert payload["recoverable"] is False
        assert payload["core_committed"] is False
        assert payload["do_not_retry"] is False
        assert payload["issues"][0]["stage"] == "workflow"
        assert payload["issues"][0]["step_id"] == "unknown_step"
        assert payload["issues"][0]["cause_type"] == "Exception"
        assert payload["issues"][0]["recoverable"] is False
        assert canary not in repr(payload)
        assert canary not in repr(legacy_messages)
        assert canary not in caplog.text
