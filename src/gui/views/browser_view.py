"""知识条目浏览器视图。

提供三栏 QSplitter 布局：
- 左栏：标签树（TagTreeModel + QTreeView）
- 中栏：条目列表（EntryTableModel + QTableView）+ 分页控件
- 右栏：Markdown 预览（只读 QTextEdit）+ 发送到对话按钮

存储单例通过 src.gui.stores 统一管理，预览逻辑通过
src.gui.utils.preview_loader 复用，避免跨模块私有变量依赖。
"""

from __future__ import annotations

import logging
import math
from typing import Optional

from PySide6.QtCore import QItemSelectionModel, QModelIndex, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QLabel,
    QMenu,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTextEdit,
    QTreeView,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from src.gui.models.entry_model import EntryTableModel, validate_entry_rows
from src.gui.models.tag_model import TagTreeModel
from src.gui.widgets.accessibility import set_automation_id

logger = logging.getLogger("pkv.gui.browser")

_TAGS_LOAD_FAILED = "browser_tags_load_failed"
_ENTRY_COUNT_FAILED = "browser_entry_count_failed"
_ENTRY_LIST_FAILED = "browser_entry_list_failed"
_PREVIEW_ADAPTER_FAILED = "browser_preview_adapter_failed"


# ============================================================
# BrowserView 主类
# ============================================================

class BrowserView(QWidget):
    """知识条目浏览器视图（三栏 QSplitter 布局）。

    左栏：标签树（支持按标签筛选条目）。
    中栏：条目列表（分页显示），点击后在右栏预览。
    右栏：Markdown 全文预览（只读文本框）+ 发送到对话按钮。

    存储实例通过 src.gui.stores 延迟获取，避免启动时重复初始化。

    Signals:
        navigate_to_browser: 外部导航信号（已有）。
        send_to_chat_requested: 发送知识条目到 AI 对话（M12）。
            参数：条目字典 dict, 全文内容 str

    Attributes:
        PAGE_SIZE: 每页显示的条目数量。
        _SPLITTER_SIZES: 三栏分割器初始尺寸（左:中:右，单位像素）。
        _current_tag: 当前选中的标签名（None 表示全部）。
        _current_page: 当前页码（从 0 开始）。
        _total_count: 当前筛选条件下的总条目数。
    """

    # M12: 发送知识条目到 AI 对话的信号（entry_dict, content_text）
    send_to_chat_requested = Signal(dict, str)

    PAGE_SIZE: int = 20

    # 三栏分割器初始尺寸（左:中:右，单位像素）
    _SPLITTER_SIZES: list[int] = [160, 380, 460]

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        """初始化浏览器视图。

        Args:
            parent: Qt 父部件。
        """
        super().__init__(parent)
        set_automation_id(self, "browser_view")
        self._current_tag: Optional[str] = None
        self._current_page: int = 0
        self._total_count: int = 0
        # M12: 缓存当前选中的条目和预览内容
        self._selected_entry: Optional[dict] = None
        self._selected_content: str = ""

        self._init_ui()
        self._connect_signals()

        # 初始加载
        self.load_tags()
        self.load_entries()

    # ------------------------------------------------------------------
    # UI 构建
    # ------------------------------------------------------------------

    def _init_ui(self) -> None:
        """构建三栏 QSplitter 布局。"""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        splitter = QSplitter(Qt.Horizontal, self)  # type: ignore[attr-defined]
        set_automation_id(splitter, "browser_splitter")

        # -- 左栏：标签树 --
        left_panel = self._build_tag_panel()
        splitter.addWidget(left_panel)

        # -- 中栏：条目列表 + 分页 --
        mid_panel = self._build_entry_panel()
        splitter.addWidget(mid_panel)

        # -- 右栏：Markdown 预览 --
        right_panel = self._build_preview_panel()
        splitter.addWidget(right_panel)

        # 初始列宽比例 1:3:4
        splitter.setSizes(self._SPLITTER_SIZES)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setStretchFactor(2, 2)

        layout.addWidget(splitter)

    def _build_tag_panel(self) -> QWidget:
        """构建左栏标签树面板。

        Returns:
            包含标签标题和 QTreeView 的 QWidget。
        """
        widget = QWidget()
        set_automation_id(widget, "browser_tag_panel")
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        # 标题标签
        header = QLabel("标签")
        set_automation_id(header, "browser_tag_header")
        header.setProperty("class", "panel-header")
        layout.addWidget(header)

        self._tag_status_label = QLabel("", self)
        set_automation_id(self._tag_status_label, "browser_tag_status")
        self._tag_status_label.setProperty("status", "error")
        self._tag_status_label.setWordWrap(True)
        self._tag_status_label.hide()
        layout.addWidget(self._tag_status_label)

        # 标签树视图
        self._tag_model = TagTreeModel(self)
        self._tag_view = QTreeView(self)
        set_automation_id(self._tag_view, "browser_tag_tree")
        self._tag_view.setModel(self._tag_model)
        self._tag_view.setHeaderHidden(True)
        self._tag_view.setRootIsDecorated(False)
        self._tag_view.setAlternatingRowColors(True)
        layout.addWidget(self._tag_view)

        return widget

    def _build_entry_panel(self) -> QWidget:
        """构建中栏条目列表面板（含分页控件）。

        Returns:
            包含条目表格和分页按钮的 QWidget。
        """
        widget = QWidget()
        set_automation_id(widget, "browser_entry_panel")
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        # 条目列表标题（含数量显示）
        self._entry_count_label = QLabel("条目")
        set_automation_id(self._entry_count_label, "browser_entry_count")
        self._entry_count_label.setProperty("class", "panel-header")
        layout.addWidget(self._entry_count_label)

        self._entry_status_label = QLabel("", self)
        set_automation_id(self._entry_status_label, "browser_entry_status")
        self._entry_status_label.setProperty("status", "error")
        self._entry_status_label.setWordWrap(True)
        self._entry_status_label.hide()
        layout.addWidget(self._entry_status_label)

        # 条目表格视图
        self._entry_model = EntryTableModel([], self)
        self._entry_view = QTableView(self)
        set_automation_id(self._entry_view, "browser_entry_table")
        self._entry_view.setModel(self._entry_model)
        self._entry_view.setSelectionBehavior(QAbstractItemView.SelectRows)  # type: ignore[attr-defined]
        self._entry_view.setSelectionMode(QAbstractItemView.SingleSelection)  # type: ignore[attr-defined]
        self._entry_view.setAlternatingRowColors(True)
        self._entry_view.setSortingEnabled(False)
        self._entry_view.setContextMenuPolicy(Qt.CustomContextMenu)  # type: ignore[attr-defined]
        self._entry_view.customContextMenuRequested.connect(self._show_context_menu)
        self._entry_view.horizontalHeader().setStretchLastSection(True)
        self._entry_view.horizontalHeader().setMinimumSectionSize(40)
        self._entry_view.verticalHeader().hide()
        # 使用 EntryTableModel.COLUMN_WIDTHS 统一设置列宽
        for col, width in EntryTableModel.COLUMN_WIDTHS.items():
            self._entry_view.setColumnWidth(col, width)
        layout.addWidget(self._entry_view)

        # 分页控件
        pagination = self._build_pagination_bar()
        layout.addWidget(pagination)

        return widget

    def _build_pagination_bar(self) -> QWidget:
        """构建分页控件栏（上一页 / 页码 / 下一页）。

        Returns:
            水平分页控件 QWidget。
        """
        widget = QWidget()
        set_automation_id(widget, "browser_pagination")
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        self._prev_btn = QPushButton("上一页")
        set_automation_id(self._prev_btn, "browser_prev_page")
        self._prev_btn.setFixedWidth(70)
        self._prev_btn.setEnabled(False)

        self._page_label = QLabel("第 1 页 / 共 1 页")
        set_automation_id(self._page_label, "browser_page_status")
        self._page_label.setAlignment(Qt.AlignCenter)  # type: ignore[attr-defined]

        self._next_btn = QPushButton("下一页")
        set_automation_id(self._next_btn, "browser_next_page")
        self._next_btn.setFixedWidth(70)
        self._next_btn.setEnabled(False)

        layout.addWidget(self._prev_btn)
        layout.addStretch()
        layout.addWidget(self._page_label)
        layout.addStretch()
        layout.addWidget(self._next_btn)

        return widget

    def _build_preview_panel(self) -> QWidget:
        """构建右栏 Markdown 预览面板（含发送到对话按钮）。

        Returns:
            包含预览标题、只读 QTextEdit 和操作按钮的 QWidget。
        """
        widget = QWidget()
        set_automation_id(widget, "browser_preview_panel")
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        # 标题标签
        self._preview_title = QLabel("预览")
        set_automation_id(self._preview_title, "browser_preview_title")
        self._preview_title.setProperty("class", "panel-header")
        layout.addWidget(self._preview_title)

        self._preview_status_label = QLabel("", self)
        set_automation_id(self._preview_status_label, "browser_preview_status")
        self._preview_status_label.setWordWrap(True)
        self._preview_status_label.hide()
        layout.addWidget(self._preview_status_label)

        # 预览文本框
        self._preview_text = QTextEdit(self)
        set_automation_id(self._preview_text, "browser_preview_text")
        self._preview_text.setReadOnly(True)
        self._preview_text.setPlaceholderText("选择条目以预览内容...")
        layout.addWidget(self._preview_text)

        # M12: 发送到对话按钮
        self._send_to_chat_btn = QPushButton("💬 发送到 AI 对话")
        self._send_to_chat_btn.setToolTip(
            "将当前知识条目发送到 AI 对话，创建新会话并以此为上下文"
        )
        self._send_to_chat_btn.setEnabled(False)  # 未选中条目时禁用
        self._send_to_chat_btn.setMinimumHeight(32)
        set_automation_id(self._send_to_chat_btn, "browser_send_to_chat")
        layout.addWidget(self._send_to_chat_btn)

        return widget

    # ------------------------------------------------------------------
    # 信号连接
    # ------------------------------------------------------------------

    def _connect_signals(self) -> None:
        """连接各控件的信号与槽。"""
        self._tag_view.clicked.connect(self.on_tag_selected)
        selection_model = self._entry_view.selectionModel()
        if selection_model is None:
            raise RuntimeError("Browser entry table has no selection model")
        selection_model.currentRowChanged.connect(
            self._on_entry_current_row_changed
        )
        selection_model.selectionChanged.connect(
            self._sync_selected_entry_to_current_row
        )
        self._entry_model.modelAboutToBeReset.connect(
            self._clear_entry_selection
        )
        self._prev_btn.clicked.connect(self._go_prev_page)
        self._next_btn.clicked.connect(self._go_next_page)
        # M12: 发送到对话
        self._send_to_chat_btn.clicked.connect(self._on_send_to_chat)

    # ------------------------------------------------------------------
    # 数据加载方法
    # ------------------------------------------------------------------

    def load_tags(self) -> None:
        """从 SQLiteStore 加载所有标签并更新 TagTreeModel。

        加载失败时保留最后一次成功数据并显示明确、稳定的错误状态。
        """
        try:
            from src.gui.stores import get_sqlite_store
            store = get_sqlite_store()
            tags = store.get_all_tags_with_count()
            self._tag_model.update_tags(tags)
            self._tag_view.setEnabled(True)
            self._tag_status_label.clear()
            self._tag_status_label.hide()
            logger.debug("已加载 %s 个标签", len(tags))
        except Exception as exc:
            logger.error(
                "标签加载失败: code=%s, error_type=%s",
                _TAGS_LOAD_FAILED,
                type(exc).__name__,
            )
            self._tag_view.setEnabled(False)
            self._tag_status_label.setText(
                f"标签加载失败（错误代码：{_TAGS_LOAD_FAILED}）"
            )
            self._tag_status_label.show()

    def load_entries(
        self,
        tag: Optional[str] = None,
        page: int = 0,
    ) -> None:
        """分页加载条目列表并更新 EntryTableModel 和分页控件。

        Args:
            tag: 标签名筛选（None 表示不筛选）。
            page: 页码（从 0 开始）。
        """
        self._current_tag = tag
        failure_code = _ENTRY_COUNT_FAILED

        try:
            from src.gui.stores import get_sqlite_store
            store = get_sqlite_store()

            total_count = store.count_entries(tag=tag)
            if (
                not isinstance(total_count, int)
                or isinstance(total_count, bool)
                or total_count < 0
            ):
                raise TypeError("entry count contract violation")

            requested_page = max(0, int(page))
            total_pages = max(1, math.ceil(total_count / self.PAGE_SIZE))
            resolved_page = min(requested_page, total_pages - 1)
            offset = resolved_page * self.PAGE_SIZE
            failure_code = _ENTRY_LIST_FAILED
            entries = store.list_entries(
                limit=self.PAGE_SIZE,
                offset=offset,
                tag=tag,
            )
            validate_entry_rows(entries)
            expected_row_count = min(self.PAGE_SIZE, total_count - offset)
            knowledge_ids = [entry["knowledge_id"] for entry in entries]
            if (
                offset < 0
                or offset > total_count
                or (total_count > 0 and offset >= total_count)
                or len(entries) != expected_row_count
                or len(set(knowledge_ids)) != len(knowledge_ids)
            ):
                raise TypeError("entry page contract violation")

            self._total_count = total_count
            self._current_page = resolved_page
            self._clear_entry_selection()
            self._entry_model.update_entries(entries)
            self._entry_view.setEnabled(True)
            self._entry_status_label.clear()
            self._entry_status_label.hide()
            self._update_pagination_ui()

            # 更新标题
            tag_label = f"[{tag}] " if tag else ""
            self._entry_count_label.setText(
                f"条目 {tag_label}（共 {self._total_count} 条）"
            )
            logger.debug(
                "已加载第 %s 页，%s 条记录",
                self._current_page + 1,
                len(entries),
            )
        except Exception as exc:
            logger.error(
                "条目加载失败: code=%s, error_type=%s",
                failure_code,
                type(exc).__name__,
            )
            self._render_entries_error(failure_code)

    def _render_entries_error(self, code: str) -> None:
        """清除不可信分页投影并呈现稳定的条目加载错误。"""

        self._entry_model.update_entries([])
        self._entry_view.clearSelection()
        self._entry_view.setEnabled(False)
        self._total_count = 0
        self._current_page = 0
        self._entry_count_label.setText("条目（加载失败）")
        self._entry_status_label.setText(
            f"条目加载失败（错误代码：{code}）"
        )
        self._entry_status_label.show()
        self._page_label.setText("分页不可用")
        self._prev_btn.setEnabled(False)
        self._next_btn.setEnabled(False)
        self._clear_entry_selection()

    def _clear_entry_selection(self) -> None:
        """Clear cached preview state when the entry projection changes."""

        self._selected_entry = None
        self._selected_content = ""
        self._send_to_chat_btn.setEnabled(False)
        self._preview_title.setText("预览")
        self._preview_text.clear()
        self._clear_preview_status()

    # ------------------------------------------------------------------
    # 事件处理
    # ------------------------------------------------------------------

    def on_tag_selected(self, index: QModelIndex) -> None:
        """响应标签树点击事件，筛选并刷新条目列表。

        Args:
            index: 被点击的标签树节点索引。
        """
        tag_name = self._tag_model.get_tag_name(index)
        # tag_name 为 None 时表示"全部"节点
        self.load_entries(tag=tag_name, page=0)
        self._clear_entry_selection()

    def on_entry_selected(self, index: QModelIndex) -> None:
        """响应条目列表点击事件，在右侧加载 Markdown 预览。

        Args:
            index: 被点击的条目行索引。
        """
        entry = self._entry_model.get_entry(index.row())
        if entry:
            self._selected_entry = entry
            preview_available = self._load_preview(entry)
            self._send_to_chat_btn.setEnabled(preview_available)

    def _sync_selected_entry_to_current_row(self, *_: object) -> None:
        """Turn one UIA-selected entry row into the current row.

        Native SelectionItem clients can update ``selectionChanged`` without
        changing the table's current index.  The existing ``currentRowChanged``
        handler remains the only route that loads a preview.
        """
        selection_model = self._entry_view.selectionModel()
        if selection_model is None:
            return

        selected_rows = {
            index.row()
            for index in selection_model.selectedIndexes()
            if index.isValid()
        }
        if len(selected_rows) != 1:
            return

        selected_row = next(iter(selected_rows))
        current = selection_model.currentIndex()
        if (
            current.isValid()
            and current.model() == self._entry_model
            and current.row() == selected_row
        ):
            return

        selected_index = self._entry_model.index(selected_row, 0)
        if selected_index.isValid():
            selection_model.setCurrentIndex(
                selected_index,
                QItemSelectionModel.SelectionFlag.NoUpdate,
            )

    def _on_entry_current_row_changed(
        self,
        current: QModelIndex,
        previous: QModelIndex,
    ) -> None:
        """Load preview for keyboard and native SelectionItem navigation."""

        del previous
        if current.isValid():
            self.on_entry_selected(current)
        else:
            self._clear_entry_selection()

    def _go_prev_page(self) -> None:
        """切换到上一页。"""
        if self._current_page > 0:
            self.load_entries(tag=self._current_tag, page=self._current_page - 1)

    def _go_next_page(self) -> None:
        """切换到下一页。"""
        total_pages = self._calc_total_pages()
        if self._current_page < total_pages - 1:
            self.load_entries(tag=self._current_tag, page=self._current_page + 1)

    # ------------------------------------------------------------------
    # 预览加载
    # ------------------------------------------------------------------

    def _load_preview(self, entry: dict) -> bool:
        """加载结构化预览，并显式呈现完整、降级或失败状态。

        Args:
            entry: 条目字典（来自 EntryTableModel）。

        Returns:
            完整正文或安全摘要是否可供后续发送。
        """
        title = entry.get("title", "未知标题")
        self._preview_title.setText(f"预览: {title[:40]}")

        try:
            from src.gui.stores import get_markdown_store
            from src.gui.utils.preview_loader import (
                is_strict_preview_outcome,
                load_entry_preview_outcome,
            )
            md_store = get_markdown_store()
            outcome = load_entry_preview_outcome(entry, md_store)
            if not is_strict_preview_outcome(outcome):
                raise TypeError("preview loader contract violation")

            if outcome.status == "success":
                self._clear_preview_status()
                self._preview_text.setPlainText(outcome.content)
                self._selected_content = outcome.content
                return True

            issue = outcome.issue
            if issue is None:
                raise TypeError("preview issue missing")
            code = issue.code.value
            if outcome.status == "degraded":
                self._show_preview_status(
                    "degraded",
                    f"预览已降级：正在显示安全摘要（错误代码：{code}）",
                )
                self._preview_text.setPlainText(outcome.content)
                self._selected_content = outcome.content
                return True

            self._show_preview_status(
                "error",
                f"预览加载失败（错误代码：{code}）",
            )
            self._preview_text.setPlainText("（正文预览不可用）")
            self._selected_content = ""
            return False
        except Exception as exc:
            logger.error(
                "预览 adapter 失败: code=%s, error_type=%s",
                _PREVIEW_ADAPTER_FAILED,
                type(exc).__name__,
            )
            self._show_preview_status(
                "error",
                f"预览加载失败（错误代码：{_PREVIEW_ADAPTER_FAILED}）",
            )
            self._preview_text.setPlainText("（正文预览不可用）")
            self._selected_content = ""
            return False

    def _show_preview_status(self, status: str, message: str) -> None:
        """显示固定的预览降级/错误提示。"""

        self._preview_status_label.setProperty(
            "status",
            "error" if status == "error" else "muted",
        )
        self._preview_status_label.setText(message)
        self._preview_status_label.show()
        self._preview_status_label.style().unpolish(self._preview_status_label)
        self._preview_status_label.style().polish(self._preview_status_label)

    def _clear_preview_status(self) -> None:
        """清除上一条预览的降级或错误提示。"""

        self._preview_status_label.clear()
        self._preview_status_label.hide()

    # ------------------------------------------------------------------
    # 公共刷新接口
    # ------------------------------------------------------------------

    def refresh(self) -> None:
        """刷新标签和条目列表（保留当前筛选与页码状态）。

        典型调用场景：归档成功后用户切换到浏览视图时，由
        MainWindow 自动触发，确保新入库条目立即可见。
        """
        self.load_tags()
        self.load_entries(tag=self._current_tag, page=self._current_page)
        logger.debug("BrowserView 数据已刷新")

    # ------------------------------------------------------------------
    # 右键上下文菜单 & 删除
    # ------------------------------------------------------------------

    def _show_context_menu(self, position) -> None:
        """在条目列表上显示右键上下文菜单。

        Args:
            position: 鼠标点击位置（相对于 _entry_view）。
        """
        index = self._entry_view.indexAt(position)
        if not index.isValid():
            return
        entry = self._entry_model.get_entry(index.row())
        if not entry:
            return

        menu = QMenu(self)
        # M12: 发送到 AI 对话
        chat_action = menu.addAction("💬 发送到 AI 对话")
        menu.addSeparator()
        delete_action = menu.addAction("删除条目")
        action = menu.exec(self._entry_view.viewport().mapToGlobal(position))
        if action == delete_action:
            self._confirm_and_delete(entry)
        elif action == chat_action:
            self._selected_entry = entry
            if self._load_preview(entry):
                self._on_send_to_chat()

    def _confirm_and_delete(self, entry: dict) -> None:
        """弹出确认对话框，确认后执行三层删除。

        Args:
            entry: 待删除的条目字典。
        """
        knowledge_id = entry.get("knowledge_id")
        title = entry.get("title", "未知标题")

        reply = QMessageBox.question(
            self,
            "确认删除",
            f"确定要删除以下知识条目吗？\n\n"
            f"ID: {knowledge_id}\n"
            f"标题: {title}\n\n"
            f"此操作将同时删除数据库记录、Markdown 文件和向量索引，不可恢复。",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply == QMessageBox.Yes:
            self._execute_delete(entry)

    def _execute_delete(self, entry: dict) -> None:
        """通过 W1 状态机执行可补偿的三层存储删除。

        Args:
            entry: 待删除的条目字典。
        """
        knowledge_id = int(entry.get("knowledge_id") or 0)
        from src.gui.stores import get_storage_coordinator, get_vector_store

        def delete_vectors(entry_id: int) -> None:
            vector_store = get_vector_store()
            if vector_store is not None:
                vector_store.delete_vectors_for_entry(entry_id)

        result = get_storage_coordinator().delete(
            knowledge_id,
            vector_operation=delete_vectors,
        )

        self.refresh()

        if result.status.value == "deleted":
            logger.info(f"条目删除成功: knowledge_id={knowledge_id}")
        else:
            messages = [str(error.get("message", "未知错误")) for error in result.errors]
            if result.repair_actions:
                messages.append("修复动作: " + ", ".join(result.repair_actions))
            if result.operation_id:
                messages.append(f"操作 ID: {result.operation_id}")
            if result.do_not_retry:
                messages.append("请勿盲目重试删除，先执行上述修复动作")
            QMessageBox.warning(
                self,
                f"删除终态: {result.status.value}",
                "删除操作未达到完整成功:\n\n"
                + "\n".join(f"• {message}" for message in messages),
            )

    # ------------------------------------------------------------------
    # 分页 UI 更新
    # ------------------------------------------------------------------

    def _calc_total_pages(self) -> int:
        """计算总页数。

        Returns:
            总页数（最小为 1）。
        """
        if self._total_count <= 0:
            return 1
        return math.ceil(self._total_count / self.PAGE_SIZE)

    def _update_pagination_ui(self) -> None:
        """根据当前页码和总数更新分页控件状态。"""
        total_pages = self._calc_total_pages()
        current_display = self._current_page + 1  # 显示从 1 开始

        self._page_label.setText(f"第 {current_display} 页 / 共 {total_pages} 页")
        self._prev_btn.setEnabled(self._current_page > 0)
        self._next_btn.setEnabled(self._current_page < total_pages - 1)

    # ------------------------------------------------------------------
    # M12: 发送到 AI 对话
    # ------------------------------------------------------------------

    def _on_send_to_chat(self) -> None:
        """将当前选中的知识条目发送到 AI 对话。

        通过 send_to_chat_requested Signal 发射条目字典和全文内容，
        由 MainWindow 路由到 ChatView。
        """
        if not self._selected_entry:
            logger.warning("未选中条目，无法发送到对话")
            return

        raw_knowledge_id = self._selected_entry.get("knowledge_id")
        knowledge_id = (
            raw_knowledge_id
            if isinstance(raw_knowledge_id, int) and not isinstance(raw_knowledge_id, bool)
            else "unknown"
        )
        logger.info(
            "发送知识条目到 AI 对话: knowledge_id=%s",
            knowledge_id,
        )
        self.send_to_chat_requested.emit(
            self._selected_entry,
            self._selected_content,
        )
