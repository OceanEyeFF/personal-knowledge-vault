# CLI 模块

[根目录](../../CLAUDE.md) > [src](../) > **cli**

---

## 模块职责

**命令行交互界面**:提供基于 Click 的 CLI 命令组和 Rich 终端 UI,是用户与系统交互的主入口。

### 核心理念

- **Click 框架**: 使用 Click 构建命令行界面,支持命令组、参数、选项
- **Rich UI**: 集成 Rich 库提供美观的终端界面(进度条、表格、面板)
- **集成工作流**: 命令直接调用 WorkflowEngine 和 QueryRouter
- **灵活输出**: 支持多种格式输出(表格、JSON、Markdown)

---

## 入口与启动

### CLI 入口

**主入口**: `src/main.py`

```python
from src.cli.commands import cli

if __name__ == "__main__":
    cli()
```

**使用方式**:

```bash
# 查看帮助
python -m src.main --help

# 归档内容
python -m src.main archive "https://mp.weixin.qq.com/xxx"

# 搜索知识库
python -m src.main search "AI 工作流"

# 列出条目
python -m src.main list --limit 10

# 显示统计
python -m src.main stats

# 显示条目详情
python -m src.main show 123

# 配置管理
python -m src.main config show
python -m src.main config get storage.vault_dir
python -m src.main config set ai.temperature 0.8
```

### 全局选项

```bash
python -m src.main --verbose <command>   # 详细输出 (INFO)
python -m src.main --debug <command>     # 调试输出 (DEBUG)
python -m src.main --version             # 显示版本
```

---

## 对外接口

### 核心命令 (src/cli/commands.py)

#### 1. archive - 归档内容

```python
@click.command()
@click.argument("url")
@click.option("--tags", help="逗号分隔的标签")
def archive(url: str, tags: Optional[str]):
    """归档网页内容到知识库"""
```

**工作流程**:
1. 调用 `WorkflowEngine.run("archive-url", {"url": url})`
2. 显示归档进度 (Rich Progress)
3. 返回成功/失败状态和详细日志

**输出格式**:
```
✅ 归档成功!
  ID: 20260216-123456
  标题: 示例文章
  文件: .data/vault/wechat/2026/02/20260216-示例文章.md
```

**集成要点**:
- 从 `WorkflowResult.data` 中提取 `knowledge_id`、`file_path`
- 处理 `WorkflowResult.error` 和 `logs`

详见: [docs/API文档.md](../../docs/API文档.md) - WorkflowEngine API

---

#### 2. search - 搜索知识库

```python
@click.command()
@click.argument("query")
@click.option("--limit", default=10, help="返回结果数量")
@click.option("--strategy", type=click.Choice(["auto", "keyword", "semantic", "hybrid"]))
@click.option("--format", type=click.Choice(["table", "json", "markdown"]))
def search(query: str, limit: int, strategy: Optional[str], format: str):
    """在知识库中搜索内容"""
```

**工作流程**:
1. 调用 `QueryRouter.route(query)` 选择检索策略
2. 执行检索: `retriever.search(query, top_k=limit)`
3. 格式化输出 (表格/JSON/Markdown)

**检索策略路由**:
- `auto`: 自动选择 (< 10 tokens → BM25, ≥ 10 → Vector)
- `keyword`: 强制使用 BM25 (精确关键词)
- `semantic`: 强制使用 Vector (语义理解)
- `hybrid`: 混合检索 (RRF k=60)

**输出格式**:

表格模式:
```
┌─────┬──────────────┬──────────┬───────┐
│ ID  │ 标题         │ 来源     │ 相关度 │
├─────┼──────────────┼──────────┼───────┤
│ 123 │ 示例文章     │ wechat   │ 0.95  │
│ 124 │ 另一篇文章   │ zhihu    │ 0.87  │
└─────┴──────────────┴──────────┴───────┘
```

JSON 模式:
```json
{
  "query": "AI 工作流",
  "strategy": "vector",
  "results": [
    {
      "entry_id": "123",
      "title": "示例文章",
      "score": 0.95,
      "snippet": "...摘要..."
    }
  ]
}
```

**关键注意事项**:
- 检索结果使用 `entry_id`,数据库查询使用 `knowledge_id` (命名不一致)
- `HybridRetriever` 初始化需要传递 `config`

详见: [docs/refactor/Retrieval检索引擎规范.md](../../docs/refactor/Retrieval检索引擎规范.md)

---

#### 3. show - 显示条目详情

```python
@click.command()
@click.argument("entry_id", type=int)
@click.option("--format", type=click.Choice(["panel", "json", "markdown"]))
def show(entry_id: int, format: str):
    """显示知识条目的详细信息"""
```

**工作流程**:
1. 从 SQLite 查询条目元数据
2. 读取对应的 Markdown 文件(可选)
3. 格式化输出

**输出格式**:

Panel 模式 (默认):
```
╭─ 示例文章 (ID: 123) ────────────────────╮
│ 来源: wechat                            │
│ 归档时间: 2026-02-16 12:30:00          │
│ 标签: [技术, AI]                       │
│ 关键词: Claude, Code, 工作流           │
│ ─────────────────────────────────────── │
│ 摘要: 这是一篇关于 Claude Code 的文章   │
╰─────────────────────────────────────────╯
```

---

#### 4. list - 列出条目

```python
@click.command(name="list")
@click.option("--limit", default=20, help="显示条目数量")
@click.option("--source", help="按来源筛选 (wechat/zhihu/...)")
@click.option("--tag", help="按标签筛选")
@click.option("--format", type=click.Choice(["table", "json"]))
def list_entries(limit: int, source: Optional[str], tag: Optional[str], format: str):
    """列出知识库中的所有条目"""
```

**工作流程**:
1. 从 SQLite 查询条目列表 (支持过滤)
2. 按归档时间降序排列
3. 格式化输出

**输出格式**:

```
┌─────┬──────────────┬──────────┬────────────────────┐
│ ID  │ 标题         │ 来源     │ 归档时间           │
├─────┼──────────────┼──────────┼────────────────────┤
│ 125 │ 最新文章     │ wechat   │ 2026-02-16 18:00   │
│ 124 │ 另一篇文章   │ zhihu    │ 2026-02-16 12:00   │
│ 123 │ 示例文章     │ wechat   │ 2026-02-15 10:00   │
└─────┴──────────────┴──────────┴────────────────────┘

共 3 条记录 (显示前 20 条)
```

---

#### 5. config - 配置管理

```python
@click.group(name="config")
def config_cmd():
    """管理系统配置"""

@config_cmd.command()
def show():
    """显示当前配置"""

@config_cmd.command()
@click.argument("key")
def get(key: str):
    """获取配置项的值"""

@config_cmd.command()
@click.argument("key")
@click.argument("value")
def set(key: str, value: str):
    """设置配置项的值"""
```

**使用示例**:

```bash
# 显示所有配置
python -m src.main config show

# 获取单个配置项
python -m src.main config get storage.vault_dir

# 设置配置项
python -m src.main config set ai.temperature 0.8
```

**配置键路径**:
- `storage.vault_dir`
- `storage.db_path`
- `ai.llm.model`
- `ai.llm.temperature`
- `ai.embedding.model`
- `ai.embedding.dim`
- `retrieval.bm25_k1`

---

#### 6. stats - 统计信息

```python
@click.command()
def stats():
    """显示知识库统计信息"""
```

**输出格式**:

```
╭─ 知识库统计 ────────────────────────╮
│                                      │
│  总条目数: 125                       │
│  总存储: 45.3 MB                     │
│  数据库: 2.1 MB                      │
│  向量索引: 15.6 MB                   │
│  Markdown 文件: 27.6 MB              │
│                                      │
│  按来源分布:                         │
│    wechat: 65 (52.0%)                │
│    zhihu: 40 (32.0%)                 │
│    text: 15 (12.0%)                  │
│    generic: 5 (4.0%)                 │
│                                      │
╰──────────────────────────────────────╯
```

---

### 终端 UI 组件 (src/cli/ui.py)

```python
class ProgressTracker:
    """进度追踪器 (Rich Progress)"""

    def __init__(self, description: str, total: int):
        """初始化进度条"""

    def update(self, advance: int):
        """更新进度"""

    def finish(self):
        """完成进度"""

class TableFormatter:
    """表格格式化器 (Rich Table)"""

    @staticmethod
    def format_search_results(results: List[Dict]) -> Table:
        """格式化搜索结果为表格"""

    @staticmethod
    def format_entry_list(entries: List[Dict]) -> Table:
        """格式化条目列表为表格"""

class PanelFormatter:
    """面板格式化器 (Rich Panel)"""

    @staticmethod
    def format_entry_detail(entry: Dict) -> Panel:
        """格式化条目详情为面板"""

    @staticmethod
    def format_stats(stats: Dict) -> Panel:
        """格式化统计信息为面板"""

class ConfirmDialog:
    """确认对话框 (Rich Confirm)"""

    @staticmethod
    def confirm(message: str) -> bool:
        """显示确认对话框"""
```

---

### 输出格式化器 (src/cli/formatters.py)

```python
class OutputFormatter:
    """输出格式化器"""

    @staticmethod
    def to_json(data: Any) -> str:
        """格式化为 JSON"""

    @staticmethod
    def to_markdown(data: Any) -> str:
        """格式化为 Markdown"""

    @staticmethod
    def to_table(data: List[Dict], columns: List[str]) -> str:
        """格式化为表格 (ASCII)"""
```

---

## 关键依赖与配置

### 依赖库

- `click>=8.0.0`: 命令行框架
- `rich>=13.0.0`: 终端 UI 库
- `python-dotenv`: 环境变量加载

### 配置项

无特殊配置,使用全局 `config.yaml`。

### CLI 版本

在 `src/main.py` 中定义:

```python
__version__ = "0.6.0"
```

---

## 数据流

### archive 命令数据流

```
用户输入 URL
    ↓
CLI 解析参数
    ↓
WorkflowEngine.run("archive-url", {"url": url})
    ↓
[工作流执行 → FetchStep → AIAnalyzeStep → StoreStep]
    ↓
返回 WorkflowResult
    ↓
CLI 提取 knowledge_id、file_path
    ↓
Rich 格式化输出
    ↓
用户看到成功/失败消息
```

### search 命令数据流

```
用户输入查询
    ↓
CLI 解析参数 (query, limit, strategy)
    ↓
QueryRouter.route(query) 选择策略
    ↓
Retriever.search(query, top_k=limit)
    ↓
返回 List[SearchResult]
    ↓
CLI 格式化为表格/JSON/Markdown
    ↓
用户看到搜索结果
```

---

## 测试与质量

### 单元测试

```bash
# 运行 CLI 单元测试
python -m pytest tests/unit/test_cli_commands.py -v
python -m pytest tests/unit/test_cli_ui.py -v
python -m pytest tests/unit/test_cli_formatters.py -v
```

**测试覆盖**:
- 命令参数解析
- UI 组件渲染
- 输出格式化

### 集成测试

```bash
# 运行 CLI 集成测试
python -m pytest tests/integration/test_cli_e2e.py -v
```

**测试场景**:
- 端到端归档流程
- 端到端搜索流程
- 配置管理

### 黑盒测试

```bash
# 运行 CLI 黑盒测试
python -m pytest tests/blackbox/test_cli_blackbox.py -v
```

**测试方法**:
- 使用 `click.testing.CliRunner` 模拟命令行调用
- 验证输出格式和内容

---

## 常见问题 (FAQ)

### Q1: 如何添加新的 CLI 命令?

1. 在 `src/cli/commands.py` 中定义新命令:

```python
@click.command()
@click.argument("arg")
@click.option("--opt", help="选项说明")
def my_command(arg: str, opt: Optional[str]):
    """命令说明"""
    # 实现逻辑
    ...
```

2. 在 `src/main.py` 中注册命令:

```python
def _register_commands(group: click.Group) -> None:
    group.add_command(my_command)
```

### Q2: 如何自定义输出格式?

使用 `OutputFormatter` 类:

```python
from src.cli.formatters import OutputFormatter

# JSON 格式
json_output = OutputFormatter.to_json(data)

# Markdown 格式
md_output = OutputFormatter.to_markdown(data)
```

### Q3: 如何集成新的检索策略?

在 `search` 命令中添加策略选项:

```python
@click.option("--strategy", type=click.Choice(["auto", "keyword", "semantic", "hybrid", "my-strategy"]))
def search(query: str, strategy: Optional[str]):
    if strategy == "my-strategy":
        retriever = MyCustomRetriever()
        results = retriever.search(query)
```

### Q4: 如何调试 CLI 命令?

```bash
# 使用 --debug 选项
python -m src.main --debug archive "https://example.com"

# 或在代码中设置断点
import pdb; pdb.set_trace()
```

---

## 关键设计

### 1. 命令组织

```
cli (主命令组)
├── archive (归档命令)
├── search (搜索命令)
├── show (显示命令)
├── list (列表命令)
├── config (配置命令组)
│   ├── show
│   ├── get
│   └── set
└── stats (统计命令)
```

### 2. 错误处理

```python
try:
    result = engine.run("archive-url", context)
    if result.success:
        console.print("[green]✅ 归档成功![/green]")
    else:
        console.print(f"[red]❌ 归档失败: {result.error}[/red]")
        for log in result.logs:
            console.print(f"  [dim]{log}[/dim]")
except Exception as e:
    console.print(f"[red]❌ 发生错误: {e}[/red]")
    if debug:
        console.print_exception()
```

### 3. 进度显示

```python
from src.cli.ui import ProgressTracker

progress = ProgressTracker("归档中...", total=5)
progress.update(1)  # 完成 1/5
progress.update(1)  # 完成 2/5
...
progress.finish()
```

---

## 相关文件清单

### 核心文件

| 文件 | 说明 |
|------|------|
| `__init__.py` | CLI 模块入口 |
| `commands.py` | 核心命令实现 (archive/search/...) |
| `ui.py` | 终端 UI 组件 (Progress/Table/Panel) |
| `formatters.py` | 输出格式化器 (JSON/Markdown) |

### 入口文件

| 文件 | 说明 |
|------|------|
| `../main.py` | CLI 主入口 (python -m src.main) |

### 测试文件

| 文件 | 说明 |
|------|------|
| `tests/unit/test_cli_commands.py` | 命令单元测试 (17 个测试) |
| `tests/unit/test_cli_ui.py` | UI 组件测试 |
| `tests/unit/test_cli_formatters.py` | 格式化器测试 |
| `tests/integration/test_cli_e2e.py` | 端到端集成测试 |
| `tests/blackbox/test_cli_blackbox.py` | 黑盒测试 |

### 文档

| 文件 | 说明 |
|------|------|
| [docs/API文档.md](../../docs/API文档.md) | CLI API 参考 |
| [docs/使用手册.md](../../docs/使用手册.md) | CLI 使用指南 (第 2 章) |

---

## 变更记录 (Changelog)

### 2026-02-16 18:51
- 生成 CLI 模块 CLAUDE.md 文档
- 添加导航面包屑
- 补充 6 个核心命令的详细说明
- 添加数据流图和测试覆盖说明

### 2026-02-16 (M6)
- 完成 CLI 模块开发
- 实现 6 个核心命令 (archive/search/show/list/config/stats)
- 集成 Rich 终端 UI
- 完成单元测试和集成测试 (测试覆盖率 ≥ 80%)

---

**模块维护者**: AI Agent
**最后更新**: 2026-02-16 18:51:32

*本文档由 Claude Code 自动生成*
