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

| 框架 | Python 集成 | Markdown | AI 聊天 | 包大小 | 5年维护 | 后端可替换 | **总分** |
|------|------------|----------|---------|--------|---------|-----------|---------|
| **PySide6** | ⭐⭐⭐⭐⭐ | ✅ 原生 | ✅ 自定义 | 80-150MB | ✅ LTS 2029 | ✅ 完全解耦 | **95/100** |
| **Flet** | ⭐⭐⭐⭐⭐ | ✅ 原生 | ✅ 自定义 | 40-80MB | ✅ Flutter 支持 | ✅ 完全解耦 | **85/100** |
| **Electron** | ⭐⭐⭐ | ✅ 完美 | ✅ 丰富库 | 180-250MB | ✅ 持续更新 | ✅ 完全解耦 | **75/100** |
| **Tauri** | ⭐⭐ | ✅ 完美 | ✅ 丰富库 | 5-15MB | ⚠️ Python 不成熟 | ✅ 完全解耦 | **70/100** |
| **Gradio** | ⭐⭐⭐⭐⭐ | ✅ 原生 | ⭐⭐⭐⭐⭐ | Web 模式 | ✅ 持续更新 | ⚠️ 受限 | **65/100** |

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

    def __init__(self):
        self.chat_list = QListView()
        self.chat_model = QStandardItemModel()
        self.input_box = QTextEdit()
        self.send_button = QPushButton("发送")

    async def on_send(self):
        """发送消息并流式显示 AI 回复"""
        user_msg = self.input_box.toPlainText()
        self._add_message("user", user_msg)

        # 调用本地 AI 服务（DeepSeek API）
        async for chunk in self.ai_service.stream_chat(user_msg):
            self._append_to_last_message(chunk)
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
- 打包体积：80-150MB（含 Qt 库）
- 许可证：PySide6 (LGPL) vs PyQt6 (GPL) — 推荐 PySide6

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

**劣势**：
- 版本仍 < 1.0（v0.80.5），API 可能变动
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

## 决策矩阵

| 评估维度 | 权重 | PySide6 | Flet | Electron |
|---------|------|---------|------|----------|
| 后端可替换性 | 25% | 10 | 10 | 10 |
| 5年维护保证 | 25% | 10 (LTS 2029) | 7 (< 1.0) | 8 |
| AI 交互能力 | 20% | 8 (自定义) | 7 (自定义) | 10 (丰富库) |
| Python 集成度 | 15% | 10 | 10 | 5 (双栈) |
| 分发体验 | 10% | 7 (80-150MB) | 8 (40-80MB) | 5 (180-250MB) |
| 学习成本 | 5% | 6 (Qt 学习曲线) | 9 (纯 Python) | 6 (双栈) |
| **加权总分** | **100%** | **9.05** | **8.55** | **7.85** |

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

1. **v0.8.0-alpha**: 先搭建基础窗口框架 + Markdown 浏览器
2. **v0.8.0-beta**: 集成搜索界面 + 归档界面
3. **v0.8.0-rc**: 实现 AI 聊天交互（idea Sharpen）
4. **v0.8.0**: 打包分发 + 用户测试

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
