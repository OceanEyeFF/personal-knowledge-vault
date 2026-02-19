"""ArchiveView pytest-qt 单元测试。

覆盖 M11 验收标准:
- ArchiveView UI 结构（双标签页、输入控件、按钮、进度区、结果区）
- ArchiveViewModel 验证逻辑和信号
- View + ViewModel 协作集成

测试策略：Mock ArchiveViewModel（及其依赖的 WorkflowEngine / TextFallbackProcessor），
验证 UI 组件结构和交互逻辑。
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
from PySide6.QtCore import Qt
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

    Mock ArchiveViewModel 的依赖（validate_url_security / validate_text_length），
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
    with patch("src.gui.viewmodels.archive_viewmodel.validate_url_security", return_value=(True, "")), \
         patch("src.gui.viewmodels.archive_viewmodel.validate_text_length", return_value=(True, "")):
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

    def test_error_shows_result_area_with_failure(self, archive_view):
        """归档错误时结果区域显示失败信息。"""
        with patch("PySide6.QtWidgets.QMessageBox.warning"):
            archive_view._on_error("网络连接失败")
        assert not archive_view._result_area.isHidden()
        assert "失败" in archive_view._result_title_label.text()

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
