"""Personal Knowledge Vault - GUI 模块。

提供基于 PySide6 (Qt6) 的桌面图形界面，包含知识条目浏览器、全文搜索、
Markdown 预览等核心功能。

启动方式:
    python -m src.gui.app        # 直接运行
    python src/gui/app.py        # 脚本方式

版本由 ``src.__version__`` 统一提供。
"""

from src import __version__


__all__ = ["__version__"]
