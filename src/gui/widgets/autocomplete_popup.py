"""自动补全弹出窗口 (M12 Phase 2)

提供 IDE 风格的自动补全下拉菜单：
- 在输入 "@" 时触发
- 支持两种引用模式：
  - @知识库/ — 按标题搜索知识条目
  - @搜索/ — 搜索关键词引用
- 键盘导航：↑↓ 选择、Enter 确认、Esc 关闭
- 实时过滤：随输入动态更新候选列表

技术说明：
- QListWidget + setWindowFlags(Popup) 实现弹出窗口
- 使用 BM25Retriever 或 SQLiteStore 进行候选项搜索
- 防抖机制：200ms 延迟触发搜索
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from PySide6.QtCore import QPoint, QTimer, Qt, Signal
from PySide6.QtGui import QFocusEvent
from PySide6.QtWidgets import QListWidget, QListWidgetItem, QWidget

from src.gui.utils.search_response_contract import is_strict_search_response

if TYPE_CHECKING:
    from src.retrieval.result import RetrievalIssue

logger = logging.getLogger("pkv.gui.widgets.autocomplete")

# 自动补全模式
MODE_KNOWLEDGE = "knowledge"  # @知识库/
MODE_SEARCH = "search"  # @搜索/


class AutocompletePopup(QListWidget):
    """自动补全弹出窗口

    Signals:
        item_selected(str, dict): 选中候选项（显示文本, 条目数据）
        popup_closed(): 弹窗关闭
    """

    item_selected = Signal(str, dict)
    popup_closed = Signal()

    # 弹窗尺寸
    _MAX_VISIBLE_ITEMS = 8
    _ITEM_HEIGHT = 32
    _POPUP_WIDTH = 350

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        """初始化自动补全弹窗

        Args:
            parent: 父控件（通常是 InputBox）
        """
        super().__init__(parent)

        # 设置为弹出窗口
        self.setWindowFlags(
            Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint
        )
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setObjectName("autocomplete_popup")

        # 当前模式和过滤文本
        self._mode: str = MODE_KNOWLEDGE
        self._filter_text: str = ""
        self._candidates: List[Dict[str, Any]] = []

        # 防抖定时器
        self._debounce_timer = QTimer(self)
        self._debounce_timer.setSingleShot(True)
        self._debounce_timer.setInterval(200)
        self._debounce_timer.timeout.connect(self._do_search)

        # 点击选择
        self.itemClicked.connect(self._on_item_clicked)

        self.hide()

    def show_popup(
        self,
        mode: str,
        filter_text: str,
        anchor_pos: QPoint,
    ) -> None:
        """显示弹窗并开始搜索

        Args:
            mode: 自动补全模式 ("knowledge" 或 "search")
            filter_text: 过滤文本（@ 后的内容）
            anchor_pos: 弹窗锚点位置（全局坐标）
        """
        self._mode = mode
        self._filter_text = filter_text

        # 设置位置（在输入框上方）
        popup_height = min(
            self._MAX_VISIBLE_ITEMS * self._ITEM_HEIGHT + 8,
            300,
        )
        self.setFixedSize(self._POPUP_WIDTH, popup_height)

        # 弹窗位置：输入框上方
        x = anchor_pos.x()
        y = anchor_pos.y() - popup_height - 4
        if y < 0:
            # 如果上方空间不够，显示在下方
            y = anchor_pos.y() + 24
        self.move(x, y)

        # 触发搜索（防抖）
        self._debounce_timer.start()
        self.show()

    def update_filter(self, filter_text: str) -> None:
        """更新过滤文本并重新搜索

        Args:
            filter_text: 新的过滤文本
        """
        self._filter_text = filter_text
        self._debounce_timer.start()

    def hide_popup(self) -> None:
        """隐藏弹窗"""
        self._debounce_timer.stop()
        self.hide()
        self.popup_closed.emit()

    def navigate_up(self) -> None:
        """向上移动选中项"""
        current = self.currentRow()
        if current > 0:
            self.setCurrentRow(current - 1)
        elif self.count() > 0:
            self.setCurrentRow(self.count() - 1)

    def navigate_down(self) -> None:
        """向下移动选中项"""
        current = self.currentRow()
        if current < self.count() - 1:
            self.setCurrentRow(current + 1)
        elif self.count() > 0:
            self.setCurrentRow(0)

    def confirm_selection(self) -> bool:
        """确认当前选中项

        Returns:
            是否成功选中
        """
        current = self.currentItem()
        if current:
            self._on_item_clicked(current)
            return True
        return False

    def _on_item_clicked(self, item: QListWidgetItem) -> None:
        """候选项点击事件

        Args:
            item: 被点击的列表项
        """
        data = item.data(Qt.ItemDataRole.UserRole)
        if data:
            display_text = item.text()
            self.item_selected.emit(display_text, data)
            self.hide_popup()

    def _do_search(self) -> None:
        """执行搜索（防抖后触发）"""
        self.clear()

        try:
            if self._mode == MODE_KNOWLEDGE:
                self._search_knowledge()
            elif self._mode == MODE_SEARCH:
                self._search_keywords()
            else:
                self._add_error_item("自动补全模式无效")
        except Exception as e:
            logger.error("自动补全搜索失败: type=%s", type(e).__name__)
            self._add_error_item("搜索失败：服务暂不可用")

    def _search_knowledge(self) -> None:
        """搜索知识库条目"""
        try:
            from src.gui.stores import get_sqlite_store
            store = get_sqlite_store()

            if self._filter_text:
                # 有过滤文本时搜索
                entries = store.list_entries(
                    limit=self._MAX_VISIBLE_ITEMS,
                )
                # 手动过滤标题
                filtered = [
                    e for e in entries
                    if self._filter_text.lower() in e.get("title", "").lower()
                ]
                self._populate_entries(filtered)
            else:
                # 无过滤时显示最近条目
                entries = store.list_entries(
                    limit=self._MAX_VISIBLE_ITEMS,
                )
                self._populate_entries(entries)

        except Exception as e:
            logger.error("知识库搜索失败: type=%s", type(e).__name__)
            self._add_error_item("知识库暂不可用")

    def _search_keywords(self) -> None:
        """搜索关键词（使用 BM25）"""
        if not self._filter_text:
            self._add_hint_item("输入关键词搜索...")
            return

        try:
            from src.gui.stores import get_bm25_retriever
            retriever = get_bm25_retriever()
            response = retriever.search(
                self._filter_text,
                limit=self._MAX_VISIBLE_ITEMS,
            )
            if not self._is_strict_response(response):
                raise TypeError("BM25Retriever 返回了非 SearchResponse 结果")

            if response.status == "no_hits":
                self._add_hint_item("无搜索结果")
                return

            if response.status == "invalid":
                self._add_error_item(
                    f"关键词无效（错误代码：{self._issue_codes(response.issues)}）"
                )
                return

            if response.status == "error":
                self._add_error_item(
                    "搜索引擎暂不可用"
                    f"（错误代码：{self._issue_codes(response.issues)}）"
                )
                return

            for result in response.results:
                title = result.title
                score = f"{result.score:.2f}"
                display = f"🔍 {title} ({score})"

                item = QListWidgetItem(display)
                item.setData(Qt.ItemDataRole.UserRole, {
                    "knowledge_id": result.knowledge_id,
                    "title": result.title,
                    "score": result.score,
                    "ref_type": "search",
                    "metadata": result.metadata,
                })
                self.addItem(item)

            if response.status == "degraded":
                self._add_error_item(
                    "搜索已降级，以上仅为可用结果"
                    f"（问题代码：{self._issue_codes(response.issues)}）"
                )

            if response.results:
                self.setCurrentRow(0)

        except Exception as e:
            logger.error("关键词搜索 adapter 异常: type=%s", type(e).__name__)
            self._add_error_item("搜索引擎暂不可用")

    @staticmethod
    def _issue_codes(issues: tuple["RetrievalIssue", ...]) -> str:
        """仅公开稳定错误代码，避免把后端异常详情放进补全弹窗。"""

        codes = []
        for issue in issues:
            raw_code = getattr(getattr(issue, "code", None), "value", None)
            code = (
                raw_code
                if isinstance(raw_code, str)
                and re.fullmatch(r"[a-z0-9_]{1,64}", raw_code)
                else "retrieval_error"
            )
            if code not in codes:
                codes.append(code)
        return ", ".join(codes) if codes else "unknown"

    @staticmethod
    def _is_strict_response(response: Any) -> bool:
        """Validate all five-state invariants without eager vector imports."""

        return is_strict_search_response(response)

    def _populate_entries(self, entries: List[Dict[str, Any]]) -> None:
        """填充条目列表

        Args:
            entries: 条目字典列表
        """
        if not entries:
            self._add_hint_item("无匹配条目")
            return

        for entry in entries:
            kid = entry.get("knowledge_id", "?")
            title = entry.get("title", "")
            source = entry.get("source_type", "")

            # 截断过长标题
            if len(title) > 35:
                title = title[:32] + "..."

            display = f"📄 [{kid}] {title}"
            if source:
                display += f" ({source})"

            item = QListWidgetItem(display)
            item.setData(Qt.ItemDataRole.UserRole, {
                **entry,
                "ref_type": "knowledge",
            })
            self.addItem(item)

        # 默认选中第一项
        if self.count() > 0:
            self.setCurrentRow(0)

    def _add_hint_item(self, text: str) -> None:
        """添加提示项（不可选中）

        Args:
            text: 提示文本
        """
        item = QListWidgetItem(f"  {text}")
        item.setFlags(Qt.ItemFlag.NoItemFlags)
        self.addItem(item)

    def _add_error_item(self, text: str) -> None:
        """添加错误提示项

        Args:
            text: 错误文本
        """
        item = QListWidgetItem(f"⚠️ {text}")
        item.setFlags(Qt.ItemFlag.NoItemFlags)
        self.addItem(item)

    def focusOutEvent(self, event: QFocusEvent) -> None:
        """失去焦点时隐藏弹窗"""
        self.hide_popup()
        super().focusOutEvent(event)
