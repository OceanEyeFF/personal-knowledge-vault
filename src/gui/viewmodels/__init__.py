"""GUI ViewModel 包。

提供 MVVM 模式的 ViewModel 层，管理视图状态与业务逻辑：
- ArchiveViewModel: 归档操作（URL / 纯文本）的状态管理与后台执行
- SettingsViewModel: 应用设置的读取与持久化
- ChatViewModel: AI 对话（M12）的会话管理与流式输出
"""

from src.gui.viewmodels.chat_viewmodel import ChatViewModel

__all__ = ["ChatViewModel"]
