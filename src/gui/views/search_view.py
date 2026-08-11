"""全文搜索视图。

提供基于 BM25Retriever（SQLite FTS5）的知识库搜索界面：
- 搜索区域：关键词输入框 + BM25 发布策略 + 搜索按钮
- 结果区域：结果表格（EntryTableModel）+ 数量标签
- 预览区域：选中结果的 Markdown 内容预览

为避免启动时加载向量索引（冷启动慢），搜索功能直接使用
BM25Retriever（SQLite FTS5），不通过 QueryRouter。
存储单例通过 src.gui.stores 统一管理，预览逻辑通过
src.gui.utils.preview_loader 复用。
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any, Optional

from PySide6.QtCore import QItemSelectionModel, QModelIndex, Qt
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
from src.gui.utils.search_response_contract import is_strict_search_response
from src.gui.widgets.accessibility import set_automation_id

if TYPE_CHECKING:
    from src.retrieval.result import RetrievalIssue, SearchResponse

logger = logging.getLogger("pkv.gui.search")

_PREVIEW_ADAPTER_ERROR = "preview_adapter_error"

# ============================================================
# 搜索策略选项
# ============================================================

_STRATEGY_OPTIONS: list[tuple[str, str]] = [
    ("BM25", "bm25"),
]


# ============================================================
# SearchView 主类
# ============================================================

class SearchView(QWidget):
    """全文搜索视图。

    包含搜索输入区域、结果列表和详情预览三个区域。M13 发布面仅公开
    已具备完整错误语义的 BM25；向量/混合检索不会以假选项出现。

    存储实例通过 src.gui.stores 延迟获取，预览逻辑通过
    src.gui.utils.preview_loader 与 BrowserView 共享复用。

    Attributes:
        _SPLITTER_SIZES: 搜索结果区分割器初始尺寸（结果:预览，单位像素）。
        _last_results: 最近一次搜索的 SearchResult 元组。
    """

    # 搜索结果区分割器初始尺寸（结果:预览，单位像素）
    _SPLITTER_SIZES: list[int] = [500, 500]

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        """初始化搜索视图。

        Args:
            parent: Qt 父部件。
        """
        super().__init__(parent)
        set_automation_id(self, "search_view")
        self._last_results: tuple = ()
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
        set_automation_id(splitter, "search_splitter")

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
        set_automation_id(widget, "search_bar")
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        # 关键词输入框
        self.search_input = QLineEdit(self)
        set_automation_id(self.search_input, "search_input")
        self.search_input.setPlaceholderText("输入关键词搜索知识库...")
        layout.addWidget(self.search_input, stretch=1)

        # 策略选择下拉框
        self._strategy_combo = QComboBox(self)
        set_automation_id(self._strategy_combo, "search_strategy")
        for display_name, _ in _STRATEGY_OPTIONS:
            self._strategy_combo.addItem(display_name)
        self._strategy_combo.setFixedWidth(90)
        layout.addWidget(self._strategy_combo)

        # 搜索按钮
        self._search_btn = QPushButton("搜索", self)
        set_automation_id(self._search_btn, "search_submit")
        self._search_btn.setFixedWidth(70)
        layout.addWidget(self._search_btn)

        return widget

    def _build_result_panel(self) -> QWidget:
        """构建结果列表面板。

        Returns:
            包含结果数量标签和结果表格的 QWidget。
        """
        widget = QWidget()
        set_automation_id(widget, "search_result_panel")
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        # 结果数量标签
        self._result_count_label = QLabel("输入关键词开始搜索")
        set_automation_id(self._result_count_label, "search_result_status")
        self._result_count_label.setProperty("class", "text-muted")
        layout.addWidget(self._result_count_label)

        # 结果表格
        self._result_model = EntryTableModel([], self)
        self._result_view = QTableView(self)
        set_automation_id(self._result_view, "search_result_table")
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
        set_automation_id(widget, "search_preview_panel")
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        self._preview_title = QLabel("详情")
        set_automation_id(self._preview_title, "search_preview_title")
        self._preview_title.setProperty("class", "panel-header")
        layout.addWidget(self._preview_title)

        self._preview_status_label = QLabel(self)
        set_automation_id(self._preview_status_label, "search_preview_status")
        self._preview_status_label.setWordWrap(True)
        self._preview_status_label.hide()
        layout.addWidget(self._preview_status_label)

        self._preview_text = QTextEdit(self)
        set_automation_id(self._preview_text, "search_preview_text")
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
        selection_model = self._result_view.selectionModel()
        if selection_model is None:
            raise RuntimeError("Search result table has no selection model")
        selection_model.currentRowChanged.connect(
            self._on_result_current_row_changed
        )
        selection_model.selectionChanged.connect(
            self._sync_selected_result_to_current_row
        )

    # ------------------------------------------------------------------
    # 搜索执行
    # ------------------------------------------------------------------

    def do_search(self) -> None:
        """执行全文搜索并更新结果列表。

        使用 BM25Retriever 进行 FTS5 全文搜索，并严格区分 success、
        no_hits、invalid、error 与 degraded。错误不允许伪装成零命中。
        """
        query = self.search_input.text().strip()
        if not query:
            self._clear_results()
            self._result_count_label.setText("查询无效：请输入搜索关键词")
            return

        try:
            from src.gui.stores import get_bm25_retriever
            retriever = get_bm25_retriever()
            response = retriever.search(query, limit=50)
            if not self._is_strict_response(response):
                raise TypeError("BM25Retriever 返回了非 SearchResponse 结果")

            self._render_response(query, response)
        except Exception as exc:
            # 不把 provider、数据库路径或凭据等底层异常原文暴露到 UI。
            logger.error("搜索 adapter 异常: type=%s", type(exc).__name__)
            self._clear_results()
            self._result_count_label.setText("搜索失败：服务暂不可用（错误代码：adapter_error）")

    def _render_response(self, query: str, response: "SearchResponse") -> None:
        """按严格五态渲染检索响应，不依赖隐式 list/bool 兼容。"""

        self._clear_results()

        if response.status == "no_hits":
            self._result_count_label.setText(f'搜索 "{query}" — 未找到匹配结果')
            logger.info("BM25 搜索完成: status=no_hits, query_length=%d", len(query))
            return

        if response.status == "invalid":
            self._result_count_label.setText(
                f"查询无效（错误代码：{self._issue_codes(response.issues)}）"
            )
            logger.warning("BM25 搜索拒绝无效查询")
            return

        if response.status == "error":
            self._result_count_label.setText(
                "搜索失败：服务暂不可用"
                f"（错误代码：{self._issue_codes(response.issues)}）"
            )
            logger.warning("BM25 搜索失败: codes=%s", self._issue_codes(response.issues))
            return

        results = response.results
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
                "word_count": meta.get("word_count", 0),
                "archived_at": meta.get("archived_at", ""),
                "file_path": meta.get("file_path", ""),
                "score": result.score,
                "highlight": result.highlight,
            })

        self._result_model.update_entries(entries)
        count = len(results)
        if response.status == "degraded":
            self._result_count_label.setText(
                "搜索降级："
                f"显示 {count} 条可用结果"
                f"（问题代码：{self._issue_codes(response.issues)}）"
            )
            logger.warning(
                "BM25 搜索降级: result_count=%d, codes=%s",
                count,
                self._issue_codes(response.issues),
            )
            return

        self._result_count_label.setText(f'搜索 "{query}" — 找到 {count} 条结果')
        logger.info("BM25 搜索完成: status=success, result_count=%d", count)

    def _clear_results(self) -> None:
        """清空上一次结果和预览，避免错误态残留旧数据。"""

        self._last_results = ()
        self._result_model.update_entries([])
        self._preview_text.clear()
        self._preview_title.setText("详情")
        self._set_preview_status("success")

    @staticmethod
    def _issue_codes(issues: tuple["RetrievalIssue", ...]) -> str:
        """只向界面公开稳定代码，不回显可能含敏感信息的异常消息。"""

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
        """Validate the five-state contract and bind it to the BM25 seam."""

        return (
            is_strict_search_response(response)
            and response.strategy == "bm25"
        )

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

    def _sync_selected_result_to_current_row(self, *_: object) -> None:
        """Turn one UIA-selected result row into the current row.

        This is intentionally an adapter only: ``currentRowChanged`` remains
        the sole detail-loading path for mouse, keyboard, and UIA selection.
        """
        selection_model = self._result_view.selectionModel()
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
            and current.model() == self._result_model
            and current.row() == selected_row
        ):
            return

        selected_index = self._result_model.index(selected_row, 0)
        if selected_index.isValid():
            selection_model.setCurrentIndex(
                selected_index,
                QItemSelectionModel.SelectionFlag.NoUpdate,
            )

    def _on_result_current_row_changed(
        self,
        current: QModelIndex,
        previous: QModelIndex,
    ) -> None:
        """Load preview for keyboard and native SelectionItem navigation."""

        del previous
        if current.isValid():
            self.on_result_selected(current)

    def _load_preview(self, entry: dict) -> None:
        """加载搜索结果条目的详情预览。

        优先显示搜索摘要（highlight），再通过 preview_loader 加载
        Markdown 全文；若全文不可用，则由 preview_loader 自动降级。

        Args:
            entry: 结果条目字典（含 file_path、highlight 等字段）。
        """
        title = entry.get("title", "未知标题")
        self._preview_title.setText(f"详情: {title[:40]}")
        self._set_preview_status("success")

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
            from src.gui.utils.preview_loader import (
                is_strict_preview_outcome,
                load_entry_preview_outcome,
            )

            md_store = get_markdown_store()
            outcome = load_entry_preview_outcome(entry, md_store)
            if not is_strict_preview_outcome(outcome):
                raise TypeError("预览加载器返回了无效结果")

            if outcome.status == "error":
                code, cause_type = self._preview_issue_diagnostics(outcome.issue)
                self._set_preview_status("error", code)
                self._preview_text.setPlainText("预览内容暂不可用。")
                logger.error(
                    "搜索预览失败: code=%s, error_type=%s",
                    code,
                    cause_type,
                )
                return

            if outcome.status == "degraded":
                code, cause_type = self._preview_issue_diagnostics(outcome.issue)
                self._set_preview_status("degraded", code)
                logger.warning(
                    "搜索预览降级: code=%s, error_type=%s",
                    code,
                    cause_type,
                )
            elif outcome.status != "success":
                raise ValueError("未知预览终态")

            if outcome.content.strip():
                preview_parts.append("\n---\n\n")
                preview_parts.append(outcome.content)
        except Exception as exc:
            cause_type = self._safe_preview_cause_type(type(exc).__name__)
            logger.error(
                "搜索预览 adapter 异常: code=%s, error_type=%s",
                _PREVIEW_ADAPTER_ERROR,
                cause_type,
            )
            self._set_preview_status("error", _PREVIEW_ADAPTER_ERROR)
            self._preview_text.setPlainText("预览内容暂不可用。")
            return

        if preview_parts:
            self._preview_text.setPlainText("".join(preview_parts))
        else:
            self._preview_text.setPlainText("（无内容）")

    def _set_preview_status(self, status: str, code: str = "") -> None:
        """Render an explicit preview terminal state using fixed public text."""

        if status == "success":
            self._preview_status_label.clear()
            self._preview_status_label.hide()
            return
        if status == "degraded":
            text = (
                "预览降级：Markdown 正文不可用，以下显示安全摘要"
                f"（问题代码：{code}）"
            )
        else:
            text = f"预览失败：正文不可用（错误代码：{code}）"
        self._preview_status_label.setProperty("previewStatus", status)
        self._preview_status_label.setText(text)
        self._preview_status_label.show()

    @staticmethod
    def _preview_issue_diagnostics(issue: Any) -> tuple[str, str]:
        """Extract only bounded identifiers from a structured preview issue."""

        raw_code = getattr(getattr(issue, "code", None), "value", None)
        code = (
            raw_code
            if isinstance(raw_code, str)
            and re.fullmatch(r"[a-z0-9_]{1,64}", raw_code)
            else _PREVIEW_ADAPTER_ERROR
        )
        cause_type = SearchView._safe_preview_cause_type(
            getattr(issue, "cause_type", None)
        )
        return code, cause_type

    @staticmethod
    def _safe_preview_cause_type(value: Any) -> str:
        """Bound a diagnostic type name before it reaches public logs."""

        return (
            value
            if isinstance(value, str)
            and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]{0,95}", value)
            else "PreviewUnavailable"
        )

    # ------------------------------------------------------------------
    # 公开方法（供 MainWindow 调用）
    # ------------------------------------------------------------------

    def focus_search_input(self) -> None:
        """聚焦搜索输入框（供快捷键 Ctrl+K 调用）。"""
        self.search_input.setFocus()
        self.search_input.selectAll()
