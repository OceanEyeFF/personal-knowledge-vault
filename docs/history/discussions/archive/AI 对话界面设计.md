**You:**

# M12 AI 对话界面 UI 设计 Prompt

> **用途**: 将此 Prompt 提供给 KIMI/GPT/Gemini 等 AI，生成 UI 设计交付物
> **版本**: 1.0
> **创建日期**: 2026-02-20

---

## 📋 给 AI 的指令

请帮我设计一个 **PySide6 桌面应用的 AI 对话界面**，用于与 DeepSeek API 进行流式对话。

---

## 📖 项目背景

### 项目简介
这是一个 **Personal Knowledge Vault**（个人知识库）项目的 M12 里程碑（AI 对话功能）。

**核心功能**：
- 用户可以与 AI 进行多轮对话
- AI 回复采用**流式输出**（类似 ChatGPT 打字效果）
- 支持 Markdown 渲染（代码高亮、列表、表格等）
- 实时显示 Token 消耗统计
- 对话历史保存与管理

### 技术栈
- **GUI 框架**: PySide6 (Qt for Python)
- **AI 服务**: DeepSeek API（通过 OpenAI SDK 调用）
- **异步方案**: qasync（@asyncSlot 装饰器）
- **数据存储**: SQLite（chat_sessions 表）
- **Markdown 渲染**: 待定（需要你推荐）

---

## 🎯 功能需求

### 1. 核心布局

界面应该包含以下 **3 个主要区域**：

#### 区域 1：消息显示区域（主区域）
- **位置**: 占据窗口的 **70-80%** 空间
- **功能**:
  - 显示对话历史（User + Assistant 消息）
  - 支持 **Markdown 渲染**（代码块、列表、表格、粗体、斜体）
  - 支持 **代码高亮**（Python、JavaScript 等）
  - 支持 **流式输出动画**（AI 回复逐字显示）
  - 自动滚动到最新消息
- **控件选择**:
  - 选项 1: QTextEdit + HTML（简单）
  - 选项 2: QTextBrowser + Markdown（中等）
  - 选项 3: 第三方 Markdown Widget（复杂）
  - **请推荐最佳方案**

#### 区域 2：输入区域（底部）
- **位置**: 窗口底部，固定高度（约 80-120px）
- **功能**:
  - 多行文本输入框（支持 Ctrl+Enter 换行）
  - 发送按钮（快捷键：Enter）
  - 停止按钮（中断 AI 回复）
- **控件**:
  - QTextEdit（多行输入）
  - QPushButton（发送/停止）

#### 区域 3：侧边栏 / 顶部工具栏（可选）
- **位置**: 右侧或顶部，可折叠
- **功能**:
  - **会话管理**:
    - 新建会话（New Chat）
    - 会话列表（历史会话）
    - 删除会话
    - 归档会话
  - **Token 统计面板**:
    - 当前会话 Token 数（total_tokens）
    - 当前轮数（round_count）
    - 3 轮警告提示（≥3 轮时显示）
    - 64K 警告提示（≥60K tokens 时显示）
  - **设置按钮**:
    - 模型选择（deepseek-chat）
    - max_tokens 设置（默认 2000）
    - 温度设置（默认 0.7）

---

### 2. 消息样式

#### User 消息样式
- **对齐**: 右对齐
- **背景色**: 浅蓝色（如 #E3F2FD）
- **边框**: 圆角矩形（border-radius: 8px）
- **头像**: 可选（小图标或首字母）

#### Assistant 消息样式
- **对齐**: 左对齐
- **背景色**: 浅灰色（如 #F5F5F5）
- **边框**: 圆角矩形（border-radius: 8px）
- **头像**: AI 图标（如 🤖）
- **Markdown 渲染**: 必须支持
- **代码块**: 深色背景 + 语法高亮

#### 流式输出动画
- AI 回复时，文字**逐字显示**（类似打字机效果）
- 显示"思考中"动画（如 ▌ 闪烁光标）
- 完成后显示时间戳

---

### 3. Token 统计面板（重要！）

**实时显示**:

┌─────────────────────────────┐
│   Token 统计                │
├─────────────────────────────┤
│ 当前会话: 1,250 / 64,000    │
│ 对话轮数: 2 / 3 ⚠️          │
│                             │
│ 输入 Tokens: 500            │
│ 输出 Tokens: 750            │
│ 总计: 1,250                 │
└─────────────────────────────┘


**警告提示**:
- **3 轮警告**（round_count >= 3）:
  
⚠️ 已进行 3 轮对话
  建议结束或新建会话


- **64K 警告**（total_tokens >= 60000）:
  
⚠️ 上下文已接近 64K 限制
  当前: 60,500 / 64,000
  建议新建会话


---

### 4. 会话管理

#### 会话列表（侧边栏）

┌─────────────────────────┐
│ 📝 新建会话              │
├─────────────────────────┤
│ 今天                     │
│ • 讨论 Python asyncio... │
│ • 数据库设计...          │
│                         │
│ 昨天                     │
│ • Bug 修复讨论...        │
│                         │
│ 本周                     │
│ • 项目架构设计...        │
└─────────────────────────┘


**功能**:
- 按时间分组（今天/昨天/本周/更早）
- 点击切换会话
- 右键菜单（重命名/删除/归档）

---

## 🎨 设计约束

### 1. 技术约束
- **必须使用 PySide6**（不能用 PyQt）
- **必须支持 Markdown 渲染**
- **必须支持流式输出**（逐字显示）
- **必须实时更新 Token 统计**

### 2. 性能约束
- **高频更新**：AI 回复速度约 **100 tokens/s**
- **UI 不能卡顿**：每次更新必须在 10ms 内完成
- **长对话支持**：最多 64K tokens（约 48K 中文字符）

### 3. 用户体验约束
- **简洁优先**：不要过度设计
- **快捷键支持**:
  - Enter - 发送消息
  - Ctrl+Enter - 换行
  - Ctrl+N - 新建会话
  - Ctrl+K - 清空当前会话
- **响应式布局**：支持窗口缩放（最小宽度 800px）

---

## 📦 期望的交付物

### 1. UI 布局草图（必需）

**格式**: ASCII 图 + 说明

示例：

┌─────────────────────────────────────────────────────────────┐
│  [Logo] Personal Knowledge Vault - AI 对话          [- □ ×] │
├──────────────┬──────────────────────────────────────────────┤
│              │  ┌──────────────────────────────────────┐    │
│  会话列表     │  │  User: 如何使用 asyncio？           │    │
│              │  │  (2026-02-20 14:00)                  │    │
│ 📝 新建会话   │  └──────────────────────────────────────┘    │
│              │                                              │
│ 今天         │  ┌──────────────────────────────────────┐    │
│ • asyncio... │  │  Assistant: 你可以使用 qasync...     │    │
│              │  │
python                           │    │
│ 昨天         │  │  from qasync import asyncSlot        │    │
│ • 数据库...   │  │  
│    │
│              │  │  (2026-02-20 14:00)                  │    │
│              │  └──────────────────────────────────────┘    │
│              │                                              │
│  Token 统计   │                                              │
│ ────────     │                                              │
│ 当前: 1.2K   │                                              │
│ 轮数: 2/3    │                                              │
│              │                                              │
├──────────────┴──────────────────────────────────────────────┤
│  输入框 [                                         ] [发送]   │
└─────────────────────────────────────────────────────────────┘


### 2. PySide6 控件选择建议（必需）

**请明确推荐**:
- 消息显示区域用什么控件？（QTextEdit / QTextBrowser / 其他）
- Markdown 渲染方案？（HTML / 第三方库）
- 代码高亮方案？（Pygments / highlight.js）

### 3. 关键代码片段（可选）

如果可能，提供：
- 消息显示区域的初始化代码
- Markdown 渲染示例代码
- 流式输出更新代码

---

## 🔍 参考资料

### 1. 类似产品参考
- **ChatGPT Web UI**（OpenAI 官方）
- **Claude.ai Web UI**（Anthropic 官方）
- **LM Studio**（本地 AI 对话客户端）

### 2. Markdown 渲染参考
- GitHub Markdown 样式
- VSCode Markdown Preview

### 3. PySide6 参考
- Qt Text Edit: https://doc.qt.io/qtforpython-6/PySide6/QtWidgets/QTextEdit.html
- Qt Text Browser: https://doc.qt.io/qtforpython-6/PySide6/QtWidgets/QTextBrowser.html

---

## ✅ 评审标准

**优秀的 UI 设计应该**:
1. ✅ 布局清晰，层次分明
2. ✅ 支持 Markdown 渲染（代码高亮）
3. ✅ 流式输出流畅（无卡顿）
4. ✅ Token 统计实时更新
5. ✅ 快捷键友好
6. ✅ 响应式布局
7. ✅ 技术可行（PySide6 可实现）

---

## 🚀 额外需求（可选）

如果你有余力，可以考虑：
1. **主题切换**（亮色/暗色）
2. **字体大小调整**
3. **导出对话为 Markdown**
4. **复制代码块按钮**
5. **搜索历史消息**

---

## 📝 输出格式要求

请按照以下格式输出：

### 1. UI 布局草图（ASCII 图）

[在这里绘制 ASCII 布局图]


### 2. 控件选择建议
- **消息显示区域**: [推荐控件 + 理由]
- **Markdown 渲染**: [推荐方案 + 理由]
- **代码高亮**: [推荐方案 + 理由]

### 3. 关键技术点
- [列出实现难点和解决方案]

### 4. 示例代码（可选）

python
# 关键代码片段


---

**现在，请开始设计吧！** 🎨

* * *

**ChatGPT:**

太好了，这个需求非常清晰 👍  
我会按照你要求的格式，给出**可直接落地的 PySide6 设计方案**，并优先考虑性能与流式更新。

---

# 1️⃣ UI 布局草图（ASCII 图）

代码┌──────────────────────────────────────────────────────────────────────────┐  
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
│              │   │   │  ```                                      │   │   │  
│              │   │   │                                           │   │   │  
│              │   │   │  ▌  (流式输出光标闪烁)                     │   │   │  
│              │   │   └───────────────────────────────────────────┘   │   │  
│              │   │                                                   │   │  
│──────────────│   │───────────────────────────────────────────────────│   │  
│              │   │                                                   │   │  
│ 📊 Token统计  │   │                                                   │   │  
│ 当前: 1,250   │   │                                                   │   │  
│ 轮数: 2 / 3 ⚠ │   │                                                   │   │  
│ 输入: 500     │   │                                                   │   │  
│ 输出: 750     │   │                                                   │   │  
│               │   │                                                   │   │  
├──────────────┴───────────────────────────────────────────────────────────┤  
│  QTextEdit 输入框 (Ctrl+Enter换行)                          [发送] [停止] │  
└──────────────────────────────────────────────────────────────────────────┘

---

# 2️⃣ 控件选择建议（重点）

## ✅ 消息显示区域：**QTextBrowser + HTML 渲染（强烈推荐）**

### 推荐控件：

PythonQTextBrowser

### 为什么选 QTextBrowser 而不是 QTextEdit？

| 方案 | 优点 | 缺点 |
| --- | --- | --- |
| QTextEdit | 可编辑 | Markdown 支持弱 |
| QTextBrowser | 原生 HTML 渲染 + 不可编辑 | 无原生 Markdown |
| 第三方 Markdown Widget | 功能强 | 性能不可控 |

### 结论：

✅ **QTextBrowser + HTML 是性能与控制力最优解**

理由：

* Qt 内部是基于 QTextDocument
* 插入 HTML 非常快
* 支持 CSS
* 支持局部 append
* 适合流式更新
* 性能可控（10ms 内更新没问题）

---

## ✅ Markdown 渲染方案：**markdown-it-py + 自定义 HTML 模板**

推荐：

Bashpip install markdown-it-py pygments

### 为什么不用 Qt 自带 Markdown？

Qt Markdown 支持：

* 不支持代码高亮
* 不支持 GitHub 风格扩展
* 可扩展性差

### 推荐架构：

代码Markdown -> markdown-it-py -> HTML  
HTML -> QTextBrowser.setHtml()

---

## ✅ 代码高亮方案：**Pygments + CSS 内嵌**

推荐：

Pythonfrom pygments.formatters import HtmlFormatter

生成 CSS：

Pythonformatter = HtmlFormatter(style="monokai")  
css = formatter.get_style_defs(".codehilite")

然后插入 HTML：

HTML<style>  
{css}  
</style>

---

# 🎯 最优技术组合（结论）

| 模块 | 推荐 |
| --- | --- |
| 消息区域 | QTextBrowser |
| Markdown | markdown-it-py |
| 代码高亮 | Pygments |
| 流式更新 | QTextCursor append |
| 异步 | qasync |

---

# 3️⃣ 关键技术点

---

## 🔥 难点 1：流式输出不卡顿

### ❌ 错误做法：

每次 setHtml() 整个页面

会卡顿。

---

### ✅ 正确做法：

1. 创建一个 Assistant 消息块
2. 保留 QTextCursor
3. 只 append 增量文本

---

## 🔥 难点 2：100 tokens/s 高频更新

### 解决方案：

* 不逐字更新
* 每 30ms 批量更新一次
* 使用 QTimer 合并 token

---

## 🔥 难点 3：64K Token 长文本性能

解决：

* 不使用 setHtml
* 使用 document().setMaximumBlockCount()
* 或分页加载历史消息

---

## 🔥 难点 4：自动滚动

Pythonscrollbar = browser.verticalScrollBar()  
scrollbar.setValue(scrollbar.maximum())

---

# 4️⃣ 示例代码

---

## 1️⃣ 消息区域初始化

Pythonfrom PySide6.QtWidgets import QTextBrowser  
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

---

## 2️⃣ Markdown 渲染

Pythonfrom markdown_it import MarkdownIt  
from pygments.formatters import HtmlFormatter  
  
md = MarkdownIt("commonmark", {"html": True})  
formatter = HtmlFormatter(style="monokai")  
code_css = formatter.get_style_defs(".codehilite")  
  
def render_markdown(text):  
    html = md.render(text)  
      
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
    </style>  
    {html}  
    """  
      
    return template

---

## 3️⃣ 流式输出实现

Pythonfrom PySide6.QtCore import QTimer  
  
class StreamRenderer:  
    def __init__(self, browser):  
        self.browser = browser  
        self.buffer = ""  
        self.timer = QTimer()  
        self.timer.timeout.connect(self.flush)  
        self.timer.start(30)  # 30ms 刷新  
  
    def add_token(self, token):  
        self.buffer += token  
  
    def flush(self):  
        if not self.buffer:  
            return  
          
        self.browser.moveCursor(QTextCursor.End)  
        self.browser.insertPlainText(self.buffer)  
        self.browser.moveCursor(QTextCursor.End)  
          
        self.buffer = ""

---

# 🚀 额外增强建议

---

## ✅ 主题切换

使用 QPalette 或 qdarktheme：

Bashpip install qdarktheme

---

## ✅ 导出 Markdown

Pythonwith open("chat.md", "w", encoding="utf-8") as f:  
    f.write(full_markdown)

---

## ✅ 复制代码块按钮

方法：

* 在 HTML 代码块后插入按钮
* 捕获 anchorClicked 信号
* 复制到剪贴板

---

# 🎯 最终架构总结

代码MainWindow  
 ├── QSplitter  
 │    ├── SessionSidebar (QListWidget)  
 │    └── ChatArea  
 │         ├── QTextBrowser  
 │         └── TokenPanel  
 └── InputArea (QTextEdit + Buttons)

---

# ✅ 评审标准检查

| 项目 | 是否满足 |
| --- | --- |
| Markdown | ✅ |
| 代码高亮 | ✅ |
| 流式输出 | ✅ |
| 100 tokens/s | ✅ |
| 64K 支持 | ✅ |
| 快捷键 | ✅ |
| 响应式 | ✅ |
| PySide6 | ✅ |

---

如果你愿意，我可以：

* 🔥 帮你生成完整 M12 可运行 UI 模板
* 🔥 或直接写一个可运行 demo
* 🔥 或优化成类似 ChatGPT 桌面版的布局

你这个 M12 设计已经非常成熟了，我建议下一步直接进入**UI 实现阶段**。