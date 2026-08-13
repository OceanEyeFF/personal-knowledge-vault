# API 文档

> Personal Knowledge Vault - API Reference
> 核心模块接口定义与数据流转协议

**文档版本**: v2.3
**创建日期**: 2026-02-14
**最后更新**: 2026-08-07
**目标读者**: 开发者（AI Agent 仅作接口参考）

> **执行边界**：本文 Python/CLI 片段主要说明公开接口，不是 Agent 可直接执行的 Runbook。AI 自动化只能使用 `scripts/run-test.ps1` 与合成 CAT-0 数据；会加载 `config/local.yaml` 的 config 命令、真实 URL archive、vector/hybrid 检索以及真实迁移均为 user-only，当前受 U1/G8（迁移另需 FT5）阻塞。当前可执行合同以 [`tests/CLAUDE.md`](../../tests/CLAUDE.md) 与 [`testing/真实数据验证Runbook.md`](./testing/真实数据验证Runbook.md) 为准。
>
> **M13 W2 接口边界**：Workflow 只加载真实、版本化的 `archive-url.yaml` 与 `archive-text.yaml`，不支持 `search.yaml`；Retrieval 返回显式五态 `SearchResponse`；GUI 发布搜索只保证 BM25；MCP 只发布 stdio，HTTP/Bearer 不受支持。三个探索 Tool 仍为 `partial-v1` / `implementation_level=partial`。

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
- `workflow_name` (str): 当前只接受 `archive-url` / `archive-text`，对应真实、带 `schema_version` 的 YAML
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

# 检查结果；degraded 仍成功产出，但必须展示 warnings/issues
if result.terminal in {"success", "degraded"}:
    print(f"✅ 已归档: {result.data['knowledge_id']}")
    print(result.warnings, result.issues)
else:
    print(f"❌ 失败: {result.errors}")
```

---

#### `register_step(step_type: str, step_class: Type[BaseStep])`

注册自定义工作流步骤。

**参数**:
- `step_type` (str): 步骤类型标识（在 YAML 中使用）
- `step_class` (Type[BaseStep]): 步骤类（继承自 `BaseStep`）；registry 属于 Engine 实例

**示例**:

```python
from src.workflow.steps import BaseStep

class CustomStep(BaseStep):
    async def execute(self, context):
        # 自定义逻辑
        ...

engine.register_step("custom_action", CustomStep)
```

---

### WorkflowContext

**职责**: 工作流执行上下文，在步骤间传递数据

#### 属性

- `state` (`State`): 步骤间共享状态容器
- `logs` (`list[str]`): 工作流日志

#### `state.set(key: str, value: Any)`

更新上下文状态。

**示例**:

```python
context.state.set("title", "我的文章标题")
context.state.set("tags", ["技术", "编程"])
```

---

## 内容处理器 API

### BaseProcessor (抽象基类)

所有内容处理器必须实现此接口。

#### `can_handle(url_or_text: str) -> bool`

判断是否能处理该内容。

**参数**:
- `url_or_text` (str): URL 或文本内容

**返回**:
- `bool`: 是否能处理

**示例**:

```python
class WechatProcessor(BaseProcessor):
    @classmethod
    def can_handle(cls, url_or_text: str) -> bool:
        return "mp.weixin.qq.com" in url_or_text
```

---

#### `async process(input: str) -> Entry`

处理内容并返回结构化数据。

**参数**:
- `input` (str): URL 或原始文本

**返回**:
- `Entry`: 处理后的知识条目

**示例**:

```python
async def process(self, url: str) -> Entry:
    response = await self._fetch_public_url(url, headers=self.headers)
    soup = BeautifulSoup(response.text, "lxml")

    return Entry(
        title=soup.find("h1").text,
        source_type="wechat",
        source_url=url,
        content=self._extract_markdown(soup),
        published_at=self._parse_date(soup),
    )
```

`_fetch_public_url()` 是 BaseProcessor 提供的唯一 URL 网络 seam，底层使用 DNS-pinned `SafeFetcher`。页面、redirect 与图片等子资源不得使用 Playwright/requests/httpx 直连或降级。

---

### 辅助函数

#### `get_processor(url_or_text: str) -> BaseProcessor`

自动选择合适的处理器。

**参数**:
- `url_or_text` (str): URL 或文本

**返回**:
- `BaseProcessor`: 匹配的处理器实例

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

### BM25Retriever / VectorRetriever / HybridRetriever / QueryRouter

所有公开 `search(query: str, limit: int = 10)` 都同步返回 `SearchResponse`。构造器接收明确的 `db_path` / `vector_index_dir`；Vector、Hybrid 与 Router 可接收 `embedder_factory: Callable[[], Embedder]`，只有语义分支实际执行时才调用工厂。

```python
from pathlib import Path
from src.retrieval import QueryRouter

router = QueryRouter(
    Path("isolated.db"),
    Path("isolated-vectors"),
    embedder_factory=application_embedder_factory,
)
response = router.search("分布式系统的权衡", limit=5)

if response.status in {"success", "degraded"}:
    for item in response.results:
        print(item.title, item.score)
elif response.status == "no_hits":
    print("没有命中")
else:
    print([issue.to_dict() for issue in response.issues])
```

五态语义：

- `success`：至少一条结果且无 issue。
- `no_hits`：检索正常完成但零命中。
- `invalid`：查询或 limit 非法，携带 `RETRIEVAL_INVALID_QUERY`。
- `error`：请求未成功执行，不携带部分结果。
- `degraded`：部分能力失败，可保留可用结果，但必须携带 issue。

`SearchResponse` 不是列表，`if response:` 会抛出 `TypeError`。adapter 必须先检查 `status`；底层异常不得映射为空命中，公开 issue 不得包含异常原文、密钥或绝对路径。

M13 GUI 只保证 BM25；CLI/MCP 可显式选择 `bm25/vector/hybrid/auto`。后 3 种可能需要 Embedding Provider，默认离线验证不会构造或连接真实 Provider。

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

#### `insert_entry(entry: Entry, file_path: str) -> int`

将条目及其已验证的 Markdown 相对路径写入 SQLite 投影。

**参数**:
- `entry` (Entry): 条目对象
- `file_path` (str): Vault 内 Markdown 路径

**返回**:
- `int`: knowledge_id

**示例**:

```python
from src.storage.sqlite_store import SQLiteStore

db = SQLiteStore(Path(".data/db/knowledge_vault.db"))

entry_id = db.insert_entry(entry, file_path="text/example.md")
print(f"✅ 已索引: {entry_id}")
```

---

#### `list_entries(..., tag: str | None = None) -> list[dict]`

按标签或其他受支持过滤条件分页列出条目；标签汇总使用
`get_all_tags_with_count(limit=...)`。

---

### VectorStore

**职责**: hnswlib 向量索引

#### `add_doc_vector(knowledge_id: int, vector: np.ndarray)`

添加向量到索引。

**参数**:
- `knowledge_id` (int): 条目 ID
- `vector` (np.ndarray): 向量（shape: `(dim,)`，由当前模型实际维度决定）

**示例**:

```python
from src.storage.vector_store import VectorStore

vector_store = VectorStore(Path(".data/vectors"), dim=1536)

vector = embedder.embed(entry.content)  # shape: (dim,)
vector_store.add_doc_vector(entry.knowledge_id, vector)
```

说明：

- 如果配置 `ai.embedding.dim: auto`，维度会在首次成功的 Embedding 请求后锁定
- 新建索引前必须确保 `VectorStore` 使用的维度与 Embedding 服务实际返回维度一致
- Embedding 模型同样属于索引契约；更换模型后，即使维度相同，也应重建向量索引并重新生成 Embedding

---

#### `search_doc(query_vector: np.ndarray, k: int = 10) -> List[Tuple[int, float]]`

向量检索。

**参数**:
- `query_vector` (np.ndarray): 查询向量
- `k` (int): 返回数量

**返回**:
- `List[Tuple[int, float]]`: `[(knowledge_id, distance), ...]`

---

## 数据结构

### Entry

条目数据模型。

```python
@dataclass
class Entry:
    title: str                   # 标题
    source_type: str             # wechat/zhihu/generic/chat/ai_chat/text
    source_url: Optional[str] = None
    event_time: Optional[str] = None
    published_at: Optional[str] = None
    archived_at: Optional[str] = None
    tags: list = field(default_factory=list)
    keywords: list = field(default_factory=list)
    abstract: str = ""
    summary_one_sentence: str = ""
    summary_100_words: str = ""
    search_strategy: str = "keyword"
    word_count: int = 0
    related_docs: list = field(default_factory=list)
    reading_status: str = ""
    rating: int = 0
    notes: str = ""
    content: str = ""
```

---

Processors 直接返回 `Entry`，不存在独立的 `ProcessedContent` 公开类型。

---

### SearchResult

搜索结果。

```python
@dataclass
class SearchResult:
    knowledge_id: int            # 条目 ID
    title: str                   # 标题
    score: float                 # 相关性分数（0-1）
    highlight: str               # 高亮片段
    metadata: dict               # 元数据

@dataclass(frozen=True)
class SearchResponse:
    status: str                  # success/no_hits/invalid/error/degraded
    results: tuple[SearchResult, ...]
    strategy: str
    issues: tuple[RetrievalIssue, ...]
```

---

### WorkflowResult

工作流执行结果。

```python
@dataclass
class WorkflowResult:
    success: bool                # terminal != "error"
    data: dict                   # 输出数据
    errors: List[str]            # 仅致命错误
    logs: List[str] = field(default_factory=list)  # 执行日志
    warnings: List[str] = field(default_factory=list)
    issues: List[dict] = field(default_factory=list)
    terminal: Optional[str] = None  # __post_init__ 推导 success/degraded/error
```

---

## 错误处理

### 稳定错误合同

跨 Workflow、Retrieval、Provider、URL/SSRF、transport 与 Chat 的机器可读失败统一使用 `src.runtime.errors.ErrorCode`。需要抛出的边界使用 `PKVRuntimeError(code, safe_message, stage, recoverable)`；需要返回的 Retrieval/MCP 边界使用 `RetrievalIssue` 或同形 `issues[]`。原始异常文本可能包含路径、查询或凭据，只写私有日志，不能直接进入 adapter 响应。

---

### 错误处理示例

```python
from src.runtime.errors import PKVRuntimeError

try:
    processor = get_processor(url)
    content = await processor.process(url)
except PKVRuntimeError as exc:
    print(exc.code.value, str(exc))  # 只展示稳定安全文案
```

---

### 错误码规范

当前枚举以 `src/runtime/errors.py` 为唯一实现真相源，包含 `WORKFLOW_*`、`RETRIEVAL_*`、`PROVIDER_*`、`URL_*`、`SSRF_*`、`TRANSPORT_*` 与 `CHAT_*` 家族。adapter 不得自行发明字符串错误码，也不得用 HTTP 状态类比替代领域终态。

---

## API 调用示例

### 完整工作流示例

```python
import asyncio
from pathlib import Path
from src.workflow.engine import WorkflowEngine
from src.retrieval import BM25Retriever

async def main():
    engine = WorkflowEngine()
    searcher = BM25Retriever(Path("isolated.db"))

    # 1. 归档网页
    result = await engine.execute_async(
        workflow_name="archive-url",
        input_data={"url": "https://example.com/article"}
    )

    if result.terminal in {"success", "degraded"}:
        knowledge_id = result.data["knowledge_id"]
        print(f"✅ 已归档: {knowledge_id}", result.warnings)
    else:
        print(f"❌ 归档失败: {result.errors}")
        return

    # 2. 默认离线搜索使用 BM25
    response = searcher.search("分布式系统", limit=5)

    print("\n🔍 搜索结果:")
    for i, item in enumerate(response.results, 1):
        print(f"{i}. {item.title} (score: {item.score:.2f})")
        print(f"   {item.highlight}\n")

if __name__ == "__main__":
    asyncio.run(main())
```

---

## 数据库迁移 API

> **新增于 v0.6.1**: 数据库增量升级系统

> **P0 安全边界**：真实迁移尚未执行。默认自动化必须从 `run-test.ps1` 启动，并且迁移逻辑只在 pytest 创建的临时 SQLite 中验证；包装器对 `scripts/migrate.py` / `scripts.migrate` 明确返回退出码 `2`。pytest 先经 `tests/offline_entrypoint.py pytest` 在 pytest/plugin 导入前建立离线基线，根 `tests/conftest.py` 再维持逐用例隔离；CLI/MCP 使用 offline entrypoint，FT7 Direct Python 只允许仓库内 `-m module` 或 `.py` 并在同进程安装 guard，拒绝 `-c`、stdin 和解释器 flags。这些 Python guard 不是 OS sandbox，只覆盖合成 CAT-0，不授权读取真实快照。

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

def test_migration_contract(tmp_path):
    # 仅在由 pytest/tmp_path 创建的临时 SQLite 上使用
    manager = MigrationManager(
        db_path=tmp_path / "knowledge_vault.db",
        migrations_dir=Path("scripts/migrations"),
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

以下示例只用于 pytest 创建的临时 SQLite。测试迁移一律设置 `auto_backup=False`；不得对快照或真实数据库调用。

```python
migration_file = Path("scripts/migrations/002_add_cli_tables.sql")
manager.apply_migration(migration_file, auto_backup=False)
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
# 仅用于 pytest 创建的临时 SQLite；测试迁移一律禁用自动备份
success_count = manager.apply_all_pending(auto_backup=False)
print(f"成功执行 {success_count} 个迁移脚本")
```

---

### 命令行工具

#### migrate.py

**位置**: [scripts/migrate.py](../scripts/migrate.py)

`migrate.py` 提供版本查询、待迁移检查、健康检查和执行迁移等接口，但当前 P0 不提供可复制执行的 CLI 命令。原因是它尚未接入 base-only 配置入口；无论是只读参数还是写入参数，默认自动化都不得绕过包装器裸跑。

自动化只运行 MigrationManager 的临时 SQLite 测试：

```powershell
.\scripts\run-test.ps1 -Direct -DataRoot .data-test\migration-unit -Command @(
  "pytest",
  "tests\unit\test_migration_manager_versions.py",
  "tests\unit\test_migration_manager_runtime.py",
  "tests\unit\test_migration_health_check.py",
  "-q"
)
```

该结果只证明合成 fresh-install、迁移顺序、幂等性和健康检查代码契约，不证明任何真实旧库可升级。

真实迁移由 FT5、U1/G8 与用户明确授权共同阻塞。在门禁完成前，真实快照保持只读，不对其运行 version、dry-run、health-check 或迁移，不执行备份/恢复，也不将其复制进默认 CAT-0 自动化。获批后的迁移验证必须以只读快照为来源制作 **disposable writable clone**，仅在该克隆上执行，并对照迁移前后的 Schema 版本、关键表行数、关系边、FTS、索引和完整性基线；任何一项无法解释都停止。正式命令必须在获批 Runbook 中固定并由用户逐项确认，而不是从本文复制。

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

Personal Knowledge Vault 提供 9 个公开命令，涵盖 URL/文本归档、检索、浏览、标签统计、已有向量近邻、配置与统计功能。

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

### pkv archive-text - 归档纯文本

归档字面文本；即使文本看起来像本地路径，也不会触发文件读取。

**基本用法**:
~~~bash
python -m src.main archive-text "一条可归档的离线笔记" --title "笔记标题"
python -m src.main archive-text "一条可归档的离线笔记" --format json
~~~

**参数**:

- text（必填）：字面纯文本。
- --title（可选）：覆盖回退处理器生成的标题。
- --format table|json（可选）：默认 table。

**集成接口**:

- 先调用 TextFallbackProcessor.process_text(text) 构造 Entry。
- 再调用 WorkflowEngine.execute_async("archive-text", input_data)，并固定
  skip_review=true、skip_sharpen=true。
- success 与 degraded 均会保留核心 Markdown/SQLite 归档终态；其余终态为错误。

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
- `auto`: `QueryRouter` 自动选择（token 数低于配置阈值 → BM25，否则 → Hybrid）
- `bm25`: 关键词检索（精确匹配）
- `vector`: 语义检索（语义理解）
- `hybrid`: 混合检索（RRF k=60）

**集成接口**:
- `auto` 调用 `QueryRouter.search(query, limit)`；显式策略调用对应 Retriever
- 底层返回 `SearchResponse`；CLI 必须分别展示 `success/no_hits/invalid/error/degraded`，不能把错误当空结果

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
- 调用 `SQLiteStore.query_by_id(knowledge_id)`
- 调用 `SQLiteStore.query_by_url(url)`
- 使用 `MarkdownStore.load(file_path)` 读取原始内容

---

### pkv list - 列出条目

列出知识库中的所有条目。

裸命令读取当前数据目录；AI 默认使用 `.\scripts\run-test.ps1 list ...`，不读取生产 `.data/`。生产查询仅由明确授权的用户执行。

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
- 调用 `SQLiteStore.list_entries(...)`

---

### pkv tags - 标签统计

读取当前 SQLite 投影中的标签及条目计数；本命令不提供跨 Markdown/SQLite 的标签写入。

**基本用法**:
~~~bash
python -m src.main tags --limit 20
python -m src.main tags --format json
~~~

**参数**:

- --limit：1 到 200，默认 50。
- --format table|json：默认 table。

**集成接口**:

- 调用 SQLiteStore.get_all_tags_with_count(limit=...)。

---

### pkv related - 已有向量索引近邻

返回指定知识条目的文档向量近邻。这是相似度推荐，不等同于关系图查询。

**基本用法**:
~~~bash
python -m src.main related 42 --limit 5
python -m src.main related 42 --format json
~~~

**参数**:

- knowledge_id（必填）：正整数条目 ID。
- --limit：1 到 20，默认 5。
- --format table|json：默认 table。

**集成接口与终态**:

- 读取 SQLite 条目，并通过 VectorStore.open_readonly(...) 查询已存在的文档向量。
- 查询不会创建向量索引、锁文件或 Provider。
- 条目不存在返回 no_hits；索引或条目向量缺失返回显式 degraded；后端错误
  不得伪装为空结果。

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
# 查询 LLM 模型
python -m src.main config get ai.llm.model

# 查询数据库路径
python -m src.main config get db_path
```

#### config set - 设置配置

`config set` 会修改真实的 `config/local.yaml`，测试包装器会拒绝该命令。只有用户明确授权时才由用户执行；AI 不执行，也不得通过命令行传入密钥。

```bash
python -m src.main config set <key> <value>
```

**示例**:
```bash
# 设置 LLM 模型
python -m src.main config set ai.llm.model local-model

# 设置日志级别
python -m src.main config set logging.level DEBUG
```

LLM、Embedding 和处理器配置统一写入 Git 忽略的 `config/local.yaml`，使用 `ai.llm.*`、`ai.embedding.*` 等点号路径键。不要再使用 `.env` 或旧的 `PKV_LLM_*` / `PKV_EMBD_*` 键。凭据建议直接在本机编辑 `config/local.yaml`，不要作为命令行参数传入，以免进入终端历史。

默认自动化由 `scripts/run-test.ps1` 锁定 `.data-test/` 路径，不创建或加载 `.env.test`，并固定 `PKV_RUN_LIVE=0`。`PKV_RUN_LIVE` 只是 pytest 收集开关，不授权应用联网；真实服务验证仍受 U1/G8 与用户授权阻塞。

**集成接口**:
- 读取 `Config` 对象属性
- 修改 `config/local.yaml`（set 命令）

---

### pkv stats - 统计信息

显示知识库统计信息。

裸命令读取当前数据目录；AI 默认使用 `.\scripts\run-test.ps1 stats`，不读取生产 `.data/`。生产统计仅由明确授权的用户执行。

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
- 调用 `SQLiteStore.get_all_tags_with_count(limit=10)`

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
