# Personal Knowledge Vault - Phase 2B 开发 Prompt

> GUI 桌面应用开发执行指令（M10 ~ M13）
>
> **版本**: 1.0
> **创建日期**: 2026-02-18
> **适用对象**: Claude Code、CodeX 等 AI 开发工具
> **前置条件**: Phase 1 (v0.6.1) 已全部完成；Phase 2A (v0.7.0) 建议先完成
> **总览文档**: [PHASE2_DEV_PROMPT.md](./PHASE2_DEV_PROMPT.md)

---

## 🎯 Phase 2B 目标

构建 **PySide6 桌面 GUI 应用**，提供图形化知识浏览、归档和内置 AI 对话能力，
使用户不再依赖命令行，并内嵌 AI 对话功能（不依赖 Claude Code 等外部工具）。

**交付版本**: v0.8.0-alpha（M10）→ v0.8.0-beta（M11）→ v0.8.0（M12）→ v0.8.1（M13）

---

## 📚 必读文档

- [PHASE2_DEV_PROMPT.md](./PHASE2_DEV_PROMPT.md) - Phase 2 总览（约束 + 原则 + 里程碑）
- [GUI_FRAMEWORK_ANALYSIS.md](../design/GUI_FRAMEWORK_ANALYSIS.md) - **GUI 框架选型分析**（必读！）
- [Storage接口规范.md](../refactor/Storage接口规范.md) - SQLiteStore / MarkdownStore API
- [Retrieval检索引擎规范.md](../refactor/Retrieval检索引擎规范.md) - 检索引擎 API
- [WorkflowEngine接口规范.md](../refactor/WorkflowEngine接口规范.md) - 工作流引擎 API

---

## ⚙️ 技术基础

- **GUI 框架**: PySide6 (Qt 6.8 LTS，官方支持至 2029 年)
- **许可证**: PySide6 (LGPL) — 允许商业闭源使用
- **架构模式**: MVVM（ViewModel 层隔离 UI 与 Service Layer）
- **asyncio 集成**（⚠️ 重要）：
  - Qt 有自己的 `QEventLoop`，不能直接 `await` asyncio 协程
  - **推荐方案**：QThread + asyncio 隔离 — AI 调用在独立线程运行 asyncio，通过 Qt Signal 回传 token
  - **禁用**：`PySide6.QtAsyncio` — DNS/Socket 未完整实现，调用 DeepSeek API (httpx) 会失败
  - **备选**：`qasync>=0.23.0` — 可行但引入额外依赖
- **新增依赖**:
  ```txt
  PySide6>=6.8.0          # Qt for Python (LTS)
  PySide6-Addons>=6.8.0   # QWebEngineView 等附加组件（M10 需要）
  ```

> **M10 Markdown 渲染建议**：优先使用 `QTextEdit.setMarkdown()` 以降低打包体积（约 150MB）；仅在需要代码高亮时再升级为 `QWebEngineView`。

---

## ⚠️ 上游接口对齐注意事项（实现前必读）

1. `SQLiteStore` 查询 API 已完整实现：`list_entries`、`count_entries`、`get_all_tags_with_count`、`get_statistics`、`query_by_url` 可用；`sort_by` 仅允许白名单字段。
2. `SearchResult` 是 frozen dataclass，仅包含 `knowledge_id`、`title`、`score`、`highlight`、`metadata`；不存在 `.abstract` / `.tags` / `.source_type` 属性。
3. `MarkdownStore.load()` 返回 `Optional[Entry]`，不是字符串；请使用 `entry.content`。
4. 数据库中的 `tags` / `keywords` 为逗号分隔字符串。
5. `WorkflowEngine` 没有进度回调（M11 使用脉冲动画）。
6. DeepSeek API 目前只有同步实现（M12 需新增异步实现）。

---

## 🏗️ Milestone 10: GUI 基础框架 + 知识浏览 (v0.8.0-alpha)

**目标**: 搭建 PySide6 GUI 框架，实现知识库浏览和搜索两大核心只读界面

**前置**: M8 完成（共享 Service Layer，但 GUI 不依赖 MCP）

**交付物**:

- [ ] `src/gui/__init__.py` - GUI 模块初始化
- [ ] `src/gui/app.py` - QApplication 主入口（事件循环、异常处理）
- [ ] `src/gui/main_window.py` - 主窗口
  - [ ] QMainWindow 框架（菜单栏 + 工具栏 + 状态栏）
  - [ ] 侧边导航栏（视图切换）
  - [ ] 全局快捷键注册
- [ ] `src/gui/styles/` - QSS 样式表
  - [ ] `light.qss` - 明亮主题
  - [ ] `dark.qss` - 暗色主题
- [ ] `src/gui/assets/` - 图标和资源文件
- [ ] `src/gui/views/browser_view.py` - 知识库浏览界面
  - [ ] 左侧：标签树 / 来源分类（QTreeView）
  - [ ] 中间：条目列表（QTableView + 自定义 Model）
  - [ ] 右侧：Markdown 预览（QWebEngineView 或 QTextEdit）
- [ ] `src/gui/views/search_view.py` - 搜索界面
  - [ ] 搜索框 + 策略选择（BM25/向量/混合/自动）
  - [ ] 结果列表 + 高亮匹配
  - [ ] 快捷键支持（Ctrl+K 全局搜索）
- [ ] `src/gui/models/` - Qt MVC 数据模型
  - [ ] `entry_model.py` - 知识条目数据模型
  - [ ] `tag_model.py` - 标签数据模型
- [ ] `tests/unit/test_gui_models.py` - 数据模型单元测试

**验收检查点**:
1. `python -m src.gui.app` 启动后显示主窗口（无崩溃）
2. 知识库浏览：标签树正确显示 → 点击标签筛选列表 → 点击条目预览 Markdown
3. 搜索功能：输入关键词 → 返回结果 → 点击查看详情
4. 明亮/暗色主题可切换
5. 窗口关闭不产生资源泄漏
6. 冷启动时间 < 3s
7. 窗口关闭时状态已保存到 QSettings（重启后恢复位置/大小）

---

## 🏗️ Milestone 11: GUI 归档 + 设置 (v0.8.0-beta)

**目标**: 实现 GUI 写入能力（归档界面）和用户配置管理

**前置**: M10 完成

> **⚠️ M11 工程注意（额外工作量）：WorkflowEngine 进度回调**
>
> 当前 `WorkflowEngine` (`src/workflow/engine.py`) **没有进度回调机制**，只能等待 `execute_async()` 完成后才有结果。
> 要实现 GUI 的实时进度条，需要在 M11 中对 WorkflowEngine 进行改造：
>
> 1. **方案 A（推荐）**：在 `WorkflowEngine.execute_async()` 中添加 `on_progress: Callable[[int, str], None] | None = None` 回调参数，每完成一个步骤时调用。GUI ViewModel 通过 `functools.partial` 绑定 Signal 发射。
> 2. **方案 B（简化）**：不改 WorkflowEngine，在 QThread 中用固定步骤数模拟进度（如：开始时 10%，完成时 100%），中间用脉冲动画（indeterminate mode）代替精确进度。
>
> **建议**: 优先方案 B（成本低），若后续有精确进度需求再升级方案 A。这是 M11 相比文档估算的 **额外 1-2 天工作量**。

**交付物**:

- [ ] `src/gui/views/archive_view.py` - 归档界面
  - [ ] URL 归档表单（输入框 + 归档按钮）
  - [ ] 文本归档编辑器（多行文本框 + 可选标题）
  - [ ] 进度显示（QProgressBar + 状态文字）
  - [ ] 结果预览和确认（归档完成后跳转查看）
- [ ] `src/gui/viewmodels/archive_viewmodel.py` - 归档 ViewModel
  - [ ] 异步工作流调用（不阻塞 UI 线程）
  - [ ] 进度信号发射（见上方工程注意）
- [ ] `src/gui/views/stats_view.py` - 统计面板
  - [ ] 知识库概况（条目总数、标签分布、来源分布）
  - [ ] 简单图表展示（可选：matplotlib 或纯 Qt 绘制）
- [ ] `src/gui/views/settings_view.py` - 设置界面
  - [ ] API Key 配置（DeepSeek、OpenAI）
  - [ ] 检索策略默认值配置
  - [ ] 主题切换
  - [ ] 数据目录设置
- [ ] `src/gui/viewmodels/settings_viewmodel.py` - 设置 ViewModel
- [ ] `tests/unit/test_gui_archive.py` - 归档流程测试

**验收检查点**:
1. 输入 URL → 点击归档 → 进度条显示 → 完成后可在浏览界面查看
2. 输入文本 → 归档为知识条目 → 搜索可命中
3. 统计面板正确显示数据概况
4. 设置修改后立即生效（无需重启）
5. 归档过程 UI 不冻结（异步执行）

---

## 🏗️ Milestone 12: AI 对话交互 (v0.8.0)

**目标**: 实现内置 AI 对话能力，包含聊天界面、对话服务、对话记录存储

**前置**: M11 完成

> **M12 asyncio 集成方案**（关键！）：
> Qt `QEventLoop` 与 Python asyncio 事件循环**不能直接混用**。
> 推荐采用 **QThread + asyncio 隔离方案**：
> - AI 聊天服务在独立 `QThread` 中运行专属 asyncio 事件循环
> - AI 的流式 token 通过 Qt Signal 传回主线程（UI 线程）
> - **禁用** `PySide6.QtAsyncio`（DNS/Socket 未完整实现，会导致 DeepSeek API 调用失败）

```python
class AIChatThread(QThread):
    token_received = Signal(str)
    finished = Signal()

    def __init__(self, messages: list, parent=None):
        super().__init__(parent)
        self.messages = messages

    def run(self):
        asyncio.run(self._stream_chat())  # 独立线程，独立事件循环

    async def _stream_chat(self):
        async for chunk in ai_service.stream_chat(self.messages):
            self.token_received.emit(chunk)  # 线程安全的 Signal 传回 UI
        self.finished.emit()
```

> **⚠️ M12 工程注意（额外工作量）：AI Streaming 需全新实现**
>
> 当前 `src/ai/` 下的所有 AI 客户端（`deepseek_client.py`、`openai_client.py`）**均为同步实现，不支持流式输出**。
> `ai_service.stream_chat()` 在代码库中 **不存在**，是 M12 必须新增的完整实现。
>
> **需要在 M12 中新增以下内容**：
>
> ```python
> # src/gui/services/ai_chat_service.py（全新文件）
> import httpx
>
> async def stream_chat(messages: list, api_key: str, model: str = "deepseek-chat") -> AsyncGenerator[str, None]:
>     """通过 httpx.AsyncClient 调用 DeepSeek API 流式接口"""
>     async with httpx.AsyncClient() as client:
>         async with client.stream("POST", "https://api.deepseek.com/chat/completions", ...) as resp:
>             async for line in resp.aiter_lines():
>                 # 解析 SSE 格式，yield token
>                 ...
> ```
>
> **要点**：
> - 使用 `httpx.AsyncClient` 的 SSE（Server-Sent Events）流式接口
> - 现有 `src/ai/deepseek_client.py` 使用的是 `httpx.Client`（同步），不能直接复用
> - 这是 M12 相比文档估算的 **额外 2-3 天工作量**（新增约 100-150 行）

**交付物**:

- [ ] `src/gui/views/chat_view.py` - AI 聊天界面
  - [ ] 聊天消息列表（QListView + 自定义 Delegate）
  - [ ] 用户输入框（QTextEdit + 发送按钮）
  - [ ] 流式输出显示（逐字显示 AI 回复）
  - [ ] 消息气泡样式（用户/AI 区分）
  - [ ] 会话管理（新建/切换/删除会话）
- [ ] `src/gui/viewmodels/chat_viewmodel.py` - 聊天 ViewModel
  - [ ] 消息发送与接收信号
  - [ ] 流式输出状态管理
- [ ] `src/gui/services/ai_chat_service.py` - AI 对话服务
  - [ ] BaseChatProvider 抽象接口（stream_chat / get_models）
  - [ ] DeepSeekProvider 具体实现（httpx.AsyncClient + SSE）
  - [ ] 读取 Config 中的 deepseek_api_key / deepseek_base_url
  - [ ] QThread + asyncio 隔离集成（见上方说明）
  - [ ] 对话预设模板加载
- [ ] `src/gui/services/knowledge_context.py` - 知识上下文管理
  - [ ] 自动检索相关知识作为对话背景
  - [ ] 上下文窗口管理（token 预算控制）
- [ ] `src/ai/chat_presets.py` - 对话预设模板（见设计决策 2）
- [ ] 数据库迁移: `scripts/migrations/003_add_chat_sessions.sql`
  - [ ] `chat_sessions` 表（见设计决策 1）
  - [ ] 相关索引
- [ ] `tests/unit/test_ai_chat_service.py` - 对话服务测试
- [ ] `tests/unit/test_chat_sessions.py` - 对话记录存储测试
- [ ] 更新 README.md、CHANGELOG.md

**验收检查点**:
1. 输入问题 → 流式显示 AI 回复（逐字输出，不卡顿）
2. AI 回复自动引用知识库中的相关条目
3. 对话记录正确存储到 SQLite 并可恢复
4. 新建/切换/删除会话正常工作
5. 网络异常时优雅降级（显示错误提示，不崩溃）
6. 所有测试通过

---

## 🏗️ Milestone 13: GUI 打包与集成验证 (v0.8.1)

**目标**: 完成 GUI 应用的打包分发、E2E 测试和用户文档

**前置**: M12 完成

**交付物**:

- [ ] 打包配置
  - [ ] PyInstaller spec 文件（或 Nuitka 配置）
  - [ ] 打包脚本（`scripts/build-gui.ps1`）
  - [ ] 打包产物验证（启动测试、资源完整性）
- [ ] E2E 测试
  - [ ] `tests/e2e/test_gui_e2e.py` - GUI 端到端测试（pytest-qt）
  - [ ] 覆盖：启动 → 浏览 → 搜索 → 归档 → 聊天 完整流程
- [ ] 用户文档
  - [ ] 安装指南（含打包产物使用说明）
  - [ ] GUI 使用手册（截图 + 操作说明）
  - [ ] 更新 README.md、CHANGELOG.md、使用手册

**打包命令**:
```bash
# 使用 PyInstaller 打包
pyinstaller --onedir --windowed src/gui/app.py --name "PKV"

# 或使用 Nuitka（更好性能）
nuitka --standalone --enable-plugin=pyside6 src/gui/app.py
```

**验收检查点**:
1. 打包产物可在干净环境中启动运行
2. E2E 测试覆盖核心用户流程
3. 用户文档清晰完整，新用户可照做使用
4. 打包体积合理（Windows 目标 < 400MB，含 QWebEngineView；若改用 `QTextEdit` 渲染 Markdown 可降至 < 200MB）
   - 若体积超标，优先考虑：① 使用 `QTextEdit.setMarkdown()` 替代 QWebEngineView；② `.spec` 排除未使用的 Qt 模块

---

## 🧩 关键设计决策

### 决策 1: 人机对话记录 — SQLite JSON 存储

**决策**: 采用 SQLite `chat_sessions` 表 + JSON `messages` 列。

```sql
-- 增量迁移: 003_add_chat_sessions.sql
CREATE TABLE IF NOT EXISTS chat_sessions (
    session_id TEXT PRIMARY KEY,              -- UUID
    title TEXT,                               -- 会话标题（AI 自动生成或用户命名）
    session_type TEXT NOT NULL DEFAULT 'default',  -- preset_id，如 "default"/"reading" 等
    context_entry_id TEXT,                    -- 关联的知识条目（可选，TEXT 与 knowledge_id 一致）
    messages TEXT NOT NULL,                   -- JSON 格式的对话记录
    message_count INTEGER DEFAULT 0,
    model_used TEXT,                          -- 使用的 AI 模型
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (context_entry_id)
        REFERENCES knowledge_items(knowledge_id) ON DELETE SET NULL
);
```

**messages JSON 格式**（与 DeepSeek/OpenAI API 对齐）:
```json
[
  {"role": "system",    "content": "...", "timestamp": "..."},
  {"role": "user",      "content": "...", "timestamp": "..."},
  {"role": "assistant", "content": "...", "timestamp": "...", "context_refs": ["KID-001"]}
]
```

**理由**:
- SQLite `json_extract()` 原生支持查询 JSON 列
- 与 AI API 的 message 数组格式零转换成本
- 对话记录是软件交互数据，不需要 Markdown "数据主权"模式

### 决策 2: AI 对话初始提示词 — 通用预设 + 场景可升级架构

**核心原则**:
- **现在简单** — M12 初期只做一个通用预设 `"default"`，不过度设计
- **架构可升级** — 数据结构预留多场景字段，后续加场景无需改 Schema
- **用户可自定义** — 设置界面支持修改预设文本

```python
# src/ai/chat_presets.py
from dataclasses import dataclass

@dataclass
class ChatPreset:
    preset_id: str
    name: str
    description: str
    system_prompt_template: str  # 支持 {entry_count}、{top_tags} 占位符
    temperature: float
    is_default: bool = False

    def render(self, context: dict) -> str:
        return self.system_prompt_template.format_map(context)


# M12 初期：只实现一个通用预设
CHAT_PRESETS: dict[str, ChatPreset] = {
    "default": ChatPreset(
        preset_id="default",
        name="通用助手",
        description="适用于任意话题，自动引用知识库相关内容",
        system_prompt_template=(
            "你是用户的个人知识助手，可以访问用户的个人知识库。\n"
            "知识库当前有 {entry_count} 条知识条目，热门标签：{top_tags}。\n\n"
            "请根据用户的问题，合理地引用知识库中的相关内容进行回答。"
            "如果知识库中没有相关内容，请如实告知并给出你的判断。"
        ),
        temperature=0.5,
        is_default=True,
    ),
    # 后续按需追加（无需改 Schema）：
    # "reading": 阅读理解场景（关联某条目，深度分析）
    # "qa":      精准问答场景（temperature 更低，更严格引用）
}

def get_preset(preset_id: str = "default") -> ChatPreset:
    return CHAT_PRESETS.get(preset_id, CHAT_PRESETS["default"])
```

**升级路径**:

| 阶段 | 操作 | 成本 |
|------|------|------|
| M12 | 只实现 `default` 预设，测试通用场景 | ✅ 零设计负债 |
| 未来 M | 在 `CHAT_PRESETS` 中追加新场景 | 约 10 行 Python，无 DB 变更 |
| 用户自定义 | 设置界面允许修改 `system_prompt_template` | 存 `user_presets` 表即可 |

---

## 🧪 测试要求

| 测试类型 | 最低覆盖率 | 工具 |
|---------|-----------|------|
| 单元测试 | 85% | pytest + mock |
| 集成测试 | 关键路径 100% | pytest |
| GUI 测试 | 核心流程 | pytest-qt |

```python
# 使用 pytest-qt 测试 GUI
def test_search_view(qtbot):
    view = SearchView()
    qtbot.addWidget(view)
    qtbot.keyClicks(view.search_input, "分布式系统")
    qtbot.mouseClick(view.search_button, Qt.LeftButton)
    assert view.result_table.rowCount() > 0
```

---

## 🛡️ 维护方案

- 日志：统一写入 `pkv-gui.log`，通过 `sys.excepthook` 捕获未处理异常。
- 配置持久化：使用 `QSettings`，包含 `settings_version` 用于兼容升级。
- 性能指标：冷启动 < 3s，搜索 < 500ms，UI 帧率 > 30fps，内存 < 300MB。
- 崩溃恢复：异常后下次启动提示恢复，尽量保留用户状态与会话。

---

## 🔌 扩展预留

- AI Provider 接口：`BaseChatProvider` 抽象基类，统一 `stream_chat` / `get_models`。
- 视图注册：使用 `dict` 映射视图标识 → View 类，便于后续扩展。
- GUI 状态持久化：使用 `QSettings` 保存窗口位置、大小与上次视图。
- 已知简化：`chat_sessions` 使用 JSON 存储、快捷键硬编码、暂不提供 i18n。

---

## 📦 交付清单汇总

### v0.8.0-alpha 交付 (M10)
- [ ] GUI 基础框架（主窗口 + 明暗主题）
- [ ] 知识库浏览界面（标签树 + 列表 + 预览）
- [ ] 搜索界面（关键词搜索 + 结果展示）

### v0.8.0-beta 交付 (M11)
- [ ] 归档界面（URL + 文本归档 + 进度显示）
- [ ] 设置界面（API Key、主题、检索策略）
- [ ] 统计面板

### v0.8.0 交付 (M12)
- [ ] AI 聊天界面（流式输出 + 消息气泡 + 会话管理）
- [ ] AI 对话服务（DeepSeek API 直调 + 知识上下文注入）
- [ ] 对话记录存储（chat_sessions 迁移）
- [ ] 对话预设模板（`default` 通用预设）
- [ ] 更新 README.md、CHANGELOG.md

### v0.8.1 交付 (M13)
- [ ] 打包脚本（PyInstaller / Nuitka）
- [ ] E2E 测试套件（pytest-qt）
- [ ] 用户安装指南 + GUI 使用手册

---

**文档版本**: v1.0
**创建日期**: 2026-02-18
**对应里程碑**: M10 (v0.8.0-alpha) + M11 (v0.8.0-beta) + M12 (v0.8.0) + M13 (v0.8.1)
