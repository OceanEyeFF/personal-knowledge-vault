"""全文搜索视图。

提供基于 BM25Retriever（SQLite FTS5）的知识库搜索界面：
- 搜索区域：关键词输入框 + 策略选择 + 搜索按钮
- 结果区域：结果表格（EntryTableModel）+ 数量标签
- 预览区域：选中结果的 Markdown 内容预览

为避免启动时加载向量索引（冷启动慢），搜索功能直接使用
BM25Retriever（SQLite FTS5），不通过 QueryRouter。
存储单例通过 src.gui.stores 统一管理，预览逻辑通过
src.gui.utils.preview_loader 复用。
"""

from __future__ import annotations

import logging
from typing import Optional

from PySide6.QtCore import QModelIndex, Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSplitter,
    QTableView,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from src.gui.models.entry_model import EntryTableModel

logger = logging.getLogger("pkv.gui.search")

# ============================================================
# 搜索策略选项
# ============================================================

_STRATEGY_OPTIONS: list[tuple[str, str]] = [
    ("自动", "auto"),
    ("BM25", "bm25"),
    ("向量", "vector"),
    ("混合", "hybrid"),
]


# ============================================================
# SearchView 主类
# ============================================================

class SearchView(QWidget):
    """全文搜索视图。

    包含搜索输入区域、结果列表和详情预览三个区域。
    搜索默认使用 BM25Retriever（策略选择为"向量"或"混合"时
    在 M10 阶段仍使用 BM25，向量检索将在后续里程碑中集成）。

    存储实例通过 src.gui.stores 延迟获取，预览逻辑通过
    src.gui.utils.preview_loader 与 BrowserView 共享复用。

    Attributes:
        _SPLITTER_SIZES: 搜索结果区分割器初始尺寸（结果:预览，单位像素）。
        _last_results: 最近一次搜索的 SearchResult 列表。
    """

    # 搜索结果区分割器初始尺寸（结果:预览，单位像素）
    _SPLITTER_SIZES: list[int] = [500, 500]

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        """初始化搜索视图。

        Args:
            parent: Qt 父部件。
        """
        super().__init__(parent)
        self._last_results: list = []
        self._init_ui()
        self._connect_signals()

    # ------------------------------------------------------------------
    # UI 构建
    # ------------------------------------------------------------------

    def _init_ui(self) -> None:
        """构建搜索视图布局（上方搜索栏 + 下方结果/预览分栏）。"""
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(8, 8, 8, 8)
        main_layout.setSpacing(8)

        # 搜索区域
        search_bar = self._build_search_bar()
        main_layout.addWidget(search_bar)

        # 结果 + 预览分割器
        splitter = QSplitter(Qt.Horizontal, self)  # type: ignore[attr-defined]

        result_panel = self._build_result_panel()
        splitter.addWidget(result_panel)

        preview_panel = self._build_preview_panel()
        splitter.addWidget(preview_panel)

        splitter.setSizes(self._SPLITTER_SIZES)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 1)

        main_layout.addWidget(splitter)

    def _build_search_bar(self) -> QWidget:
        """构建搜索区域（输入框 + 策略选择 + 搜索按钮）。

        Returns:
            搜索区域 QWidget。
        """
        widget = QWidget()
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        # 关键词输入框
        self.search_input = QLineEdit(self)
        self.search_input.setPlaceholderText("输入关键词搜索知识库...")
        layout.addWidget(self.search_input, stretch=1)

        # 策略选择下拉框
        self._strategy_combo = QComboBox(self)
        for display_name, _ in _STRATEGY_OPTIONS:
            self._strategy_combo.addItem(display_name)
        self._strategy_combo.setFixedWidth(90)
        layout.addWidget(self._strategy_combo)

        # 搜索按钮
        self._search_btn = QPushButton("搜索", self)
        self._search_btn.setFixedWidth(70)
        layout.addWidget(self._search_btn)

        return widget

    def _build_result_panel(self) -> QWidget:
        """构建结果列表面板。

        Returns:
            包含结果数量标签和结果表格的 QWidget。
        """
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        # 结果数量标签
        self._result_count_label = QLabel("输入关键词开始搜索")
        self._result_count_label.setStyleSheet("color: gray; font-size: 12px;")
        layout.addWidget(self._result_count_label)

        # 结果表格
        self._result_model = EntryTableModel([], self)
        self._result_view = QTableView(self)
        self._result_view.setModel(self._result_model)
        self._result_view.setSelectionBehavior(QAbstractItemView.SelectRows)  # type: ignore[attr-defined]
        self._result_view.setSelectionMode(QAbstractItemView.SingleSelection)  # type: ignore[attr-defined]
        self._result_view.setAlternatingRowColors(True)
        self._result_view.horizontalHeader().setStretchLastSection(True)
        self._result_view.verticalHeader().hide()
        # 使用 EntryTableModel.COLUMN_WIDTHS 统一设置列宽
        for col, width in EntryTableModel.COLUMN_WIDTHS.items():
            self._result_view.setColumnWidth(col, width)
        layout.addWidget(self._result_view)

        return widget

    def _build_preview_panel(self) -> QWidget:
        """构建结果详情预览面板。

        Returns:
            包含预览标题和只读 QTextEdit 的 QWidget。
        """
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        self._preview_title = QLabel("详情")
        self._preview_title.setStyleSheet("font-weight: bold;")
        layout.addWidget(self._preview_title)

        self._preview_text = QTextEdit(self)
        self._preview_text.setReadOnly(True)
        self._preview_text.setPlaceholderText("选择搜索结果以查看详情...")
        layout.addWidget(self._preview_text)

        return widget

    # ------------------------------------------------------------------
    # 信号连接
    # ------------------------------------------------------------------

    def _connect_signals(self) -> None:
        """连接控件信号与槽。"""
        self._search_btn.clicked.connect(self.do_search)
        self.search_input.returnPressed.connect(self.do_search)
        self._result_view.clicked.connect(self.on_result_selected)

    # ------------------------------------------------------------------
    # 搜索执行
    # ------------------------------------------------------------------

    def do_search(self) -> None:
        """执行全文搜索并更新结果列表。

        使用 BM25Retriever 进行 FTS5 全文搜索（M10 阶段）。
        搜索结果转换为 EntryTableModel 兼容的字典格式。
        """
        query = self.search_input.text().strip()
        if not query:
            self._result_count_label.setText("请输入搜索关键词")
            self._result_model.update_entries([])
            return

        try:
            from src.gui.stores import get_bm25_retriever
            retriever = get_bm25_retriever()
            results = retriever.search(query, limit=50)
            self._last_results = results

            # 将 SearchResult 转换为 dict 格式（兼容 EntryTableModel）
            entries = []
            for result in results:
                meta = result.metadata or {}
                entries.append({
                    "knowledge_id": result.knowledge_id,
                    "title": result.title,
                    "source_type": meta.get("source_type", ""),
                    "tags": meta.get("tags", ""),
                    "word_count": meta.get("word_count", 0),  # 从 metadata 中取真实字数
                    "archived_at": meta.get("archived_at", ""),
                    "file_path": meta.get("file_path", ""),
                    "score": result.score,
                    "highlight": result.highlight,
                })

            self._result_model.update_entries(entries)
            count = len(results)
            self._result_count_label.setText(
                f'搜索 "{query}" — 找到 {count} 条结果'
            )
            # 清空预览
            self._preview_text.clear()
            self._preview_title.setText("详情")
            logger.info(f"搜索 '{query}' 完成，找到 {count} 条结果")
        except Exception as exc:
            logger.error(f"搜索失败: {exc}", exc_info=True)
            self._result_count_label.setText(f"搜索失败: {exc}")
            self._result_model.update_entries([])

    # ------------------------------------------------------------------
    # 结果选中处理
    # ------------------------------------------------------------------

    def on_result_selected(self, index: QModelIndex) -> None:
        """响应结果列表点击，在右侧加载详情预览。

        Args:
            index: 被点击的结果行索引。
        """
        entry = self._result_model.get_entry(index.row())
        if entry:
            self._load_preview(entry)

    def _load_preview(self, entry: dict) -> None:
        """加载搜索结果条目的详情预览。

        优先显示搜索摘要（highlight），再通过 preview_loader 加载
        Markdown 全文；若全文不可用，则由 preview_loader 自动降级。

        Args:
            entry: 结果条目字典（含 file_path、highlight 等字段）。
        """
        title = entry.get("title", "未知标题")
        self._preview_title.setText(f"详情: {title[:40]}")

        # 构建预览内容前缀（搜索摘要 + 元数据）
        preview_parts: list[str] = []

        # 搜索摘要片段
        highlight = entry.get("highlight", "")
        if highlight:
            preview_parts.append(f"**搜索摘要**: {highlight}\n\n---\n\n")

        # 元数据信息
        source_type = entry.get("source_type", "")
        archived_at = entry.get("archived_at", "")
        tags_raw = entry.get("tags", "")
        if source_type:
            preview_parts.append(f"**来源**: {source_type}\n")
        if archived_at:
            preview_parts.append(f"**归档时间**: {archived_at[:10] if len(archived_at) >= 10 else archived_at}\n")
        if tags_raw:
            preview_parts.append(f"**标签**: {tags_raw}\n")

        # 使用 preview_loader 加载 Markdown 正文
        try:
            from src.gui.stores import get_markdown_store
            from src.gui.utils.preview_loader import load_entry_preview
            md_store = get_markdown_store()
            full_content = load_entry_preview(entry, md_store)
            # 若 full_content 非空，追加到预览
            if full_content and full_content.strip():
                preview_parts.append("\n---\n\n")
                preview_parts.append(full_content)
        except Exception as exc:
            logger.warning(f"加载 Markdown 预览失败: {exc}")

        if preview_parts:
            self._preview_text.setPlainText("".join(preview_parts))
        else:
            self._preview_text.setPlainText("（无内容）")

    # ------------------------------------------------------------------
    # 公开方法（供 MainWindow 调用）
    # ------------------------------------------------------------------

    def focus_search_input(self) -> None:
        """聚焦搜索输入框（供快捷键 Ctrl+K 调用）。"""
        self.search_input.setFocus()
        self.search_input.selectAll()
