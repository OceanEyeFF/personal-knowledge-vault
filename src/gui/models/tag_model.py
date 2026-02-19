"""标签树形数据模型。

提供 QStandardItemModel 实现，用于在 QTreeView 中展示标签列表。
每个标签节点存储标签名（UserRole）和显示文本（"名称 (数量)"）。
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtGui import QStandardItem, QStandardItemModel

from src.utils.logger import get_logger

logger = get_logger(__name__)

# 存储原始标签名的自定义 Qt 角色
_TAG_NAME_ROLE = Qt.UserRole  # type: ignore[attr-defined]


class TagTreeModel(QStandardItemModel):
    """标签树形数据模型。

    将标签列表（list[dict]）适配为 Qt 标准 Item 模型，
    支持 QTreeView 展示及标签筛选操作。

    每个标签 item 的 UserRole 存储原始标签名（str），
    DisplayRole 显示 "标签名 (数量)" 格式文本。

    Attributes:
        HEADER_LABELS: 列标题列表。

    Example:
        model = TagTreeModel()
        view = QTreeView()
        view.setModel(model)
        model.update_tags([{"name": "AI", "count": 5}])

        # 获取选中标签名
        index = view.currentIndex()
        tag_name = model.data(index, Qt.UserRole)
    """

    HEADER_LABELS: list[str] = ["标签 (数量)"]

    def __init__(self, parent: Any = None) -> None:
        """初始化标签树形模型。

        Args:
            parent: Qt 父对象，通常为 None。
        """
        super().__init__(parent)
        self.setColumnCount(1)
        self.setHorizontalHeaderLabels(self.HEADER_LABELS)

    def update_tags(self, tags: list[dict]) -> None:
        """替换全部标签数据并通知视图刷新。

        先清空现有数据，再重建"全部"根节点和各标签节点。
        "全部"节点的 UserRole 为 None（表示不按标签筛选）。

        Args:
            tags: 标签字典列表，每个字典包含 "name"（str）和 "count"（int）字段。
                  通常来自 SQLiteStore.get_all_tags_with_count()。

        Example:
            model.update_tags([
                {"name": "AI", "count": 10},
                {"name": "Python", "count": 5},
            ])
        """
        self.clear()
        self.setHorizontalHeaderLabels(self.HEADER_LABELS)

        # 添加"全部"根节点（UserRole 为 None，表示不筛选）
        all_item = QStandardItem("全部")
        all_item.setData(None, _TAG_NAME_ROLE)
        all_item.setEditable(False)
        self.appendRow(all_item)

        # 添加各标签节点
        for tag in tags:
            name = tag.get("name", "")
            count = tag.get("count", 0)
            if not name:
                continue

            item = QStandardItem(f"{name} ({count})")
            item.setData(name, _TAG_NAME_ROLE)
            item.setEditable(False)
            self.appendRow(item)

        logger.debug(f"TagTreeModel 已更新：{len(tags)} 个标签")

    def get_tag_name(self, index: Any) -> str | None:
        """从模型索引获取原始标签名。

        Args:
            index: QModelIndex 对象。

        Returns:
            标签名字符串，或 None（"全部"节点或无效索引时）。
        """
        item = self.itemFromIndex(index)
        if item is None:
            return None
        return item.data(_TAG_NAME_ROLE)
