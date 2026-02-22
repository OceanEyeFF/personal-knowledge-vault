"""AI 对话界面 (M12)

完整的 AI 对话 UI 实现，包含：
- SessionSidebar：会话列表 + 新建按钮 + Token 统计面板
- ChatArea：消息显示区 + 输入区
- StreamRenderer：30ms 批量更新流式输出
- MessageDisplay：QTextBrowser + Markdown 渲染

技术栈：
- QTextBrowser + markdown2 + Pygments（轻量级，避免 QWebEngineView）
- StreamRenderer：30ms QTimer 批量更新（100 tokens/s 无卡顿）
- qasync @asyncSlot：异步流式输出
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from PySide6.QtCore import QPoint, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QKeyEvent, QTextCursor
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPushButton,
    QSplitter,
    QTextBrowser,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)
from markdown2 import Markdown
from pygments.formatters import HtmlFormatter

from src.gui.viewmodels import ChatViewModel
from src.gui.styles import theme_colors

logger = logging.getLogger("pkv.gui.views.chat")


# ===================================================================
# Markdown 渲染配置
# ===================================================================

# 初始化 Markdown 渲染器
md_renderer = Markdown(extras=[
    "fenced-code-blocks",  # 代码块支持
    "tables",              # 表格支持
    "strike",              # 删除线
    "code-friendly"        # 代码友好
])

# 生成 Pygments CSS
pygments_formatter = HtmlFormatter(style="monokai")
pygments_css = pygments_formatter.get_style_defs(".codehilite")


def render_markdown(text: str, role: str) -> str:
    """渲染 Markdown 为 HTML

    Args:
        text: Markdown 文本
        role: 角色（"user" 或 "assistant"）

    Returns:
        渲染后的 HTML 字符串
    """
    html_content = md_renderer.convert(text)
    colors = theme_colors.get_current_colors()

    css_class = "assistant" if role == "assistant" else "user"
    role_label = "🤖 Assistant" if role == "assistant" else "👤 You"

    template = f"""
    <style>
    {pygments_css}
    .msg-bubble {{
        padding: 10px 14px;
        border-radius: 10px;
        margin: 4px 0;
        line-height: 1.6;
        font-size: 14px;
        color: {colors['msg_fg']};
    }}
    .assistant {{
        background-color: {colors['assistant_bg']};
        border-left: 3px solid {colors['assistant_border']};
    }}
    .user {{
        background-color: {colors['user_bg']};
        border-left: 3px solid {colors['user_border']};
    }}
    .role-label {{
        font-size: 12px;
        color: {colors['role_label']};
        margin-bottom: 4px;
    }}
    pre {{
        background-color: {colors['code_bg']};
        color: {colors['code_fg']};
        padding: 10px;
        border-radius: 4px;
        overflow-x: auto;
        font-family: "Consolas", "Monaco", monospace;
        font-size: 13px;
    }}
    h1, h2, h3, h4 {{
        margin-top: 8px;
        margin-bottom: 4px;
    }}
    ul, ol {{
        margin: 4px 0;
        padding-left: 20px;
    }}
    p {{
        margin: 4px 0;
    }}
    </style>
    <div class="msg-bubble {css_class}">
        <div class="role-label">{role_label}</div>
        {html_content}
    </div>
    """

    return template


# ===================================================================
# TokenPanel - Token 统计面板
# ===================================================================

class TokenPanel(QWidget):
    """Token 统计面板

    显示：
    - 当前: {total} / 64,000
    - 轮数: {round_count} / 3
    - 输入: {input_tokens}
    - 输出: {output_tokens}

    警告：
    - 3 轮警告
    - 64K 警告
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        """初始化 Token 统计面板

        Args:
            parent: Qt 父对象
        """
        super().__init__(parent)

        layout = QVBoxLayout(self)

        # 标题
        title = QLabel("📊 Token 统计")
        title.setObjectName("token_panel_title")
        layout.addWidget(title)

        # 当前会话
        self.session_label = QLabel("当前: 0 / 64,000")
        layout.addWidget(self.session_label)

        # 轮数
        self.round_label = QLabel("轮数: 0 / 3")
        layout.addWidget(self.round_label)

        # 输入/输出
        self.input_label = QLabel("输入: 0")
        self.output_label = QLabel("输出: 0")
        layout.addWidget(self.input_label)
        layout.addWidget(self.output_label)

        # 警告区域
        self.warning_label = QLabel("")
        self.warning_label.setObjectName("token_warning")
        self.warning_label.setVisible(False)
        self.warning_label.setWordWrap(True)
        layout.addWidget(self.warning_label)

        layout.addStretch()  # 底部留白

    def update_stats(self, input_tokens: int, output_tokens: int, total_tokens: int, round_count: int = 0) -> None:
        """更新统计数据

        Args:
            input_tokens: 本轮输入 token 数
            output_tokens: 本轮输出 token 数
            total_tokens: 累计 token 数
            round_count: 对话轮数
        """
        self.session_label.setText(f"当前: {total_tokens:,} / 64,000")
        self.round_label.setText(f"轮数: {round_count} / 3")
        self.input_label.setText(f"输入: {input_tokens:,}")
        self.output_label.setText(f"输出: {output_tokens:,}")

        # 3 轮警告
        if round_count >= 3:
            self.warning_label.setText("⚠️ 已进行 3 轮对话\n建议结束或新建会话")
            self.warning_label.setVisible(True)
        # 64K 警告
        elif total_tokens >= 60000:
            self.warning_label.setText(f"⚠️ 上下文已接近 64K 限制\n当前: {total_tokens:,} / 64,000")
            self.warning_label.setVisible(True)
        else:
            self.warning_label.setVisible(False)


# ===================================================================
# SessionSidebar - 会话列表侧边栏
# ===================================================================

class SessionSidebar(QWidget):
    """会话列表侧边栏

    包含：
    - 新建按钮
    - 会话列表（QListWidget）
    - Token 统计面板
    """

    session_selected = Signal(str)  # 选中会话时发射（session_id）
    session_delete_requested = Signal(str)  # 请求删除会话时发射（session_id）
    session_save_to_kb_requested = Signal(str)  # 请求保存对话到知识库（session_id）

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        """初始化会话侧边栏

        Args:
            parent: Qt 父对象
        """
        super().__init__(parent)
        self.setObjectName("session_sidebar")

        layout = QVBoxLayout(self)

        # 新建按钮
        self.new_btn = QPushButton("📝 新建会话")
        self.new_btn.setMinimumHeight(40)
        layout.addWidget(self.new_btn)

        # 会话列表
        self.session_list = QListWidget()
        self.session_list.setObjectName("session_list")
        self.session_list.itemClicked.connect(self._on_item_clicked)
        self.session_list.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu
        )
        self.session_list.customContextMenuRequested.connect(
            self._on_context_menu
        )
        layout.addWidget(self.session_list)

        # Token 统计面板
        self.token_panel = TokenPanel()
        layout.addWidget(self.token_panel)

    def _on_item_clicked(self, item: QListWidgetItem) -> None:
        """会话列表项点击事件

        Args:
            item: 点击的列表项
        """
        session_id = item.data(Qt.ItemDataRole.UserRole)
        if session_id:
            self.session_selected.emit(session_id)

    def _on_context_menu(self, pos: QPoint) -> None:
        """会话列表右键菜单

        Args:
            pos: 鼠标相对于列表控件的位置
        """
        item = self.session_list.itemAt(pos)
        if not item:
            return

        session_id = item.data(Qt.ItemDataRole.UserRole)
        if not session_id:
            return

        menu = QMenu(self)

        save_action = QAction("📥 保存到知识库", self)
        save_action.triggered.connect(
            lambda: self.session_save_to_kb_requested.emit(session_id)
        )
        menu.addAction(save_action)

        menu.addSeparator()

        delete_action = QAction("🗑️ 删除会话", self)
        delete_action.triggered.connect(
            lambda: self.session_delete_requested.emit(session_id)
        )
        menu.addAction(delete_action)

        menu.exec(self.session_list.mapToGlobal(pos))

    def load_sessions(self, sessions: list) -> None:
        """加载会话列表

        Args:
            sessions: 会话字典列表
        """
        self.session_list.clear()

        for session in sessions:
            # 截断标题（最多 30 字符）
            title = session["title"]
            if len(title) > 30:
                title = title[:27] + "..."

            item = QListWidgetItem(f"• {title}")
            item.setData(Qt.ItemDataRole.UserRole, session["session_id"])
            self.session_list.addItem(item)


# ===================================================================
# StreamRenderer - 流式输出渲染器（30ms 批量更新）
# ===================================================================

class StreamRenderer(QWidget):
    """流式输出渲染器

    使用 QTimer 30ms 批量更新，避免高频 UI 刷新导致卡顿。

    核心逻辑：
    - 每次调用 add_token() 只是添加到缓冲区
    - QTimer 每 30ms 触发一次 flush()，批量插入文本
    - 100 tokens/s → 每 30ms 处理 3 个 token，减少 97% 的 UI 刷新
    """

    def __init__(self, browser: QTextBrowser, parent: Optional[QWidget] = None) -> None:
        """初始化流式渲染器

        Args:
            browser: QTextBrowser 实例
            parent: Qt 父对象
        """
        super().__init__(parent)

        self.browser = browser
        self.buffer = ""
        self.full_text = ""  # 累计完整文本，流式结束后用于 Markdown 渲染

        # 30ms 定时器
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.flush)
        self.timer.start(30)  # 30ms 刷新一次

    def add_token(self, token: str) -> None:
        """添加 token 到缓冲区

        Args:
            token: 新到达的 token
        """
        self.buffer += token
        self.full_text += token

    def flush(self) -> None:
        """批量更新到 UI（每 30ms 执行一次）"""
        if not self.buffer:
            return

        # 移动光标到末尾
        self.browser.moveCursor(QTextCursor.MoveOperation.End)
        # 插入文本（不会重新渲染整个页面）
        self.browser.insertPlainText(self.buffer)
        # 再次移动到末尾（自动滚动）
        self.browser.moveCursor(QTextCursor.MoveOperation.End)

        # 清空缓冲区
        self.buffer = ""

    def stop(self) -> None:
        """停止定时器并最后一次刷新"""
        self.timer.stop()
        self.flush()  # 最后一次刷新

    def get_full_text(self) -> str:
        """获取本轮完整文本并重置

        Returns:
            累计的完整 assistant 文本
        """
        text = self.full_text
        self.full_text = ""
        return text


# ===================================================================
# InputBox - 自定义输入框（快捷键 + @ 自动补全）
# ===================================================================

class InputBox(QTextEdit):
    """自定义输入框

    快捷键：
    - Enter: 发送消息（不换行）；自动补全激活时确认选择
    - Ctrl+Enter: 换行
    - @: 触发自动补全
    - ↑↓: 自动补全激活时导航候选列表
    - Esc: 关闭自动补全

    M12 Phase 2: 支持 @ 语法引用自动补全
    """

    send_triggered = Signal()  # Enter 键发送信号

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        """初始化输入框

        Args:
            parent: Qt 父对象
        """
        super().__init__(parent)

        # M12: 自动补全弹窗
        from src.gui.widgets.autocomplete_popup import AutocompletePopup
        self._popup = AutocompletePopup(self)
        self._popup_active = False
        self._at_start_pos = -1  # @ 符号在文本中的位置

        # 连接弹窗信号
        self._popup.item_selected.connect(self._on_autocomplete_selected)
        self._popup.popup_closed.connect(self._on_popup_closed)

        # 文本变化检测 @ 输入
        self.textChanged.connect(self._on_text_changed)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        """重写键盘事件

        Args:
            event: 键盘事件
        """
        # 自动补全激活时的特殊处理
        if self._popup_active and self._popup.isVisible():
            if event.key() == Qt.Key.Key_Return and event.modifiers() == Qt.KeyboardModifier.NoModifier:
                # Enter 确认自动补全选择
                if self._popup.confirm_selection():
                    event.accept()
                    return
            elif event.key() == Qt.Key.Key_Up:
                self._popup.navigate_up()
                event.accept()
                return
            elif event.key() == Qt.Key.Key_Down:
                self._popup.navigate_down()
                event.accept()
                return
            elif event.key() == Qt.Key.Key_Escape:
                self._popup.hide_popup()
                event.accept()
                return

        # Enter 发送（不换行）
        if event.key() == Qt.Key.Key_Return and event.modifiers() == Qt.KeyboardModifier.NoModifier:
            self.send_triggered.emit()
            event.accept()
        # Ctrl+Enter 换行
        elif event.key() == Qt.Key.Key_Return and event.modifiers() == Qt.KeyboardModifier.ControlModifier:
            super().keyPressEvent(event)
        else:
            super().keyPressEvent(event)

    def _on_text_changed(self) -> None:
        """文本变化时检测 @ 输入"""
        text = self.toPlainText()
        cursor_pos = self.textCursor().position()

        if not text or cursor_pos == 0:
            if self._popup_active:
                self._popup.hide_popup()
            return

        # 查找光标前最近的 @
        text_before_cursor = text[:cursor_pos]

        # 检测 @知识库/ 或 @搜索/
        at_knowledge_idx = text_before_cursor.rfind("@知识库/")
        at_search_idx = text_before_cursor.rfind("@搜索/")

        if at_knowledge_idx >= 0 and at_knowledge_idx > at_search_idx:
            # @知识库/ 模式
            prefix = "@知识库/"
            filter_start = at_knowledge_idx + len(prefix)
            filter_text = text_before_cursor[filter_start:]
            self._at_start_pos = at_knowledge_idx
            self._show_autocomplete("knowledge", filter_text)
        elif at_search_idx >= 0 and at_search_idx > at_knowledge_idx:
            # @搜索/ 模式
            prefix = "@搜索/"
            filter_start = at_search_idx + len(prefix)
            filter_text = text_before_cursor[filter_start:]
            self._at_start_pos = at_search_idx
            self._show_autocomplete("search", filter_text)
        elif self._popup_active:
            # 不在任何 @ 上下文中，关闭弹窗
            self._popup.hide_popup()

    def _show_autocomplete(self, mode: str, filter_text: str) -> None:
        """显示自动补全弹窗

        Args:
            mode: 补全模式
            filter_text: 过滤文本
        """
        from src.gui.widgets.autocomplete_popup import (
            MODE_KNOWLEDGE,
            MODE_SEARCH,
        )

        # 计算弹窗锚点位置（光标位置的全局坐标）
        cursor_rect = self.cursorRect()
        anchor = self.mapToGlobal(cursor_rect.bottomLeft())

        if self._popup_active:
            self._popup.update_filter(filter_text)
        else:
            self._popup_active = True
            self._popup.show_popup(mode, filter_text, anchor)

    def _on_autocomplete_selected(
        self, display_text: str, data: dict
    ) -> None:
        """自动补全选中回调

        将选中的引用插入到文本中，替换 @ 前缀和过滤文本。

        Args:
            display_text: 显示文本
            data: 条目数据
        """
        if self._at_start_pos < 0:
            return

        ref_type = data.get("ref_type", "knowledge")
        knowledge_id = data.get("knowledge_id", "")

        if ref_type == "knowledge":
            replacement = f"@知识库/{knowledge_id} "
        else:
            title = data.get("title", "")
            replacement = f"@搜索/{title} "

        # 替换文本
        text = self.toPlainText()
        cursor_pos = self.textCursor().position()
        new_text = (
            text[: self._at_start_pos]
            + replacement
            + text[cursor_pos:]
        )

        self.setPlainText(new_text)

        # 移动光标到替换文本之后
        cursor = self.textCursor()
        new_pos = self._at_start_pos + len(replacement)
        cursor.setPosition(min(new_pos, len(new_text)))
        self.setTextCursor(cursor)

        self._at_start_pos = -1
        self._popup_active = False

    def _on_popup_closed(self) -> None:
        """弹窗关闭回调"""
        self._popup_active = False
        self._at_start_pos = -1


# ===================================================================
# InputArea - 输入区域
# ===================================================================

class InputArea(QWidget):
    """输入区域

    包含：
    - 多行输入框（InputBox）
    - 发送按钮
    - 停止按钮（流式输出时显示）
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        """初始化输入区域

        Args:
            parent: Qt 父对象
        """
        super().__init__(parent)

        layout = QHBoxLayout(self)

        # 输入框
        self.input_box = InputBox()
        self.input_box.setObjectName("chat_input")
        self.input_box.setPlaceholderText(
            "输入消息... (Ctrl+Enter 换行, @知识库/ 或 @搜索/ 引用知识)"
        )
        self.input_box.setMaximumHeight(120)
        layout.addWidget(self.input_box)

        # 发送按钮
        self.send_btn = QPushButton("🚀 发送")
        self.send_btn.setObjectName("btn_send_to_chat")
        self.send_btn.setMinimumWidth(80)
        layout.addWidget(self.send_btn)

        # 停止按钮
        self.stop_btn = QPushButton("⏹ 停止")
        self.stop_btn.setObjectName("btn_stop")
        self.stop_btn.setMinimumWidth(80)
        self.stop_btn.setVisible(False)
        layout.addWidget(self.stop_btn)

        # 连接 InputBox 的 Enter 快捷键
        self.input_box.send_triggered.connect(self.send_btn.click)


# ===================================================================
# ChatArea - 消息显示 + 输入区
# ===================================================================

class ChatArea(QWidget):
    """聊天区域

    包含：
    - MessageDisplay（QTextBrowser）
    - InputArea
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        """初始化聊天区域

        Args:
            parent: Qt 父对象
        """
        super().__init__(parent)

        layout = QVBoxLayout(self)

        # 消息显示区
        self.message_display = QTextBrowser()
        self.message_display.setObjectName("message_display")
        self.message_display.setReadOnly(True)
        self.message_display.setOpenExternalLinks(True)
        layout.addWidget(self.message_display)

        # 输入区
        self.input_area = InputArea()
        layout.addWidget(self.input_area)

        # 流式渲染器
        self.stream_renderer = StreamRenderer(self.message_display)

    def add_user_message(self, content: str) -> None:
        """添加 User 消息

        Args:
            content: 消息内容
        """
        html = render_markdown(content, role="user")
        self.message_display.append(html)

    def start_assistant_message(self) -> None:
        """开始 Assistant 消息（流式输出前调用）

        记录当前文档长度，流式结束后用于定位并替换纯文本。
        """
        self._stream_start_pos = self.message_display.document().characterCount()
        colors = theme_colors.get_current_colors()
        self.message_display.append(
            f"<div class='role-label' style='font-size:12px; color:{colors['role_label']};'>🤖 Assistant</div>"
        )

    def finish_assistant_message(self) -> None:
        """结束 Assistant 消息（流式输出后调用）

        将流式期间插入的纯文本替换为渲染后的 Markdown HTML。
        """
        self.stream_renderer.stop()

        # 获取流式累计的完整文本
        full_text = self.stream_renderer.get_full_text()
        if not full_text:
            return

        # 删除流式期间的纯文本 + 开始标记，用 Markdown 渲染替换
        cursor = self.message_display.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.Start)
        # 定位到流式开始位置
        cursor.movePosition(
            QTextCursor.MoveOperation.Right,
            QTextCursor.MoveMode.MoveAnchor,
            self._stream_start_pos - 1,
        )
        # 选择到文档末尾
        cursor.movePosition(
            QTextCursor.MoveOperation.End,
            QTextCursor.MoveMode.KeepAnchor,
        )
        cursor.removeSelectedText()

        # 插入渲染后的 Markdown HTML
        html = render_markdown(full_text, role="assistant")
        self.message_display.append(html)


# ===================================================================
# ChatView - 主视图
# ===================================================================

class ChatView(QWidget):
    """AI 对话主视图

    布局：
    - 左侧：SessionSidebar（会话列表 + Token 面板）
    - 右侧：ChatArea（消息 + 输入）
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        """初始化 ChatView

        Args:
            parent: Qt 父对象
        """
        super().__init__(parent)

        # ViewModel
        self.viewmodel = ChatViewModel()

        # 主布局
        splitter = QSplitter(Qt.Orientation.Horizontal)

        # 左侧：会话侧边栏
        self.sidebar = SessionSidebar()
        splitter.addWidget(self.sidebar)

        # 右侧：聊天区域
        self.chat_area = ChatArea()
        splitter.addWidget(self.chat_area)

        # 设置分割比例（1:3）
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 3)

        # 设置主布局
        layout = QVBoxLayout(self)
        layout.addWidget(splitter)

        # 连接信号
        self._connect_signals()

        # 加载会话列表
        self._load_sessions()

        logger.info("ChatView 初始化完成")

    def _connect_signals(self) -> None:
        """连接 Signal/Slot"""
        # ViewModel → UI
        self.viewmodel.token_received.connect(self.chat_area.stream_renderer.add_token)
        self.viewmodel.token_usage_updated.connect(self._on_token_usage_updated)
        self.viewmodel.stream_finished.connect(self._on_stream_finished)
        self.viewmodel.error_occurred.connect(self._on_error)
        self.viewmodel.session_created.connect(self._on_session_created)

        # M12 Phase 3: URL 归档信号
        self.viewmodel.url_archive_started.connect(self._on_url_archive_started)
        self.viewmodel.url_archive_completed.connect(self._on_url_archive_completed)
        self.viewmodel.url_archive_failed.connect(self._on_url_archive_failed)

        # 对话保存到知识库
        self.viewmodel.session_saved_to_kb.connect(self._on_session_saved_to_kb)
        self.viewmodel.session_save_to_kb_failed.connect(self._on_session_save_to_kb_failed)

        # UI → ViewModel
        self.sidebar.new_btn.clicked.connect(self._on_new_session)
        self.sidebar.session_selected.connect(self._on_session_selected)
        self.sidebar.session_delete_requested.connect(self._on_delete_session)
        self.sidebar.session_save_to_kb_requested.connect(self._on_save_to_kb)
        self.chat_area.input_area.send_btn.clicked.connect(self._on_send_clicked)
        self.chat_area.input_area.stop_btn.clicked.connect(self._on_stop_clicked)

    def _load_sessions(self) -> None:
        """加载会话列表"""
        sessions = self.viewmodel.list_sessions(is_archived=False)
        self.sidebar.load_sessions(sessions)

    def _on_new_session(self) -> None:
        """新建会话"""
        try:
            self.viewmodel.create_new_session()
            self.chat_area.message_display.clear()
            self.sidebar.token_panel.update_stats(0, 0, 0, 0)
            logger.info("✅ 新建会话成功")
        except Exception as e:
            logger.error(f"新建会话失败: {e}")

    def _on_delete_session(self, session_id: str) -> None:
        """删除指定会话

        Args:
            session_id: 要删除的会话 ID
        """
        if self.viewmodel.delete_session(session_id):
            # 如果删除的是当前显示的会话，清空聊天区域
            if not self.viewmodel.current_session_id:
                self.chat_area.message_display.clear()
                self.sidebar.token_panel.update_stats(0, 0, 0, 0)
            self._load_sessions()
            logger.info(f"🗑️ 会话已删除: {session_id}")

    def _on_save_to_kb(self, session_id: str) -> None:
        """保存对话到知识库

        Args:
            session_id: 会话 ID
        """
        self.viewmodel.save_session_to_knowledge_base(session_id)
        colors = theme_colors.get_current_colors()
        self.chat_area.message_display.append(
            f"<p style='color: {colors['status_info']};'>📥 正在保存对话到知识库...</p>"
        )

    def _on_session_saved_to_kb(self, session_id: str, knowledge_id: int) -> None:
        """对话保存到知识库成功

        Args:
            session_id: 会话 ID
            knowledge_id: 新建的知识条目 ID
        """
        colors = theme_colors.get_current_colors()
        self.chat_area.message_display.append(
            f"<p style='color: {colors['status_success']};'>✅ 对话已保存到知识库 (ID: {knowledge_id})</p>"
        )

    def _on_session_save_to_kb_failed(self, session_id: str, error_msg: str) -> None:
        """对话保存到知识库失败

        Args:
            session_id: 会话 ID
            error_msg: 错误信息
        """
        colors = theme_colors.get_current_colors()
        self.chat_area.message_display.append(
            f"<p style='color: {colors['status_error']};'>❌ 保存失败: {error_msg}</p>"
        )

    def _on_session_created(self, session_id: str, title: str) -> None:
        """会话创建完成（刷新列表）

        Args:
            session_id: 会话 ID
            title: 会话标题
        """
        self._load_sessions()

    def _on_session_selected(self, session_id: str) -> None:
        """会话选中事件

        Args:
            session_id: 会话 ID
        """
        if self.viewmodel.load_session(session_id):
            # 加载成功，显示历史消息
            self.chat_area.message_display.clear()
            messages = self.viewmodel.get_current_messages()

            for msg in messages:
                role = msg["role"]
                content = msg["content"]
                if role == "user":
                    self.chat_area.add_user_message(content)
                elif role == "assistant":
                    html = render_markdown(content, role="assistant")
                    self.chat_area.message_display.append(html)
                    self.chat_area.message_display.append("")

            # 更新 Token 统计
            stats = self.viewmodel.get_token_stats()
            self.sidebar.token_panel.update_stats(0, 0, stats["total_tokens"], stats["round_count"])

            logger.info(f"✅ 加载会话: {session_id}")

    def _on_send_clicked(self) -> None:
        """发送按钮点击事件

        M12 Phase 2: 发送前解析 @ 引用，注入知识上下文
        """
        user_message = self.chat_area.input_area.input_box.toPlainText().strip()
        if not user_message:
            return

        # M12 Phase 2: 解析 @ 引用
        self._resolve_and_inject_references(user_message)

        # M12 Phase 3: 检测 URL 并触发异步归档
        self._detect_and_archive_urls(user_message)

        # 显示 User 消息（保留 @ 语法供用户查看）
        self.chat_area.add_user_message(user_message)

        # 清空输入框
        self.chat_area.input_area.input_box.clear()

        # 开始 Assistant 流式输出
        self.chat_area.start_assistant_message()

        # 切换按钮状态
        self.chat_area.input_area.send_btn.setVisible(False)
        self.chat_area.input_area.stop_btn.setVisible(True)

        # 发送消息时移除 @ 语法（API 只需要纯文本 + 上下文）
        from src.gui.utils.knowledge_ref import strip_at_references
        clean_message = strip_at_references(user_message)
        if not clean_message:
            clean_message = user_message  # 全是引用时保留原文

        # 调用 ViewModel（异步）
        self.viewmodel.send_message(clean_message)

    def _resolve_and_inject_references(self, text: str) -> None:
        """解析消息中的 @ 引用并注入知识上下文

        Args:
            text: 用户输入的消息文本
        """
        from src.gui.utils.knowledge_ref import (
            parse_at_references,
            build_knowledge_reference,
            format_context_message,
            format_reference_card_html,
        )

        refs = parse_at_references(text)
        if not refs:
            return

        knowledge_refs = []
        for ref in refs:
            try:
                if ref.ref_type == "knowledge":
                    # 直接按 ID 查找
                    entry, content = self._load_entry_by_id(int(ref.value))
                    if entry:
                        kref = build_knowledge_reference(entry, content)
                        knowledge_refs.append(kref)
                elif ref.ref_type == "search":
                    # 搜索并取第一个结果
                    entry, content = self._search_entry(ref.value)
                    if entry:
                        kref = build_knowledge_reference(entry, content)
                        knowledge_refs.append(kref)
            except Exception as e:
                logger.warning(f"解析引用失败 ({ref.original_text}): {e}")

        if knowledge_refs:
            # 显示引用卡片
            for kref in knowledge_refs:
                card = format_reference_card_html(kref)
                self.chat_area.message_display.append(card)

            # 注入上下文
            context = format_context_message(knowledge_refs)
            self.viewmodel.set_knowledge_context(context)

            logger.info(f"📎 已注入 {len(knowledge_refs)} 个知识引用")

    def _load_entry_by_id(self, knowledge_id: int) -> tuple:
        """按 ID 加载知识条目

        Args:
            knowledge_id: 知识条目 ID

        Returns:
            (entry_dict, content_text) 或 (None, "")
        """
        try:
            from src.gui.stores import get_sqlite_store, get_markdown_store
            from src.gui.utils.preview_loader import load_entry_preview

            store = get_sqlite_store()
            entry = store.query_by_id(knowledge_id)
            if not entry:
                return None, ""

            md_store = get_markdown_store()
            content = load_entry_preview(entry, md_store)
            return entry, content
        except Exception as e:
            logger.error(f"加载条目 {knowledge_id} 失败: {e}")
            return None, ""

    def _search_entry(self, keyword: str) -> tuple:
        """搜索知识条目（取第一个结果）

        Args:
            keyword: 搜索关键词

        Returns:
            (entry_dict, content_text) 或 (None, "")
        """
        try:
            from src.gui.stores import get_bm25_retriever, get_sqlite_store, get_markdown_store
            from src.gui.utils.preview_loader import load_entry_preview

            retriever = get_bm25_retriever()
            results = retriever.search(keyword, limit=1)
            if not results:
                return None, ""

            store = get_sqlite_store()
            entry = store.query_by_id(results[0].knowledge_id)
            if not entry:
                return None, ""

            md_store = get_markdown_store()
            content = load_entry_preview(entry, md_store)
            return entry, content
        except Exception as e:
            logger.error(f"搜索 '{keyword}' 失败: {e}")
            return None, ""

    # ------------------------------------------------------------------
    # M12 Phase 3: URL 自动检测和归档
    # ------------------------------------------------------------------

    def _detect_and_archive_urls(self, text: str) -> None:
        """检测消息中的 URL 并触发异步归档

        Args:
            text: 用户输入的消息文本
        """
        from src.gui.utils.knowledge_ref import detect_urls

        urls = detect_urls(text)
        if not urls:
            return

        for url in urls:
            logger.info(f"🔗 检测到 URL: {url}")
            # 异步归档（不阻塞消息发送）
            self.viewmodel.archive_url_and_inject(url)

    def _on_url_archive_started(self, url: str) -> None:
        """URL 归档开始

        Args:
            url: 正在归档 of the URL
        """
        colors = theme_colors.get_current_colors()
        self.chat_area.message_display.append(
            f"<p style='color: {colors['status_progress']}; font-size: 12px;'>"
            f"🔄 正在归档: {url}...</p>"
        )

    def _on_url_archive_completed(self, url: str, entry: dict) -> None:
        """URL 归档完成，注入上下文

        Args:
            url: 已归档的 URL
            entry: 归档后的条目字典
        """
        from src.gui.utils.knowledge_ref import (
            build_knowledge_reference,
            format_context_message,
            format_reference_card_html,
        )

        try:
            # 构建引用（使用摘要作为内容，不加载全文）
            ref = build_knowledge_reference(entry)
            card = format_reference_card_html(ref)
            self.chat_area.message_display.append(card)

            # 注入上下文
            context = format_context_message([ref])
            self.viewmodel.set_knowledge_context(context)

            title = entry.get("title", url)
            logger.info(f"✅ URL 归档并注入: {title}")

        except Exception as e:
            logger.error(f"URL 归档后处理失败: {e}")

    def _on_url_archive_failed(self, url: str, error_msg: str) -> None:
        """URL 归档失败

        Args:
            url: 归档失败的 URL
            error_msg: 错误消息
        """
        colors = theme_colors.get_current_colors()
        self.chat_area.message_display.append(
            f"<p style='color: {colors['status_warning']}; font-size: 12px;'>"
            f"⚠️ URL 归档失败: {url}<br>"
            f"原因: {error_msg}</p>"
        )

    def _on_stop_clicked(self) -> None:
        """停止按钮点击事件"""
        self.viewmodel.stop_stream()
        self.chat_area.finish_assistant_message()

        # 切换按钮状态
        self.chat_area.input_area.send_btn.setVisible(True)
        self.chat_area.input_area.stop_btn.setVisible(False)

        logger.info("🛑 用户停止流式输出")

    def _on_stream_finished(self) -> None:
        """流式输出完成"""
        self.chat_area.finish_assistant_message()

        # 切换按钮状态
        self.chat_area.input_area.send_btn.setVisible(True)
        self.chat_area.input_area.stop_btn.setVisible(False)

        logger.info("✅ 流式输出完成")

    def _on_token_usage_updated(self, input_tokens: int, output_tokens: int, total_tokens: int) -> None:
        """Token 统计更新

        Args:
            input_tokens: 本轮输入 token
            output_tokens: 本轮输出 token
            total_tokens: 累计 token
        """
        stats = self.viewmodel.get_token_stats()
        self.sidebar.token_panel.update_stats(
            input_tokens,
            output_tokens,
            total_tokens,
            stats["round_count"]
        )

    def _on_error(self, error_msg: str) -> None:
        """错误处理

        Args:
            error_msg: 错误消息
        """
        logger.error(f"❌ 错误: {error_msg}")
        colors = theme_colors.get_current_colors()
        self.chat_area.message_display.append(f"<p style='color: {colors['status_error']};'>❌ 错误: {error_msg}</p>")

        # 恢复按钮状态
        self.chat_area.input_area.send_btn.setVisible(True)
        self.chat_area.input_area.stop_btn.setVisible(False)

    # ------------------------------------------------------------------
    # M12 Phase 1: 从 BrowserView 接收知识条目
    # ------------------------------------------------------------------

    def start_session_with_reference(
        self, entry: dict, content: str
    ) -> None:
        """创建带知识引用的新对话会话

        从 BrowserView 接收知识条目，创建新会话并：
        1. 构建知识引用（智能截断）
        2. 显示引用卡片
        3. 设置 system message 上下文
        4. 刷新会话列表

        Args:
            entry: 知识条目字典（来自 SQLiteStore）
            content: 条目全文内容
        """
        from src.gui.utils.knowledge_ref import (
            build_knowledge_reference,
            format_context_message,
            format_reference_card_html,
        )

        try:
            # 1. 构建知识引用（智能截断）
            ref = build_knowledge_reference(entry, content)

            # 2. 创建新会话（标题包含引用信息）
            title = f"讨论: {ref.title[:20]}"
            self.viewmodel.create_new_session(title=title)

            # 3. 清空显示区并显示引用卡片
            self.chat_area.message_display.clear()
            card_html = format_reference_card_html(ref)
            self.chat_area.message_display.append(card_html)

            # 4. 设置知识引用上下文
            context_msg = format_context_message([ref])
            self.viewmodel.set_knowledge_context(context_msg)

            # 5. 更新 Token 面板
            self.sidebar.token_panel.update_stats(
                0, 0, ref.token_count, 0
            )

            # 6. 刷新会话列表
            self._load_sessions()

            # 7. 聚焦输入框
            self.chat_area.input_area.input_box.setFocus()

            logger.info(
                f"✅ 创建引用会话: {ref.title} "
                f"(~{ref.token_count} tokens, "
                f"截断: {ref.is_truncated})"
            )

        except Exception as e:
            error_msg = f"创建引用会话失败: {e}"
            logger.error(error_msg, exc_info=True)
            self._on_error(error_msg)
