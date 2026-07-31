# GUI 桌面应用模块

[根目录](../../CLAUDE.md) > [src](..) > **gui**

---

## 模块职责

**PySide6 (Qt6) 桌面图形界面**：提供完整的知识管理与 AI 对话功能，包含知识浏览、全文搜索、URL/文本归档、AI 对话（流式输出 + 知识引用）、统计面板和应用设置。

### 核心理念

- **MVVM 架构**: View (PySide6 Widget) + ViewModel (QObject/QThread) + Model (数据层)
- **异步非阻塞**: qasync 集成 asyncio，@asyncSlot 驱动流式输出
- **存储单例**: 通过 `stores.py` 统一管理存储实例，避免重复初始化
- **主题系统**: 双主题 (light/dark) QSS 样式 + Python 语义颜色字典

---

## 入口与启动

GUI 进程会加载正常本机配置、初始化数据库，并可能连接真实 Provider。下列启动命令仅供用户在本机手动使用；AI/Agent 不执行。自动化验证使用本文测试章节中的 `run-test.ps1` 命令。

```bash
# 用户本机启动方式（Agent 不执行）
python -m src.gui           # 通过 __main__.py
python -m src.gui.app       # 直接运行 app.py
python src/gui/app.py       # 脚本方式
```

**启动流程**:

1. `app.py:main()` -- 创建 QApplication、配置日志
2. `ensure_database_initialized()` -- 检查数据库版本，首次运行自动迁移
3. `MainWindow()` -- 构建主窗口 UI
4. `QEventLoop(app)` -- 使用 qasync 集成 asyncio 事件循环

---

## 架构分层

```
┌────────────────────────────────────────────────────┐
│  Views (PySide6 Widgets)                            │
│  BrowserView / SearchView / ArchiveView / ChatView  │
│  StatsView / SettingsView                           │
├────────────────────────────────────────────────────┤
│  ViewModels (QObject / QThread)                     │
│  ArchiveViewModel / ChatViewModel / SettingsViewModel│
├────────────────────────────────────────────────────┤
│  Models (Qt Models)                                 │
│  EntryTableModel / TagTreeModel                     │
├────────────────────────────────────────────────────┤
│  Stores (单例管理)                                  │
│  stores.py → SQLiteStore / MarkdownStore / etc.     │
├────────────────────────────────────────────────────┤
│  Widgets (可复用组件)                                │
│  AutocompletePopup                                  │
├────────────────────────────────────────────────────┤
│  Styles (主题系统)                                   │
│  dark.qss / light.qss / theme_colors.py             │
└────────────────────────────────────────────────────┘
```

---

## 对外接口

### MainWindow

主窗口控制器，管理导航和视图切换。

```python
class MainWindow(QMainWindow):
    def switch_to_browser(self) -> None:   # Ctrl+B
    def switch_to_search(self) -> None:    # Ctrl+K
    def switch_to_archive(self) -> None:   # Ctrl+N
    def switch_to_chat(self) -> None:      # M12
    def switch_to_stats(self) -> None:
    def switch_to_settings(self) -> None:
    def apply_theme(self, theme: str) -> None:  # "light" / "dark"
```

**导航索引**: 0=浏览, 1=搜索, 2=归档, 3=AI对话, 4=统计, 5=设置

### ChatViewModel (M12)

AI 对话 ViewModel，管理流式输出和会话生命周期。

```python
class ChatViewModel(QObject):
    # Signals
    token_received: Signal(str)              # 流式 token
    token_usage_updated: Signal(int,int,int)  # input, output, total
    stream_finished: Signal()
    error_occurred: Signal(str)
    session_created: Signal(str, str)
    url_archive_started: Signal(str)
    url_archive_completed: Signal(str, dict)
    url_archive_failed: Signal(str, str)
    session_saved_to_kb: Signal(str, int)

    # Methods
    def create_new_session(self, title=None) -> str:
    def load_session(self, session_id) -> bool:
    def list_sessions(self, is_archived=False) -> List[dict]:
    def set_knowledge_context(self, context_text) -> None:
    @asyncSlot()
    async def send_message(self, user_message) -> None:
    @asyncSlot()
    async def archive_url_and_inject(self, url) -> None:
    def save_session_to_knowledge_base(self, session_id) -> bool:
    def stop_stream(self) -> None:
    def delete_session(self, session_id) -> bool:
```

### knowledge_ref (M12)

知识引用工具，提供 @ 语法解析和智能截断。

```python
# Token 估算
def estimate_tokens(text: str) -> int:

# 智能截断（前 3000 tokens + 摘要）
def smart_truncate(content, max_tokens=3000) -> (str, int, bool):

# 构建引用对象
def build_knowledge_reference(entry, content="") -> KnowledgeReference:

# 格式化为 system message
def format_context_message(refs) -> str:

# @ 语法解析
def parse_at_references(text) -> List[AtReference]:
def strip_at_references(text) -> str:

# URL 检测
def detect_urls(text) -> List[str]:
```

---

## 关键依赖与配置

### 依赖库

- **PySide6**: Qt6 GUI 框架
- **qasync**: asyncio + Qt 事件循环集成
- **openai**: DeepSeek API SDK（流式输出）
- **markdown2**: Markdown 渲染
- **pygments**: 代码语法高亮

### AI 对话配置

- **API**: DeepSeek API (base_url: `https://api.deepseek.com/v1`)
- **模型**: `deepseek-chat`
- **最大输出**: 2000 tokens / 次
- **上下文窗口**: 64K tokens
- **Token 统计**: `stream_options={"include_usage": True}`
- **知识引用截断**: 前 3000 tokens

---

## 数据模型

### chat_sessions 表 (M12)

```sql
CREATE TABLE chat_sessions (
    session_id TEXT PRIMARY KEY,       -- UUID
    title TEXT NOT NULL,               -- 会话标题
    created_at TIMESTAMP,              -- 创建时间
    updated_at TIMESTAMP,              -- 更新时间
    messages TEXT NOT NULL,            -- JSON 格式对话历史
    summary TEXT,                      -- AI 生成摘要（可选）
    total_tokens INTEGER DEFAULT 0,    -- 累计 Token
    round_count INTEGER DEFAULT 0,     -- 对话轮数
    is_archived BOOLEAN DEFAULT 0,     -- 归档标志
    knowledge_id INTEGER,              -- 关联知识条目（可选）
);
```

迁移脚本: `scripts/migrations/004_add_chat_sessions.sql` (v1.1.0)

### KnowledgeReference 数据类

```python
@dataclass
class KnowledgeReference:
    knowledge_id: int
    title: str
    source_type: str
    source_url: str
    summary: str
    content_truncated: str   # 智能截断后内容
    token_count: int         # 估算 token 数
    is_truncated: bool       # 是否被截断
```

---

## 测试与质量

### 单元测试

| 文件 | 测试模块 | 说明 |
|------|----------|------|
| `test_gui_models.py` | EntryTableModel, TagTreeModel | Qt Model 测试 |
| `test_gui_search_view.py` | SearchView | 搜索视图测试 |
| `test_gui_archive.py` | ArchiveView + ArchiveViewModel | 归档流程测试 |
| `test_gui_browser_view.py` | BrowserView | 浏览视图测试 |
| `test_gui_main_window.py` | MainWindow | 主窗口测试 |
| `test_gui_settings.py` | SettingsView | 设置视图测试 |
| `test_gui_stats.py` | StatsView | 统计视图测试 |
| `test_chat_viewmodel.py` | ChatViewModel | M12 对话 ViewModel 测试 |
| `test_knowledge_ref.py` | knowledge_ref | M12 知识引用工具测试 |
| `test_autocomplete_popup.py` | AutocompletePopup | M12 自动补全弹窗测试 |

### 运行测试

```powershell
# GUI 单元测试
.\scripts\run-test.ps1 -Direct -DataRoot .data-test\gui-unit -Command @(
  "pytest", "tests/unit", "-k",
  "gui or chat_viewmodel or knowledge_ref or autocomplete_popup", "-v"
)
```

### 手动测试 (M12)

M12 手动脚本不是默认自动化。DeepSeek 流式脚本涉及真实 Provider，必须等待
U1/G8 user-only launcher 与明确授权；QThread/qasync 交互脚本也只由用户按文件
说明手动运行，不得用裸 Python 作为 Agent 流程。

---

## 常见问题 (FAQ)

### Q1: qasync 未安装时怎么办?

`app.py` 会自动回退到标准 Qt 事件循环，但 AI 对话的 `@asyncSlot` 功能将不可用。安装方式: `pip install qasync`

### Q2: 如何添加新视图?

1. 在 `views/` 下创建 `my_view.py`
2. 在 `main_window.py` 中 `_init_ui()` 添加到 `_stacked`
3. 在 `_build_nav_panel()` 添加导航项
4. 更新导航索引常量

### Q3: 主题颜色如何扩展?

在 `styles/theme_colors.py` 的 `THEME_COLORS` 字典中添加新的语义化颜色键，同时更新 `light` 和 `dark` 两套配色。

---

## 相关文件清单

### 核心文件

| 文件 | 说明 |
|------|------|
| `app.py` | 应用入口 + 数据库初始化 |
| `main_window.py` | 主窗口（导航 + 视图切换 + 主题） |
| `stores.py` | 存储单例管理 |
| `__main__.py` | `python -m src.gui` 入口 |

### Views

| 文件 | 说明 |
|------|------|
| `views/browser_view.py` | 三栏浏览器（标签树 + 列表 + 预览） |
| `views/search_view.py` | 全文搜索视图 |
| `views/archive_view.py` | URL/文本归档视图 |
| `views/chat_view.py` | AI 对话视图 (M12) |
| `views/stats_view.py` | 统计面板 |
| `views/settings_view.py` | 应用设置 |

### ViewModels

| 文件 | 说明 |
|------|------|
| `viewmodels/chat_viewmodel.py` | AI 对话 ViewModel (M12) |
| `viewmodels/archive_viewmodel.py` | 归档 ViewModel + ArchiveWorker |
| `viewmodels/settings_viewmodel.py` | 设置 ViewModel |

### Models

| 文件 | 说明 |
|------|------|
| `models/entry_model.py` | EntryTableModel (QAbstractTableModel) |
| `models/tag_model.py` | TagTreeModel (QStandardItemModel) |

### Utils / Widgets / Styles

| 文件 | 说明 |
|------|------|
| `utils/knowledge_ref.py` | 知识引用工具 (M12) |
| `utils/preview_loader.py` | Markdown 预览加载器 |
| `widgets/autocomplete_popup.py` | 自动补全弹窗 (M12) |
| `styles/theme_colors.py` | 语义化颜色定义 (M12) |
| `styles/dark.qss` | 暗色主题 QSS |
| `styles/light.qss` | 明亮主题 QSS |

---

## 变更记录 (Changelog)

### 2026-02-23 10:45 (M12)
- 新增 AI 对话完整实现: ChatView + ChatViewModel + StreamRenderer
- 新增知识引用系统: @知识库/ID 和 @搜索/keyword 语法
- 新增自动补全弹窗: AutocompletePopup (IDE 风格)
- 新增 URL 自动检测归档: 消息中的 URL 自动归档并注入上下文
- 新增对话保存到知识库: 右键菜单导出对话
- 新增 chat_sessions 数据库表 (004 迁移脚本)
- 新增语义化主题颜色: theme_colors.py
- 新增 BrowserView "发送到对话" 功能
- 新增 3 个单元测试文件 + 6 个手动测试脚本

### 2026-02-20 (M10+M11)
- 初始 GUI 框架: MainWindow + 5 个视图
- MVVM 架构: ArchiveViewModel + SettingsViewModel
- 三栏浏览器: TagTreeModel + EntryTableModel
- 全文搜索: BM25Retriever 集成
- 双主题系统: light.qss + dark.qss
- 存储单例管理: stores.py

---

**模块维护者**: AI Agent
**最后更新**: 2026-02-23 10:45:33

*本文档由 Claude Code 自动生成*
