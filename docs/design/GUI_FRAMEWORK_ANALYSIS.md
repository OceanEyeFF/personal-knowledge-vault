# GUI 框架选型分析报告

> Personal Knowledge Vault - Phase 2 GUI 技术选型
>
> **文档版本**: v1.0
> **创建日期**: 2026-02-16
> **作者**: 幽浮喵 (猫娘工程师)

---

## 背景与需求

### 核心诉求

1. **后端可替换**：改内核但前端可以不用大改
2. **长期可维护**：前端能跟着技术迭代维护 5 年以上
3. **AI 交互内置**：AI 协作功能在自己软件内搞定，不依赖 Claude Code
4. **Python 友好**：与现有 Python 后端无缝集成

### 功能要求

| 要求 | 优先级 | 说明 |
|------|--------|------|
| Markdown 渲染 | **P0** | 知识条目展示（YAML Front Matter + 正文） |
| AI 聊天界面 | **P0** | idea Sharpen 对话、知识问答 |
| 搜索结果展示 | **P0** | 列表+高亮+排序 |
| 知识库浏览 | **P1** | 标签筛选、时间线、统计图表 |
| 跨平台 | **P1** | Windows 优先，macOS/Linux 次要 |
| 分发便捷 | **P2** | 一键安装，包体积合理 |

---

## 候选框架对比

### 综合评分表

| 框架 | Python 集成 | Markdown | AI 聊天 | 包大小（实测） | 5年维护 | 后端可替换 | **总分** |
|------|------------|----------|---------|--------|---------|-----------|---------|
| **PySide6** | ⭐⭐⭐⭐⭐ | ✅ 原生 | ✅ 自定义 | **250-500MB**（含 QWebEngineView）| ✅ LTS 2029 | ✅ 完全解耦 | **90/100** |
| **Flet** | ⭐⭐⭐⭐⭐ | ✅ 原生 | ✅ 自定义 | 40-80MB | ✅ Beta v0.80.x（迈向 1.0） | ✅ 完全解耦 | **83/100** |
| **Electron** | ⭐⭐⭐ | ✅ 完美 | ✅ 丰富库 | 180-250MB | ✅ 持续更新 | ✅ 完全解耦 | **75/100** |
| **Tauri** | ⭐⭐ | ✅ 完美 | ✅ 丰富库 | 5-15MB | ⚠️ Python 不成熟 | ✅ 完全解耦 | **70/100** |
| **Gradio** | ⭐⭐⭐⭐⭐ | ✅ 原生 | ⭐⭐⭐⭐⭐ | Web 模式 | ✅ 持续更新 | ⚠️ 受限 | **65/100** |

> **注意**：PySide6 实际打包体积数据来自 2024-2025 实测报告：
> - Windows 基础包（无 QWebEngineView）：~100-250MB（PyInstaller v5.7+ 起因 Hook 变化体积增大）
> - 含 QWebEngineView（`QtWebEngineCore` 本身约 130MB）：250-500MB
> - Linux 因拉取全套 Qt 库可能超过 600MB
> - 缓解方案：`.spec` 文件中使用 `--exclude-module` 剔除无用 Qt 模块

---

## Top 3 深度分析

### 🥇 方案 A：PySide6 / PyQt6（推荐）

**推荐理由**：

1. **Qt 6.8 LTS → 2029 年** — 浮浮酱认为这是最关键的优势，完全满足 5 年维护要求
2. **纯 Python** — 与现有后端代码库同一技术栈，无需引入 JS/TS
3. **QWebEngineView** — 可嵌入完整的 Web 渲染引擎，支持复杂 Markdown + 代码高亮
4. **信号/槽机制** — 天然支持 UI 与后端解耦

**AI 聊天界面方案**：

```python
# 自定义 ChatWidget：QListView + QStyledItemDelegate
class ChatWidget(QWidget):
    """AI 聊天交互组件"""

    # Qt 信号：将 AI 流式 token 从 asyncio 线程传回 UI 线程
    token_received = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)  # 必须调用！PySide6 控件不调用 super().__init__ 会崩溃
        self.chat_list = QListView()
        self.chat_model = QStandardItemModel()
        self.input_box = QTextEdit()
        self.send_button = QPushButton("发送")
        self.token_received.connect(self._append_to_last_message)

    def on_send(self):
        """发送消息（同步槽函数）— Qt 槽不能直接是 async def。

        AI 调用在独立 QThread 中运行 asyncio 事件循环，
        通过 token_received Signal 将流式 token 传回 UI 线程。
        """
        user_msg = self.input_box.toPlainText()
        self._add_message("user", user_msg)
        # 启动 AI 工作线程（见 M12 QThread + asyncio 方案）
        self.ai_thread = AIChatThread(user_msg, self.token_received)
        self.ai_thread.start()
```

**Markdown 渲染方案**：

```python
# 方案 1：QTextEdit (简单场景，原生支持)
self.text_edit.setMarkdown(markdown_content)

# 方案 2：QWebEngineView + markdown-it.js (复杂场景)
html = markdown_to_html(content, code_highlight=True)
self.web_view.setHtml(html)
```

**架构设计**：

```
┌─────────────────────────────────────┐
│  PySide6 UI Layer                   │
│  ├── MainWindow (QMainWindow)       │
│  ├── KnowledgeBrowser (QTreeView)   │
│  ├── MarkdownViewer (QWebEngineView)│
│  ├── SearchPanel (QLineEdit+QTable) │
│  └── ChatWidget (Custom QListView)  │
├─────────────────────────────────────┤
│  ViewModel Layer (Signals/Slots)    │
│  ├── ArchiveViewModel               │
│  ├── SearchViewModel                │
│  └── ChatViewModel                  │
├─────────────────────────────────────┤
│  Service Layer (Existing Code!)     │
│  ├── WorkflowEngine                 │
│  ├── RetrievalEngine                │
│  ├── AI Services                    │
│  └── Storage Layer                  │
└─────────────────────────────────────┘
```

**优势**：Service Layer 直接复用现有代码，只需新增 UI + ViewModel 层

**劣势**：
- 学习曲线：需要理解 Qt 信号/槽机制
- 打包体积（实测）：250-500MB（含 QWebEngineView），Windows 约 250MB，Linux 可达 600MB+
  - 缓解：使用 `QTextEdit.setMarkdown()` 替代 QWebEngineView 可显著减小体积（退回 100-150MB 区间）
- 许可证：PySide6 (LGPL) vs PyQt6 (GPL) — 推荐 PySide6
- asyncio 集成需要额外方案（见下方"PySide6 + asyncio 集成"小节）

---

### 🥈 方案 B：Flet（备选）

**推荐理由**：

1. **纯 Python** — 零前端知识门槛
2. **Material Design** — 现代美观的 UI
3. **移动扩展** — 未来可一键打包 Android/iOS
4. **包体积小** — 40-80MB

**适合场景**：
- 快速原型开发
- 未来需要移动端支持
- 团队纯 Python 背景

**当前状态（2026-02 更新）**：
- v0.80.5（Beta 阶段，PyPI 最新稳定版，2026-01-30 发布）
- 1.0 路线：0.70=Alpha → **0.80=Beta** → 0.90=RC → 1.0 正式版
- API 在 0.80.x 阶段已较稳定（0.90.x 才冻结 API），比原评分时（< 1.0）更可用

**劣势**：
- 尚未到 1.0 正式版，核心 API 仍有小幅变动可能
- 社区比 Qt 小得多
- 自定义控件灵活性不如 Qt

---

### 🥉 方案 C：Electron + Vue/React（Web 路线）

**推荐理由**：

1. **最强 AI 聊天 UI 生态** — react-chat-elements、chatscope 等
2. **Markdown 渲染最强** — marked.js、prismjs 代码高亮
3. **Web 技术人才多** — 容易招人维护

**劣势**：
- 包体积 180-250MB
- 内存占用 150-300MB
- 需要 JS/TS + Python 双技术栈
- 前后端通信需要 FastAPI/REST 桥接

---

## PySide6 + asyncio 集成方案

> Qt 有自己的事件循环（`QEventLoop`），Python asyncio 也有独立事件循环，两者不能直接混用。
> 以下是经过验证的三种方案：

| 方案 | 库 | 维护方 | 适用场景 | 本项目适用性 |
|------|-----|--------|---------|------------|
| **`PySide6.QtAsyncio`** | PySide6 内置 | Qt 官方 | 简单协程（非网络 I/O） | ⚠️ **不适用** — DNS/Socket 未完整实现，调用 DeepSeek API (httpx) 会失败 |
| **`qasync`** | 第三方 | 社区维护 | 需要 aiohttp/httpx 等完整 asyncio | ✅ 可用 — 将 asyncio 事件循环替换为 Qt 事件循环，需额外依赖 `qasync>=0.23.0` |
| **QThread + asyncio** | 标准库 | DIY | 完全隔离 Qt 和 asyncio | ✅ **推荐** — AI 线程独立运行 asyncio，通过 Qt Signal 回传 token，零额外依赖 |

**本项目建议方案**（M12 AI 聊天功能）：

```python
# 推荐：QThread + asyncio 隔离方案（无额外依赖，DeepSeek API 完全兼容）
from PySide6.QtCore import QThread, Signal
import asyncio

class AIChatThread(QThread):
    """在独立线程中运行 asyncio 事件循环，通过 Signal 回传流式 token"""
    token_received = Signal(str)
    finished = Signal()

    def __init__(self, messages: list, parent=None):
        super().__init__(parent)
        self.messages = messages

    def run(self):
        # 独立线程有自己的 asyncio 事件循环，不干扰 Qt 主线程
        asyncio.run(self._stream_chat())

    async def _stream_chat(self):
        async for chunk in ai_service.stream_chat(self.messages):
            self.token_received.emit(chunk)  # 通过 Signal 传回 UI 线程
        self.finished.emit()
```

**注意事项**：
- `PySide6.QtAsyncio`：DNS/Socket 事件未完整实现，**本项目禁用**（调用 DeepSeek httpx SDK 会失败）
- `qasync`：可行但引入了额外依赖，仅在 QThread 方案不满足需求时考虑
- **推荐 QThread 方案**：AI 在独立线程中运行完整 asyncio，Qt Signal 是线程安全的跨线程通信机制
- 依赖追加：无（仅需标准库 asyncio + PySide6 内置 QThread/Signal）

---

## 决策矩阵

| 评估维度 | 权重 | PySide6 | Flet | Electron |
|---------|------|---------|------|----------|
| 后端可替换性 | 25% | 10 | 10 | 10 |
| 5年维护保证 | 25% | 10 (LTS 2029) | 7.5 (Beta 0.80.x，API 渐趋稳定) | 8 |
| AI 交互能力 | 20% | 8 (自定义 + asyncio 需额外方案) | 7 (自定义) | 10 (丰富库) |
| Python 集成度 | 15% | 10 | 10 | 5 (双栈) |
| 分发体验 | 10% | 5 (250-500MB，含 QWebEngineView) | 8 (40-80MB) | 5 (180-250MB) |
| 学习成本 | 5% | 6 (Qt 学习曲线 + asyncio 集成) | 9 (纯 Python) | 6 (双栈) |
| **加权总分** | **100%** | **8.775** | **8.625** | **7.85** |

> 修订说明（2026-02）：PySide6 分发体验评分从 7 → 5（基于实测体积数据修正），Flet 维护评分从 7 → 7.5（Beta 阶段较前稳定）。PySide6 仍为推荐选择，但两者差距收窄，如打包体积是关键约束可考虑 Flet。

---

## 最终建议

### ✅ 推荐选择：PySide6

**理由总结**：

1. **最符合"改内核不改前端"** — Qt 的 MVC 架构天然支持 UI/业务解耦
2. **最稳定的长期维护** — Qt 6.8 LTS 官方支持至 2029 年
3. **与现有代码库同栈** — Python → Python，复用 src/ 下所有现有代码
4. **AI 交互可自研** — 通过 QWebEngineView + 自定义 Widget 实现嵌入式 AI 对话
5. **许可证友好** — PySide6 (LGPL) 允许商业闭源使用

### 实施建议

1. **v0.8.0-alpha** (M10): 搭建基础窗口框架 + 知识浏览 + 搜索界面
2. **v0.8.0-beta** (M11): 归档界面 + 设置界面 + 统计面板
3. **v0.8.0** (M12): 实现内置 AI 对话交互（DeepSeek 直调 + 会话记录）
4. **v0.8.1** (M13): 打包分发 + E2E 测试 + 用户文档

### 替代方案触发条件

如果以下情况出现，可以考虑切换到 **Flet**：
- Qt 学习曲线导致开发效率过低
- 未来确定需要移动端支持
- PySide6 许可证出现变化

如果以下情况出现，可以考虑 **Electron**：
- 前端需要非常复杂的 Web 特性（如知识图谱可视化）
- 团队引入了前端开发者

---

**文档结束**

*本文档基于 2026-02 的框架状态分析，建议在实际开发启动前重新验证框架版本*
