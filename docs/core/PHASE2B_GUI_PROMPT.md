# Personal Knowledge Vault - Phase 2B 开发 Prompt

> GUI 桌面应用开发执行指令（M10 ~ M13）
>
> **版本**: 1.5
> **创建日期**: 2026-02-18
> **最后更新**: 2026-02-24 (v1.5: 标记 M13 被跳过，直接推进 M14 审核系统)
> **适用对象**: Claude Code、CodeX 等 AI 开发工具
> **前置条件**: Phase 1 (v0.6.1) 已全部完成；**Phase 2A (v0.7.0) 已完成**；**M10 (v0.8.0-alpha) 已完成**；**M11 (v0.8.0-beta) 已完成**
> **总览文档**: [PHASE2_DEV_PROMPT.md](./PHASE2_DEV_PROMPT.md)
>
> **⚠️ 进度状态（2026-02-23）**:
> - M10+M11 已完成（v0.8.0-beta，69 GUI + 11 删除 + 15 知乎测试全通过）
> - M12~M13 待开始（v0.8.0 → v0.8.1，不受后续 M14 影响）
> - **M14 审核系统作为独立后续里程碑开发**，详见 [M14 PRD](../../docs/review-system-prd.md)

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

> **M10 Markdown 渲染实际选择**：采用 `QTextEdit.setMarkdown()`，打包体积约 150MB（无需 QWebEngineView 的约 250MB 额外开销）。若后续需要代码高亮可在 M11+ 升级为 `QWebEngineView`。

---

## ⚠️ 上游接口对齐注意事项（实现前必读）

> 以下注意事项综合了 Phase2A 实际实现中发现的所有接口细节，**比原始规范文档更准确**，请以此为准。

### 1. SQLiteStore 查询 API（已全部实现，可直接调用）

`SQLiteStore` 所有查询方法均已在 Phase2A (M8) 中实现，GUI 可直接使用：

```python
from src.storage.sqlite_store import SQLiteStore
from src.utils.config import get_config

store = SQLiteStore(get_config().db_path)

# 分页列表（sort_by 白名单：archived_at/title/knowledge_id/word_count/source_type）
entries = store.list_entries(limit=20, offset=0, sort_by="archived_at",
                              sort_order="desc", source_type=None, tag=None)
# 计数
total = store.count_entries(source_type=None, tag=None)

# 标签排行
tags = store.get_all_tags_with_count(limit=20)
# 返回: [{"name": "AI", "count": 5}, ...]

# 综合统计（直接调用，供 stats_view 使用）
stats = store.get_statistics()
# 返回: {"total_entries": N, "by_source_type": [(type, count)...], "top_tags": [...]}

# 按 URL 查询（避免重复归档时有用）
entry = store.query_by_url(source_url)  # 返回 dict 或 None

# 按 ID 查询
entry = store.query_by_id(knowledge_id_int)  # 返回 dict 或 None
```

**⚠️ 关键细节**：
- `sort_by` 参数有白名单校验，传入非法字段会抛 `ValueError`，GUI 需处理
- `list_entries` 返回的每个条目 dict 包含：`knowledge_id`(int)、`title`、`summary_one_sentence`、`tags`(逗号字符串)、`keywords`(逗号字符串)、`source_type`、`source_url`、`word_count`、`archived_at`、`file_path`

### 2. SearchResult 字段（只有 5 个，无 .abstract/.tags/.source_type）

```python
# SearchResult 是 frozen dataclass
result.knowledge_id  # int
result.title         # str
result.score         # float [0.0, 1.0]
result.highlight     # str — 摘要/snippet（用作 abstract）
result.metadata      # dict — 含 source_type, tags(逗号字符串), archived_at, file_path 等
```

GUI 从 SearchResult 取标签：`tags_list = result.metadata.get("tags", "").split(",")`

### 3. tags/keywords 在数据库中是逗号分隔字符串

`entry["tags"]` → `"AI,知识管理"` 而非列表。Phase2A 实现了 `parse_tags_string()` 可复用：

```python
from src.mcp.utils import parse_tags_string
tags_list = parse_tags_string(entry.get("tags", ""))
```

或 GUI 自行实现：`[t.strip() for t in tag_str.split(",") if t.strip()]`

### 4. MarkdownStore.load() 返回 Entry 对象，接收 Path

```python
from src.storage.markdown_store import MarkdownStore
from pathlib import Path

md_store = MarkdownStore(get_config().vault_dir)
entry = md_store.load(Path(file_path_str))  # 必须是 Path 对象
content = entry.content if entry else ""    # 返回 Optional[Entry]
```

### 5. Config 属性速查（已验证可用）

```python
from src.utils.config import get_config
config = get_config()

config.db_path           # Path — SQLite 数据库路径（受 DB_PATH 环境变量覆盖）
config.vault_dir         # Path — Markdown 文件存储目录
config.vector_index_dir  # Path — hnswlib 向量索引目录
config.log_dir           # Path — 日志目录
config.log_level         # str — 日志级别（优先 LOG_LEVEL 环境变量）

config.deepseek_api_key   # Optional[str] — 从 DEEPSEEK_API_KEY 环境变量读取
config.deepseek_base_url  # str — 默认 "https://api.deepseek.com/v1"
config.openai_api_key     # Optional[str] — 从 OPENAI_API_KEY 环境变量读取
config.openai_base_url    # str — 默认 "https://api.openai.com/v1"

# 通用 get 方法（支持点分隔路径）
config.get("ai.openai.embedding_dim", 1536)  # 读取 config.yaml 中的嵌套字段
```

**⚠️ GUI 设置界面注意**：API Key 存储在 `.env` 文件 / 系统环境变量中，`config` 对象通过 `get_env()` 读取。
设置界面修改 API Key 时，需要写入 `.env` 文件并调用 `os.environ` 更新当前进程，或提示重启。

### 6. WorkflowEngine 接口（确认无进度回调）

```python
from src.workflow.engine import WorkflowEngine
from src.workflow.models import WorkflowResult  # dataclass: success, data, errors, logs

engine = WorkflowEngine()  # 无参构造，内部自动 get_config()

# 原生 async，在 QThread 中用 asyncio.run() 调用
result: WorkflowResult = await engine.execute_async(
    "archive-url",         # 工作流名称
    {"url": "https://..."}  # 输入数据
)

# result.success  — bool
# result.data     — dict，含 knowledge_id, title, file_path, tags, summary_one_sentence 等
# result.errors   — List[str]
# result.logs     — List[str]
```

**工作流名称**：`"archive-url"` (URL 归档)、`"archive-text"` (文本归档，M9 新增)、`"search"` (搜索)

**M11 进度显示方案**：由于 `execute_async()` 无进度回调，推荐使用"脉冲动画"（`QProgressBar` indeterminate 模式）。
若后续需要精确进度，可在 `execute_async()` 签名中添加 `on_progress: Callable[[int, str], None] | None = None` 回调。

### 7. QueryRouter 接口（GUI 搜索直接调用）

```python
from src.retrieval.query_router import QueryRouter
from src.ai.openai_client import OpenAIClient

config = get_config()
router = QueryRouter(
    db_path=config.db_path,
    vector_index_dir=config.vector_index_dir,
    embedder=OpenAIClient(config),
)

results = router.search(query, limit=10)  # 返回 List[SearchResult]
```

**⚠️ 单例复用**：`QueryRouter` 内含 hnswlib 索引（加载耗时 1-3s），GUI 必须将其作为单例管理（同 MCP 服务的做法）。建议在 ViewModel 层初始化一次后复用。

### 8. DeepSeek 客户端现状（同步实现，M12 需新增异步）

- **现有**：`src/ai/deepseek_client.py` 使用 `httpx.Client`（同步），提供 `generate_summary()` 和 `extract_tags()`
- **不存在**：流式输出 / `stream_chat()` 方法
- **M12 需新建**：`src/gui/services/ai_chat_service.py`，使用 `httpx.AsyncClient` 实现 SSE 流式接口

```python
# M12 新建文件，DeepSeek 流式接口参考实现
# 注意：openai SDK 的 stream=True 也可用（DeepSeek 兼容 OpenAI API 格式）
import httpx

async def stream_chat(messages: list, api_key: str,
                      base_url: str = "https://api.deepseek.com/v1",
                      model: str = "deepseek-chat"):
    async with httpx.AsyncClient() as client:
        async with client.stream("POST", f"{base_url}/chat/completions",
                                 headers={"Authorization": f"Bearer {api_key}"},
                                 json={"model": model, "messages": messages,
                                       "stream": True}) as resp:
            async for line in resp.aiter_lines():
                if line.startswith("data: ") and line != "data: [DONE]":
                    import json
                    chunk = json.loads(line[6:])
                    token = chunk["choices"][0]["delta"].get("content", "")
                    if token:
                        yield token
```

### 9. VectorStore 新增 API（Phase2A 新增，M10 可用于"相关条目"功能）

```python
from src.storage.vector_store import VectorStore

vector_store = VectorStore(
    index_dir=config.vector_index_dir,
    dim=config.get("ai.openai.embedding_dim", 1536),
)

# 取回某条目的 doc 向量（Phase2A M9 新增）
vec = vector_store.get_doc_vector(knowledge_id_int)  # Optional[np.ndarray]

# 相似文档搜索
results = vector_store.search_doc(vec, k=5)  # List[(knowledge_id, distance)]
# distance 是 cosine distance，score = 1 - distance

# 分块向量搜索（细粒度）
results = vector_store.search_chunk(vec, k=10)  # List[(knowledge_id, chunk_index, distance)]
```

---

## 🏗️ Milestone 10: GUI 基础框架 + 知识浏览 (v0.8.0-alpha)

**目标**: 搭建 PySide6 GUI 框架，实现知识库浏览和搜索两大核心只读界面

**前置**: M8 完成（共享 Service Layer，但 GUI 不依赖 MCP）

**交付物**:

- [x] `src/gui/__init__.py` - GUI 模块初始化
- [x] `src/gui/app.py` - QApplication 主入口（事件循环、异常处理）
- [x] `src/gui/main_window.py` - 主窗口
  - [x] QMainWindow 框架（菜单栏 + 工具栏 + 状态栏）
  - [x] 侧边导航栏（视图切换）
  - [x] 全局快捷键注册
- [x] `src/gui/styles/` - QSS 样式表
  - [x] `light.qss` - 明亮主题
  - [x] `dark.qss` - 暗色主题
- [x] `src/gui/assets/` - 图标和资源文件
- [x] `src/gui/views/browser_view.py` - 知识库浏览界面
  - [x] 左侧：标签树 / 来源分类（QTreeView）
  - [x] 中间：条目列表（QTableView + 自定义 Model）
  - [x] 右侧：Markdown 预览（QTextEdit.setMarkdown()）
- [x] `src/gui/views/search_view.py` - 搜索界面
  - [x] 搜索框 + 策略选择（BM25/向量/混合/自动）
  - [x] 结果列表 + 高亮匹配
  - [x] 快捷键支持（Ctrl+K 全局搜索）
- [x] `src/gui/models/` - Qt MVC 数据模型
  - [x] `entry_model.py` - 知识条目数据模型
  - [x] `tag_model.py` - 标签数据模型
- [x] `tests/unit/test_gui_models.py` - 数据模型单元测试
- [x] `tests/unit/test_gui_main_window.py` - 主窗口 pytest-qt 测试（23 用例）
- [x] `tests/unit/test_gui_browser_view.py` - 浏览界面 pytest-qt 测试（27 用例）
- [x] `tests/unit/test_gui_search_view.py` - 搜索界面 pytest-qt 测试（21 用例）

**额外交付（文档未预期）**:
- [x] `src/gui/stores.py` - GUI 层存储单例管理（延迟初始化，参考 MCP 单例模式）
- [x] `src/gui/preview_loader.py` - Markdown 预览加载器（浏览/搜索共享）
- [x] `src/gui/models/search_result_model.py` - 搜索结果专用 Model

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

- [x] `src/gui/views/archive_view.py` - 归档界面（343 行）
  - [x] URL 归档表单（输入框 + 归档按钮）
  - [x] 文本归档编辑器（多行文本框 + 可选标题）
  - [x] 进度显示（QProgressBar 脉冲动画 + 状态文字）
  - [x] 结果预览和确认（归档完成后显示 ID/标题/路径）
- [x] `src/gui/viewmodels/archive_viewmodel.py` - 归档 ViewModel（301 行）
  - [x] 异步工作流调用（QThread + asyncio.run，不阻塞 UI 线程）
  - [x] 进度信号发射（方案 B 脉冲动画）
- [x] `src/gui/views/stats_view.py` - 统计面板（260 行）
  - [x] 知识库概况（条目总数、标签分布、来源分布）
  - [x] 纯 Qt 绘制条形图（QLabel + QProgressBar 模拟，不引入 matplotlib）
- [x] `src/gui/views/settings_view.py` - 设置界面（412 行）
  - [x] API Key 配置（DeepSeek、OpenAI，密码遮罩 + 明文切换）
  - [x] Embedding 模型/维度配置
  - [x] 数据目录显示（只读展示 DB/Vault/Vector 路径）
  - [x] 保存到 .env + 更新 os.environ
- [x] `src/gui/viewmodels/settings_viewmodel.py` - 设置 ViewModel（198 行）
- [x] `tests/unit/test_gui_archive.py` - 归档流程测试（20 用例）
- [x] `tests/unit/test_gui_settings.py` - 设置功能测试（15 用例）
- [x] `tests/unit/test_gui_stats.py` - 统计面板测试（9 用例）

**验收检查点**:
1. ✅ 输入 URL → 点击归档 → 进度条显示 → 完成后可在浏览界面查看
2. ✅ 输入文本 → 归档为知识条目 → 搜索可命中
3. ✅ 统计面板正确显示数据概况
4. ✅ 设置修改后立即生效（写入 .env + 更新 os.environ）
5. ✅ 归档过程 UI 不冻结（QThread 异步执行）

**额外交付（M11 计划外加固）**:

> 以下功能不在原始 M11 计划中，是在实际开发和使用过程中发现并解决的关键问题。
> 详见 [M11 完成报告](../milestones/M11_COMPLETION_REPORT.md)。

- [x] **知乎登录墙检测与 Cookie 注入** — `src/processors/zhihu_processor.py` (+95 行)
  - 登录墙关键词检测 + Cookie 自动注入重试 + 用户引导配置
  - `src/utils/config.py` 新增 `zhihu_cookie` 属性
  - 13 个新测试用例 + `tests/fixtures/zhihu_login_wall.html`
- [x] **Embedding 可配置化** — 模型名称/维度从环境变量读取
  - `src/utils/config.py` 新增 `openai_embedding_model`、`embedding_dim`
  - `src/ai/openai_client.py` 动态读取模型名
- [x] **VectorStore 维度不匹配自动重建** — `src/storage/vector_store.py` (+82 行)
  - 加载索引时检测维度，不匹配时自动删除旧索引并重建
- [x] **知识条目三层删除功能**
  - `src/storage/sqlite_store.py`: `delete_entry()` + `_decrement_tag_counts()`
  - `src/storage/vector_store.py`: `delete_vectors_for_entry()` (hnswlib mark_deleted)
  - `src/gui/views/browser_view.py`: 右键菜单 + 确认对话框 + 三层删除
  - `src/gui/stores.py`: `get_vector_store()` 延迟单例
  - 11 个存储层删除测试 + 3 个 GUI 删除测试
- [x] **BrowserView 归档后自动刷新** — 导航切换时调用 `refresh()`
- [x] **openai + httpx 版本兼容修复** — `requirements.txt` 固定版本约束

**M11 实现经验**:
- 归档进度采用方案 B（脉冲动画），不改 WorkflowEngine，成本最低
- 设置界面不包含"检索策略"和"主题切换"（这两项在主窗口菜单栏已有），聚焦 API Key 和路径
- 三层删除的标签计数递减必须在 `DELETE FROM knowledge_items` **之前**执行（因 CASCADE 会先删除关联行）
- hnswlib `mark_deleted()` 对个人知识库规模的空间开销可忽略

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

> ⚠️ **决策更新（2026-02-24）**：
>
> **M13 已被跳过，直接推进 M14 审核系统**
>
> **跳过原因**：
> 1. **核心工作流缺陷** — 现有系统缺少用户对 AI 生成内容的审核阶段，这是**所有工作流的必经步骤**（web 归档 → 审核 → 入库；AI 对话 → 审核 → 入库）。M13 的打包在这个阶段进行没有意义，因为用户无法完整验收。
> 2. **商业价值优先** — 审核系统 + 多端客户端（OpenClaw、手机应用）的架构设计优先级更高，打包只是工程细节，可以后续补充。
> 3. **架构逻辑** — M13 的打包脚本依赖最终的工作流完整性验证，不如先完成 M14 再打包分发。
>
> **后续计划**：
> - M14 完成后，将打包集成到 M14 的交付物中（或独立为 M15 轻量级打包里程碑）
> - 多端客户端（OpenClaw、移动应用）可基于 M14 审核系统的 Service Layer 独立开发

**前置**: M12 完成

**交付物**（已废弃，仅供参考）:

- [ ] 打包配置
  - [ ] PyInstaller spec 文件（或 Nuitka 配置）
  - [ ] 打包脚本（`scripts/build-gui.ps1`）
  - [ ] 打包产物验证（启动测试、资源完整性）
- [ ] E2E 测试
  - [ ] `tests/e2e/test_gui_e2e.py` - GUI 端到端测试（pytest-qt）
  - [ ] 覆盖：启动 → 浏览 → 搜索 → 归档 → 删除 → 聊天 完整流程
  - [ ] 注意：M11 新增的删除功能需纳入 E2E 覆盖
- [ ] 用户文档
  - [ ] 安装指南（含打包产物使用说明）
  - [ ] GUI 使用手册（截图 + 操作说明）
  - [ ] 配置指南（知乎 Cookie 配置、Embedding 模型/维度配置——M11 新增）
  - [ ] 更新 README.md、CHANGELOG.md、使用手册

**打包命令**（已废弃）:
```bash
# 使用 PyInstaller 打包
pyinstaller --onedir --windowed src/gui/app.py --name "PKV"

# 或使用 Nuitka（更好性能）
nuitka --standalone --enable-plugin=pyside6 src/gui/app.py
```

**验收检查点**（已废弃）:
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
# ⚠️ M10 实践经验：
# 1. 使用 yield 而非 return，确保 mock 上下文在整个测试期间保持活跃
# 2. 中文输入使用 setText() 而非 keyClicks()（offscreen 平台会崩溃）
# 3. hasFocus() 在 offscreen 平台不可靠，需放宽断言
@pytest.fixture
def search_view(qtbot, mock_retriever):
    with patch("src.gui.stores.get_bm25_retriever", return_value=mock_retriever):
        from src.gui.views.search_view import SearchView
        view = SearchView()
        qtbot.addWidget(view)
        yield view  # yield 保持 mock 上下文

def test_search_view(search_view, qtbot):
    search_view.search_input.setText("分布式系统")  # setText 代替 keyClicks
    search_view.do_search()
    assert search_view._result_model.rowCount() > 0
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

### v0.8.0-alpha 交付 (M10) ✅ 已完成
- [x] GUI 基础框架（主窗口 + 明暗主题）
- [x] 知识库浏览界面（标签树 + 列表 + 预览）
- [x] 搜索界面（关键词搜索 + 结果展示）
- [x] pytest-qt 测试覆盖（130 测试全通过）

### v0.8.0-beta 交付 (M11) ✅ 已完成
- [x] 归档界面（URL + 文本归档 + QThread 异步 + 脉冲进度）
- [x] 设置界面（API Key 配置 + Embedding 配置 + .env 持久化）
- [x] 统计面板（条目/来源/标签概况，纯 Qt 绘制）
- [x] **额外**: 知乎登录墙检测 + Cookie 注入
- [x] **额外**: Embedding 可配置化 + 维度不匹配自动重建
- [x] **额外**: 知识条目三层删除（SQLite + Markdown + Vector）
- [x] **额外**: BrowserView 归档后自动刷新

### v0.8.0 交付 (M12) ✅ 已完成
- [x] AI 聊天界面（流式输出 + 消息气泡 + 会话管理）
- [x] AI 对话服务（DeepSeek API 直调 + 知识上下文注入）
- [x] 对话记录存储（chat_sessions 迁移）
- [x] 对话预设模板（`default` 通用预设）
- [x] 更新 README.md、CHANGELOG.md
- [x] **额外**: 向量索引优化、知识库引用系统、流式输出优化、Token 预算控制
- [x] **额外**: 三层测试体系（单元 + Layer 2 + Layer 3 E2E）— 218 tests 全通过

### v0.8.1 交付 (M13) ⏭️ **被跳过**

> **理由**：打包与发布是工程细节，应在工作流完整性验证后（M14 完成）再进行。
>
> 预计后续与 M14 或 M15 打包里程碑合并交付。

- ~~打包脚本（PyInstaller / Nuitka）~~
- ~~E2E 测试套件（pytest-qt）~~
- ~~用户安装指南 + GUI 使用手册~~

---

---

## 📋 Phase2A 代码产出速查（GUI 开发参考）

> Phase2A (v0.7.0) 已完成。GUI 开发时可直接复用以下模块，无需重新实现。

### 可直接复用的函数（来自 `src/mcp/utils.py`）

| 函数 | 用途 | GUI 使用场景 |
|------|------|------------|
| `parse_tags_string(tags_str)` | 逗号字符串 → 列表 | 所有显示标签的 View |
| `serialize_entry_summary(entry)` | SQLiteStore dict → 展示用 dict | BrowserView 条目列表 |
| `serialize_search_result(result)` | SearchResult → 展示用 dict | SearchView 结果列表 |
| `clamp_param(value, min, max)` | 参数范围限制 | 分页参数校验 |
| `validate_url_security(url)` | URL 格式 + SSRF 验证 | ArchiveView URL 归档前验证 |
| `validate_text_length(text, max=100000)` | 文本长度验证 | ArchiveView 文本归档前验证 |

```python
# 导入方式（直接复用 MCP 层的工具函数）
from src.mcp.utils import parse_tags_string, serialize_entry_summary, validate_url_security
```

### 单例管理模式参考（来自 `src/mcp/server.py`）

MCP 服务中采用模块级懒初始化单例，GUI 已在 M10 中实现了独立的单例管理模块：

```python
# M10 实际实现：src/gui/stores.py（参考 MCP 的单例模式，为 GUI 独立实现）
from src.gui.stores import get_sqlite_store, get_markdown_store, get_bm25_retriever

store = get_sqlite_store()       # SQLiteStore 单例
md_store = get_markdown_store()  # MarkdownStore 单例
retriever = get_bm25_retriever() # BM25Retriever 单例（不走 QueryRouter，避免 hnswlib 冷启动）
```

> **M10 设计决策**：GUI 搜索使用 `BM25Retriever` 而非 `QueryRouter`，避免触发 hnswlib 向量索引加载（1-3s），确保冷启动时间 < 3s。`QueryRouter` 支持可在 M11 或后续按需引入。

### archive-text 工作流的实际实现模式（M11 ArchiveView 文本归档参考）

Phase2A M9 中 `archive_text` Tool 的实际实现比文档设计更复杂，GUI 归档文本时需参考：

```python
# archive_text 的实际流程（来自 src/mcp/tools.py archive_text 函数）
# 步骤 1：先用 TextFallbackProcessor 解析文本，生成 Entry 对象
from src.processors.text_fallback_processor import TextFallbackProcessor
processor = TextFallbackProcessor()
entry = await processor.process(text)  # 生成带 title/tags/content 的 Entry
if title:
    entry.title = title.strip()

# 步骤 2：将 Entry 注入工作流上下文（archive-text.yaml 只做 ai_analyze + store_entry）
from src.workflow.engine import WorkflowEngine
engine = WorkflowEngine()
result = await engine.execute_async(
    "archive-text",
    {"text": text, "title": entry.title, "entry": entry, "content": entry.content},
)
```

### 可用的工作流配置文件

| 文件 | 工作流名 | 用途 | 步骤 |
|------|---------|------|------|
| `config/workflows/archive-url.yaml` | `"archive-url"` | 归档网页 | fetch → ai_analyze → idea_sharpen → store |
| `config/workflows/archive-text.yaml` | `"archive-text"` | 归档文本（M9 新增）| ai_analyze → store（无 fetch）|
| `config/workflows/search.yaml` | `"search"` | 搜索 | search |

### MCP 三层测试体系（可参考模式编写 GUI 测试）

| 层级 | 文件 | 技术 | GUI 对应 |
|------|------|------|---------|
| Layer 1 单元测试 | `tests/unit/test_mcp_*.py` | pytest + Mock | `tests/unit/test_gui_*.py` |
| Layer 2 进程内集成 | `tests/integration/test_mcp_functional.py` | FastMCP.call_tool() | `tests/integration/test_gui_viewmodels.py` |
| Layer 3 黑盒 | `tests/blackbox/test_mcp_blackbox.py` | stdio 子进程 + JSON-RPC | pytest-qt E2E（M13） |

---

**文档版本**: v1.5
**创建日期**: 2026-02-18
**最后更新**: 2026-02-24 (v1.5: M13 被跳过，直接推进 M14 审核系统)
**对应里程碑**: M10 (v0.8.0-alpha) ✅ + M11 (v0.8.0-beta) ✅ + M12 (v0.8.0) ✅ + M13 (v0.8.1) ⏭️ 被跳过
