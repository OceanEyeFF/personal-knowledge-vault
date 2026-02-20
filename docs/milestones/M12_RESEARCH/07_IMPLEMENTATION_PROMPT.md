# M12 AI 对话界面实现 Prompt

> **版本**: 1.0
> **创建日期**: 2026-02-20
> **阶段**: Day 3 - 原型实现
> **预计耗时**: 5 小时（1 天）
> **状态**: 📝 待开始

---

## 📋 任务目标

基于 Day 2 技术预研成果，实现 **AI 对话界面原型**（chat_view.py），包含：

1. ✅ 完整的 UI 布局（侧边栏 + 消息区 + 输入区）
2. ✅ Markdown 渲染（markdown2 + Pygments）
3. ✅ 流式输出（30ms 批量更新）
4. ✅ 集成 DeepSeek API（qasync 异步调用）
5. ✅ 端到端测试（真实 API 流式对话）

---

## 📚 技术上下文

### 已完成的技术预研

| 调研项 | 文档 | 核心结论 |
|--------|------|----------|
| DeepSeek API | `01_DEEPSEEK_API_RESEARCH.md` | 使用 OpenAI SDK，stream_usage=True 精确统计 Token |
| qasync 集成 | `01_ASYNCIO_QT_RESEARCH.md` | @asyncSlot() 装饰器，QEventLoop 事件循环融合 |
| 数据库设计 | `03_DATABASE_DESIGN.md` | chat_sessions 表，JSON messages 字段 |
| UI 设计 | `06_UI_DESIGN_FINAL.md` | QTextBrowser + markdown2 + 30ms 批量更新 |

### 技术选型（最终确定）

```yaml
GUI 框架: PySide6 6.6+
异步方案: qasync 0.28+
Markdown: markdown2 2.4+
代码高亮: Pygments 2.17+
AI 服务: DeepSeek API (OpenAI SDK)
流式渲染: QTimer 30ms 批量更新
```

### 关键技术决策

- **D003**: 使用 qasync（而非 qt-async-threads）
- **D005**: Token 控制策略（单轮 max_tokens=2000，3 轮提示，64K 警告）
- **D006**: 采用 ChatGPT 轻量级方案（QTextBrowser）

---

## 🎯 实现步骤（4 个 Phase）

### Phase 1: 基础布局（2 小时）

#### 1.1 创建主窗口（`chat_view.py`）

**文件路径**: `src/gui/chat_view.py`

**功能需求**:
- 使用 `QMainWindow` 作为主窗口
- 使用 `QSplitter` 实现响应式布局（左侧侧边栏 20%，右侧主区域 80%）
- 窗口标题：`"🧠 Personal Knowledge Vault - AI 对话"`
- 最小窗口大小：800x600

**关键代码模板**:
```python
from PySide6.QtWidgets import QMainWindow, QSplitter, QWidget, QVBoxLayout
from PySide6.QtCore import Qt

class ChatWindow(QMainWindow):
    """AI 对话主窗口"""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("🧠 Personal Knowledge Vault - AI 对话")
        self.setMinimumSize(800, 600)

        # 创建主布局
        central_widget = QWidget()
        self.setCentralWidget(central_widget)

        main_layout = QVBoxLayout(central_widget)
        main_layout.setContentsMargins(0, 0, 0, 0)

        # 创建 Splitter（左侧侧边栏 + 右侧主区域）
        splitter = QSplitter(Qt.Horizontal)
        main_layout.addWidget(splitter)

        # 左侧侧边栏
        sidebar = self._create_sidebar()
        splitter.addWidget(sidebar)

        # 右侧主区域
        main_area = self._create_main_area()
        splitter.addWidget(main_area)

        # 设置 Splitter 比例（20% : 80%）
        splitter.setSizes([200, 800])
```

#### 1.2 实现侧边栏（SessionSidebar）

**功能需求**:
- 顶部：新建会话按钮（`📝 新建会话`）
- 中间：会话列表（QListWidget）
- 底部：Token 统计面板

**参考设计**: `06_UI_DESIGN_FINAL.md` 第 6、5 节

**关键代码模板**:
```python
from PySide6.QtWidgets import QWidget, QVBoxLayout, QPushButton, QListWidget

class SessionSidebar(QWidget):
    """会话侧边栏"""

    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)

        # 新建会话按钮
        new_session_btn = QPushButton("📝 新建会话")
        layout.addWidget(new_session_btn)

        # 会话列表
        self.session_list = QListWidget()
        self.session_list.setStyleSheet("""
            QListWidget {
                background-color: #F5F5F5;
                border: none;
            }
            QListWidget::item {
                padding: 8px;
                border-bottom: 1px solid #E0E0E0;
            }
            QListWidget::item:selected {
                background-color: #E3F2FD;
            }
        """)
        layout.addWidget(self.session_list)

        # Token 统计面板
        self.token_panel = TokenPanel()
        layout.addWidget(self.token_panel)
```

#### 1.3 实现 Token 统计面板（TokenPanel）

**功能需求**:
- 显示当前会话 Token 数（`当前: X / 64,000`）
- 显示对话轮数（`轮数: X / 3`）
- 显示输入/输出 Token 数
- 3 轮警告（round_count >= 3）
- 64K 警告（total_tokens >= 60000）

**参考设计**: `06_UI_DESIGN_FINAL.md` 第 5 节

**关键代码模板**:
```python
from PySide6.QtWidgets import QWidget, QVBoxLayout, QLabel

class TokenPanel(QWidget):
    """Token 统计面板"""

    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)

        # 标题
        title = QLabel("📊 Token 统计")
        title.setStyleSheet("font-weight: bold; font-size: 14px;")
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
        self.warning_label.setStyleSheet("""
            background-color: #FFF3E0;
            color: #E65100;
            padding: 8px;
            border-radius: 4px;
            font-weight: bold;
        """)
        self.warning_label.setVisible(False)
        layout.addWidget(self.warning_label)

    def update_stats(self, total: int, round_count: int, input_t: int, output_t: int):
        """更新统计数据"""
        self.session_label.setText(f"当前: {total:,} / 64,000")
        self.round_label.setText(f"轮数: {round_count} / 3")
        self.input_label.setText(f"输入: {input_t:,}")
        self.output_label.setText(f"输出: {output_t:,}")

        # 3 轮警告
        if round_count >= 3:
            self.warning_label.setText("⚠️ 已进行 3 轮对话\n建议结束或新建会话")
            self.warning_label.setVisible(True)
        # 64K 警告
        elif total >= 60000:
            self.warning_label.setText(f"⚠️ 上下文已接近 64K 限制\n当前: {total:,} / 64,000")
            self.warning_label.setVisible(True)
        else:
            self.warning_label.setVisible(False)
```

#### 1.4 实现主区域（ChatArea）

**功能需求**:
- 上部：消息显示区域（QTextBrowser）
- 下部：输入区域（QTextEdit + 发送/停止按钮）

**参考设计**: `06_UI_DESIGN_FINAL.md` 第 1、7 节

**关键代码模板**:
```python
from PySide6.QtWidgets import QWidget, QVBoxLayout, QTextBrowser

class ChatArea(QWidget):
    """聊天主区域"""

    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)

        # 消息显示区域
        self.chat_view = ChatView()
        layout.addWidget(self.chat_view)

        # 输入区域
        self.input_area = InputArea()
        layout.addWidget(self.input_area)
```

#### 验收标准（Phase 1）

- [ ] 窗口启动成功，最小尺寸 800x600
- [ ] Splitter 可拖动调整左右比例
- [ ] 侧边栏显示"新建会话"按钮、会话列表、Token 统计面板
- [ ] 主区域显示消息区和输入区
- [ ] Token 统计面板默认显示 0

---

### Phase 2: Markdown 渲染（1 小时）

#### 2.1 实现 ChatView（消息显示区域）

**功能需求**:
- 使用 `QTextBrowser`（只读）
- 支持外部链接（`setOpenExternalLinks(True)`）
- 保留光标（`self.cursor = self.textCursor()`）

**参考设计**: `06_UI_DESIGN_FINAL.md` 第 1 节

**关键代码模板**:
```python
from PySide6.QtWidgets import QTextBrowser
from PySide6.QtGui import QTextCursor

class ChatView(QTextBrowser):
    """消息显示区域"""

    def __init__(self):
        super().__init__()
        self.setReadOnly(True)
        self.setOpenExternalLinks(True)
        self.setStyleSheet("""
            QTextBrowser {
                background-color: #FFFFFF;
                border: none;
                padding: 12px;
            }
        """)

        self.cursor = self.textCursor()
```

#### 2.2 实现 Markdown 渲染器

**功能需求**:
- 使用 `markdown2` 渲染 Markdown
- 使用 `Pygments` 生成代码高亮 CSS
- 支持 fenced-code-blocks、tables、strike

**参考设计**: `06_UI_DESIGN_FINAL.md` 第 2 节

**关键代码模板**:
```python
from markdown2 import Markdown
from pygments.formatters import HtmlFormatter

# 初始化 Markdown 渲染器
md = Markdown(extras=[
    "fenced-code-blocks",  # 代码块支持
    "tables",              # 表格支持
    "strike",              # 删除线
    "code-friendly"        # 代码友好
])

# 生成 Pygments CSS
formatter = HtmlFormatter(style="monokai")
code_css = formatter.get_style_defs(".codehilite")

def render_markdown(text: str) -> str:
    """将 Markdown 渲染为 HTML"""
    html = md.convert(text)

    template = f"""
    <style>
    {code_css}
    .assistant {{
        background-color: #F5F5F5;
        padding: 10px;
        border-radius: 8px;
        margin: 6px;
    }}
    .user {{
        background-color: #E3F2FD;
        padding: 10px;
        border-radius: 8px;
        margin: 6px;
        text-align: right;
    }}
    pre {{
        background-color: #272822;
        color: #f8f8f2;
        padding: 10px;
        border-radius: 4px;
        overflow-x: auto;
    }}
    </style>
    {html}
    """

    return template
```

#### 2.3 实现消息添加方法

**功能需求**:
- User 消息：右对齐、浅蓝色背景
- Assistant 消息：左对齐、浅灰色背景
- 显示时间戳

**参考设计**: `06_UI_DESIGN_FINAL.md` 第 4 节

**关键代码模板**:
```python
from datetime import datetime

class ChatView(QTextBrowser):
    # ... 前面的代码 ...

    def add_user_message(self, text: str):
        """添加用户消息"""
        timestamp = datetime.now().strftime("%H:%M:%S")
        html = f"""
        <div class="user">
            <strong>👤 User</strong><br>
            {text}<br>
            <small style="color: #888;">{timestamp}</small>
        </div>
        """
        self.moveCursor(QTextCursor.End)
        self.insertHtml(html)
        self.moveCursor(QTextCursor.End)

    def add_assistant_message_start(self, text: str = ""):
        """开始添加 Assistant 消息（流式输出前调用）"""
        html = f"""
        <div class="assistant">
            <strong>🤖 Assistant</strong><br>
        """
        self.moveCursor(QTextCursor.End)
        self.insertHtml(html)
        # 保存当前位置，流式输出会继续在这里追加
```

#### 验收标准（Phase 2）

- [ ] 手动测试：添加 User 消息显示正确（右对齐、浅蓝色）
- [ ] 手动测试：添加 Assistant 消息显示正确（左对齐、浅灰色）
- [ ] Markdown 渲染测试：代码块高亮正常（monokai 主题）
- [ ] 测试表格、粗体、斜体、删除线渲染

---

### Phase 3: 流式输出（1 小时）

#### 3.1 实现 StreamRenderer（核心性能优化）

**功能需求**:
- 使用 `QTimer` 每 30ms 批量更新一次
- 缓冲 token 到 `self.buffer`
- flush() 批量插入到 QTextBrowser

**参考设计**: `06_UI_DESIGN_FINAL.md` 第 3 节

**关键代码模板**:
```python
from PySide6.QtCore import QTimer
from PySide6.QtGui import QTextCursor

class StreamRenderer:
    """流式输出渲染器（30ms 批量更新）"""

    def __init__(self, browser: QTextBrowser):
        self.browser = browser
        self.buffer = ""
        self.timer = QTimer()
        self.timer.timeout.connect(self.flush)
        self.timer.start(30)  # 30ms 刷新一次

    def add_token(self, token: str):
        """添加新 token 到缓冲区"""
        self.buffer += token

    def flush(self):
        """批量更新到 UI（每 30ms 执行一次）"""
        if not self.buffer:
            return

        # 移动光标到末尾
        self.browser.moveCursor(QTextCursor.End)
        # 插入文本（不会重新渲染整个页面）
        self.browser.insertPlainText(self.buffer)
        # 再次移动到末尾（自动滚动）
        self.browser.moveCursor(QTextCursor.End)

        # 清空缓冲区
        self.buffer = ""

    def stop(self):
        """停止定时器"""
        self.timer.stop()
        self.flush()  # 最后一次刷新
```

#### 3.2 实现自动滚动

**功能需求**:
- 每次更新后自动滚动到最新消息
- 使用 `verticalScrollBar()` 控制

**参考设计**: `06_UI_DESIGN_FINAL.md` 关键技术点第 2 节

**关键代码模板**:
```python
def auto_scroll(browser: QTextBrowser):
    """自动滚动到最新消息"""
    scrollbar = browser.verticalScrollBar()
    scrollbar.setValue(scrollbar.maximum())
```

#### 验收标准（Phase 3）

- [ ] 创建测试脚本：模拟 100 tokens/s 高频输入
- [ ] 验证 UI 无卡顿
- [ ] 验证自动滚动正常
- [ ] 验证 buffer 正确清空（无遗漏 token）

**测试脚本模板**:
```python
import asyncio
from qasync import asyncSlot

class TestStreamWindow(ChatWindow):
    @asyncSlot()
    async def test_high_frequency(self):
        """测试高频流式输出（100 tokens/s）"""
        renderer = StreamRenderer(self.chat_view)

        # 模拟 300 个 token（持续 3s）
        for i in range(300):
            renderer.add_token(f"Token{i} ")
            await asyncio.sleep(0.01)  # 10ms = 100 tokens/s

        renderer.stop()
```

---

### Phase 4: 集成测试（1 小时）

#### 4.1 实现 ChatViewModel（业务逻辑层）

**功能需求**:
- 使用 `@asyncSlot()` 装饰器
- 调用 DeepSeek API（OpenAI SDK）
- 发送 `token_received` Signal

**参考文档**:
- `01_DEEPSEEK_API_RESEARCH.md` OpenAI SDK 示例
- `01_ASYNCIO_QT_RESEARCH.md` qasync 集成示例

**关键代码模板**:
```python
from PySide6.QtCore import QObject, Signal
from qasync import asyncSlot
from openai import AsyncOpenAI
import os

class ChatViewModel(QObject):
    """聊天业务逻辑（ViewModel 层）"""

    token_received = Signal(str)  # 接收到新 token
    error_occurred = Signal(str)  # 发生错误

    def __init__(self):
        super().__init__()
        self.client = AsyncOpenAI(
            api_key=os.getenv("DEEPSEEK_API_KEY"),
            base_url="https://api.deepseek.com"
        )
        self.messages = []  # 对话历史

    @asyncSlot()
    async def send_message(self, user_message: str):
        """发送消息并接收流式回复"""
        try:
            # 添加用户消息到历史
            self.messages.append({
                "role": "user",
                "content": user_message
            })

            # 调用 DeepSeek API（流式）
            stream = await self.client.chat.completions.create(
                model="deepseek-chat",
                messages=self.messages,
                stream=True,
                stream_options={"include_usage": True},
                max_tokens=2000
            )

            assistant_reply = ""

            # 流式接收 token
            async for chunk in stream:
                if chunk.choices:
                    delta = chunk.choices[0].delta
                    if delta.content:
                        token = delta.content
                        assistant_reply += token
                        self.token_received.emit(token)

            # 添加 Assistant 回复到历史
            self.messages.append({
                "role": "assistant",
                "content": assistant_reply
            })

        except Exception as e:
            self.error_occurred.emit(str(e))
```

#### 4.2 连接 ViewModel 与 View

**功能需求**:
- ChatWindow 持有 ChatViewModel 实例
- 连接 Signal 与 Slot
- 发送按钮触发 `send_message()`

**关键代码模板**:
```python
class ChatWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        # ... UI 初始化 ...

        # 创建 ViewModel
        self.view_model = ChatViewModel()

        # 创建 StreamRenderer
        self.renderer = None

        # 连接信号
        self.input_area.send_clicked.connect(self.on_send_clicked)
        self.view_model.token_received.connect(self.on_token_received)
        self.view_model.error_occurred.connect(self.on_error)

    @asyncSlot()
    async def on_send_clicked(self):
        """发送按钮点击"""
        user_message = self.input_area.get_text()
        if not user_message.strip():
            return

        # 显示用户消息
        self.chat_view.add_user_message(user_message)

        # 开始 Assistant 消息
        self.chat_view.add_assistant_message_start()

        # 创建流式渲染器
        self.renderer = StreamRenderer(self.chat_view)

        # 发送消息（异步）
        await self.view_model.send_message(user_message)

        # 停止渲染器
        self.renderer.stop()

        # 清空输入框
        self.input_area.clear()

    def on_token_received(self, token: str):
        """接收到新 token"""
        if self.renderer:
            self.renderer.add_token(token)

    def on_error(self, error: str):
        """发生错误"""
        print(f"Error: {error}")
```

#### 4.3 实现输入区域（InputArea）

**功能需求**:
- QTextEdit 多行输入
- Enter 发送，Ctrl+Enter 换行
- 发送/停止按钮

**参考设计**: `06_UI_DESIGN_FINAL.md` 第 7 节

**关键代码模板**:
```python
from PySide6.QtWidgets import QWidget, QHBoxLayout, QTextEdit, QPushButton
from PySide6.QtCore import Signal, Qt
from PySide6.QtGui import QKeyEvent

class InputBox(QTextEdit):
    """输入框（支持快捷键）"""

    send_triggered = Signal()

    def keyPressEvent(self, event: QKeyEvent):
        # Enter 发送（不换行）
        if event.key() == Qt.Key_Return and event.modifiers() == Qt.NoModifier:
            self.send_triggered.emit()
            event.accept()
        # Ctrl+Enter 换行
        elif event.key() == Qt.Key_Return and event.modifiers() == Qt.ControlModifier:
            super().keyPressEvent(event)
        else:
            super().keyPressEvent(event)


class InputArea(QWidget):
    """输入区域"""

    send_clicked = Signal()

    def __init__(self):
        super().__init__()
        layout = QHBoxLayout(self)

        # 输入框
        self.input_box = InputBox()
        self.input_box.setPlaceholderText("输入消息... (Ctrl+Enter 换行)")
        self.input_box.setMaximumHeight(120)
        self.input_box.send_triggered.connect(self.send_clicked.emit)
        layout.addWidget(self.input_box)

        # 发送按钮
        self.send_btn = QPushButton("🚀 发送")
        self.send_btn.setMinimumWidth(80)
        self.send_btn.clicked.connect(self.send_clicked.emit)
        layout.addWidget(self.send_btn)

        # 停止按钮
        self.stop_btn = QPushButton("⏹ 停止")
        self.stop_btn.setMinimumWidth(80)
        self.stop_btn.setVisible(False)
        layout.addWidget(self.stop_btn)

    def get_text(self) -> str:
        """获取输入文本"""
        return self.input_box.toPlainText()

    def clear(self):
        """清空输入框"""
        self.input_box.clear()
```

#### 4.4 主程序入口（qasync 事件循环）

**功能需求**:
- 使用 `QEventLoop` 融合 asyncio 与 Qt
- 环境变量加载（dotenv）

**参考文档**: `01_ASYNCIO_QT_RESEARCH.md` 主程序集成

**关键代码模板**:
```python
# src/gui/main.py
import sys
import asyncio
from PySide6.QtWidgets import QApplication
from qasync import QEventLoop
from dotenv import load_dotenv
from chat_view import ChatWindow

def main():
    # 加载环境变量
    load_dotenv()

    # 创建 Qt 应用
    app = QApplication(sys.argv)

    # 创建 qasync 事件循环
    loop = QEventLoop(app)
    asyncio.set_event_loop(loop)

    # 创建主窗口
    window = ChatWindow()
    window.show()

    # 运行事件循环
    with loop:
        loop.run_forever()

if __name__ == "__main__":
    main()
```

#### 验收标准（Phase 4）

- [ ] 端到端测试：输入消息 → DeepSeek API → 流式回复 → 显示在界面
- [ ] 验证 Token 统计更新（手动计算 vs API 返回）
- [ ] 验证快捷键：Enter 发送，Ctrl+Enter 换行
- [ ] 验证错误处理：无 API Key、网络错误等
- [ ] 验证多轮对话：历史上下文正确传递

---

## 📦 新增依赖

**安装命令**:
```bash
pip install markdown2>=2.4.0 Pygments>=2.17.0
```

**依赖清单**:
```txt
PySide6>=6.6.0        # GUI 框架（已安装）
qasync>=0.28.0        # 异步支持（已安装）
markdown2>=2.4.0      # Markdown 渲染（新增）
Pygments>=2.17.0      # 代码高亮（新增）
openai>=1.0.0         # DeepSeek API（已安装）
python-dotenv>=1.0.0  # 环境变量（已安装）
```

---

## 🧪 测试计划

### 单元测试

**待创建**: `tests/unit/test_chat_view.py`

```python
import pytest
from src.gui.chat_view import TokenPanel, StreamRenderer

def test_token_panel_update():
    """测试 Token 统计面板更新"""
    panel = TokenPanel()
    panel.update_stats(total=1500, round_count=2, input_t=500, output_t=1000)

    assert "1,500" in panel.session_label.text()
    assert "2 / 3" in panel.round_label.text()
    assert panel.warning_label.isVisible() == False

def test_token_panel_3_round_warning():
    """测试 3 轮警告"""
    panel = TokenPanel()
    panel.update_stats(total=1500, round_count=3, input_t=500, output_t=1000)

    assert panel.warning_label.isVisible() == True
    assert "3 轮" in panel.warning_label.text()

def test_token_panel_64k_warning():
    """测试 64K 警告"""
    panel = TokenPanel()
    panel.update_stats(total=62000, round_count=2, input_t=30000, output_t=32000)

    assert panel.warning_label.isVisible() == True
    assert "64K" in panel.warning_label.text()
```

### 手动测试

**测试脚本**: `tests/manual_test_m12/test_chat_window.py`

```python
import sys
import asyncio
from PySide6.QtWidgets import QApplication
from qasync import QEventLoop
from dotenv import load_dotenv
from src.gui.chat_view import ChatWindow

def main():
    load_dotenv()
    app = QApplication(sys.argv)
    loop = QEventLoop(app)
    asyncio.set_event_loop(loop)

    window = ChatWindow()
    window.show()

    with loop:
        loop.run_forever()

if __name__ == "__main__":
    main()
```

**手动测试清单**:
- [ ] 启动窗口正常
- [ ] 侧边栏布局正确
- [ ] 输入"如何使用 asyncio？" → 发送 → 流式回复显示
- [ ] 验证 Markdown 渲染（输入包含代码块的问题）
- [ ] 验证 Token 统计更新
- [ ] 验证快捷键（Enter、Ctrl+Enter）
- [ ] 验证多轮对话（3 轮警告触发）

---

## 🚨 编码规范

### 必须遵守

1. **类型提示**: 所有函数必须有完整类型注解
   ```python
   def add_token(self, token: str) -> None:
   ```

2. **文档字符串**: 所有公开类/方法必须有 docstring
   ```python
   class StreamRenderer:
       """流式输出渲染器（30ms 批量更新）"""
   ```

3. **Signal/Slot 命名**:
   - Signal: 过去式（`token_received`, `error_occurred`）
   - Slot: `on_` 前缀（`on_send_clicked`, `on_token_received`）

4. **样式规范**:
   - 使用 `setStyleSheet()` 内嵌 CSS
   - 颜色使用 Hex 格式（`#E3F2FD`）
   - 布局使用 `QVBoxLayout` / `QHBoxLayout`

5. **错误处理**:
   - 必须捕获 API 调用异常
   - 使用 Signal 传递错误（`error_occurred.emit()`）
   - 禁止裸 `except:`

6. **环境隔离**:
   - 使用 `.env` 文件管理 API Key
   - 使用 `dotenv` 加载环境变量
   - 禁止硬编码 API Key

---

## 📊 进度跟踪

### Phase 1: 基础布局（预计 2 小时）

- [ ] 创建 `src/gui/chat_view.py` 主窗口
- [ ] 实现 `SessionSidebar` 侧边栏
- [ ] 实现 `TokenPanel` Token 统计面板
- [ ] 实现 `ChatArea` 主区域
- [ ] 验收测试通过

### Phase 2: Markdown 渲染（预计 1 小时）

- [ ] 实现 `ChatView` 消息显示区域
- [ ] 实现 `render_markdown()` 渲染函数
- [ ] 实现 `add_user_message()` / `add_assistant_message_start()`
- [ ] 验收测试通过（代码高亮、表格、粗体）

### Phase 3: 流式输出（预计 1 小时）

- [ ] 实现 `StreamRenderer` 流式渲染器
- [ ] 实现 `auto_scroll()` 自动滚动
- [ ] 创建高频测试脚本（100 tokens/s）
- [ ] 验收测试通过（无卡顿）

### Phase 4: 集成测试（预计 1 小时）

- [ ] 实现 `ChatViewModel` 业务逻辑层
- [ ] 实现 `InputArea` 输入区域
- [ ] 实现 `main.py` 主程序入口（qasync 事件循环）
- [ ] 端到端测试通过（真实 DeepSeek API）

### 总计进度

- [ ] 完成 Phase 1
- [ ] 完成 Phase 2
- [ ] 完成 Phase 3
- [ ] 完成 Phase 4
- [ ] 创建单元测试
- [ ] 更新 `M12_DEV_LOG.md`
- [ ] Git 提交

---

## 🎯 验收标准（最终）

### 功能完整性

- ✅ UI 布局完整（侧边栏 + 消息区 + 输入区）
- ✅ Markdown 渲染正确（代码高亮、表格、粗体）
- ✅ 流式输出流畅（100 tokens/s 无卡顿）
- ✅ Token 统计实时更新
- ✅ 快捷键支持（Enter、Ctrl+Enter）
- ✅ 真实 API 集成成功

### 性能指标

- ✅ 30ms 批量更新（StreamRenderer）
- ✅ 100 tokens/s 流式输出无卡顿
- ✅ UI 刷新延迟 < 50ms
- ✅ 内存占用稳定（无泄漏）

### 代码质量

- ✅ 所有函数有类型注解
- ✅ 所有公开 API 有 docstring
- ✅ 单元测试覆盖核心组件
- ✅ 手动测试清单全部通过
- ✅ 无硬编码 API Key

---

## 📚 参考文档

### 技术预研成果

- [01_DEEPSEEK_API_RESEARCH.md](./01_DEEPSEEK_API_RESEARCH.md) - DeepSeek API 调研
- [01_ASYNCIO_QT_RESEARCH.md](./01_ASYNCIO_QT_RESEARCH.md) - qasync 集成方案
- [03_DATABASE_DESIGN.md](./03_DATABASE_DESIGN.md) - 数据库设计
- [06_UI_DESIGN_FINAL.md](./06_UI_DESIGN_FINAL.md) - UI 设计最终方案

### PySide6 官方文档

- [QTextBrowser](https://doc.qt.io/qtforpython-6/PySide6/QtWidgets/QTextBrowser.html)
- [QTextEdit](https://doc.qt.io/qtforpython-6/PySide6/QtWidgets/QTextEdit.html)
- [QSplitter](https://doc.qt.io/qtforpython-6/PySide6/QtWidgets/QSplitter.html)
- [Signal/Slot](https://doc.qt.io/qtforpython-6/overviews/signalsandslots.html)

### 外部库文档

- [markdown2](https://github.com/trentm/python-markdown2)
- [Pygments](https://pygments.org/)
- [qasync](https://github.com/CabbageDevelopment/qasync)
- [OpenAI Python SDK](https://github.com/openai/openai-python)

---

## ⚠️ 注意事项

### API Key 安全

- **禁止**: 硬编码 API Key
- **必须**: 使用 `.env` 文件存储
- **必须**: 在 `.gitignore` 中忽略 `.env`

### 测试环境隔离

- **手动测试**: 使用真实 API（会消耗 Token）
- **单元测试**: 使用 Mock（不调用真实 API）
- **建议**: 测试时使用小模型或限制 `max_tokens`

### 性能注意事项

- **禁止**: 每个 token 都调用 `setHtml()`（会卡顿）
- **必须**: 使用 StreamRenderer 30ms 批量更新
- **建议**: 长文本使用 `document().setMaximumBlockCount()` 限制

### qasync 使用规范

- **必须**: 使用 `@asyncSlot()` 装饰器
- **必须**: 主程序使用 `QEventLoop`
- **禁止**: 在 Qt 主线程中使用 `asyncio.run()`

---

**文档版本**: v1.0
**创建时间**: 2026-02-20 (Day 2 晚上)
**下一步**: Day 3 原型实现
**预计完成**: 2026-02-21 下午

---

**祝你编码愉快！** 🚀✨
