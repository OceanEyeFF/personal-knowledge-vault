"""GUI 数据模型包。

提供与 PySide6 Qt 模型视图架构兼容的数据模型：
- EntryTableModel: 知识条目表格模型
- TagTreeModel: 标签树形模型
"""

from src.gui.models.entry_model import EntryTableModel
from src.gui.models.tag_model import TagTreeModel

__all__ = ["EntryTableModel", "TagTreeModel"]
