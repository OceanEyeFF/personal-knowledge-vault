# API 文档

> Personal Knowledge Vault - API Reference
> 核心模块接口定义与数据流转协议

**文档版本**: v2.1
**创建日期**: 2026-02-14
**最后更新**: 2026-02-16 17:35
**目标读者**: AI Agent / 开发者

---

## 📋 目录

1. [工作流引擎 API](#工作流引擎-api)
2. [内容处理器 API](#内容处理器-api)
3. [检索引擎 API](#检索引擎-api)
4. [存储层 API](#存储层-api)
5. [数据库迁移 API](#数据库迁移-api) ⭐ **NEW**
6. [CLI 命令参考](#cli-命令参考)
7. [数据结构](#数据结构)
8. [错误处理](#错误处理)

---

## 工作流引擎 API

### WorkflowEngine

**职责**: 编排和执行工作流

#### `execute(workflow_name: str, input_data: dict) -> WorkflowResult`

执行指定的工作流。

**参数**:
- `workflow_name` (str): 工作流名称，对应 `config/workflows/{name}.yaml`
- `input_data` (dict): 输入数据，根据工作流类型不同而变化

**返回**:
- `WorkflowResult`: 工作流执行结果

**示例**:

```python
from src.workflow.engine import WorkflowEngine

engine = WorkflowEngine()

# 归档 URL
result = engine.execute(
    workflow_name="archive-url",
    input_data={"url": "https://example.com/article"}
)

# 检查结果
if result.success:
    print(f"✅ 已归档: {result.data['entry_id']}")
else:
    print(f"❌ 失败: {result.error}")
```

---

#### `register_step(step_type: str, step_class: Type[WorkflowStep])`

注册自定义工作流步骤。

**参数**:
- `step_type` (str): 步骤类型标识（在 YAML 中使用）
- `step_class` (Type[WorkflowStep]): 步骤类（继承自 `WorkflowStep`）

**示例**:

```python
from src.workflow.steps.base import WorkflowStep

class CustomStep(WorkflowStep):
    async def execute(self, context):
        # 自定义逻辑
        ...

engine.register_step("custom_action", CustomStep)
```

---

### WorkflowContext

**职责**: 工作流执行上下文，在步骤间传递数据

#### 属性

- `input_data` (dict): 初始输入数据
- `state` (dict): 步骤间共享状态
- `output` (dict): 最终输出数据

#### `update(key: str, value: Any)`

更新上下文状态。

**示例**:

```python
context.update("title", "我的文章标题")
context.update("tags", ["技术", "编程"])
```

---

## 内容处理器 API

### ContentProcessor (抽象基类)

所有内容处理器必须实现此接口。

#### `can_handle(url_or_text: str) -> bool`

判断是否能处理该内容。

**参数**:
- `url_or_text` (str): URL 或文本内容

**返回**:
- `bool`: 是否能处理

**示例**:

```python
class WechatProcessor(ContentProcessor):
    def can_handle(self, url_or_text: str) -> bool:
        return "mp.weixin.qq.com" in url_or_text
```

---

#### `async process(input: str) -> ProcessedContent`

处理内容并返回结构化数据。

**参数**:
- `input` (str): URL 或原始文本

**返回**:
- `ProcessedContent`: 处理后的内容（见[数据结构](#processedcontent)）

**示例**:

```python
async def process(self, url: str) -> ProcessedContent:
    html = await self.fetch(url)
    soup = BeautifulSoup(html, 'lxml')

    return ProcessedContent(
        title=soup.find('h1').text,
        content=self._extract_markdown(soup),
        metadata={
            "source": url,
            "author": soup.find('meta', {'name': 'author'})['content'],
            "publish_date": self._parse_date(soup)
        }
    )
```

---

### 辅助函数

#### `get_processor(url_or_text: str) -> ContentProcessor`

自动选择合适的处理器。

**参数**:
- `url_or_text` (str): URL 或文本

**返回**:
- `ContentProcessor`: 匹配的处理器实例

**抛出**:
- `ValueError`: 没有合适的处理器

**示例**:

```python
from src.processors import get_processor

processor = get_processor("https://zhuanlan.zhihu.com/p/123456")
content = await processor.process(url)
```

---

## 检索引擎 API

### HybridSearch

**职责**: 混合检索（BM25 + 向量）

#### `search(query: str, top_k: int = 10, strategy: str = "auto") -> List[SearchResult]`

执行混合检索。

**参数**:
- `query` (str): 用户查询
- `top_k` (int): 返回结果数量，默认 10
- `strategy` (str): 检索策略，可选 `"auto"` / `"bm25"` / `"vector"` / `"hybrid"`

**返回**:
- `List[SearchResult]`: 搜索结果列表（按相关性排序）

**示例**:

```python
from src.retrieval.hybrid_search import HybridSearch

searcher = HybridSearch()

results = searcher.search(
    query="分布式系统的权衡",
    top_k=5,
    strategy="auto"  # 自动选择最优策略
)

for result in results:
    print(f"{result.title} (score: {result.score:.2f})")
    print(f"  {result.snippet}")
```

---

### BM25Search

**职责**: 关键词全文检索

#### `search(query: str, top_k: int = 10) -> List[SearchResult]`

BM25 关键词检索。

**参数/返回**: 同 `HybridSearch.search`

**实现细节**:

```python
# 伪代码
class BM25Search:
    def search(self, query: str, top_k: int = 10):
        # 1. 使用 jieba 分词
        tokens = jieba.lcut(query)

        # 2. 查询 SQLite FTS5 虚拟表
        results = self.db.execute("""
            SELECT entry_id, bm25(entries_fts) as score
            FROM entries_fts
            WHERE entries_fts MATCH ?
            ORDER BY score
            LIMIT ?
        """, (" ".join(tokens), top_k))

        return [self._build_result(row) for row in results]
```

---

### VectorSearch

**职责**: 向量语义检索

#### `search(query: str, top_k: int = 10) -> List[SearchResult]`

向量语义检索。

**参数/返回**: 同 `HybridSearch.search`

**实现细节**:

```python
# 伪代码
class VectorSearch:
    def search(self, query: str, top_k: int = 10):
        # 1. 查询向量化
        query_vector = self.embedder.embed(query)

        # 2. HNSW 检索
        labels, distances = self.index.knn_query(
            query_vector,
            k=top_k
        )

        # 3. 转换为 SearchResult
        return [self._build_result(label, dist)
                for label, dist in zip(labels[0], distances[0])]
```

---

## 存储层 API

### MarkdownStore

**职责**: Markdown 文件存储（真理之源）

#### `save(entry: Entry) -> Path`

保存条目为 Markdown 文件。

**参数**:
- `entry` (Entry): 条目对象（见[数据结构](#entry)）

**返回**:
- `Path`: 保存的文件路径

**示例**:

```python
from src.storage.markdown_store import MarkdownStore

store = MarkdownStore(vault_dir=".data/vault")

entry = Entry(
    title="分布式系统设计",
    content="# 核心概念\n\n...",
    tags=["技术", "分布式"],
    metadata={"source": "https://example.com"}
)

file_path = store.save(entry)
print(f"✅ 已保存: {file_path}")
```

---

#### `load(file_path: Path) -> Entry`

从 Markdown 文件加载条目。

**参数**:
- `file_path` (Path): Markdown 文件路径

**返回**:
- `Entry`: 条目对象

**示例**:

```python
entry = store.load(Path(".data/vault/2026/02/20260214-distributed-systems.md"))
print(entry.title)  # 分布式系统设计
```

---

### SQLiteStore

**职责**: SQLite 元数据索引

#### `index_entry(entry: Entry) -> str`

将条目索引到 SQLite。

**参数**:
- `entry` (Entry): 条目对象

**返回**:
- `str`: entry_id

**示例**:

```python
from src.storage.sqlite_store import SQLiteStore

db = SQLiteStore(db_path=".data/db/knowledge_vault.db")

entry_id = db.index_entry(entry)
print(f"✅ 已索引: {entry_id}")
```

---

#### `query_by_tag(tag: str) -> List[str]`

按标签查询 entry_id 列表。

**参数**:
- `tag` (str): 标签名

**返回**:
- `List[str]`: entry_id 列表

---

### VectorStore

**职责**: hnswlib 向量索引

#### `add(entry_id: str, embedding: np.ndarray)`

添加向量到索引。

**参数**:
- `entry_id` (str): 条目 ID
- `embedding` (np.ndarray): 向量（shape: `(dim,)`，由当前模型实际维度决定）

**示例**:

```python
from src.storage.vector_store import VectorStore

vector_store = VectorStore(index_path=".data/vectors/embeddings.index")

embedding = embedder.embed(entry.content)  # shape: (dim,)
vector_store.add(entry.id, embedding)
```

说明：

- 如果配置 `OPENAI_EMBEDDING_DIM=auto`，`dim` 会在首次成功的 Embedding 请求后锁定
- 新建索引前必须确保 `VectorStore` 使用的维度与 Embedding 服务实际返回维度一致

---

#### `search(query_vector: np.ndarray, k: int = 10) -> List[Tuple[str, float]]`

向量检索。

**参数**:
- `query_vector` (np.ndarray): 查询向量
- `k` (int): 返回数量

**返回**:
- `List[Tuple[str, float]]`: `[(entry_id, distance), ...]`

---

## 数据结构

### Entry

条目数据模型。

```python
@dataclass
class Entry:
    id: str                      # 唯一 ID（UUID）
    title: str                   # 标题
    content: str                 # Markdown 内容
    summary: str                 # AI 生成的摘要
    tags: List[str]              # 标签列表
    concepts: List[str]          # 概念列表
    metadata: dict               # 元数据（来源、作者、时间等）
    created_at: datetime         # 创建时间
    updated_at: datetime         # 更新时间

    # 可选字段
    source_url: Optional[str] = None
    author: Optional[str] = None
    publish_date: Optional[datetime] = None
```

---

### ProcessedContent

内容处理器输出。

```python
@dataclass
class ProcessedContent:
    title: str                   # 标题
    content: str                 # Markdown 格式内容
    metadata: dict               # 元数据

    # 可选字段
    raw_html: Optional[str] = None     # 原始 HTML
    images: List[str] = field(default_factory=list)  # 图片 URL 列表
```

---

### SearchResult

搜索结果。

```python
@dataclass
class SearchResult:
    entry_id: str                # 条目 ID
    title: str                   # 标题
    snippet: str                 # 摘要/片段（高亮匹配部分）
    score: float                 # 相关性分数（0-1）
    metadata: dict               # 元数据

    # 可选字段
    highlights: List[str] = field(default_factory=list)  # 匹配的关键词
```

---

### WorkflowResult

工作流执行结果。

```python
@dataclass
class WorkflowResult:
    success: bool                # 是否成功
    data: dict                   # 输出数据
    error: Optional[str] = None  # 错误信息（如果失败）
    logs: List[str] = field(default_factory=list)  # 执行日志
```

---

## 错误处理

### 异常层次

```
KnowledgeVaultError (基类)
├── ProcessorError           # 内容处理错误
│   ├── FetchError           # 网页抓取失败
│   └── ParseError           # 解析失败
├── StorageError             # 存储错误
│   ├── DuplicateEntryError  # 重复条目
│   └── FileNotFoundError    # 文件不存在
├── RetrievalError           # 检索错误
│   └── EmptyIndexError      # 索引为空
└── WorkflowError            # 工作流错误
    ├── StepFailedError      # 步骤执行失败
    └── InvalidConfigError   # 配置错误
```

---

### 错误处理示例

```python
from src.exceptions import ProcessorError, FetchError

try:
    processor = get_processor(url)
    content = await processor.process(url)
except FetchError as e:
    # 网页抓取失败，提示用户手动粘贴
    print(f"❌ 无法访问 URL: {e}")
    print("请复制网页内容后使用 archive-text 命令")
except ParseError as e:
    # 解析失败，记录日志
    logger.error(f"解析失败: {url}, 错误: {e}")
except ProcessorError as e:
    # 其他处理器错误
    logger.error(f"处理器错误: {e}")
```

---

### 错误码规范

| 错误码 | 含义 | HTTP 类比 |
|--------|------|-----------|
| `E1001` | URL 无法访问 | 404 |
| `E1002` | 内容解析失败 | 422 |
| `E2001` | 重复条目 | 409 |
| `E2002` | 文件不存在 | 404 |
| `E3001` | 索引为空 | 400 |
| `E4001` | 工作流配置错误 | 500 |
| `E4002` | 步骤执行失败 | 500 |

---

## API 调用示例

### 完整工作流示例

```python
import asyncio
from src.workflow.engine import WorkflowEngine
from src.retrieval.hybrid_search import HybridSearch

async def main():
    engine = WorkflowEngine()
    searcher = HybridSearch()

    # 1. 归档网页
    result = await engine.execute_async(
        workflow_name="archive-url",
        input_data={"url": "https://example.com/article"}
    )

    if result.success:
        entry_id = result.data["entry_id"]
        print(f"✅ 已归档: {entry_id}")
    else:
        print(f"❌ 归档失败: {result.error}")
        return

    # 2. 搜索内容
    results = searcher.search(
        query="分布式系统",
        top_k=5,
        strategy="hybrid"
    )

    print("\n🔍 搜索结果:")
    for i, result in enumerate(results, 1):
        print(f"{i}. {result.title} (score: {result.score:.2f})")
        print(f"   {result.snippet}\n")

if __name__ == "__main__":
    asyncio.run(main())
```

---

## 数据库迁移 API

> **新增于 v0.6.1**: 数据库增量升级系统

### MigrationManager

**职责**: 管理数据库 Schema 的版本升级和回滚

#### `__init__(db_path: Path, migrations_dir: Path)`

初始化迁移管理器。

**参数**:
- `db_path` (Path): 数据库文件路径
- `migrations_dir` (Path): 迁移脚本目录路径

**示例**:

```python
from pathlib import Path
from src.storage.migration_manager import MigrationManager

manager = MigrationManager(
    db_path=Path(".data/db/knowledge_vault.db"),
    migrations_dir=Path("scripts/migrations")
)
```

---

#### `get_current_version() -> str`

获取当前数据库版本。

**返回**:
- `str`: 版本号字符串（如 "1.0.0"），如果数据库未初始化返回 "0.0.0"

**示例**:

```python
version = manager.get_current_version()
print(f"当前版本: {version}")  # 输出: 当前版本: 1.0.0
```

---

#### `get_pending_migrations() -> List[Tuple[str, Path]]`

获取待执行的迁移脚本。

**返回**:
- `List[Tuple[str, Path]]`: (版本号, 脚本路径) 的列表，按版本号升序排列

**示例**:

```python
pending = manager.get_pending_migrations()
for version, migration_file in pending:
    print(f"待迁移: {migration_file.name} (v{version})")
```

---

#### `apply_migration(migration_file: Path, auto_backup: bool = True)`

执行迁移脚本。

**参数**:
- `migration_file` (Path): 迁移脚本文件路径
- `auto_backup` (bool): 是否自动备份数据库，默认 True

**异常**:
- `Exception`: 迁移执行失败

**示例**:

```python
migration_file = Path("scripts/migrations/002_add_cli_tables.sql")
manager.apply_migration(migration_file, auto_backup=True)
```

---

#### `apply_all_pending(auto_backup: bool = True) -> int`

执行所有待迁移脚本。

**参数**:
- `auto_backup` (bool): 是否自动备份，默认 True

**返回**:
- `int`: 成功执行的迁移脚本数量

**异常**:
- `Exception`: 迁移执行失败

**示例**:

```python
success_count = manager.apply_all_pending(auto_backup=True)
print(f"成功执行 {success_count} 个迁移脚本")
```

---

### 命令行工具

#### migrate.py

**位置**: [scripts/migrate.py](../scripts/migrate.py)

**用法**:

```bash
# 交互式升级
python scripts/migrate.py

# 自动升级（无需确认）
python scripts/migrate.py --auto

# 仅检查待迁移脚本（不执行）
python scripts/migrate.py --dry-run

# 查看当前版本
python scripts/migrate.py --version

# 跳过自动备份（不推荐）
python scripts/migrate.py --auto --no-backup
```

**完整升级流程示例**:

```bash
# 1. 检查当前版本
python scripts/migrate.py --version

# 2. 查看待迁移脚本
python scripts/migrate.py --dry-run

# 3. 在测试环境验证
export DB_PATH=".data-test/db/knowledge_vault.db"
python scripts/migrate.py --auto

# 4. 备份生产数据
./scripts/backup-data.ps1 -Message "v1.1.0 升级前备份"

# 5. 执行生产环境迁移
python scripts/migrate.py  # 输入 YES 确认

# 6. 验证结果
python -m src.main stats
```

---

### 迁移脚本格式

**位置**: [scripts/migrations/](../scripts/migrations/)

**命名规范**: `{序号}_{描述}.sql`，例如：
- `001_initial_schema.sql`
- `002_add_cli_tables.sql`

**脚本结构**:

```sql
-- Migration: 002_add_cli_tables.sql
-- Version: 1.1.0
-- Description: 新增 CLI 使用统计表
-- Author: 幽浮酱
-- Date: 2026-02-16

-- ========================================
-- 向上迁移（Upgrade）
-- ========================================

CREATE TABLE IF NOT EXISTS cli_command_history (
    command_id INTEGER PRIMARY KEY AUTOINCREMENT,
    command TEXT NOT NULL,
    ...
);

-- 更新版本号
INSERT INTO schema_version (version, description)
VALUES ('1.1.0', '新增 CLI 使用统计表');

-- ========================================
-- 向下迁移（Rollback）
-- ========================================

-- DROP TABLE IF EXISTS cli_command_history;
-- DELETE FROM schema_version WHERE version = '1.1.0';
```

**关键要求**:
- 使用 `IF NOT EXISTS` / `IF EXISTS` 保证幂等性
- 新增列时设置默认值（向后兼容）
- 避免删除列（SQLite 不支持且会丢失数据）
- 提供回滚 SQL（可选）

---

## CLI 命令参考

> **新增于 v0.6.0 (M6)**: 完整的命令行界面

### 概述

Personal Knowledge Vault 提供了 6 个核心命令，涵盖归档、搜索、查看、配置等功能。

**安装后使用**:
```bash
python -m src.main <command> [options]
```

**全局参数**:
- `--verbose`: 显示详细日志
- `--debug`: 显示调试日志
- `--version`: 显示版本信息
- `--help`: 显示帮助信息

---

### pkv archive - 归档内容

归档网页、聊天记录等内容到知识库。

**基本用法**:
```bash
python -m src.main archive <url_or_path> [options]
```

**参数**:
- `url_or_path` (必填): URL 或文件路径
- `--type` (可选): 内容类型（auto/webpage/chat/news），默认 auto
- `--skip-sharpen` (可选): 跳过 idea Sharpen 交互
- `--tags` (可选): 手动指定标签（逗号分隔）
- `--quiet` (可选): 静默模式（最小化输出）

**示例**:
```bash
# 归档网页
python -m src.main archive "https://example.com/article"

# 跳过 idea Sharpen
python -m src.main archive "https://example.com" --skip-sharpen

# 手动指定标签
python -m src.main archive "https://example.com" --tags "AI,技术,教程"

# 静默模式
python -m src.main archive "https://example.com" --quiet
```

**集成接口**:
- 调用 `WorkflowEngine.execute_async("archive-url", input_data)`
- `input_data` 键名：`url`, `skip_sharpen`, `manual_tags`
- 返回 `WorkflowResult` 包含 `knowledge_id`, `file_path`

---

### pkv search - 搜索知识库

搜索知识库中的内容。

**基本用法**:
```bash
python -m src.main search <query> [options]
```

**参数**:
- `query` (必填): 搜索关键词
- `--strategy` (可选): 检索策略（auto/bm25/vector/hybrid），默认 auto
- `--limit` (可选): 结果数量，默认 10
- `--format` (可选): 输出格式（table/json/markdown），默认 table

**示例**:
```bash
# 基本搜索
python -m src.main search "Python 异步编程"

# 指定 BM25 策略
python -m src.main search "Python" --strategy bm25

# JSON 输出
python -m src.main search "AI" --format json --limit 5
```

**检索策略**:
- `auto`: 自动选择（< 10 tokens → BM25, ≥ 10 → Vector）
- `bm25`: 关键词检索（精确匹配）
- `vector`: 语义检索（语义理解）
- `hybrid`: 混合检索（RRF k=60）

**集成接口**:
- 使用 `QueryRouter.get_retriever(query)` 自动路由
- 返回 `List[SearchResult]` 包含 `entry_id`, `title`, `snippet`, `score`

---

### pkv show - 显示条目详情

显示单个知识条目的详细信息。

**基本用法**:
```bash
python -m src.main show <id_or_url> [options]
```

**参数**:
- `id_or_url` (可选): knowledge_id 或 URL
- `--url` (可选): 通过 URL 查询
- `--raw` (可选): 输出原始 Markdown 内容

**示例**:
```bash
# 通过 ID 查看
python -m src.main show 42

# 通过 URL 查看
python -m src.main show --url "https://example.com/article"

# 输出原始 Markdown
python -m src.main show 42 --raw
```

**集成接口**:
- 调用 `SQLiteStore.get_entry_by_id(knowledge_id)`
- 调用 `SQLiteStore.get_entry_by_url(url)`
- 使用 `MarkdownStore.read(file_path)` 读取原始内容

---

### pkv list - 列出条目

列出知识库中的所有条目。

**基本用法**:
```bash
python -m src.main list [options]
```

**参数**:
- `--tag` (可选): 按标签过滤
- `--sort` (可选): 排序字段（time/title/id），默认 time
- `--desc` (可选): 降序排列
- `--limit` (可选): 结果数量，默认 20

**示例**:
```bash
# 列出所有条目
python -m src.main list

# 按标签过滤
python -m src.main list --tag Python

# 按时间降序排列
python -m src.main list --sort time --desc --limit 10
```

**集成接口**:
- 调用 `SQLiteStore.query_entries(filters, order_by, desc, limit)`

---

### pkv config - 配置管理

管理系统配置（查看、查询、设置）。

**子命令**:

#### config show - 显示所有配置
```bash
python -m src.main config show
```

#### config get - 查询单个配置
```bash
python -m src.main config get <key>
```

**示例**:
```bash
# 查询 DeepSeek 模型
python -m src.main config get ai.deepseek.model

# 查询数据库路径
python -m src.main config get db_path
```

#### config set - 设置配置
```bash
python -m src.main config set <key> <value>
```

**示例**:
```bash
# 设置 API Key
python -m src.main config set DEEPSEEK_API_KEY sk-xxxxx

# 设置日志级别
python -m src.main config set LOG_LEVEL DEBUG
```

**集成接口**:
- 读取 `Config` 对象属性
- 修改 `.env` 文件（set 命令）

---

### pkv stats - 统计信息

显示知识库统计信息。

**基本用法**:
```bash
python -m src.main stats
```

**输出内容**:
- 总条目数（按类型分组）
- 存储大小（Markdown + SQLite）
- Top 10 标签

**示例输出**:
```
📊 知识库统计

总条目数: 156
  ├─ webpage: 89
  ├─ chat: 45
  └─ news: 22

存储大小:
  ├─ Markdown: 12.5 MB
  └─ SQLite: 8.3 MB

标签统计 (Top 10):
  1. Python (42)
  2. AI (38)
  3. 技术 (35)
  ...
```

**集成接口**:
- 调用 `SQLiteStore.count_entries()`
- 调用 `SQLiteStore.count_entries_by_source_type()`
- 调用 `SQLiteStore.get_top_tags(limit=10)`

---

### 从 Python 代码调用 CLI

如果需要在 Python 脚本中调用 CLI 命令：

```python
import subprocess

# 归档 URL
result = subprocess.run(
    ['python', '-m', 'src.main', 'archive', 'https://example.com'],
    capture_output=True,
    text=True
)

if result.returncode == 0:
    print("归档成功")
    print(result.stdout)
else:
    print("归档失败")
    print(result.stderr)

# 搜索知识库
result = subprocess.run(
    ['python', '-m', 'src.main', 'search', 'Python', '--format', 'json'],
    capture_output=True,
    text=True
)

import json
search_results = json.loads(result.stdout)
```

---

## 下一步

了解 API 接口后，建议阅读：

- [使用手册](./使用手册.md) - CLI 命令详细使用指南 ✨ **NEW**
- [维护指南](./维护指南.md) - 系统维护和扩展开发 ✨ **NEW**
- [工作流开发指南](../modules/workflow/工作流开发指南.md) - 如何扩展功能
- [项目结构说明](../overview/项目结构说明.md) - 模块组织方式
- [数据库Schema设计](../specs/database/数据库Schema设计.md) - 数据模型详解

---

**文档版本**: v2.1 (更新于 2026-02-16 17:35, M6+M7+数据库迁移)

*API 设计遵循 KISS 原则，保持简洁清晰*
