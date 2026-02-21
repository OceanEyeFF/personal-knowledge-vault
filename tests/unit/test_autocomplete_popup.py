"""自动补全弹出窗口单元测试 (M12)

测试 src.gui.widgets.autocomplete_popup 中的核心方法：
- show_popup / hide_popup: 弹窗显示/隐藏
- update_filter: 更新过滤文本
- navigate_up / navigate_down: 键盘导航
- confirm_selection: 确认选择
- _populate_entries: 填充条目列表
- _add_hint_item / _add_error_item: 提示/错误项
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QListWidgetItem

from src.gui.widgets.autocomplete_popup import (
    AutocompletePopup,
    MODE_KNOWLEDGE,
    MODE_SEARCH,
)


@pytest.fixture
def popup(qtbot):
    """创建 AutocompletePopup 实例"""
    p = AutocompletePopup()
    qtbot.addWidget(p)
    return p


# ===================================================================
# 基本功能
# ===================================================================


class TestBasicFunctionality:
    """基本功能测试"""

    def test_initial_state_hidden(self, popup) -> None:
        """初始状态：隐藏"""
        assert not popup.isVisible()

    def test_initial_mode(self, popup) -> None:
        """初始模式：knowledge"""
        assert popup._mode == MODE_KNOWLEDGE

    def test_initial_filter_empty(self, popup) -> None:
        """初始过滤文本为空"""
        assert popup._filter_text == ""


# ===================================================================
# show_popup / hide_popup
# ===================================================================


class TestShowHidePopup:
    """弹窗显示/隐藏测试"""

    def test_show_popup(self, popup) -> None:
        """显示弹窗"""
        with patch.object(popup, "_do_search"):
            popup.show_popup(MODE_KNOWLEDGE, "test", QPoint(100, 200))
            assert popup._mode == MODE_KNOWLEDGE
            assert popup._filter_text == "test"

    def test_hide_popup(self, popup, qtbot) -> None:
        """隐藏弹窗"""
        popup.show()
        assert popup.isVisible()

        with qtbot.waitSignal(popup.popup_closed, timeout=1000):
            popup.hide_popup()

        assert not popup.isVisible()

    def test_hide_stops_debounce_timer(self, popup) -> None:
        """隐藏时停止防抖定时器"""
        popup._debounce_timer.start()
        assert popup._debounce_timer.isActive()

        popup.hide_popup()
        assert not popup._debounce_timer.isActive()


# ===================================================================
# update_filter
# ===================================================================


class TestUpdateFilter:
    """更新过滤文本测试"""

    def test_updates_filter_text(self, popup) -> None:
        """更新过滤文本"""
        popup.update_filter("新关键词")
        assert popup._filter_text == "新关键词"

    def test_restarts_debounce_timer(self, popup) -> None:
        """重启防抖定时器"""
        popup.update_filter("test")
        assert popup._debounce_timer.isActive()


# ===================================================================
# 键盘导航
# ===================================================================


class TestKeyboardNavigation:
    """键盘导航测试"""

    def _add_items(self, popup, count=5):
        """辅助：添加测试项"""
        for i in range(count):
            item = QListWidgetItem(f"Item {i}")
            item.setData(Qt.ItemDataRole.UserRole, {"id": i})
            popup.addItem(item)
        popup.setCurrentRow(0)

    def test_navigate_down(self, popup) -> None:
        """向下导航"""
        self._add_items(popup)
        popup.setCurrentRow(0)
        popup.navigate_down()
        assert popup.currentRow() == 1

    def test_navigate_down_wraps(self, popup) -> None:
        """向下导航循环到开头"""
        self._add_items(popup, count=3)
        popup.setCurrentRow(2)
        popup.navigate_down()
        assert popup.currentRow() == 0

    def test_navigate_up(self, popup) -> None:
        """向上导航"""
        self._add_items(popup)
        popup.setCurrentRow(2)
        popup.navigate_up()
        assert popup.currentRow() == 1

    def test_navigate_up_wraps(self, popup) -> None:
        """向上导航循环到末尾"""
        self._add_items(popup, count=3)
        popup.setCurrentRow(0)
        popup.navigate_up()
        assert popup.currentRow() == 2

    def test_navigate_empty_list(self, popup) -> None:
        """空列表导航不崩溃"""
        popup.navigate_up()
        popup.navigate_down()
        # 不抛异常即为通过


# ===================================================================
# confirm_selection
# ===================================================================


class TestConfirmSelection:
    """确认选择测试"""

    def test_confirm_with_selection(self, popup, qtbot) -> None:
        """有选中项时确认"""
        item = QListWidgetItem("测试项")
        item.setData(Qt.ItemDataRole.UserRole, {"knowledge_id": 42})
        popup.addItem(item)
        popup.setCurrentRow(0)

        with qtbot.waitSignal(popup.item_selected, timeout=1000):
            result = popup.confirm_selection()

        assert result is True

    def test_confirm_without_selection(self, popup) -> None:
        """无选中项时返回 False"""
        result = popup.confirm_selection()
        assert result is False


# ===================================================================
# _populate_entries
# ===================================================================


class TestPopulateEntries:
    """填充条目列表测试"""

    def test_empty_entries(self, popup) -> None:
        """空条目列表显示提示"""
        popup._populate_entries([])
        assert popup.count() == 1  # 提示项
        # 提示项不可选
        item = popup.item(0)
        assert item.flags() == Qt.ItemFlag.NoItemFlags

    def test_populate_with_entries(self, popup) -> None:
        """填充条目"""
        entries = [
            {"knowledge_id": 1, "title": "测试文章A", "source_type": "wechat"},
            {"knowledge_id": 2, "title": "测试文章B", "source_type": "zhihu"},
        ]
        popup._populate_entries(entries)
        assert popup.count() == 2

        # 验证数据绑定
        item0_data = popup.item(0).data(Qt.ItemDataRole.UserRole)
        assert item0_data["knowledge_id"] == 1
        assert item0_data["ref_type"] == "knowledge"

    def test_long_title_truncated(self, popup) -> None:
        """过长标题被截断"""
        entries = [
            {
                "knowledge_id": 1,
                "title": "A" * 50,  # 超过 35 字符
                "source_type": "",
            },
        ]
        popup._populate_entries(entries)
        display = popup.item(0).text()
        assert "..." in display

    def test_first_item_selected(self, popup) -> None:
        """默认选中第一项"""
        entries = [
            {"knowledge_id": 1, "title": "文章1"},
            {"knowledge_id": 2, "title": "文章2"},
        ]
        popup._populate_entries(entries)
        assert popup.currentRow() == 0


# ===================================================================
# _add_hint_item / _add_error_item
# ===================================================================


class TestHintAndErrorItems:
    """提示项和错误项测试"""

    def test_add_hint_item(self, popup) -> None:
        """添加提示项"""
        popup._add_hint_item("无匹配结果")
        assert popup.count() == 1
        item = popup.item(0)
        assert "无匹配结果" in item.text()
        assert item.flags() == Qt.ItemFlag.NoItemFlags

    def test_add_error_item(self, popup) -> None:
        """添加错误项"""
        popup._add_error_item("搜索失败")
        assert popup.count() == 1
        item = popup.item(0)
        assert "搜索失败" in item.text()
        assert item.flags() == Qt.ItemFlag.NoItemFlags


# ===================================================================
# _do_search 路由
# ===================================================================


class TestDoSearch:
    """搜索路由测试"""

    def test_search_routes_to_knowledge(self, popup) -> None:
        """knowledge 模式路由到 _search_knowledge"""
        popup._mode = MODE_KNOWLEDGE
        with patch.object(popup, "_search_knowledge") as mock_sk:
            popup._do_search()
            mock_sk.assert_called_once()

    def test_search_routes_to_keywords(self, popup) -> None:
        """search 模式路由到 _search_keywords"""
        popup._mode = MODE_SEARCH
        with patch.object(popup, "_search_keywords") as mock_sk:
            popup._do_search()
            mock_sk.assert_called_once()

    def test_search_error_shows_error_item(self, popup) -> None:
        """搜索异常时显示错误项"""
        popup._mode = MODE_KNOWLEDGE
        with patch.object(
            popup, "_search_knowledge", side_effect=RuntimeError("boom")
        ):
            popup._do_search()
            assert popup.count() == 1
            assert "搜索失败" in popup.item(0).text()


# ===================================================================
# _search_knowledge
# ===================================================================


class TestSearchKnowledge:
    """知识库搜索测试"""

    def test_search_no_filter(self, popup) -> None:
        """无过滤文本时显示最近条目"""
        popup._filter_text = ""

        mock_store = MagicMock()
        mock_store.list_entries.return_value = [
            {"knowledge_id": 1, "title": "文章A"},
            {"knowledge_id": 2, "title": "文章B"},
        ]

        with patch(
            "src.gui.stores.get_sqlite_store",
            return_value=mock_store,
        ):
            popup._search_knowledge()

        assert popup.count() == 2

    def test_search_with_filter(self, popup) -> None:
        """有过滤文本时按标题过滤"""
        popup._filter_text = "AI"

        mock_store = MagicMock()
        mock_store.list_entries.return_value = [
            {"knowledge_id": 1, "title": "AI 发展趋势"},
            {"knowledge_id": 2, "title": "Python 入门"},
        ]

        with patch(
            "src.gui.stores.get_sqlite_store",
            return_value=mock_store,
        ):
            popup._search_knowledge()

        # 只有 "AI 发展趋势" 匹配
        assert popup.count() == 1

    def test_search_knowledge_error(self, popup) -> None:
        """知识库搜索异常时显示错误提示"""
        popup._filter_text = ""

        with patch(
            "src.gui.stores.get_sqlite_store",
            side_effect=RuntimeError("DB error"),
        ):
            popup._search_knowledge()

        assert popup.count() == 1
        assert "暂不可用" in popup.item(0).text()


# ===================================================================
# _search_keywords
# ===================================================================


class TestSearchKeywords:
    """关键词搜索测试"""

    def test_search_empty_filter_shows_hint(self, popup) -> None:
        """空过滤文本显示提示"""
        popup._filter_text = ""
        popup._search_keywords()
        assert popup.count() == 1
        assert "输入关键词" in popup.item(0).text()

    def test_search_no_results(self, popup) -> None:
        """无搜索结果"""
        popup._filter_text = "不存在的关键词"

        mock_retriever = MagicMock()
        mock_retriever.search.return_value = []

        with patch(
            "src.gui.stores.get_bm25_retriever",
            return_value=mock_retriever,
        ):
            popup._search_keywords()

        assert popup.count() == 1
        assert "无搜索结果" in popup.item(0).text()

    def test_search_with_results(self, popup) -> None:
        """有搜索结果"""
        popup._filter_text = "AI"

        mock_result = MagicMock()
        mock_result.title = "AI 文章"
        mock_result.score = 0.85
        mock_result.knowledge_id = 42
        mock_result.metadata = {}

        mock_retriever = MagicMock()
        mock_retriever.search.return_value = [mock_result]

        with patch(
            "src.gui.stores.get_bm25_retriever",
            return_value=mock_retriever,
        ):
            popup._search_keywords()

        assert popup.count() == 1
        item_data = popup.item(0).data(Qt.ItemDataRole.UserRole)
        assert item_data["knowledge_id"] == 42
        assert item_data["ref_type"] == "search"

    def test_search_keywords_error(self, popup) -> None:
        """搜索引擎异常时显示错误提示"""
        popup._filter_text = "test"

        with patch(
            "src.gui.stores.get_bm25_retriever",
            side_effect=RuntimeError("engine error"),
        ):
            popup._search_keywords()

        assert popup.count() == 1
        assert "暂不可用" in popup.item(0).text()


# ===================================================================
# focusOutEvent
# ===================================================================


class TestFocusOutEvent:
    """失去焦点测试"""

    def test_focus_out_hides_popup(self, popup, qtbot) -> None:
        """失去焦点时隐藏弹窗"""
        from PySide6.QtGui import QFocusEvent
        from PySide6.QtCore import Qt

        popup.show()

        with qtbot.waitSignal(popup.popup_closed, timeout=1000):
            event = QFocusEvent(
                QFocusEvent.Type.FocusOut, Qt.FocusReason.OtherFocusReason
            )
            popup.focusOutEvent(event)
