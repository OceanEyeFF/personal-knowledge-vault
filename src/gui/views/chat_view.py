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

from dataclasses import dataclass
import html
import logging
from typing import Any, Optional

from PySide6.QtCore import QPoint, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QDesktopServices, QKeyEvent, QTextCursor
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

from src.gui.viewmodels.chat_viewmodel import (
    ChatViewModel,
    is_strict_session_list_projection,
)
from src.gui.styles import theme_colors
from src.gui.utils.preview_loader import (
    PreviewIssue,
    is_strict_preview_outcome,
    load_entry_preview_outcome,
)
from src.gui.utils.search_response_contract import is_strict_search_response
from src.gui.widgets.accessibility import set_automation_id
from src.processors.safe_fetch import describe_url_target
from src.runtime.errors import ErrorCode

logger = logging.getLogger("pkv.gui.views.chat")

_REFERENCE_PUBLIC_STAGES = frozenset({
    "reference_lookup",
    "reference_search",
    "reference_preview",
    "reference_prepare",
    "preview_input",
    "preview_path",
    "preview_content",
    "preview_markdown",
    "preview_summary",
})
_REFERENCE_ENTRY_OPTIONAL_TEXT_FIELDS = (
    "source_type",
    "source_url",
    "summary_one_sentence",
    "summary_100_words",
    "file_path",
    "archived_at",
)
_PUBLIC_ERROR_CODES = frozenset(code.value for code in ErrorCode)
_REQUEST_STATUS_TEXT = {
    "idle": "就绪",
    "running": "请求中",
    "running_other": "请求中（其他会话）",
    "completed": "已完成",
    "stopped": "已停止且未保存",
}


@dataclass(frozen=True)
class _ReferenceResolution:
    """Structured result for one explicit @knowledge/@search reference."""

    status: str
    entry: dict[str, Any] | None = None
    content: str = ""
    issues: tuple[PreviewIssue, ...] = ()


def _reference_issue(
    code: ErrorCode,
    *,
    stage: str,
    recoverable: bool,
    cause_type: str,
) -> PreviewIssue:
    return PreviewIssue(
        code=code,
        stage=stage,
        recoverable=recoverable,
        cause_type=cause_type,
    )


def _reference_error(
    code: ErrorCode,
    *,
    stage: str,
    recoverable: bool,
    cause_type: str,
) -> _ReferenceResolution:
    return _ReferenceResolution(
        status="error",
        issues=(
            _reference_issue(
                code,
                stage=stage,
                recoverable=recoverable,
                cause_type=cause_type,
            ),
        ),
    )


def _project_reference_entry(
    entry: Any,
    *,
    expected_knowledge_id: int,
) -> dict[str, Any] | None:
    """Snapshot only fields consumed by preview/reference rendering."""

    if type(entry) is not dict:
        return None
    knowledge_id = entry.get("knowledge_id")
    title = entry.get("title")
    if (
        type(knowledge_id) is not int
        or knowledge_id <= 0
        or knowledge_id != expected_knowledge_id
        or type(title) is not str
        or not title.strip()
    ):
        return None

    projected: dict[str, Any] = {
        "knowledge_id": knowledge_id,
        "title": title,
    }
    for field in _REFERENCE_ENTRY_OPTIONAL_TEXT_FIELDS:
        if field not in entry:
            continue
        value = entry[field]
        if value is not None and type(value) is not str:
            return None
        projected[field] = value

    if "tags" in entry:
        tags = entry["tags"]
        if tags is not None and type(tags) not in {str, list}:
            return None
        if type(tags) is list:
            if not all(type(tag) is str for tag in tags):
                return None
            tags = list(tags)
        projected["tags"] = tags
    return projected


def _preview_resolution(
    entry: dict[str, Any],
    outcome: Any,
    *,
    prior_issues: tuple[PreviewIssue, ...] = (),
) -> _ReferenceResolution:
    """Combine preview and earlier retrieval degradation without flattening."""

    if not is_strict_preview_outcome(outcome):
        return _reference_error(
            ErrorCode.RESOURCE_NOT_READABLE,
            stage="reference_preview",
            recoverable=False,
            cause_type="InvalidPreviewOutcome",
        )
    if outcome.status == "error":
        return _ReferenceResolution(status="error", issues=(outcome.issue,))
    issues = prior_issues
    if outcome.status == "degraded":
        issues = (*issues, outcome.issue)
    return _ReferenceResolution(
        status="degraded" if issues else "success",
        entry=entry,
        content=outcome.content,
        issues=issues,
    )


def _preview_issue_from_retrieval(issue: Any) -> PreviewIssue:
    """Project retrieval diagnostics onto fixed Chat reference metadata."""

    code_value = getattr(issue, "code", None)
    recoverable_value = getattr(issue, "recoverable", None)
    code = (
        code_value
        if isinstance(code_value, ErrorCode)
        else ErrorCode.RETRIEVAL_BACKEND_FAILED
    )
    recoverable = (
        recoverable_value if type(recoverable_value) is bool else False
    )
    return _reference_issue(
        code,
        stage="reference_search",
        recoverable=recoverable,
        cause_type="RetrievalDegraded",
    )


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
    # markdown2 accepts raw HTML by default.  Escape it before Markdown
    # conversion so user/provider text cannot inject img/a/object tags while
    # ordinary Markdown syntax (headings, lists, code, links) still works.
    html_content = md_renderer.convert(html.escape(str(text), quote=False))
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


def _html_text(value: object) -> str:
    """Escape one untrusted scalar before inserting it into hand-written HTML."""

    return html.escape(str(value), quote=True)


def _public_error_code(value: object) -> str:
    """Return only an enum-backed machine code for persistent UI status."""

    return (
        value
        if type(value) is str and value in _PUBLIC_ERROR_CODES
        else ErrorCode.CHAT_PROVIDER_FAILED.value
    )


class SafeMessageBrowser(QTextBrowser):
    """QTextBrowser that never resolves document-supplied resources."""

    _ALLOWED_CLICK_SCHEMES = frozenset({"http", "https"})

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setOpenExternalLinks(False)
        self.setOpenLinks(False)
        self.anchorClicked.connect(self._open_allowed_link)

    def loadResource(self, resource_type: int, name: QUrl):  # noqa: N802
        """Deny HTTP/file/qrc/data image or document fetches by default."""

        return None

    def _open_allowed_link(self, url: QUrl) -> None:
        """Open only an explicit http(s) click; all other schemes stay inert."""

        scheme = url.scheme().lower()
        if scheme not in self._ALLOWED_CLICK_SCHEMES or not url.isValid():
            logger.warning("已拦截 Chat 链接: scheme=%s", scheme or "relative")
            return
        QDesktopServices.openUrl(url)


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
        set_automation_id(self, "chat_token_panel")

        layout = QVBoxLayout(self)

        # 标题
        title = QLabel("📊 Token 统计")
        set_automation_id(title, "chat_token_panel_title")
        layout.addWidget(title)

        # 当前会话
        self.session_label = QLabel("当前: 0 / 64,000")
        set_automation_id(self.session_label, "chat_token_total")
        layout.addWidget(self.session_label)

        # 轮数
        self.round_label = QLabel("轮数: 0 / 3")
        set_automation_id(self.round_label, "chat_round_count")
        layout.addWidget(self.round_label)

        # 输入/输出
        self.input_label = QLabel("输入: 0")
        self.output_label = QLabel("输出: 0")
        set_automation_id(self.input_label, "chat_token_input")
        set_automation_id(self.output_label, "chat_token_output")
        layout.addWidget(self.input_label)
        layout.addWidget(self.output_label)

        # 警告区域
        self.warning_label = QLabel("")
        set_automation_id(self.warning_label, "chat_token_warning")
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
        set_automation_id(self, "session_sidebar")

        layout = QVBoxLayout(self)

        # 新建按钮
        self.new_btn = QPushButton("📝 新建会话")
        set_automation_id(self.new_btn, "chat_new_session")
        self.new_btn.setMinimumHeight(40)
        layout.addWidget(self.new_btn)

        # 会话列表
        self.session_list = QListWidget()
        set_automation_id(self.session_list, "session_list")
        self.session_list.currentItemChanged.connect(
            self._on_current_item_changed
        )
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

    def _on_current_item_changed(
        self,
        current: QListWidgetItem | None,
        previous: QListWidgetItem | None,
    ) -> None:
        """Load sessions selected by mouse, keyboard, or native UIA."""

        del previous
        if current is not None:
            self._on_item_clicked(current)

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

    def load_sessions(self, sessions: Any) -> bool:
        """加载会话列表

        Args:
            sessions: SQLite 会话列表投影

        Returns:
            是否以一个完整、有效的投影替换了当前列表。
        """
        if not is_strict_session_list_projection(sessions):
            logger.error("拒绝无效会话列表投影: code=session_projection_invalid")
            return False

        items: list[QListWidgetItem] = []
        for session in sessions:
            # 截断标题（最多 30 字符）
            title = session["title"]
            if len(title) > 30:
                title = title[:27] + "..."

            item = QListWidgetItem(f"• {title}")
            item.setData(Qt.ItemDataRole.UserRole, session["session_id"])
            items.append(item)

        # Validation and item construction complete before the mutation point.
        self.session_list.clear()
        for item in items:
            self.session_list.addItem(item)
        return True


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

    def start(self) -> None:
        """Start a fresh request-scoped render buffer."""

        self.buffer = ""
        self.full_text = ""
        self.timer.start(30)

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

    def discard(self) -> None:
        """Discard a provisional response without rendering it."""

        self.timer.stop()
        self.buffer = ""
        self.full_text = ""

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
        set_automation_id(self, "chat_input_area")

        layout = QHBoxLayout(self)

        # 输入框
        self.input_box = InputBox()
        set_automation_id(self.input_box, "chat_input")
        self.input_box.setPlaceholderText(
            "输入消息... (Ctrl+Enter 换行, @知识库/ 或 @搜索/ 引用知识)"
        )
        self.input_box.setMaximumHeight(120)
        layout.addWidget(self.input_box)

        # 发送按钮
        self.send_btn = QPushButton("🚀 发送")
        set_automation_id(self.send_btn, "chat_send")
        self.send_btn.setMinimumWidth(80)
        layout.addWidget(self.send_btn)

        # 停止按钮
        self.stop_btn = QPushButton("⏹ 停止")
        set_automation_id(self.stop_btn, "chat_stop")
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
        set_automation_id(self, "chat_area")

        layout = QVBoxLayout(self)

        # 消息显示区
        self.message_display = SafeMessageBrowser()
        set_automation_id(self.message_display, "chat_messages")
        self.message_display.setReadOnly(True)
        layout.addWidget(self.message_display)

        # 请求终态必须持续可见，不能只存在于日志或消息 HTML 中。
        self.request_status = QLabel("就绪")
        set_automation_id(self.request_status, "chat_request_status")
        self.request_status.setTextFormat(Qt.TextFormat.PlainText)
        self.request_status.setProperty("requestStatus", "idle")
        self.request_status.setWordWrap(True)
        layout.addWidget(self.request_status)

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
        self.stream_renderer.start()
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

    def discard_assistant_message(self) -> None:
        """Drop request-scoped render state before re-rendering history."""

        self.stream_renderer.discard()


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
        set_automation_id(self, "chat_view")

        # ViewModel
        self.viewmodel = ChatViewModel()
        self._active_ui_request: tuple[str, str] | None = None
        self._pending_user_messages: dict[tuple[str, str], str] = {}
        self._pending_url_archives: set[tuple[str, str]] = set()

        # 主布局
        splitter = QSplitter(Qt.Orientation.Horizontal)
        set_automation_id(splitter, "chat_splitter")

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
        self.viewmodel.chat_request_started.connect(
            self._on_chat_request_started
        )
        self.viewmodel.chat_token_received.connect(self._on_chat_token_received)
        self.viewmodel.chat_token_usage_updated.connect(
            self._on_chat_token_usage_updated
        )
        self.viewmodel.chat_request_completed.connect(
            self._on_chat_request_completed
        )
        self.viewmodel.chat_request_stopped.connect(
            self._on_chat_request_stopped
        )
        self.viewmodel.chat_request_failed.connect(
            self._on_chat_request_failed
        )
        self.viewmodel.chat_request_rejected.connect(
            self._on_chat_request_rejected
        )
        self.viewmodel.error_occurred.connect(self._on_error)
        self.viewmodel.session_created.connect(self._on_session_created)

        # M12 Phase 3: URL 归档信号
        self.viewmodel.url_archive_operation_started.connect(
            self._on_url_archive_started
        )
        self.viewmodel.url_archive_operation_completed.connect(
            self._on_url_archive_completed
        )
        self.viewmodel.url_archive_operation_failed.connect(
            self._on_url_archive_failed
        )
        self.viewmodel.url_archive_operation_warning.connect(
            self._on_url_archive_warning
        )

        # 对话保存到知识库
        self.viewmodel.session_saved_to_kb.connect(self._on_session_saved_to_kb)
        self.viewmodel.session_save_to_kb_failed.connect(self._on_session_save_to_kb_failed)
        self.viewmodel.session_save_to_kb_warning.connect(
            self._on_session_save_to_kb_warning
        )

        # UI → ViewModel
        self.sidebar.new_btn.clicked.connect(self._on_new_session)
        self.sidebar.session_selected.connect(self._on_session_selected)
        self.sidebar.session_delete_requested.connect(self._on_delete_session)
        self.sidebar.session_save_to_kb_requested.connect(self._on_save_to_kb)
        self.chat_area.input_area.send_btn.clicked.connect(self._on_send_clicked)
        self.chat_area.input_area.stop_btn.clicked.connect(self._on_stop_clicked)

    def _set_request_status(
        self,
        state: str,
        error_code: str = "",
    ) -> None:
        """Render one persistent, machine-readable Chat request status."""

        if state == "error":
            text = f"失败（错误代码：{_public_error_code(error_code)}）"
        elif state == "rejected":
            text = f"未发送（错误代码：{_public_error_code(error_code)}）"
        else:
            text = _REQUEST_STATUS_TEXT.get(state)
            if text is None:
                raise ValueError(f"Unknown Chat request status: {state!r}")

        label = self.chat_area.request_status
        label.setProperty("requestStatus", state)
        label.setText(text)
        label.show()
        label.style().unpolish(label)
        label.style().polish(label)

    def _sync_request_status(self) -> None:
        """Project global request ownership onto the currently shown session."""

        if not self.viewmodel.is_busy:
            self._set_request_status("idle")
            return
        state = (
            "running"
            if self.viewmodel.active_session_id
            == self.viewmodel.current_session_id
            else "running_other"
        )
        self._set_request_status(state)

    def _load_sessions(self) -> bool:
        """加载会话列表"""
        try:
            sessions = self.viewmodel.list_sessions(is_archived=False)
        except Exception:
            logger.error("加载 Chat 会话列表失败: code=session_list_failed")
            return False
        if sessions is None:
            return False
        return self.sidebar.load_sessions(sessions)

    def _on_new_session(self) -> None:
        """新建会话"""
        try:
            self.viewmodel.create_new_session()
            self.chat_area.discard_assistant_message()
            self.chat_area.message_display.clear()
            self.sidebar.token_panel.update_stats(0, 0, 0, 0)
            self._sync_request_status()
            logger.info("✅ 新建会话成功")
        except Exception as e:
            logger.error("新建会话失败: error_type=%s", type(e).__name__)

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
            self._sync_request_status()
            logger.info("会话已删除")

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
        safe_knowledge_id = _html_text(knowledge_id)
        self.chat_area.message_display.append(
            f"<p style='color: {colors['status_success']};'>"
            f"✅ 对话已保存到知识库 (ID: {safe_knowledge_id})</p>"
        )

    def _on_session_save_to_kb_failed(self, session_id: str, error_msg: str) -> None:
        """对话保存到知识库失败

        Args:
            session_id: 会话 ID
            error_msg: 错误信息
        """
        colors = theme_colors.get_current_colors()
        safe_error = _html_text(error_msg)
        self.chat_area.message_display.append(
            f"<p style='color: {colors['status_error']};'>"
            f"❌ 保存失败: {safe_error}</p>"
        )

    def _on_session_save_to_kb_warning(
        self,
        session_id: str,
        warning_msg: str,
    ) -> None:
        """Expose a committed-but-degraded archive result to the user."""

        colors = theme_colors.get_current_colors()
        safe_warning = _html_text(warning_msg)
        self.chat_area.message_display.append(
            f"<p style='color: {colors['status_warning']};'>"
            f"⚠️ {safe_warning}</p>"
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
            self._render_current_session()

            logger.info("会话已加载")

    def _render_current_session(self) -> None:
        """Render committed state plus this session's provisional active turn."""

        self.chat_area.discard_assistant_message()
        self.chat_area.message_display.clear()
        for msg in self.viewmodel.get_current_messages():
            role = msg["role"]
            content = msg["content"]
            if role == "user":
                self.chat_area.add_user_message(content)
            elif role == "assistant":
                self.chat_area.message_display.append(
                    render_markdown(content, role="assistant")
                )
                self.chat_area.message_display.append("")

        session_id = self.viewmodel.current_session_id
        active_turn = (
            self.viewmodel.get_active_turn(session_id) if session_id else None
        )
        if active_turn is not None:
            request_key = (session_id, active_turn["request_id"])
            display_user = self._pending_user_messages.get(
                request_key,
                active_turn["user"],
            )
            self.chat_area.add_user_message(display_user)
            self.chat_area.start_assistant_message()
            if active_turn["assistant"]:
                self.chat_area.stream_renderer.add_token(
                    active_turn["assistant"]
                )

        stats = self.viewmodel.get_token_stats()
        self.sidebar.token_panel.update_stats(
            0,
            0,
            stats["total_tokens"],
            stats["round_count"],
        )
        self._sync_request_status()

    def _on_send_clicked(self) -> None:
        """发送按钮点击事件

        M12 Phase 2: 发送前解析 @ 引用，注入知识上下文
        """
        user_message = self.chat_area.input_area.input_box.toPlainText().strip()
        if not user_message:
            return

        # Admission must happen before context injection or optimistic UI,
        # otherwise busy/config rejection would create a ghost turn.
        if not self.viewmodel.can_dispatch_message(user_message):
            return
        checkpoint = self.viewmodel.capture_turn_checkpoint()

        # M12 Phase 2: 解析 @ 引用
        if not self._resolve_and_inject_references(user_message):
            self.viewmodel.restore_turn_checkpoint(checkpoint)
            return

        # 发送消息时移除 @ 语法（API 只需要纯文本 + 上下文）
        from src.gui.utils.knowledge_ref import strip_at_references
        clean_message = strip_at_references(user_message)
        if not clean_message:
            clean_message = user_message  # 全是引用时保留原文

        # 原子接纳后才渲染 provisional turn。
        if not self.viewmodel.dispatch_message(
            clean_message,
            checkpoint=checkpoint,
        ):
            return

        session_id = self.viewmodel.active_session_id
        request_id = self.viewmodel.active_request_id
        if session_id and request_id:
            request_key = (session_id, request_id)
            self._active_ui_request = request_key
            self._pending_user_messages[request_key] = user_message
        self.chat_area.add_user_message(user_message)
        self.chat_area.input_area.input_box.clear()
        self.chat_area.start_assistant_message()
        self.chat_area.input_area.send_btn.setVisible(False)
        self.chat_area.input_area.stop_btn.setVisible(True)
        self.chat_area.input_area.stop_btn.setEnabled(True)
        self._set_request_status("running")

        # URL 归档独立于本轮冻结的 Provider 请求，在接纳后再触发，避免
        # 被拒绝的 send 产生额外副作用。
        self._detect_and_archive_urls(user_message)

    def _resolve_and_inject_references(self, text: str) -> bool:
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
            return True

        knowledge_refs = []
        degraded_issues: list[PreviewIssue] = []
        for ref in refs:
            try:
                if ref.ref_type == "knowledge":
                    resolution = self._load_entry_by_id(int(ref.value))
                elif ref.ref_type == "search":
                    resolution = self._search_entry(ref.value)
                else:
                    resolution = _reference_error(
                        ErrorCode.RETRIEVAL_INVALID_QUERY,
                        stage="reference_lookup",
                        recoverable=True,
                        cause_type="UnsupportedReferenceType",
                    )
            except Exception as e:
                logger.error(
                    "解析知识引用失败: ref_type=%s, error_type=%s",
                    ref.ref_type,
                    type(e).__name__,
                )
                resolution = _reference_error(
                    ErrorCode.RESOURCE_NOT_READABLE,
                    stage="reference_prepare",
                    recoverable=False,
                    cause_type="ReferenceResolutionFailed",
                )

            if resolution.status == "error":
                issue = (
                    resolution.issues[0]
                    if resolution.issues
                    else _reference_issue(
                        ErrorCode.RESOURCE_NOT_READABLE,
                        stage="reference_prepare",
                        recoverable=False,
                        cause_type="MissingReferenceIssue",
                    )
                )
                self._show_reference_status("error", issue)
                return False

            if (
                resolution.status not in {"success", "degraded"}
                or not isinstance(resolution.entry, dict)
                or not resolution.content.strip()
            ):
                self._show_reference_status(
                    "error",
                    _reference_issue(
                        ErrorCode.RESOURCE_NOT_READABLE,
                        stage="reference_prepare",
                        recoverable=False,
                        cause_type="InvalidReferenceResolution",
                    ),
                )
                return False

            try:
                knowledge_refs.append(
                    build_knowledge_reference(
                        resolution.entry,
                        resolution.content,
                    )
                )
            except Exception as e:
                logger.error(
                    "构建知识引用失败: error_type=%s",
                    type(e).__name__,
                )
                self._show_reference_status(
                    "error",
                    _reference_issue(
                        ErrorCode.RESOURCE_NOT_READABLE,
                        stage="reference_prepare",
                        recoverable=False,
                        cause_type="ReferenceBuildFailed",
                    ),
                )
                return False

            if resolution.status == "degraded":
                degraded_issues.extend(resolution.issues)

        try:
            cards = [format_reference_card_html(kref) for kref in knowledge_refs]
            context = format_context_message(knowledge_refs)
            if not context:
                raise ValueError("知识引用上下文为空")
            self.viewmodel.set_knowledge_context(context)
            for issue in degraded_issues:
                self._show_reference_status("degraded", issue)
            for card in cards:
                self.chat_area.message_display.append(card)
        except Exception as e:
            logger.error(
                "知识引用注入失败: error_type=%s",
                type(e).__name__,
            )
            try:
                self._show_reference_status(
                    "error",
                    _reference_issue(
                        ErrorCode.RESOURCE_NOT_READABLE,
                        stage="reference_prepare",
                        recoverable=False,
                        cause_type="ReferenceInjectionFailed",
                    ),
                )
            except Exception:
                logger.error("知识引用失败状态无法显示")
            return False

        logger.info("已注入知识引用: count=%s", len(knowledge_refs))
        return True

    def _load_entry_by_id(self, knowledge_id: int) -> _ReferenceResolution:
        """按 ID 加载知识条目

        Args:
            knowledge_id: 知识条目 ID

        Returns:
            显式 success/degraded/error 引用结果
        """
        if type(knowledge_id) is not int or knowledge_id <= 0:
            return _reference_error(
                ErrorCode.RESOURCE_MISSING,
                stage="reference_lookup",
                recoverable=True,
                cause_type="InvalidKnowledgeId",
            )
        try:
            from src.gui.stores import get_sqlite_store, get_markdown_store

            store = get_sqlite_store()
            entry = store.query_by_id(knowledge_id)
        except Exception as e:
            logger.error(
                "加载知识条目失败: knowledge_id=%s, error_type=%s",
                knowledge_id,
                type(e).__name__,
            )
            return _reference_error(
                ErrorCode.STORAGE_PRIMARY_FAILED,
                stage="reference_lookup",
                recoverable=False,
                cause_type="KnowledgeLookupFailed",
            )

        if entry is None:
            return _reference_error(
                ErrorCode.RESOURCE_MISSING,
                stage="reference_lookup",
                recoverable=True,
                cause_type="KnowledgeEntryMissing",
            )
        projected_entry = _project_reference_entry(
            entry,
            expected_knowledge_id=knowledge_id,
        )
        if projected_entry is None:
            return _reference_error(
                ErrorCode.RESOURCE_NOT_READABLE,
                stage="reference_lookup",
                recoverable=False,
                cause_type="InvalidKnowledgeEntry",
            )

        try:
            md_store = get_markdown_store()
            outcome = load_entry_preview_outcome(projected_entry, md_store)
        except Exception as e:
            logger.error(
                "加载知识预览失败: knowledge_id=%s, error_type=%s",
                knowledge_id,
                type(e).__name__,
            )
            return _reference_error(
                ErrorCode.RESOURCE_NOT_READABLE,
                stage="reference_preview",
                recoverable=False,
                cause_type="KnowledgePreviewFailed",
            )
        return _preview_resolution(projected_entry, outcome)

    def _search_entry(self, keyword: str) -> _ReferenceResolution:
        """搜索知识条目（取第一个结果）

        Args:
            keyword: 搜索关键词

        Returns:
            显式 success/degraded/error 引用结果
        """
        try:
            from src.gui.stores import (
                get_bm25_retriever,
                get_markdown_store,
                get_sqlite_store,
            )

            retriever = get_bm25_retriever()
            response = retriever.search(keyword, limit=1)
        except Exception as e:
            logger.error(
                "知识引用搜索异常: error_type=%s",
                type(e).__name__,
            )
            return _reference_error(
                ErrorCode.RETRIEVAL_BACKEND_FAILED,
                stage="reference_search",
                recoverable=False,
                cause_type="ReferenceSearchFailed",
            )

        if (
            not is_strict_search_response(response)
            or response.strategy != "bm25"
        ):
            return _reference_error(
                ErrorCode.RETRIEVAL_BACKEND_FAILED,
                stage="reference_search",
                recoverable=False,
                cause_type="InvalidSearchResponse",
            )
        if response.status == "no_hits":
            return _reference_error(
                ErrorCode.RESOURCE_MISSING,
                stage="reference_search",
                recoverable=True,
                cause_type="ReferenceSearchNoHits",
            )
        if response.status in {"invalid", "error"}:
            issue = (
                _preview_issue_from_retrieval(response.issues[0])
                if response.issues
                else _reference_issue(
                    ErrorCode.RETRIEVAL_BACKEND_FAILED,
                    stage="reference_search",
                    recoverable=False,
                    cause_type="MissingRetrievalIssue",
                )
            )
            return _ReferenceResolution(status="error", issues=(issue,))
        if response.status not in {"success", "degraded"}:
            return _reference_error(
                ErrorCode.RETRIEVAL_BACKEND_FAILED,
                stage="reference_search",
                recoverable=False,
                cause_type="InvalidRetrievalStatus",
            )

        retrieval_issues = tuple(
            _preview_issue_from_retrieval(issue) for issue in response.issues
        )
        if not response.results:
            issue = (
                retrieval_issues[0]
                if retrieval_issues
                else _reference_issue(
                    ErrorCode.RESOURCE_MISSING,
                    stage="reference_search",
                    recoverable=True,
                    cause_type="ReferenceSearchNoUsableResult",
                )
            )
            return _ReferenceResolution(status="error", issues=(issue,))

        knowledge_id = response.results[0].knowledge_id
        if type(knowledge_id) is not int or knowledge_id <= 0:
            return _reference_error(
                ErrorCode.RETRIEVAL_METADATA_INCONSISTENT,
                stage="reference_lookup",
                recoverable=False,
                cause_type="InvalidSearchKnowledgeId",
            )

        try:
            store = get_sqlite_store()
            entry = store.query_by_id(knowledge_id)
        except Exception as e:
            logger.error(
                "知识搜索条目回读失败: error_type=%s",
                type(e).__name__,
            )
            return _reference_error(
                ErrorCode.STORAGE_PRIMARY_FAILED,
                stage="reference_lookup",
                recoverable=False,
                cause_type="SearchEntryLookupFailed",
            )
        projected_entry = _project_reference_entry(
            entry,
            expected_knowledge_id=knowledge_id,
        )
        if projected_entry is None:
            return _reference_error(
                ErrorCode.RETRIEVAL_METADATA_INCONSISTENT,
                stage="reference_lookup",
                recoverable=False,
                cause_type="SearchEntryMissing",
            )

        try:
            md_store = get_markdown_store()
            outcome = load_entry_preview_outcome(projected_entry, md_store)
        except Exception as e:
            logger.error(
                "知识搜索预览失败: error_type=%s",
                type(e).__name__,
            )
            return _reference_error(
                ErrorCode.RESOURCE_NOT_READABLE,
                stage="reference_preview",
                recoverable=False,
                cause_type="SearchPreviewFailed",
            )
        return _preview_resolution(
            projected_entry,
            outcome,
            prior_issues=retrieval_issues,
        )

    def _show_reference_status(
        self,
        status: str,
        issue: PreviewIssue,
    ) -> None:
        """Expose only enum-backed diagnostics in the Chat surface."""

        colors = theme_colors.get_current_colors()
        degraded = status == "degraded"
        color = (
            colors["status_warning"]
            if degraded
            else colors["status_error"]
        )
        label = "知识引用已降级，正在使用安全摘要" if degraded else "知识引用未完成，本轮未发送"
        public_status = "degraded" if degraded else "error"
        code = (
            issue.code.value
            if isinstance(issue, PreviewIssue)
            and isinstance(issue.code, ErrorCode)
            else ErrorCode.RESOURCE_NOT_READABLE.value
        )
        stage = (
            issue.stage
            if isinstance(issue, PreviewIssue)
            and issue.stage in _REFERENCE_PUBLIC_STAGES
            else "reference"
        )
        self.chat_area.message_display.append(
            f"<p style='color: {color}; font-size: 12px;'>"
            f"⚠️ {label}（status={public_status}, code={code}, "
            f"stage={stage}）</p>"
        )
        if not degraded:
            self._set_request_status("rejected", code)

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
            logger.info("检测到 URL target=%s", describe_url_target(url))
            # 同步冻结 origin session + operation identity，再异步归档。
            self.viewmodel.begin_url_archive(url)

    def _on_url_archive_started(
        self,
        session_id: str,
        operation_id: str,
        url: str,
    ) -> None:
        """URL 归档开始

        Args:
            url: 正在归档 of the URL
        """
        self._pending_url_archives.add((session_id, operation_id))
        if self.viewmodel.current_session_id != session_id:
            return
        colors = theme_colors.get_current_colors()
        safe_target = _html_text(describe_url_target(url))
        self.chat_area.message_display.append(
            f"<p style='color: {colors['status_progress']}; font-size: 12px;'>"
            f"🔄 正在归档: {safe_target}...</p>"
        )

    def _on_url_archive_completed(
        self,
        session_id: str,
        operation_id: str,
        url: str,
        entry: dict,
    ) -> None:
        """URL 归档完成，注入上下文

        Args:
            url: 已归档的 URL
            entry: 归档后的条目字典
        """
        operation_key = (session_id, operation_id)
        if operation_key not in self._pending_url_archives:
            return
        self._pending_url_archives.discard(operation_key)
        if self.viewmodel.current_session_id != session_id:
            logger.info("URL 归档完成时原会话已离开，安全丢弃上下文注入")
            return

        try:
            # ViewModel prepares the injectable reference before publishing
            # completed, keeping completed/failed mutually exclusive.
            card = entry.get(
                self.viewmodel.URL_ARCHIVE_REFERENCE_CARD_HTML_KEY,
            )
            if type(card) is not str or not card:
                raise ValueError("URL archive completion payload is not prepared")
            self.chat_area.message_display.append(card)

            logger.info(
                "URL 归档引用已显示 target=%s",
                describe_url_target(url),
            )

        except Exception as e:
            logger.error(
                "URL 归档后处理失败: error_type=%s",
                type(e).__name__,
            )

    def _on_url_archive_failed(
        self,
        session_id: str,
        operation_id: str,
        url: str,
        error_msg: str,
    ) -> None:
        """URL 归档失败

        Args:
            url: 归档失败的 URL
            error_msg: 错误消息
        """
        operation_key = (session_id, operation_id)
        if operation_key not in self._pending_url_archives:
            return
        self._pending_url_archives.discard(operation_key)
        if self.viewmodel.current_session_id != session_id:
            return
        colors = theme_colors.get_current_colors()
        safe_target = _html_text(describe_url_target(url))
        safe_error = _html_text(error_msg)
        self.chat_area.message_display.append(
            f"<p style='color: {colors['status_warning']}; font-size: 12px;'>"
            f"⚠️ URL 归档失败: {safe_target}<br>"
            f"原因: {safe_error}</p>"
        )

    def _on_url_archive_warning(
        self,
        session_id: str,
        operation_id: str,
        url: str,
        warning_msg: str,
    ) -> None:
        """URL 归档降级/需修复警告（核心已提交，请勿盲目重试）

        Args:
            url: 归档的 URL
            warning_msg: 警告消息
        """
        operation_key = (session_id, operation_id)
        if (
            operation_key not in self._pending_url_archives
            or self.viewmodel.current_session_id != session_id
        ):
            return
        colors = theme_colors.get_current_colors()
        safe_target = _html_text(describe_url_target(url))
        safe_warning = _html_text(warning_msg)
        self.chat_area.message_display.append(
            f"<p style='color: {colors['status_warning']}; font-size: 12px;'>"
            f"⚠️ URL 归档需修复: {safe_target}<br>"
            f"{safe_warning}</p>"
        )

    def _on_stop_clicked(self) -> None:
        """停止按钮点击事件"""
        if self.viewmodel.stop_stream():
            # 终态信号会在 stream/provider 完成 close 后统一回滚 UI。
            self.chat_area.input_area.stop_btn.setEnabled(False)
            logger.info("🛑 用户请求停止流式输出")

    def _on_chat_request_started(
        self,
        session_id: str,
        request_id: str,
    ) -> None:
        if (
            self.viewmodel.active_session_id != session_id
            or self.viewmodel.active_request_id != request_id
        ):
            return
        self._active_ui_request = (session_id, request_id)
        if self.viewmodel.current_session_id == session_id:
            self._set_request_status("running")

    def _on_chat_token_received(
        self,
        session_id: str,
        request_id: str,
        token: str,
    ) -> None:
        """Only render tokens owned by the currently displayed session."""

        if (
            self._active_ui_request == (session_id, request_id)
            and self.viewmodel.current_session_id == session_id
        ):
            self.chat_area.stream_renderer.add_token(token)

    def _on_chat_request_completed(
        self,
        session_id: str,
        request_id: str,
    ) -> None:
        """Finalize exactly one successfully committed turn."""

        request_key = (session_id, request_id)
        if self._active_ui_request != request_key:
            return
        if self.viewmodel.current_session_id == session_id:
            self.chat_area.finish_assistant_message()
        self._pending_user_messages.pop(request_key, None)
        self._active_ui_request = None
        self._restore_send_controls()
        if self.viewmodel.current_session_id == session_id:
            self._set_request_status("completed")
        else:
            self._sync_request_status()
        logger.info("流式输出完成")

    def _on_chat_request_stopped(
        self,
        session_id: str,
        request_id: str,
    ) -> None:
        """Discard the complete provisional turn after active cancellation."""

        request_key = (session_id, request_id)
        if self._active_ui_request != request_key:
            return
        pending = self._pending_user_messages.pop(request_key, "")
        self._active_ui_request = None
        if self.viewmodel.current_session_id == session_id:
            self._render_current_session()
            if pending and not self.chat_area.input_area.input_box.toPlainText():
                self.chat_area.input_area.input_box.setPlainText(pending)
        self._restore_send_controls()
        if self.viewmodel.current_session_id == session_id:
            self._set_request_status("stopped")
        else:
            self._sync_request_status()
        logger.info("流式输出已停止并回滚")

    def _on_chat_request_failed(
        self,
        session_id: str,
        request_id: str,
        error_code: str,
        error_msg: str,
    ) -> None:
        """Render one terminal error only for its active provisional turn."""

        request_key = (session_id, request_id)
        if self._active_ui_request != request_key:
            return

        colors = theme_colors.get_current_colors()
        safe_error_code = _html_text(error_code)
        safe_error_message = _html_text(error_msg)
        pending = self._pending_user_messages.pop(request_key, "")
        self._active_ui_request = None
        if not session_id or self.viewmodel.current_session_id == session_id:
            if session_id:
                self._render_current_session()
                if (
                    pending
                    and "持久化状态需检查" not in error_msg
                    and not self.chat_area.input_area.input_box.toPlainText()
                ):
                    self.chat_area.input_area.input_box.setPlainText(pending)
            self.chat_area.message_display.append(
                f"<p style='color: {colors['status_error']};'>"
                f"❌ 错误 [{safe_error_code}]: {safe_error_message}</p>"
            )
        self._restore_send_controls()
        if not session_id or self.viewmodel.current_session_id == session_id:
            self._set_request_status("error", error_code)
        else:
            self._sync_request_status()

    def _on_chat_request_rejected(
        self,
        session_id: str,
        request_id: str,
        error_code: str,
        error_msg: str,
    ) -> None:
        """Show admission rejection without altering an active request."""

        if self.viewmodel.latest_attempt_id != request_id:
            return
        # A queued rejection from an older attempt must not affect a newer
        # provisional request in the same session.
        if self.viewmodel.is_busy and error_code != "chat_busy":
            return
        if session_id and self.viewmodel.current_session_id != session_id:
            return
        if error_code == "chat_busy":
            input_box = self.chat_area.input_area.input_box
            input_box.setToolTip(f"[{error_code}] {error_msg}")
            input_box.setFocus()
            self._sync_request_status()
            logger.warning("Chat send rejected while another request is active")
            return
        colors = theme_colors.get_current_colors()
        safe_error_code = _html_text(error_code)
        safe_error_message = _html_text(error_msg)
        if session_id:
            self._render_current_session()
        self.chat_area.message_display.append(
            f"<p style='color: {colors['status_error']};'>"
            f"❌ [{safe_error_code}] "
            f"{safe_error_message}</p>"
        )
        self._set_request_status("rejected", error_code)

    def _on_chat_token_usage_updated(
        self,
        session_id: str,
        request_id: str,
        input_tokens: int,
        output_tokens: int,
        total_tokens: int,
    ) -> None:
        if (
            self._active_ui_request != (session_id, request_id)
            or self.viewmodel.current_session_id != session_id
        ):
            return
        stats = self.viewmodel.get_token_stats()
        self.sidebar.token_panel.update_stats(
            input_tokens,
            output_tokens,
            total_tokens,
            stats["round_count"],
        )

    def _restore_send_controls(self) -> None:
        """Restore global one-request controls after a terminal event."""

        self.chat_area.input_area.send_btn.setVisible(True)
        self.chat_area.input_area.stop_btn.setVisible(False)
        self.chat_area.input_area.stop_btn.setEnabled(True)

    # Backward-compatible handlers retained for external callers; production
    # signal wiring uses the session-scoped variants above.
    def _on_stream_finished(self) -> None:
        session_id = self.viewmodel.current_session_id
        request_id = self.viewmodel.active_request_id
        if session_id and request_id:
            self._on_chat_request_completed(session_id, request_id)

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
        logger.error("GUI 非 Chat 操作失败")
        colors = theme_colors.get_current_colors()
        safe_error = _html_text(error_msg)
        self.chat_area.message_display.append(
            f"<p style='color: {colors['status_error']};'>"
            f"❌ 错误: {safe_error}</p>"
        )

        # 非 Chat 错误不得把仍在运行的请求误显示为已结束。
        if not self.viewmodel.is_busy:
            self._restore_send_controls()

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
            self._sync_request_status()

            logger.info(
                "创建引用会话成功: tokens=%s truncated=%s",
                ref.token_count,
                ref.is_truncated,
            )

        except Exception as e:
            error_msg = "创建引用会话失败，请检查本地数据库状态"
            logger.error(
                "创建引用会话失败: error_type=%s",
                type(e).__name__,
            )
            self._on_error(error_msg)
