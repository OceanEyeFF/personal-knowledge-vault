"""知识条目表格数据模型。

提供 QAbstractTableModel 实现，用于在 QTableView 中展示知识条目列表。
列定义：ID、标题、来源类型、标签、字数、归档时间（共 6 列）。
"""

from __future__ import annotations

from typing import Any, Optional

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt

from src.utils.logger import get_logger

logger = get_logger(__name__)


class EntryTableModel(QAbstractTableModel):
    """知识条目表格数据模型。

    将知识条目列表（list[dict]）适配为 Qt 表格模型，支持 QTableView 展示。
    每条记录对应 SQLiteStore.list_entries() 返回的字典格式。

    Attributes:
        COLUMNS: 列标题列表，共 6 列。
        _entries: 内部存储的条目字典列表。

    Example:
        model = EntryTableModel(entries=[])
        view = QTableView()
        view.setModel(model)
        model.update_entries(store.list_entries(limit=20))
    """

    COLUMNS: list[str] = ["ID", "标题", "来源", "标签", "字数", "归档时间"]

    # 列索引常量，便于代码可读性
    COL_ID = 0
    COL_TITLE = 1
    COL_SOURCE_TYPE = 2
    COL_TAGS = 3
    COL_WORD_COUNT = 4
    COL_ARCHIVED_AT = 5

    # 列显示宽度（像素），供视图统一使用
    # 标题列（COL_TITLE=1）由 setStretchLastSection 或 horizontalHeader 自动伸展，不在此设置
    COLUMN_WIDTHS: dict[int, int] = {
        0: 50,   # COL_ID
        2: 70,   # COL_SOURCE_TYPE
        3: 120,  # COL_TAGS
        4: 60,   # COL_WORD_COUNT
        5: 90,   # COL_ARCHIVED_AT
    }

    def __init__(self, entries: list[dict], parent: Any = None) -> None:
        """初始化条目表格模型。

        Args:
            entries: 知识条目字典列表，每个字典来自 SQLiteStore.list_entries()。
            parent: Qt 父对象，通常为 None。
        """
        super().__init__(parent)
        self._entries: list[dict] = entries

    # ------------------------------------------------------------------
    # QAbstractTableModel 必须实现的接口
    # ------------------------------------------------------------------

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: B008
        """返回行数（条目总数）。

        Args:
            parent: 父索引，表格模型忽略此参数。

        Returns:
            当前存储的条目数量。
        """
        if parent.isValid():
            return 0
        return len(self._entries)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: B008
        """返回列数。

        Args:
            parent: 父索引，表格模型忽略此参数。

        Returns:
            固定返回 6（列数）。
        """
        if parent.isValid():
            return 0
        return len(self.COLUMNS)

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole) -> Any:  # type: ignore[assignment]
        """返回指定单元格的数据。

        Args:
            index: 单元格索引（行、列）。
            role: Qt 数据角色，仅处理 DisplayRole 和 TextAlignmentRole。

        Returns:
            单元格显示文本，或 None（不支持的 role）。
        """
        if not index.isValid():
            return None

        row = index.row()
        col = index.column()

        if row < 0 or row >= len(self._entries):
            return None

        entry = self._entries[row]

        if role == Qt.DisplayRole:  # type: ignore[attr-defined]
            return self._get_display_text(entry, col)

        if role == Qt.TextAlignmentRole:  # type: ignore[attr-defined]
            # 字数列右对齐
            if col == self.COL_WORD_COUNT:
                return Qt.AlignRight | Qt.AlignVCenter  # type: ignore[attr-defined]
            return Qt.AlignLeft | Qt.AlignVCenter  # type: ignore[attr-defined]

        return None

    def headerData(
        self,
        section: int,
        orientation: Qt.Orientation,  # type: ignore[name-defined]
        role: int = Qt.DisplayRole,  # type: ignore[assignment]
    ) -> Any:
        """返回表头数据。

        Args:
            section: 行或列编号。
            orientation: 水平（列标题）或垂直（行号）。
            role: Qt 数据角色。

        Returns:
            列标题字符串，或行号字符串。
        """
        if role != Qt.DisplayRole:  # type: ignore[attr-defined]
            return None

        if orientation == Qt.Horizontal:  # type: ignore[attr-defined]
            if 0 <= section < len(self.COLUMNS):
                return self.COLUMNS[section]
        else:
            # 垂直表头显示行号（从 1 开始）
            return str(section + 1)

        return None

    # ------------------------------------------------------------------
    # 公开方法
    # ------------------------------------------------------------------

    def update_entries(self, entries: list[dict]) -> None:
        """替换全部条目数据并通知视图刷新。

        Args:
            entries: 新的条目字典列表。
        """
        self.beginResetModel()
        self._entries = entries
        self.endResetModel()
        logger.debug(f"EntryTableModel 已更新：{len(entries)} 条记录")

    def get_entry(self, row: int) -> Optional[dict]:
        """获取指定行的原始条目字典。

        Args:
            row: 行索引（从 0 开始）。

        Returns:
            条目字典，或 None（行索引越界时）。
        """
        if 0 <= row < len(self._entries):
            return self._entries[row]
        return None

    # ------------------------------------------------------------------
    # 私有辅助方法
    # ------------------------------------------------------------------

    def _get_display_text(self, entry: dict, col: int) -> str:
        """根据列索引提取并格式化条目字段为显示文本。

        Args:
            entry: 条目字典。
            col: 列索引。

        Returns:
            格式化后的显示字符串。
        """
        if col == self.COL_ID:
            return str(entry.get("knowledge_id", ""))

        if col == self.COL_TITLE:
            title = entry.get("title", "")
            # 截断超长标题
            if len(title) > 60:
                title = title[:60] + "..."
            return title

        if col == self.COL_SOURCE_TYPE:
            return entry.get("source_type", "")

        if col == self.COL_TAGS:
            return self._format_tags(entry.get("tags", ""))

        if col == self.COL_WORD_COUNT:
            word_count = entry.get("word_count", 0)
            return str(word_count) if word_count else "0"

        if col == self.COL_ARCHIVED_AT:
            archived_at = entry.get("archived_at", "")
            # 仅显示日期部分（前 10 字符：YYYY-MM-DD）
            if archived_at and len(archived_at) >= 10:
                return archived_at[:10]
            return archived_at or ""

        return ""

    @staticmethod
    def _format_tags(tags_raw: Any) -> str:
        """将标签字段格式化为最多 3 个标签的显示字符串。

        SQLiteStore 存储的 tags 字段可能是逗号分隔字符串或列表。
        最多显示 3 个标签，用空格连接。

        Args:
            tags_raw: 标签原始值（str 或 list）。

        Returns:
            格式化后的标签显示字符串，最多包含 3 个标签。
        """
        if not tags_raw:
            return ""

        if isinstance(tags_raw, list):
            tag_list = [t.strip() for t in tags_raw if t and t.strip()]
        elif isinstance(tags_raw, str):
            tag_list = [t.strip() for t in tags_raw.split(",") if t.strip()]
        else:
            return ""

        # 最多显示 3 个标签
        display_tags = tag_list[:3]
        remaining = len(tag_list) - len(display_tags)

        result = " ".join(display_tags)
        if remaining > 0:
            result += f" +{remaining}"
        return result
