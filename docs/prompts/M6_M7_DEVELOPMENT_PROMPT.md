# M6+M7 开发任务 Prompt

> **目标**: 完成 CLI 入口、命令行界面和完整文档
>
> **前置条件**: M1-M5 整理复盘已完成
> **版本**: 1.0
> **创建日期**: 2026-02-15
> **适用对象**: Claude Code/CodeX 等 AI 开发工具

---

## 🎯 开发目标

基于 M1-M5 的完整功能模块，开发用户友好的 CLI 工具，并提供完善的使用文档。

**核心交付物**:
1. ✅ CLI 入口和命令定义（M6）
2. ✅ 终端 UI 和交互体验（M6）
3. ✅ 使用手册和维护文档（M7）
4. ✅ 版本管理和变更日志（M7）

---

## 📚 前置知识

### 必读文档（按顺序）

**M5 完成文档**（必须先读）:
1. `docs/M5_COMPLETION_SUMMARY.md` - M5 完整总结（93% 测试覆盖率，6 项技术决策）
2. `docs/M5_REAL_ENV_TEST_REPORT.md` - 真实环境测试报告（发现并修复 2 个关键 Bug）
3. `docs/M5_WORKFLOW_ENGINE_DESIGN.md` - M5 设计文档
4. `docs/M5_TEST_SUMMARY.md` - 测试总结

**复盘文档**（必须先读）:
1. `docs/refactor/M1-M5整理复盘总结.md` - 整体总结
2. `docs/refactor/WorkflowEngine接口规范.md` - 工作流引擎 API
3. `docs/refactor/归档流程数据流图.md` - 端到端数据流
4. `docs/refactor/配置文件规范.md` - 配置管理
5. `docs/refactor/Bug修复记录.md` - 已知问题和修复记录

**设计文档**:
1. `docs/personal-knowledge-vault-prd.md` - 核心需求（重点阅读 CLI 部分）
2. `docs/架构设计.md` - 工作流驱动架构

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

### 6.7 与工作流引擎集成（重要！）

**说明**: M6 CLI 的核心功能依赖 M5 WorkflowEngine，必须正确集成

#### 6.7.1 archive 命令集成

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

#### 6.7.2 search 命令集成

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

#### 6.7.3 进度显示集成

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

#### 6.7.4 错误处理和友好提示

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

#### 6.7.5 idea Sharpen 交互集成

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

#### 6.7.6 配置访问

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

#### 6.7.7 关键依赖关系

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

### 6.8 测试要求

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

## [1.0.0] - 2026-02-15

### Added
- CLI 入口和命令定义
- 归档、搜索、查看、配置等核心命令
- Rich 终端 UI
- 工作流引擎
- 混合检索引擎（BM25 + Vector）

### Fixed
- 配置字段名统一（targets）
- 引擎传参错误修复

### Changed
- 数据库 Schema 优化
```

---

## 📦 最终交付清单

### M6 交付物

**代码**:
- [x] `src/main.py` - CLI 入口
- [x] `src/cli/commands.py` - 命令定义
- [x] `src/cli/ui.py` - 终端 UI
- [x] `src/cli/formatters.py` - 输出格式化

**测试**:
- [x] `tests/unit/test_cli_*.py` - 单元测试
- [x] `tests/integration/test_cli_e2e.py` - 集成测试

**文档**:
- [x] CLI 命令参考（README 或单独文档）

---

### M7 交付物

**文档**:
- [x] `docs/使用手册.md` - 用户文档
- [x] `docs/维护指南.md` - 维护文档
- [x] `docs/API文档.md` - API 文档
- [x] `CHANGELOG.md` - 版本历史

**辅助文件**:
- [x] `.env.example` - 环境变量模板
- [x] `scripts/init_db.py` - 数据库初始化脚本
- [x] `scripts/backup.sh` - 备份脚本

---

## 🎯 开始开发

### Step 1: 阅读复盘文档

```bash
# 必读文档
1. docs/refactor/M1-M5整理复盘总结.md
2. docs/refactor/Workflow接口规范.md
3. docs/refactor/归档流程数据流图.md
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

## 🚀 Let's Go!

**现在开始**: 阅读复盘文档，然后从 `src/main.py` 开始实现 CLI 入口！
