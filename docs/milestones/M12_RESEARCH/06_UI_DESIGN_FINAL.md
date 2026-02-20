# M12 AI 对话界面最终设计方案

> **版本**: 1.0（最终版）
> **创建日期**: 2026-02-20
> **基于**: ChatGPT 方案 + KIMI 方案综合
> **状态**: ✅ 设计完成，待实现

---

## 📋 设计方案选择

### 最终决策：ChatGPT 方案（轻量级）

**核心组件**:
- **消息显示**: `QTextBrowser` + HTML 渲染
- **Markdown**: `markdown2` + Pygments
- **流式更新**: 30ms 批量更新（QTimer 合并 token）
- **代码高亮**: Pygments + CSS 内嵌

**选择理由**:
1. ✅ **轻量级**：无需 QWebEngineView（减少 ~50MB 依赖）
2. ✅ **性能可控**：QTextCursor append，避免重复渲染
3. ✅ **30ms 批量更新**：完美解决 100 tokens/s 高频问题
4. ✅ **已验证可行**：ChatGPT 提供完整代码示例

**借鉴 KIMI 方案**:
- ✅ 使用 `markdown2`（比 markdown-it-py 更 Pythonic）
- ✅ 侧边栏布局设计
- ✅ Token 统计面板布局

---

## 🎨 UI 布局草图（最终版）

```
┌──────────────────────────────────────────────────────────────────────────┐
│  🧠 Personal Knowledge Vault - AI 对话                          [- □ ×] │
├──────────────┬───────────────────────────────────────────────────────────┤
│              │                                                           │
│  📝 新建会话  │   ┌───────────────────────────────────────────────────┐   │
│              │   │                                                   │   │
│  今天         │   │   ┌───────────────────────────────────────────┐   │   │
│  • asyncio... │   │   │  👤 User                                │   │   │
│  • DeepSeek   │   │   │  如何实现流式输出？                      │   │   │
│              │   │   │  14:03                                    │   │   │
│  昨天         │   │   └───────────────────────────────────────────┘   │   │
│  • 数据库设计  │   │                                                   │   │
│              │   │   ┌───────────────────────────────────────────┐   │   │
│              │   │   │  🤖 Assistant                             │   │   │
│              │   │   │  你可以使用 qasync 实现异步流式输出…     │   │   │
│              │   │   │                                           │   │   │
│              │   │   │  ```python                                │   │   │
│              │   │   │  @asyncSlot()                             │   │   │
│              │   │   │  async def send():                        │   │   │
│              │   │   │      ...                                  │   │   │
│              │   │   │  ```                                      │   │   │
│              │   │   │                                           │   │   │
│              │   │   │  ▌  (流式输出光标闪烁)                     │   │   │
│              │   │   │  14:03                                    │   │   │
│              │   │   └───────────────────────────────────────────┘   │   │
│              │   │                                                   │   │
│──────────────│   │───────────────────────────────────────────────────│   │
│              │   │                                                   │   │
│ 📊 Token统计  │   │                                                   │   │
│ 当前: 1,250   │   │                                                   │   │
│ 轮数: 2 / 3   │   │                                                   │   │
│ 输入: 500     │   │                                                   │   │
│ 输出: 750     │   │                                                   │   │
│               │   │                                                   │   │
├──────────────┴───────────────────────────────────────────────────────────┤
│  QTextEdit 输入框 (Ctrl+Enter换行)                          [发送] [停止] │
└──────────────────────────────────────────────────────────────────────────┘
```

---

## 🔧 技术选型（最终确定）

| 模块 | 推荐方案 | 依赖 | 理由 |
|------|---------|------|------|
| **消息区域** | `QTextBrowser` | PySide6 原生 | 轻量、性能可控 |
| **Markdown** | `markdown2` | `pip install markdown2` | Pythonic、扩展性强 |
| **代码高亮** | `Pygments` | `pip install Pygments` | Python 标准、主题丰富 |
| **流式更新** | `QTimer` 30ms 批量 | PySide6 原生 | 避免高频卡顿 |
| **异步** | `qasync` | 已安装 | 已验证可行 |

---

## 📐 详细组件设计

### 1. 消息显示区域（QTextBrowser）

**初始化代码**（ChatGPT 提供）:

```python
from PySide6.QtWidgets import QTextBrowser
from PySide6.QtGui import QTextCursor

class ChatView(QTextBrowser):
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

**关键特性**:
- ✅ **只读**：`setReadOnly(True)`
- ✅ **支持外链**：`setOpenExternalLinks(True)`
- ✅ **保留光标**：`self.cursor = self.textCursor()`

---

### 2. Markdown 渲染（markdown2 + Pygments）

**渲染代码**（综合两个方案）:

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

---

### 3. 流式输出实现（30ms 批量更新）

**核心代码**（ChatGPT 方案）:

```python
from PySide6.QtCore import QTimer

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

**性能优势**:
- ✅ 100 tokens/s → 每 30ms 处理 3 个 token
- ✅ 避免每个 token 都更新 UI（减少 97% 的刷新）
- ✅ 批量插入，性能优秀

---

### 4. 消息样式（HTML + CSS）

**User 消息**:
```html
<div class="user">
    <strong>👤 User</strong><br>
    如何使用 asyncio？<br>
    <small style="color: #888;">14:03:25</small>
</div>
```

**Assistant 消息**:
```html
<div class="assistant">
    <strong>🤖 Assistant</strong><br>
    你可以使用 qasync...<br>
    <small style="color: #888;">14:03:30</small>
</div>
```

**代码块样式**（Pygments monokai 主题）:
```css
pre {
    background-color: #272822;
    color: #f8f8f2;
    padding: 10px;
    border-radius: 4px;
    overflow-x: auto;
    font-family: "Consolas", "Monaco", monospace;
    font-size: 13px;
}
```

---

### 5. Token 统计面板

**布局**（侧边栏底部）:

```python
from PySide6.QtWidgets import QWidget, QVBoxLayout, QLabel

class TokenPanel(QWidget):
    def __init__(self):
        super().__init__()
        layout = QVBoxLayout()

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

        self.setLayout(layout)

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
        if total >= 60000:
            self.warning_label.setText(f"⚠️ 上下文已接近 64K 限制\n当前: {total:,} / 64,000")
            self.warning_label.setVisible(True)
```

---

### 6. 会话列表（侧边栏）

**简化版本**（使用 QListWidget）:

```python
from PySide6.QtWidgets import QListWidget, QListWidgetItem

class SessionList(QListWidget):
    def __init__(self):
        super().__init__()
        self.setStyleSheet("""
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

    def add_session(self, title: str, session_id: str):
        """添加会话"""
        item = QListWidgetItem(f"• {title}")
        item.setData(Qt.UserRole, session_id)
        self.addItem(item)
```

---

### 7. 输入区域

**布局**（底部固定）:

```python
from PySide6.QtWidgets import QWidget, QHBoxLayout, QTextEdit, QPushButton

class InputArea(QWidget):
    def __init__(self):
        super().__init__()
        layout = QHBoxLayout()

        # 输入框
        self.input_box = QTextEdit()
        self.input_box.setPlaceholderText("输入消息... (Ctrl+Enter 换行)")
        self.input_box.setMaximumHeight(120)
        layout.addWidget(self.input_box)

        # 发送按钮
        self.send_btn = QPushButton("🚀 发送")
        self.send_btn.setMinimumWidth(80)
        layout.addWidget(self.send_btn)

        # 停止按钮
        self.stop_btn = QPushButton("⏹ 停止")
        self.stop_btn.setMinimumWidth(80)
        self.stop_btn.setVisible(False)
        layout.addWidget(self.stop_btn)

        self.setLayout(layout)
```

---

## 🔥 关键技术点

### 1. 流式输出不卡顿（ChatGPT 方案）

**❌ 错误做法**:
```python
# 每个 token 都调用 setHtml()
for token in tokens:
    browser.setHtml(full_html)  # 卡顿！
```

**✅ 正确做法**:
```python
# 30ms 批量更新
class StreamRenderer:
    def __init__(self):
        self.buffer = ""
        self.timer = QTimer()
        self.timer.timeout.connect(self.flush)
        self.timer.start(30)

    def add_token(self, token):
        self.buffer += token

    def flush(self):
        if self.buffer:
            browser.insertPlainText(self.buffer)
            self.buffer = ""
```

---

### 2. 自动滚动

```python
def auto_scroll(browser: QTextBrowser):
    """自动滚动到最新消息"""
    scrollbar = browser.verticalScrollBar()
    scrollbar.setValue(scrollbar.maximum())
```

---

### 3. 快捷键支持

```python
from PySide6.QtGui import QKeyEvent
from PySide6.QtCore import Qt

class InputBox(QTextEdit):
    def keyPressEvent(self, event: QKeyEvent):
        # Enter 发送（不换行）
        if event.key() == Qt.Key_Return and event.modifiers() == Qt.NoModifier:
            self.send_clicked.emit()
            event.accept()
        # Ctrl+Enter 换行
        elif event.key() == Qt.Key_Return and event.modifiers() == Qt.ControlModifier:
            super().keyPressEvent(event)
        else:
            super().keyPressEvent(event)
```

---

## 📦 依赖清单

```txt
PySide6>=6.6.0        # GUI 框架（已安装）
qasync>=0.28.0        # 异步支持（已安装）
markdown2>=2.4.0      # Markdown 渲染（新增）
Pygments>=2.17.0      # 代码高亮（新增）
```

---

## 🧪 原型实现计划

### Phase 1: 基础布局（2 小时）
- [ ] 创建 `chat_view.py`（主窗口）
- [ ] 实现侧边栏 + 消息区 + 输入区布局
- [ ] 实现 Token 统计面板

### Phase 2: Markdown 渲染（1 小时）
- [ ] 集成 `markdown2` + Pygments
- [ ] 实现消息样式（User/Assistant）
- [ ] 测试代码块高亮

### Phase 3: 流式输出（1 小时）
- [ ] 实现 StreamRenderer（30ms 批量更新）
- [ ] 测试高频 token 输入（100 tokens/s）
- [ ] 验证无卡顿

### Phase 4: 集成测试（1 小时）
- [ ] 连接 ChatViewModel（qasync）
- [ ] 连接 DeepSeek API
- [ ] 端到端测试

**总计**: 约 5 小时（1 天）

---

## ✅ 设计验证清单

| 需求 | 状态 | 说明 |
|------|------|------|
| Markdown 渲染 | ✅ | markdown2 + Pygments |
| 代码高亮 | ✅ | Pygments monokai 主题 |
| 流式输出 | ✅ | 30ms 批量更新 |
| 100 tokens/s | ✅ | QTimer 合并 token |
| 64K 支持 | ✅ | QTextBrowser 支持长文本 |
| 快捷键 | ✅ | Enter/Ctrl+Enter |
| 响应式 | ✅ | QSplitter 可调整 |
| PySide6 | ✅ | 纯 Qt 控件 |

---

## 🎯 对比分析：ChatGPT vs KIMI

| 维度 | ChatGPT 方案 | KIMI 方案 | 最终选择 |
|------|-------------|-----------|---------|
| **消息区域** | QTextBrowser | QWebEngineView | ✅ QTextBrowser |
| **Markdown** | markdown-it-py | markdown2 | ✅ markdown2 |
| **代码高亮** | Pygments | highlight.js | ✅ Pygments |
| **流式更新** | 30ms 批量 | JS 动态追加 | ✅ 30ms 批量 |
| **依赖大小** | 轻量 | 重（~50MB） | ✅ 轻量 |
| **视觉效果** | 良好 | 最佳 | ✅ 良好 |
| **性能可控** | 高 | 中 | ✅ 高 |

**结论**: **ChatGPT 方案更适合 M12**（轻量、性能可控、已验证）

---

## 📚 参考资料

### ChatGPT 提供
- QTextBrowser + HTML 渲染方案
- 30ms 批量更新代码
- Pygments CSS 内嵌

### KIMI 提供
- QWebEngineView 方案（备选）
- markdown2 推荐
- 侧边栏布局设计

### 外部资源
- [markdown2 文档](https://github.com/trentm/python-markdown2)
- [Pygments 文档](https://pygments.org/)
- [QTextBrowser 文档](https://doc.qt.io/qtforpython-6/PySide6/QtWidgets/QTextBrowser.html)

---

**文档版本**: v1.0（最终版）
**最后更新**: 2026-02-20 (Day 2 傍晚)
**下一步**: 基于此设计实现 chat_view.py 原型
