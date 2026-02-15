# M6+M7 开发任务 Prompt

> **目标**: 完成 CLI 入口、命令行界面和完整文档
>
> **前置条件**: ✅ M1-M5 已完成 | ✅ M5.1 Bug 修复已完成
> **版本**: 2.0
> **创建日期**: 2026-02-15
> **最后更新**: 2026-02-16
> **适用对象**: Claude Code/CodeX 等 AI 开发工具

---

## 📊 当前项目状态

### ✅ 已完成里程碑

- **M1**: 基础设施层（存储、配置）
- **M2**: AI 服务层（DeepSeek、OpenAI Embedding）
- **M3**: 内容处理器（微信、知乎、通用网页、聊天）
- **M3.5**: AI 聊天处理器与文本 Fallback
- **M4**: 检索引擎（BM25、向量、混合检索）
- **M5**: 工作流引擎（编排、步骤、上下文）
- **M5.1**: Bug 修复（3 个关键问题已修复）✨ **NEW**

### 🎯 当前任务

- **M6**: CLI 入口与交互界面（即将开始）
- **M7**: 文档完善与交付

### ✨ M5.1 重要更新

**修复时间**: 2026-02-16
**测试结果**: 122/122 单元测试通过 (100%)

**已修复问题**:
1. ✅ **source_type CHECK 约束不完整** (高优先级)
   - 添加 `ai_chat`, `text`, `test` 枚举值
   - 修复 AIChatProcessor 和 TextFallbackProcessor 保存失败

2. ✅ **长文档向量化策略不一致** (中优先级)
   - 统一为分块取平均策略
   - 保留更多文档信息

3. ✅ **DeepSeek JSON 解析脆弱** (中优先级)
   - 改进 Prompt 明确要求纯 JSON
   - 实现三层降级解析策略

**详细报告**: [`docs/milestones/M5_1_BUGFIX_COMPLETE.md`](../milestones/M5_1_BUGFIX_COMPLETE.md)

---

## 🎯 开发目标

基于 M1-M5.1 的**稳定功能模块**，开发用户友好的 CLI 工具，并提供完善的使用文档。

**核心交付物**:
1. ⏳ CLI 入口和命令定义（M6）
2. ⏳ 终端 UI 和交互体验（M6）
3. ⏳ 使用手册和维护文档（M7）
4. ⏳ 版本管理和变更日志（M7）

---

## 📚 前置知识

### 必读文档（按优先级顺序）

#### 🔥 第一优先级：M5.1 Bug 修复（必读）

**为什么必读**：M5.1 修复了 3 个影响 CLI 集成的关键问题，避免开发时踩坑

1. **[`docs/milestones/M5_1_BUGFIX_COMPLETE.md`](../milestones/M5_1_BUGFIX_COMPLETE.md)** ✨ **NEW**
   - M5.1 完成报告
   - 3 个 Bug 详细修复方案
   - 测试结果 (122/122 通过)

2. **[`docs/refactor/Bug修复记录.md`](../refactor/Bug修复记录.md)**
   - 已修复问题清单（5 个已修复，3 个待修复）
   - 技术债务优先级

#### 📖 第二优先级：M1-M5 复盘文档（必读）

**为什么必读**：提供系统化的架构理解和接口规范

1. **[`docs/refactor/M1-M5整理复盘总结.md`](../refactor/M1-M5整理复盘总结.md)**
   - 整体架构总结
   - 主要问题清单（M5.1 已全部解决近期修复任务）

2. **[`docs/refactor/WorkflowEngine接口规范.md`](../refactor/WorkflowEngine接口规范.md)**
   - 工作流引擎 API
   - **CLI 集成关键**: `WorkflowEngine.execute()` 接口

3. **[`docs/refactor/归档流程数据流图.md`](../refactor/归档流程数据流图.md)**
   - 端到端数据流
   - **CLI 集成关键**: State 传递约定

4. **[`docs/refactor/Storage接口规范.md`](../refactor/Storage接口规范.md)**
   - 三层存储架构
   - **CLI 集成关键**: `SQLiteStore.query_*()` 方法

5. **[`docs/refactor/Retrieval检索引擎规范.md`](../refactor/Retrieval检索引擎规范.md)**
   - 检索策略设计
   - **CLI 集成关键**: `QueryRouter.search()` 接口

#### 📋 第三优先级：设计文档（参考）

1. **[`docs/core/personal-knowledge-vault-prd.md`](../core/personal-knowledge-vault-prd.md)**
   - 核心需求（重点阅读 CLI 部分）

2. **[`docs/core/架构设计.md`](../core/架构设计.md)**
   - 工作流驱动架构

3. **[`CLAUDE.md`](../../CLAUDE.md)**
   - 项目索引和模块导航

---

## 🏗️ Milestone 6: CLI 入口和命令行界面

**时间估计**: 2-3 天
**测试覆盖率要求**: ≥ 80%

### 6.1 技术选型

**CLI 框架**: Click
- 简单易用，文档完善
- 支持子命令、参数验证、帮助信息
- 与 Python 生态系统兼容好

**终端 UI**: Rich
- 丰富的终端渲染能力
- 进度条、表格、语法高亮
- 与 idea Sharpen 交互已验证（M5）

**配置管理**:
- 继续使用 `src/utils/config.py`
- 环境变量优先级：CLI 参数 > 环境变量 > 配置文件

---

### 6.2 文件结构

```
src/
├── main.py                 # CLI 入口（新建）
└── cli/
    ├── __init__.py        # CLI 模块初始化（新建）
    ├── commands.py        # 命令定义（新建）
    ├── ui.py              # 终端 UI 组件（新建）
    └── formatters.py      # 输出格式化（新建）
```

---

### 6.3 核心命令定义

#### 命令 1: `pkv archive`

**作用**: 归档网页、聊天记录、新闻

**使用方式**:
```bash
# 归档网页
pkv archive <url>

# 归档聊天记录文件
pkv archive --type chat <file_path>

# 跳过 idea Sharpen
pkv archive <url> --skip-sharpen

# 指定标签
pkv archive <url> --tags "AI,技术,教程"
```

**参数**:
- `url_or_path` (位置参数): URL 或文件路径
- `--type`: 内容类型（auto, webpage, chat, news）
- `--skip-sharpen`: 跳过 idea Sharpen 交互
- `--tags`: 手动指定标签（逗号分隔）
- `--quiet`: 静默模式（最小化输出）

**实现要点**:
```python
@click.command()
@click.argument('url_or_path')
@click.option('--type', type=click.Choice(['auto', 'webpage', 'chat', 'news']), default='auto')
@click.option('--skip-sharpen', is_flag=True, help='跳过 idea Sharpen 交互')
@click.option('--tags', help='手动指定标签（逗号分隔）')
@click.option('--quiet', is_flag=True, help='静默模式')
def archive(url_or_path, type, skip_sharpen, tags, quiet):
    """归档内容到知识库"""

    # 1. 确定输入类型（URL vs 文件）
    # 2. 调用 WorkflowEngine.execute_async("archive-url", {...})
    # 3. 显示进度（Rich.Progress）
    # 4. 处理 idea Sharpen 交互
    # 5. 显示结果摘要
```

**输出示例**:
```
📥 正在归档: https://example.com/article

[████████████████████████████████] 100% 完成

✅ 归档成功!

  标题: 深入理解 Python 异步编程
  作者: 技术博主
  标签: Python, 异步编程, 教程
  文件: .data/vault/generic/深入理解Python异步编程.md
  ID: 42
```

---

#### 命令 2: `pkv search`

**作用**: 搜索知识库

**使用方式**:
```bash
# 基本搜索
pkv search "Python 异步"

# 指定策略
pkv search "Python 异步" --strategy bm25
pkv search "深度学习原理" --strategy vector

# 限制结果数量
pkv search "Python" --limit 5

# 输出格式
pkv search "Python" --format json
pkv search "Python" --format markdown
```

**参数**:
- `query` (位置参数): 搜索关键词
- `--strategy`: 检索策略（auto, bm25, vector, hybrid）
- `--limit`: 结果数量（默认 10）
- `--format`: 输出格式（table, json, markdown）

**实现要点**:
```python
@click.command()
@click.argument('query')
@click.option('--strategy', type=click.Choice(['auto', 'bm25', 'vector', 'hybrid']), default='auto')
@click.option('--limit', type=int, default=10, help='结果数量')
@click.option('--format', type=click.Choice(['table', 'json', 'markdown']), default='table')
def search(query, strategy, limit, format):
    """搜索知识库"""

    # 1. 根据 strategy 选择检索器
    # 2. 执行检索
    # 3. 格式化输出（Table/JSON/Markdown）
```

**输出示例**（table 格式）:
```
🔍 搜索: Python 异步

找到 8 条结果 (BM25 策略)

┏━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━┳━━━━━━━━━━━┓
┃ ID ┃ 标题                     ┃ 得分   ┃ 标签      ┃
┡━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━╇━━━━━━━━━━━┩
│ 42 │ 深入理解 Python 异步编程  │ 0.95   │ Python... │
│ 38 │ asyncio 实战指南         │ 0.87   │ Python... │
│ 25 │ 协程和事件循环           │ 0.76   │ 编程...   │
└────┴──────────────────────────┴────────┴───────────┘

提示: 使用 'pkv show <id>' 查看详情
```

---

#### 命令 3: `pkv show`

**作用**: 显示单个条目详情

**使用方式**:
```bash
# 通过 ID 查看
pkv show 42

# 通过 URL 查看
pkv show --url "https://example.com/article"

# 输出原始 Markdown
pkv show 42 --raw
```

**参数**:
- `id_or_url` (位置参数): knowledge_id 或 URL
- `--raw`: 输出原始 Markdown 内容

**输出示例**:
```
📄 知识条目 #42

标题: 深入理解 Python 异步编程
作者: 技术博主
来源: https://example.com/article
时间: 2026-02-01 10:30
标签: Python, 异步编程, 教程

摘要:
  本文深入探讨了 Python 的异步编程机制，包括 asyncio 库的
  使用、协程的概念以及事件循环的工作原理...

文件: .data/vault/generic/深入理解Python异步编程.md
```

---

#### 命令 4: `pkv list`

**作用**: 列出所有条目

**使用方式**:
```bash
# 列出所有条目
pkv list

# 按标签过滤
pkv list --tag Python

# 按时间排序
pkv list --sort time --desc

# 限制数量
pkv list --limit 20
```

**参数**:
- `--tag`: 按标签过滤
- `--sort`: 排序字段（time, title, id）
- `--desc`: 降序排列
- `--limit`: 结果数量

---

#### 命令 5: `pkv config`

**作用**: 配置管理

**使用方式**:
```bash
# 查看配置
pkv config show

# 设置 DeepSeek API Key
pkv config set DEEPSEEK_API_KEY sk-xxxxx

# 查看单个配置
pkv config get ai.deepseek.model
```

**参数**:
- `action` (子命令): show, set, get
- `key`: 配置键名
- `value`: 配置值

---

#### 命令 6: `pkv stats`

**作用**: 显示统计信息

**使用方式**:
```bash
pkv stats
```

**输出示例**:
```
📊 知识库统计

总条目数: 156
  ├─ 网页: 89
  ├─ 聊天记录: 45
  └─ 新闻: 22

存储大小:
  ├─ Markdown: 12.5 MB
  ├─ SQLite: 8.3 MB
  └─ 向量索引: 45.2 MB

标签统计 (Top 10):
  1. Python (42)
  2. AI (38)
  3. 技术 (35)
  ...
```

---

### 6.4 终端 UI 组件 (`cli/ui.py`)

**需要实现的组件**:

#### 进度条
```python
from rich.progress import Progress

def show_progress(description: str, total: int):
    """显示进度条"""
    with Progress() as progress:
        task = progress.add_task(description, total=total)
        # ...
```

#### 表格
```python
from rich.table import Table

def format_search_results(results: List[Dict]) -> Table:
    """格式化搜索结果为表格"""
    table = Table(title="搜索结果")
    table.add_column("ID", style="cyan")
    table.add_column("标题")
    table.add_column("得分", style="green")
    # ...
    return table
```

#### 面板
```python
from rich.panel import Panel

def show_entry_detail(entry: Entry) -> Panel:
    """显示条目详情面板"""
    content = f"""
    [bold]标题[/bold]: {entry.title}
    [bold]作者[/bold]: {entry.author}
    ...
    """
    return Panel(content, title=f"条目 #{entry.id}")
```

#### 确认对话框
```python
from rich.prompt import Confirm

def confirm_action(message: str) -> bool:
    """确认对话框"""
    return Confirm.ask(message)
```

---

### 6.5 输出格式化 (`cli/formatters.py`)

**支持的格式**:

#### JSON 格式
```python
def format_as_json(data: Any) -> str:
    """格式化为 JSON"""
    return json.dumps(data, indent=2, ensure_ascii=False)
```

#### Markdown 格式
```python
def format_as_markdown(entry: Entry) -> str:
    """格式化为 Markdown"""
    return f"""
# {entry.title}

**作者**: {entry.author}
**时间**: {entry.publish_time}
**标签**: {', '.join(entry.keywords)}

## 摘要
{entry.summary_100_words}

---
"""
```

---

### 6.6 错误处理和用户体验

#### 友好的错误消息
```python
try:
    result = workflow_engine.execute("archive-url", {...})
except ProcessorError as e:
    console.print(f"[red]❌ 无法处理该 URL: {e}[/red]")
    console.print("[yellow]💡 提示: 请检查 URL 是否正确或网络连接[/yellow]")
    sys.exit(1)
```

#### 日志级别控制
```bash
# 普通模式：只显示关键信息
pkv archive <url>

# 详细模式：显示详细日志
pkv archive <url> --verbose

# 调试模式：显示所有日志
pkv archive <url> --debug
```

---

### ⚠️ 6.7 M5.1 关键修复后的注意事项 ✨ **NEW**

**说明**: M5.1 修复了 3 个关键问题，CLI 开发时需要注意以下要点

#### 6.7.1 source_type 枚举值已更新

**背景**: M5.1 添加了 `ai_chat`, `text`, `test` 到 CHECK 约束

**CLI 影响**:
```python
# ✅ 现在支持的 source_type（已修复）
VALID_SOURCE_TYPES = [
    'wechat', 'zhihu', 'bilibili', 'webpage',
    'article', 'document', 'generic', 'personal',
    'ai_chat',  # ✨ 新增：AI 对话
    'text',     # ✨ 新增：纯文本 Fallback
    'test'      # ✨ 新增：测试数据
]

# ✅ 正确：使用这些类型不会失败
entry = Entry(title="...", source_type="ai_chat", ...)
sqlite_store.insert_entry(entry, file_path)  # ✅ 成功

# ❌ 错误：使用未定义的类型会失败
entry = Entry(title="...", source_type="unknown", ...)
sqlite_store.insert_entry(entry, file_path)  # ❌ CHECK 约束错误
```

**CLI 使用场景**:
- `pkv archive <file.html>` - 可能识别为 `ai_chat` 类型（ChatGPT/DeepSeek 导出）
- `pkv archive "纯文本内容"` - 可能识别为 `text` 类型（TextFallbackProcessor）

#### 6.7.2 长文档向量化策略已统一

**背景**: M5.1 统一了 `embed_document()` 和 `embed_batch_documents()` 的长文档处理策略

**CLI 影响**:
```python
# ✅ 现在统一为分块取平均策略（已修复）
from src.ai.embedder import Embedder

embedder = Embedder()

# 短文档 (< 8000 字符): 直接向量化
short_text = "..." * 1000  # 1000 字符
vector = embedder.embed_document(short_text)  # 直接 embed

# 长文档 (≥ 8000 字符): 分块后取平均
long_text = "..." * 10000  # 10000 字符
vector = embedder.embed_document(long_text)  # 自动分块取平均

# 批量处理: 同样的策略
texts = [short_text, long_text]
vectors = embedder.embed_batch_documents(texts)  # 统一策略
```

**CLI 使用场景**:
- 归档长文章（如技术博客、论文）时，向量表示更准确
- 搜索长文档时，语义检索结果更一致

#### 6.7.3 DeepSeek JSON 解析更鲁棒

**背景**: M5.1 增强了 DeepSeek 标签提取的 JSON 解析（三层降级）

**CLI 影响**:
```python
# ✅ 现在支持多种 JSON 返回格式（已修复）

# 格式 1: 纯 JSON 数组（最理想）
response = '["Python", "AI", "教程"]'
tags = deepseek_client.extract_tags(content)  # ✅ 策略 1

# 格式 2: 包含说明文字（降级处理）
response = '以下是标签：["Python", "AI", "教程"]'
tags = deepseek_client.extract_tags(content)  # ✅ 策略 2（JSON 数组模式）

# 格式 3: 引号分隔（最后降级）
response = '"Python" "AI" "教程"'
tags = deepseek_client.extract_tags(content)  # ✅ 策略 3（正则提取）
```

**CLI 使用场景**:
- `pkv archive <url>` - AI 标签提取更稳定，减少失败率
- 用户看到的标签更一致，体验更好

#### 6.7.4 数据库重建提醒

**重要**: M5.1 修改了 SQLite CHECK 约束，需要重建测试数据库

```bash
# ✅ 首次开发 CLI 前，确认测试数据库已重建
rm -f .data/db/knowledge_vault.db  # 删除旧数据库
python -m pytest tests/unit/ -v     # 自动重建

# ✅ 如果遇到 CHECK 约束错误
# Error: CHECK constraint failed: source_type IN (...)
# 解决: 删除 .data/db/knowledge_vault.db 并重新运行
```

---

### 6.8 与工作流引擎集成（重要！）

**说明**: M6 CLI 的核心功能依赖 M5 WorkflowEngine，必须正确集成

#### 6.8.1 archive 命令集成

**核心代码模式**:
```python
from src.workflow.engine import WorkflowEngine
from src.utils.config import Config
import asyncio

@click.command()
@click.argument('url_or_path')
@click.option('--skip-sharpen', is_flag=True)
@click.option('--tags', help='手动指定标签（逗号分隔）')
def archive(url_or_path, skip_sharpen, tags):
    """归档内容到知识库"""

    # 1. 初始化引擎
    config = Config()
    engine = WorkflowEngine(config)

    # 2. 构造输入数据
    input_data = {
        "url": url_or_path,
        "source": "cli",
    }

    # 3. 传递 CLI 参数给工作流
    if skip_sharpen:
        input_data["skip_sharpen"] = True  # IdeaSharpenStep 会读取此标志

    if tags:
        input_data["manual_tags"] = tags.split(",")  # 手动标签

    # 4. 执行工作流（同步包装）
    result = asyncio.run(engine.execute_async("archive-url", input_data))

    # 5. 处理结果
    if result.success:
        knowledge_id = result.data.get("knowledge_id")
        file_path = result.data.get("file_path")
        console.print(f"[green]✅ 归档成功! ID: {knowledge_id}[/green]")
        console.print(f"   文件: {file_path}")
    else:
        console.print(f"[red]❌ 归档失败[/red]")
        for error in result.errors:
            console.print(f"   - {error}")
```

**关键集成点**:
1. **skip_sharpen 参数传递**:
   - CLI 通过 `input_data["skip_sharpen"] = True` 传递
   - IdeaSharpenStep 通过 `context.state.get("skip_sharpen", False)` 读取
   - 如果为 True，IdeaSharpenStep 跳过交互

2. **manual_tags 参数传递**:
   - CLI 通过 `input_data["manual_tags"] = [...]` 传递
   - AnalyzeStep 检查此字段，如果存在则跳过 AI 标签提取

3. **进度显示**:
   - 工作流引擎已有日志记录（`context.logs`）
   - CLI 可以订阅日志或使用 Rich.Progress 包装

---

#### 6.8.2 search 命令集成

**核心代码模式**:
```python
from src.retrieval.hybrid_retriever import HybridRetriever
from src.retrieval.query_router import QueryRouter
from src.storage.sqlite_store import SQLiteStore
from src.storage.vector_store import VectorStore

@click.command()
@click.argument('query')
@click.option('--strategy', default='auto')
@click.option('--limit', type=int, default=10)
def search(query, strategy, limit):
    """搜索知识库"""

    config = Config()

    # 1. 初始化检索器
    if strategy == 'auto':
        router = QueryRouter(config)
        retriever = router.get_retriever(query)
    elif strategy == 'bm25':
        retriever = BM25Retriever(config)
    elif strategy == 'vector':
        retriever = VectorRetriever(config)
    elif strategy == 'hybrid':
        retriever = HybridRetriever(config)

    # 2. 执行搜索
    results = retriever.search(query, top_k=limit)

    # 3. 格式化输出
    table = format_search_results(results)
    console.print(table)
```

**关键问题**:
- ❓ 如果没有工作流配置，search 命令也能工作吗？**答案**：能，search 命令直接使用 Retrieval 模块
- ❓ HybridRetriever 需要 VectorStore 和 SQLiteStore 初始化吗？**答案**：是的，需要传递配置

---

#### 6.8.3 进度显示集成

**方案 1: 轮询日志**（推荐）

```python
from rich.progress import Progress

with Progress() as progress:
    task = progress.add_task("[cyan]归档中...", total=100)

    # 启动异步任务
    async_task = asyncio.create_task(engine.execute_async("archive-url", input_data))

    # 轮询进度（简化版）
    while not async_task.done():
        # 可以根据已完成的步骤更新进度
        await asyncio.sleep(0.5)
        progress.update(task, advance=10)

    result = await async_task
```

**方案 2: 订阅 WorkflowContext 日志**

```python
# 修改 WorkflowEngine 支持日志回调（可选）
def log_callback(message: str):
    console.print(f"[dim]{message}[/dim]")

result = await engine.execute_async("archive-url", input_data, log_callback=log_callback)
```

**方案 3: Rich.Live 实时更新**

```python
from rich.live import Live
from rich.panel import Panel

with Live(Panel("初始化..."), refresh_per_second=4) as live:
    # 执行工作流
    result = await engine.execute_async("archive-url", input_data)

    # 根据 result.logs 更新显示
    for log in result.logs:
        live.update(Panel(log))
```

**浮浮酱的建议**: 先使用简单的进度条（方案 1），后期可以优化为实时日志订阅

---

#### 6.8.4 错误处理和友好提示

**工作流错误处理**:

```python
try:
    result = asyncio.run(engine.execute_async("archive-url", input_data))

    if not result.success:
        console.print("[red]❌ 归档失败[/red]")

        # 显示具体错误
        for error in result.errors:
            console.print(f"   - {error}")

        # 根据错误类型提供建议
        if "ProcessorError" in str(result.errors):
            console.print("[yellow]💡 提示: 请检查 URL 是否正确或网络连接[/yellow]")
        elif "OpenAI API" in str(result.errors):
            console.print("[yellow]💡 提示: 请检查 OPENAI_API_KEY 配置[/yellow]")

        sys.exit(1)

except Exception as e:
    console.print(f"[red]❌ 未知错误: {e}[/red]")
    console.print("[yellow]💡 提示: 使用 --debug 查看详细日志[/yellow]")
    sys.exit(1)
```

**网络超时处理**:

```python
import asyncio

try:
    result = await asyncio.wait_for(
        engine.execute_async("archive-url", input_data),
        timeout=300  # 5 分钟超时
    )
except asyncio.TimeoutError:
    console.print("[red]❌ 归档超时（超过 5 分钟）[/red]")
    console.print("[yellow]💡 提示: 可能是网络问题或内容过长[/yellow]")
    sys.exit(1)
```

---

#### 6.8.5 idea Sharpen 交互集成

**关键问题**: 如何在 CLI 中显示 IdeaSharpenStep 的交互？

**答案**: IdeaSharpenStep 已经使用 Rich.Prompt，CLI 无需额外处理！

**工作流程**:
1. CLI 调用 `engine.execute_async("archive-url", input_data)`
2. 工作流执行到 IdeaSharpenStep
3. IdeaSharpenStep 检查触发条件（内容长度 > 3000）
4. 如果触发，IdeaSharpenStep 使用 `rich.prompt.Prompt.ask()` 显示问题
5. 用户在终端输入答案
6. IdeaSharpenStep 继续执行
7. 工作流完成，返回 result

**CLI 需要做的**:
- 确保 `--skip-sharpen` 参数正确传递到 `input_data`
- 在静默模式（`--quiet`）下设置 `skip_sharpen=True`

**示例代码**:
```python
@click.option('--quiet', is_flag=True)
def archive(url_or_path, quiet, ...):
    input_data = {"url": url_or_path}

    if quiet:
        input_data["skip_sharpen"] = True  # 静默模式跳过交互

    result = asyncio.run(engine.execute_async("archive-url", input_data))
```

---

#### 6.8.6 配置访问

**CLI 和 WorkflowEngine 共享配置**:

```python
# CLI 初始化
from src.utils.config import Config

config = Config()  # 自动加载 config/config.yaml 和 .env

# 传递给引擎
engine = WorkflowEngine(config)

# CLI 也可以直接访问配置
vault_dir = config.vault_dir
db_path = config.db_path
```

**环境变量优先级**（已在 Config 类中实现）:
1. 环境变量（`.env` 文件）
2. 配置文件（`config/config.yaml`）
3. 硬编码默认值

---

#### 6.8.7 关键依赖关系

**CLI 依赖清单**:

```python
# 核心依赖
from src.workflow.engine import WorkflowEngine       # 工作流编排
from src.workflow.models import WorkflowResult       # 结果类型

from src.retrieval.query_router import QueryRouter   # 搜索路由
from src.retrieval.hybrid_retriever import HybridRetriever  # 混合检索

from src.storage.sqlite_store import SQLiteStore     # 查询数据库
from src.storage.markdown_store import MarkdownStore # 读取 Markdown

from src.utils.config import Config                  # 配置管理

# Rich UI 组件（已安装）
from rich.console import Console
from rich.progress import Progress
from rich.table import Table
from rich.panel import Panel
from rich.prompt import Prompt, Confirm
```

**导入检查**:
- 所有模块都已在 M1-M5 实现
- Rich 库已在 M5 中使用（idea Sharpen）
- Click 库需要新增到 `requirements.txt`

---

### 6.9 测试要求

#### 单元测试
- 每个命令必须有单元测试
- Mock WorkflowEngine 和存储层
- 测试参数解析和验证

#### 集成测试
- 端到端命令执行测试
- 使用临时数据库
- 验证输出格式

#### 测试覆盖率
- 目标：≥ 80%
- 关键命令：≥ 90%

**测试文件**:
```
tests/
├── unit/
│   ├── test_cli_commands.py
│   ├── test_cli_ui.py
│   └── test_cli_formatters.py
└── integration/
    └── test_cli_e2e.py
```

**测试数据准备**:
- 复用 `tests/fixtures/test_urls.json` 中的真实 URL
- 微信文章、知乎内容、CSDN 博客样例
- Fixture 数据文件：
  - `tests/fixtures/chat_sample.json` - 聊天记录样本
  - `tests/fixtures/wechat_sample.html` - 微信文章 HTML
- Mock 数据：
  - Mock WorkflowEngine 响应（WorkflowResult 对象）
  - Mock 存储层查询结果（SQLite 查询）

**测试 URL 示例**（从 test_urls.json）:
```json
{
  "wechat": "https://mp.weixin.qq.com/s/ZET927baoFCj3In_11fKeA",
  "zhihu": "https://zhuanlan.zhihu.com/p/123456789",
  "csdn": "https://blog.csdn.net/example/article/details/123456"
}
```

---

## 🏗️ Milestone 7: 文档和部署

**时间估计**: 1-2 天

### 7.1 使用手册 (`docs/使用手册.md`)

**章节结构**:

#### 1. 快速开始
```markdown
## 快速开始

### 安装

\`\`\`bash
# 克隆仓库
git clone https://github.com/your-repo/personal-knowledge-vault.git
cd personal-knowledge-vault

# 安装依赖
pip install -r requirements.txt

# 配置 API Keys
cp .env.example .env
nano .env  # 编辑 DEEPSEEK_API_KEY 和 OPENAI_API_KEY
\`\`\`

### 第一次归档

\`\`\`bash
# 归档一篇技术文章
pkv archive "https://example.com/python-tutorial"
\`\`\`

### 第一次搜索

\`\`\`bash
# 搜索 Python 相关内容
pkv search "Python 教程"
\`\`\`
```

#### 2. 核心功能详解
- 归档网页内容
- 归档聊天记录
- 搜索和检索
- idea Sharpen 使用

#### 3. 配置和定制
- 环境变量配置
- 工作流配置
- 自定义词典

#### 4. 故障排查
- 常见错误
- 日志查看
- Bug 报告

---

### 7.2 维护指南 (`docs/维护指南.md`)

**章节结构**:

#### 1. 系统架构
- 模块依赖关系图
- 数据流图
- 关键设计决策

#### 2. 日常维护
- 数据库维护（VACUUM, REINDEX）
- 日志管理
- 备份策略

#### 3. 性能监控
- 关键指标（KPI）
- 性能分析工具
- 优化建议

#### 4. 扩展开发
- 添加新的 Processor
- 添加新的工作流
- 数据库 Schema 迁移

---

### 7.3 API 文档 (`docs/API文档.md`)

**更新内容**:
- WorkflowEngine API
- CLI 命令参考
- Python API 使用示例

---

### 7.4 版本管理

#### CHANGELOG.md
```markdown
# Changelog

## [Unreleased] - M6+M7

### Added
- CLI 入口和命令定义
- 归档、搜索、查看、配置等核心命令
- Rich 终端 UI

---

## [0.5.1] - 2026-02-16 (M5.1 Bug Fix)

### Fixed
- **source_type CHECK 约束不完整** (#3)
  - 添加 `ai_chat`, `text`, `test` 枚举值到 SQLite CHECK 约束
  - 修复 AIChatProcessor 和 TextFallbackProcessor 保存失败问题
  - 位置: `src/storage/sqlite_store.py:91`

- **长文档向量化策略不一致** (#4)
  - 统一 `embed_document()` 和 `embed_batch_documents()` 为分块取平均策略
  - 改进长文档（≥8000 字符）的向量表示质量
  - 位置: `src/ai/embedder.py:153-195`

- **DeepSeek JSON 解析脆弱** (#5)
  - 改进 Prompt 明确要求纯 JSON 输出
  - 实现三层降级解析策略（纯 JSON → JSON 数组模式 → 正则提取）
  - 提高标签提取成功率
  - 位置: `src/ai/deepseek_client.py:267-320`

### Changed
- 更新测试用例匹配新的 `embed_batch_documents()` 行为
- 完善错误处理和日志记录

### Tests
- 单元测试: 122/122 通过 (100%)
- 集成测试: 网络环境问题（非代码 Bug）

---

## [0.5.0] - 2026-02-15 (M5)

### Added
- 工作流引擎（WorkflowEngine）
- 混合检索引擎（BM25 + Vector + RRF）
- idea Sharpen 交互步骤

### Fixed
- 配置字段名统一（`targets` 字段）
- 引擎传参错误修复（提取 `config` 字段）

### Changed
- 数据库 Schema 优化
```

---

## 📦 最终交付清单

### ✅ M5.1 交付物（已完成）✨ **NEW**

**Bug 修复**:
- [x] source_type CHECK 约束修复 (`src/storage/sqlite_store.py`)
- [x] 长文档向量化策略统一 (`src/ai/embedder.py`)
- [x] DeepSeek JSON 解析增强 (`src/ai/deepseek_client.py`)

**测试**:
- [x] 单元测试全部通过 (122/122)
- [x] 测试用例更新 (`tests/unit/test_ai_embedder.py`)

**文档**:
- [x] M5.1 完成报告 (`docs/milestones/M5_1_BUGFIX_COMPLETE.md`)
- [x] Bug 修复记录更新 (`docs/refactor/Bug修复记录.md`)

---

### M6 交付物

**代码**:
- [ ] `src/main.py` - CLI 入口
- [ ] `src/cli/commands.py` - 命令定义
- [ ] `src/cli/ui.py` - 终端 UI
- [ ] `src/cli/formatters.py` - 输出格式化

**测试**:
- [ ] `tests/unit/test_cli_*.py` - 单元测试
- [ ] `tests/integration/test_cli_e2e.py` - 集成测试

**文档**:
- [ ] CLI 命令参考（README 或单独文档）

---

### M7 交付物

**文档**:
- [ ] `docs/使用手册.md` - 用户文档
- [ ] `docs/维护指南.md` - 维护文档
- [ ] `docs/API文档.md` - API 文档
- [ ] `CHANGELOG.md` - 版本历史

**辅助文件**:
- [ ] `.env.example` - 环境变量模板
- [ ] `scripts/init_db.py` - 数据库初始化脚本
- [ ] `scripts/backup.sh` - 备份脚本

---

## 🎯 开始开发

### Step 1: 阅读复盘文档

```bash
# 🔥 第一优先级：M5.1 Bug 修复（必读）
1. docs/milestones/M5_1_BUGFIX_COMPLETE.md  # ✨ NEW
2. docs/refactor/Bug修复记录.md

# 📖 第二优先级：M1-M5 复盘（必读）
3. docs/refactor/M1-M5整理复盘总结.md
4. docs/refactor/WorkflowEngine接口规范.md
5. docs/refactor/归档流程数据流图.md
```

### Step 2: 创建 CLI 基础结构

```bash
# 创建文件
touch src/main.py
mkdir -p src/cli
touch src/cli/__init__.py
touch src/cli/commands.py
touch src/cli/ui.py
touch src/cli/formatters.py
```

### Step 3: 实现核心命令

优先级顺序：
1. `pkv archive` - 最核心功能
2. `pkv search` - 最常用功能
3. `pkv show` - 查看详情
4. `pkv list`, `pkv config`, `pkv stats` - 辅助功能

### Step 4: 编写测试

每实现一个命令，立即编写测试

### Step 5: 编写文档

最后完成使用手册和维护指南

---

## ✅ 验收标准

### M6 验收

- [ ] 所有核心命令实现并测试通过
- [ ] CLI 测试覆盖率 ≥ 80%
- [ ] 端到端测试通过
- [ ] 用户体验友好（错误提示、进度显示）

### M7 验收

- [ ] 使用手册完整且易懂
- [ ] 维护指南详细且可执行
- [ ] 新用户能在 5 分钟内完成第一次归档
- [ ] 所有文档已更新

---

**开发负责人**: AI Agent (Claude Code/CodeX)
**开发周期**: M6 (2-3天) + M7 (1-2天)
**质量要求**: 测试覆盖率 ≥ 80%，文档完整性 100%

---

## 📝 Prompt 版本历史

### v2.0 (2026-02-16) ✨ **CURRENT**

**更新内容**:
- 添加 M5.1 完成状态和重要更新说明
- 新增"M5.1 关键修复后的注意事项"章节（6.7）
- 重组"必读文档"为三级优先级结构
- 更新 CHANGELOG 示例，包含 M5.1 修复记录
- 更新最终交付清单，标注 M5.1 完成状态
- 修正 Section 编号（6.7.2-6.7.7 → 6.8.2-6.8.7）

**关键新增**:
- source_type 枚举值更新说明（`ai_chat`, `text`, `test`）
- 长文档向量化策略统一说明
- DeepSeek JSON 解析增强说明
- 数据库重建提醒

---

### v1.0 (2026-02-15)

**初始版本**:
- M6 CLI 入口和命令行界面开发指引
- M7 文档完善和交付要求
- 基于 M1-M5 复盘文档的架构设计
- 6 个核心 CLI 命令定义
- 与工作流引擎集成方案
- 测试和文档要求

---

## 🚀 Let's Go!

**现在开始**: 阅读复盘文档，然后从 `src/main.py` 开始实现 CLI 入口！
