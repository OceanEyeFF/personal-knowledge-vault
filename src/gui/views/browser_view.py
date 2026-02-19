"""知识条目浏览器视图。

提供三栏 QSplitter 布局：
- 左栏：标签树（TagTreeModel + QTreeView）
- 中栏：条目列表（EntryTableModel + QTableView）+ 分页控件
- 右栏：Markdown 预览（只读 QTextEdit）

存储单例通过 src.gui.stores 统一管理，预览逻辑通过
src.gui.utils.preview_loader 复用，避免跨模块私有变量依赖。
"""

from __future__ import annotations

import logging
import math
from typing import Optional

from PySide6.QtCore import QModelIndex, Qt
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

from src.gui.models.entry_model import EntryTableModel
from src.gui.models.tag_model import TagTreeModel

logger = logging.getLogger("pkv.gui.browser")


# ============================================================
# BrowserView 主类
# ============================================================

class BrowserView(QWidget):
    """知识条目浏览器视图（三栏 QSplitter 布局）。

    左栏：标签树（支持按标签筛选条目）。
    中栏：条目列表（分页显示），点击后在右栏预览。
    右栏：Markdown 全文预览（只读文本框）。

    存储实例通过 src.gui.stores 延迟获取，避免启动时重复初始化。

    Attributes:
        PAGE_SIZE: 每页显示的条目数量。
        _SPLITTER_SIZES: 三栏分割器初始尺寸（左:中:右，单位像素）。
        _current_tag: 当前选中的标签名（None 表示全部）。
        _current_page: 当前页码（从 0 开始）。
        _total_count: 当前筛选条件下的总条目数。
    """

    PAGE_SIZE: int = 20

    # 三栏分割器初始尺寸（左:中:右，单位像素）
    _SPLITTER_SIZES: list[int] = [160, 380, 460]

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        """初始化浏览器视图。

        Args:
            parent: Qt 父部件。
        """
        super().__init__(parent)
        self._current_tag: Optional[str] = None
        self._current_page: int = 0
        self._total_count: int = 0

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
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        # 标题标签
        header = QLabel("标签")
        header.setStyleSheet("font-weight: bold; padding: 2px 0;")
        layout.addWidget(header)

        # 标签树视图
        self._tag_model = TagTreeModel(self)
        self._tag_view = QTreeView(self)
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
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        # 条目列表标题（含数量显示）
        self._entry_count_label = QLabel("条目")
        self._entry_count_label.setStyleSheet("font-weight: bold; padding: 2px 0;")
        layout.addWidget(self._entry_count_label)

        # 条目表格视图
        self._entry_model = EntryTableModel([], self)
        self._entry_view = QTableView(self)
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
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        self._prev_btn = QPushButton("上一页")
        self._prev_btn.setFixedWidth(70)
        self._prev_btn.setEnabled(False)

        self._page_label = QLabel("第 1 页 / 共 1 页")
        self._page_label.setAlignment(Qt.AlignCenter)  # type: ignore[attr-defined]

        self._next_btn = QPushButton("下一页")
        self._next_btn.setFixedWidth(70)
        self._next_btn.setEnabled(False)

        layout.addWidget(self._prev_btn)
        layout.addStretch()
        layout.addWidget(self._page_label)
        layout.addStretch()
        layout.addWidget(self._next_btn)

        return widget

    def _build_preview_panel(self) -> QWidget:
        """构建右栏 Markdown 预览面板。

        Returns:
            包含预览标题和只读 QTextEdit 的 QWidget。
        """
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        # 标题标签
        self._preview_title = QLabel("预览")
        self._preview_title.setStyleSheet("font-weight: bold; padding: 2px 0;")
        layout.addWidget(self._preview_title)

        # 预览文本框
        self._preview_text = QTextEdit(self)
        self._preview_text.setReadOnly(True)
        self._preview_text.setPlaceholderText("选择条目以预览内容...")
        layout.addWidget(self._preview_text)

        return widget

    # ------------------------------------------------------------------
    # 信号连接
    # ------------------------------------------------------------------

    def _connect_signals(self) -> None:
        """连接各控件的信号与槽。"""
        self._tag_view.clicked.connect(self.on_tag_selected)
        self._entry_view.clicked.connect(self.on_entry_selected)
        self._prev_btn.clicked.connect(self._go_prev_page)
        self._next_btn.clicked.connect(self._go_next_page)

    # ------------------------------------------------------------------
    # 数据加载方法
    # ------------------------------------------------------------------

    def load_tags(self) -> None:
        """从 SQLiteStore 加载所有标签并更新 TagTreeModel。

        忽略加载异常（如数据库不存在），显示空标签列表。
        """
        try:
            from src.gui.stores import get_sqlite_store
            store = get_sqlite_store()
            tags = store.get_all_tags_with_count()
            self._tag_model.update_tags(tags)
            logger.debug(f"已加载 {len(tags)} 个标签")
        except Exception as exc:
            logger.warning(f"加载标签失败: {exc}")
            self._tag_model.update_tags([])

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
        self._current_page = page

        try:
            from src.gui.stores import get_sqlite_store
            store = get_sqlite_store()
            # 获取总数
            self._total_count = store.count_entries(tag=tag)
            # 获取当前页条目
            offset = page * self.PAGE_SIZE
            entries = store.list_entries(
                limit=self.PAGE_SIZE,
                offset=offset,
                tag=tag,
            )
            self._entry_model.update_entries(entries)
            self._update_pagination_ui()

            # 更新标题
            tag_label = f"[{tag}] " if tag else ""
            self._entry_count_label.setText(
                f"条目 {tag_label}（共 {self._total_count} 条）"
            )
            logger.debug(f"已加载第 {page + 1} 页，{len(entries)} 条记录")
        except Exception as exc:
            logger.error(f"加载条目失败: {exc}", exc_info=True)
            self._entry_model.update_entries([])
            self._total_count = 0
            self._update_pagination_ui()

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
        # 清空右侧预览
        self._preview_text.clear()
        self._preview_title.setText("预览")

    def on_entry_selected(self, index: QModelIndex) -> None:
        """响应条目列表点击事件，在右侧加载 Markdown 预览。

        Args:
            index: 被点击的条目行索引。
        """
        entry = self._entry_model.get_entry(index.row())
        if entry:
            self._load_preview(entry)

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

    def _load_preview(self, entry: dict) -> None:
        """加载条目的 Markdown 全文到预览区域。

        使用 src.gui.utils.preview_loader.load_entry_preview 加载预览内容，
        失败时由 preview_loader 自动降级到摘要显示。

        Args:
            entry: 条目字典（来自 EntryTableModel）。
        """
        title = entry.get("title", "未知标题")
        self._preview_title.setText(f"预览: {title[:40]}")

        try:
            from src.gui.stores import get_markdown_store
            from src.gui.utils.preview_loader import load_entry_preview
            md_store = get_markdown_store()
            content = load_entry_preview(entry, md_store)
            self._preview_text.setPlainText(content)
        except Exception as exc:
            logger.error(f"加载预览失败: {exc}", exc_info=True)
            # 最终降级：显示基本元数据
            lines = [
                f"标题: {entry.get('title', '')}",
                f"来源: {entry.get('source_type', '')}",
                f"归档时间: {entry.get('archived_at', '')}",
                "",
                entry.get("summary_one_sentence", "") or "（无摘要）",
            ]
            self._preview_text.setPlainText("\n".join(lines))

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
        delete_action = menu.addAction("删除条目")
        action = menu.exec(self._entry_view.viewport().mapToGlobal(position))
        if action == delete_action:
            self._confirm_and_delete(entry)

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
        """执行三层存储删除（best-effort 策略）。

        删除顺序：SQLite → Markdown → Vector。
        向量删除失败不阻断（辅助索引）。

        Args:
            entry: 待删除的条目字典。
        """
        knowledge_id = entry.get("knowledge_id")
        file_path = entry.get("file_path", "")
        errors: list[str] = []

        # 1. 删除 SQLite 记录（含 CASCADE + FTS5 触发器）
        try:
            from src.gui.stores import get_sqlite_store
            store = get_sqlite_store()
            if not store.delete_entry(knowledge_id):
                errors.append("数据库记录不存在")
        except Exception as exc:
            logger.error(f"SQLite 删除失败: {exc}", exc_info=True)
            errors.append(f"数据库删除失败: {exc}")

        # 2. 删除 Markdown 文件
        if file_path:
            try:
                from src.gui.stores import get_markdown_store
                from pathlib import Path
                md_store = get_markdown_store()
                full_path = Path(md_store.vault_dir) / file_path
                md_store.delete(full_path)
            except Exception as exc:
                logger.warning(f"Markdown 删除失败: {exc}")
                errors.append(f"文件删除失败: {exc}")

        # 3. 删除向量索引（best-effort，失败不报错）
        try:
            from src.gui.stores import get_vector_store
            vs = get_vector_store()
            vs.delete_vectors_for_entry(knowledge_id)
        except Exception as exc:
            logger.warning(f"向量索引删除失败（不影响主功能）: {exc}")

        # 4. 刷新视图
        self.refresh()

        # 5. 反馈结果
        if not errors:
            logger.info(f"条目删除成功: knowledge_id={knowledge_id}")
        else:
            QMessageBox.warning(
                self,
                "删除部分失败",
                "删除过程中出现问题:\n\n" + "\n".join(f"• {e}" for e in errors),
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
