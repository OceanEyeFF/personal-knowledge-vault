"""知识库统计视图。

展示知识库的综合统计信息，包括:
- 概况: 总条目数、来源类型数、标签总数
- 来源分布: 按来源类型的条目数量柱状图
- 热门标签 Top 10: 按引用次数排名的标签柱状图

数据通过 SQLiteStore.get_statistics() 获取，
手动刷新或视图初始化时自动加载。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

logger = logging.getLogger("pkv.gui.stats")


# ============================================================
# StatsView 主类
# ============================================================


class StatsView(QWidget):
    """知识库统计视图。

    展示知识库的条目总数、来源分布和热门标签信息。
    数据从 SQLiteStore 延迟获取，支持手动刷新。

    统计数据格式（来自 SQLiteStore.get_statistics()）:
    - total_entries: int -- 总条目数
    - by_source_type: List[Tuple[str, int]] -- 来源类型分布
    - top_tags: List[Dict] -- 热门标签（每项含 name, count）

    Attributes:
        _stats: 最近一次加载的统计数据（可能为 None）。
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        """初始化统计视图。

        Args:
            parent: Qt 父部件。
        """
        super().__init__(parent)
        self._stats: Optional[Dict[str, Any]] = None

        self._init_ui()
        self._connect_signals()

        # 初始加载
        self.refresh()

    # ------------------------------------------------------------------
    # UI 构建
    # ------------------------------------------------------------------

    def _init_ui(self) -> None:
        """构建统计视图布局（概况 + 来源分布 + 热门标签 + 刷新按钮）。"""
        self._main_layout = QVBoxLayout(self)
        self._main_layout.setContentsMargins(16, 16, 16, 16)
        self._main_layout.setSpacing(12)

        # -- 概况分组 --
        self._overview_group = QGroupBox("概况", self)
        self._overview_layout = QVBoxLayout(self._overview_group)
        self._overview_layout.setSpacing(6)

        self._total_label = QLabel("总条目数: --", self)
        self._source_type_count_label = QLabel("来源类型数: --", self)
        self._tag_count_label = QLabel("标签总数: --", self)

        self._overview_layout.addWidget(self._total_label)
        self._overview_layout.addWidget(self._source_type_count_label)
        self._overview_layout.addWidget(self._tag_count_label)

        self._main_layout.addWidget(self._overview_group)

        # -- 来源分布分组 --
        self._source_group = QGroupBox("来源分布", self)
        self._source_layout = QVBoxLayout(self._source_group)
        self._source_layout.setSpacing(4)
        self._source_placeholder = QLabel("暂无数据", self)
        self._source_placeholder.setProperty("class", "text-muted")
        self._source_layout.addWidget(self._source_placeholder)
        self._main_layout.addWidget(self._source_group)

        # -- 热门标签分组 --
        self._tags_group = QGroupBox("热门标签 Top 10", self)
        self._tags_layout = QVBoxLayout(self._tags_group)
        self._tags_layout.setSpacing(4)
        self._tags_placeholder = QLabel("暂无数据", self)
        self._tags_placeholder.setProperty("class", "text-muted")
        self._tags_layout.addWidget(self._tags_placeholder)
        self._main_layout.addWidget(self._tags_group)

        # -- 刷新按钮 --
        self._refresh_btn = QPushButton("刷新统计", self)
        self._refresh_btn.setFixedWidth(100)
        self._main_layout.addWidget(self._refresh_btn, alignment=Qt.AlignRight)  # type: ignore[arg-type]

        # 弹性空间
        self._main_layout.addStretch()

    # ------------------------------------------------------------------
    # 信号连接
    # ------------------------------------------------------------------

    def _connect_signals(self) -> None:
        """连接控件信号与槽。"""
        self._refresh_btn.clicked.connect(self.refresh)

    # ------------------------------------------------------------------
    # 数据刷新
    # ------------------------------------------------------------------

    def refresh(self) -> None:
        """从 SQLiteStore 重新加载统计数据并更新 UI。

        加载失败时显示错误提示。
        """
        try:
            from src.gui.stores import get_sqlite_store
            store = get_sqlite_store()
            self._stats = store.get_statistics()
            self._render_stats()
            logger.debug("统计数据已刷新")
        except Exception as exc:
            logger.warning(f"加载统计数据失败: {exc}")
            self._stats = None
            self._render_error()

    # ------------------------------------------------------------------
    # UI 渲染
    # ------------------------------------------------------------------

    def _render_stats(self) -> None:
        """根据已加载的统计数据渲染所有分组。"""
        if self._stats is None:
            self._render_error()
            return

        # 概况
        total = self._stats.get("total_entries", 0)
        by_source: List[Tuple[str, int]] = self._stats.get("by_source_type", [])
        top_tags: List[Dict[str, Any]] = self._stats.get("top_tags", [])

        self._total_label.setText(f"总条目数: {total}")
        self._source_type_count_label.setText(f"来源类型数: {len(by_source)}")
        self._tag_count_label.setText(f"标签总数: {len(top_tags)}")

        # 来源分布
        self._rebuild_bar_group(
            self._source_group,
            self._source_layout,
            [(name, count) for name, count in by_source],
        )

        # 热门标签 Top 10
        tag_items = [(t["name"], t["count"]) for t in top_tags[:10]]
        self._rebuild_bar_group(
            self._tags_group,
            self._tags_layout,
            tag_items,
        )

    def _render_error(self) -> None:
        """渲染加载失败状态。"""
        self._total_label.setText("总条目数: 加载失败")
        self._source_type_count_label.setText("来源类型数: --")
        self._tag_count_label.setText("标签总数: --")

        self._clear_layout(self._source_layout)
        error_label = QLabel("加载失败", self)
        error_label.setProperty("status", "error")
        self._source_layout.addWidget(error_label)

        self._clear_layout(self._tags_layout)
        error_label2 = QLabel("加载失败", self)
        error_label2.setProperty("status", "error")
        self._tags_layout.addWidget(error_label2)

    def _rebuild_bar_group(
        self,
        group: QGroupBox,
        layout: QVBoxLayout,
        items: List[Tuple[str, int]],
    ) -> None:
        """重建分组内的进度条列表。

        Args:
            group: 父 QGroupBox。
            layout: 分组内的 QVBoxLayout。
            items: (名称, 数量) 元组列表。
        """
        self._clear_layout(layout)

        if not items:
            placeholder = QLabel("暂无数据", self)
            placeholder.setProperty("class", "text-muted")
            layout.addWidget(placeholder)
            return

        # 找到最大值作为进度条满格参考
        max_count = max(count for _, count in items) if items else 1
        if max_count <= 0:
            max_count = 1

        for name, count in items:
            row_widget = QWidget(self)
            row_layout = QHBoxLayout(row_widget)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(8)

            # 名称标签（固定宽度，右对齐）
            name_label = QLabel(name, self)
            name_label.setFixedWidth(80)
            name_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)  # type: ignore[arg-type]
            row_layout.addWidget(name_label)

            # 进度条
            bar = QProgressBar(self)
            bar.setRange(0, max_count)
            bar.setValue(count)
            bar.setTextVisible(False)
            bar.setFixedHeight(16)
            row_layout.addWidget(bar, stretch=1)

            # 数量标签
            count_label = QLabel(f"{count}", self)
            count_label.setFixedWidth(40)
            count_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)  # type: ignore[arg-type]
            row_layout.addWidget(count_label)

            layout.addWidget(row_widget)

    @staticmethod
    def _clear_layout(layout: QVBoxLayout) -> None:
        """清空布局中的所有子部件。

        Args:
            layout: 需要清空的 QVBoxLayout。
        """
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
