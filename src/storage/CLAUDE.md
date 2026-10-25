# Storage 模块

[根目录](../../CLAUDE.md) > [src](..) > **storage**

---

## 模块职责

**三层存储架构**：提供 Markdown 主存储、SQLite 索引层和 generation-bound 向量语义层的内部接口。

### 核心理念

- **Markdown 主存储**: 人类可读，Git 友好，数据主权
- **SQLite 索引层**: 元数据索引 + FTS5 全文检索
- **向量语义层**: hnswlib HNSW generation，只有经过验证并 pointer publish 的 generation 可读
- **数据一致性**: Markdown/SQLite core mutation 由 Q1′ 的 operation proof 协调；向量是可派生层。受限 public rebuild 只可经 `pkv_kernel.embedding`、CLI `embedding` 或三个 MCP rebuild Tool 进入，Storage 本身不公开任何 rebuild/Provider/path 操作。
- **可追溯成果**: P1-B 的 Topic/Evidence/Claim/research brief 结构状态由 SQLite
  `outcome_*` 表权威保存；`outcomes/` 下的 Markdown 是 revisioned、可重建投影，
  不是可编辑的第二真相源。

---

## 入口与启动

### 初始化存储

```python
from src.storage.markdown_store import MarkdownStore
from src.storage.sqlite_store import SQLiteStore
from src.storage.vector_store import VectorStore

# 在操作开始时捕获同一个 Config snapshot；Application / Kernel 正常负责该组合。
# 外部 Wrapper 只能使用 pkv_kernel，不能直接导入这些内部存储类。
from src.utils.config import Config

config = Config()
md_store = MarkdownStore(config.vault_dir)
sql_store = SQLiteStore(config.db_path)
vec_store = VectorStore(
    config.vector_index_dir,
    runtime_config=config,
    layout=config.layout,
)

# 用户数据根的目录/Schema 初始化须走 inspect → plan → 确认 → execute 生命周期，
# 而不是把此依赖组合示例当作对真实数据根的直接初始化命令。
```

### 产品归档的唯一写入路径

```python
from src.application import KnowledgeApplication

# CLI/MCP/Kernel 通过 Application 提交 archive；不要拼装三层 Store 写入。
result = await KnowledgeApplication(config).archive_text("示例正文", title="示例文章")
```

R4 中该调用先进入 Q0 admission/PreparedDocument；Q1′ 是唯一允许写 Markdown、SQLite、
chunk 和 AI patch 的内容 writer，并以 StorageCoordinator 的 operation-bound proof 协调。
Q2 只在 core commit/handoff 后处理 Provider、usage/reservation 和 staged generation，不能
直接写 Markdown/SQLite 或平铺 vector 目录。上述 `MarkdownStore` / `SQLiteStore` /
`VectorStore` 实例是内部组件或受控测试 seam，不是外部组合 API。

---

## 对外接口

### MarkdownStore

**主存储层，YAML Front Matter 管理**

```python
class MarkdownStore:
    def __init__(self, vault_dir: Path):
        """初始化 Markdown 存储"""

    def save(self, entry: Entry) -> Path:
        """保存条目为 Markdown 文件"""

    def load(self, file_path: Path) -> Entry:
        """从 Markdown 文件加载条目"""

    def list_all(self) -> List[Path]:
        """列出所有 Markdown 文件"""

    def delete(self, file_path: Path) -> bool:
        """删除 Markdown 文件"""
```

**文件命名规则**:
```
格式: {YYYYMMDD}-{title-slug}.md
示例: 20260216-claude-code-intro.md
路径: <data-root>/vault/2026/02/20260216-claude-code-intro.md
```

**YAML Front Matter 结构**:
```yaml
---
title: "示例文章"
source_type: "wechat"
source_url: "https://mp.weixin.qq.com/xxx"
archived_at: "2026-02-16 10:30:00"
tags: ["技术", "AI"]
keywords: ["Claude", "Code"]
abstract: "这是一篇关于 Claude Code 的文章"
summary_one_sentence: "介绍 Claude Code 的核心功能"
summary_100_words: "详细介绍..."
search_strategy: "keyword"
word_count: 1500
---

# 正文内容

...
```

---

### SQLiteStore

**索引层，元数据 + FTS5 全文检索**

```python
class SQLiteStore:
    def __init__(self, db_path: Path):
        """初始化 SQLite 存储"""

    def initialize(self):
        """初始化数据库 Schema"""

    def save_entry(self, entry: Entry):
        """保存条目元数据到数据库"""

    def get_entry(self, knowledge_id: str) -> Optional[Dict]:
        """根据 knowledge_id 获取条目"""

    def search_by_fts5(self, query: str, limit: int = 10) -> List[Dict]:
        """FTS5 全文检索"""

    def get_all_entries(self) -> List[Dict]:
        """获取所有条目元数据"""

    def list_entries(self, limit, offset, sort_by, sort_order, source_type, tag) -> List[Dict]:
        """分页列出条目（MCP list_entries Tool 使用）"""

    def count_entries(self, source_type, tag) -> int:
        """统计条目数量（支持过滤）"""

    def get_all_tags_with_count(self) -> List[Dict]:
        """获取所有标签及关联条目数"""

    def get_statistics(self) -> Dict:
        """获取知识库综合统计"""
```

**Schema 设计**:

核心表:
- `knowledge_items`: 知识条目主表（knowledge_id 主键）
- `tags`: 标签表（tag_id 主键）
- `keywords`: 关键词表（keyword_id 主键）
- `knowledge_item_tags`: 多对多关联表
- `knowledge_item_keywords`: 多对多关联表
- `knowledge_fts`: FTS5 虚拟表（全文检索）

详细设计: [docs/specs/database/SQLite_Schema完整规范.md](../../docs/specs/database/SQLite_Schema完整规范.md)

### OutcomeStore（内部 P1-B authority）

`OutcomeStore` 仅使用已存在、已迁移的 SQLite 数据库，且所有 mutation 都要求
`knowledge_outcomes` root writer lease。它不读取 locator 指向的 source 或 Markdown body、
不创建 schema，也不写 outcome Markdown；这些 policy 分别属于 OutcomeService 与
Application。Evidence 保留 canonical `pkv://` locator、captured digest、状态和 limitation，
而非网页正文、私有路径、Provider 请求/响应或凭据。

对 entry/chunk Evidence，OutcomeService 只在该 Entry 的 Markdown 主记录与 SQLite/chunk
projection 一致时绑定其 opaque digest；任一侧随后漂移都会在 readonly revalidation 中外显为
`stale`，可确认的 Markdown 缺失为 `unavailable`；权限或 I/O 状态不确定时安全失败。
不一致的 source 不会被隐式修复或重绑，read/render/review 的复核都不自行修改 input binding。

W3/schema `1.2.8` 中，Topic 创建事务保留稳定 `outcome_id`；所有实际输入变更在单一 SQLite
事务内修改 Topic/Evidence/Claim/边、推进一次 `input_revision`、计算 `input_digest` 并将已有 brief
标记为 `needs_review`。规范化 no-op 不推动版本或失效，Evidence/Claim/边变更不冒充 Topic
自身字段 revision。输入摘要在模型转换前验证原始持久类型、精确字段与 locator 派生 ID，
普通 mutation 不能为损坏输入重新签名。

渲染时先发布不可覆盖的
`outcomes/topic-<topic_id>/<outcome_id>-r<revision:04d>-p<publication_id>.md`，再提交 SQLite pointer。
`publication_id` 包含稳定 outcome、候选 revision 与完整 input digest；相同输入可收养字节相同
的 orphan，不同输入产生不同 publication，规则同样覆盖 r1/r2/rN。Store 的 pointer CAS 比较
旧 `(outcome_id, revision, path, content_sha256, publication_id)` 或 absent，以及捕获的
`(input_revision, input_digest, render_contract_version)`；失败保留当前 pointer 与文件。

Store 连接显式 `BEGIN`，bundle 的输入、边、pointer 与 receipt 校验共享 read snapshot。
即使调用方直接构造 Application/Store，也会在该连接上复用迁移结构校验，验证 DDL、索引、
外键、跨 Topic 边及 locator 派生 ID；旧 schema 返回 `database_upgrade_required`，伪版本或
结构/绑定损坏返回 `database_schema_drift`，读取不升级数据库。

版本审阅在同一 writer-owned 事务内核对精确确认元组、rendered/current input binding 与状态，
CAS review state 并追加 `outcome_review_receipts`。幂等重放要求相同 receipt 且当前仍为目标
状态；旧 approve 不得撤销后来 return，return reason 不同也不能复用回执。历史未绑定 brief
保持 `legacy_unverified` 并外显 `needs_review`，不推定历史批准凭据。W3 保留 major-1 的旧无版本
入口作为已知兼容缺口，R04 的最终关闭仍取决于 W5 major-2 切换与验收。

已发布 pointer 的每次只读解析都通过同一 containment/no-follow gateway 读取 Markdown 并核对
`content_sha256`。只有可直接确认缺失的文件或字节已变时，SQLite 结构行仍保留，但 public DTO
分别显示 `projection_state=unavailable` 或 `stale`，effective state 为 `needs_review`；权限、I/O
或路径状态不确定必须安全失败。reader 不会隐式重写投影或把它作为仍然 approved 的可用内容。
Topic/question/Claim 与 BM25 query/候选标题中的绝对路径、`file:` URI 或带凭据 URL 也在 Store 前
fail-closed，不能进入 SQLite 或 Markdown projection。

---

### VectorStore

**语义层，hnswlib 向量索引**

```python
class VectorStore:
    def __init__(self, index_dir: Path, embedder: Optional[Embedder] = None):
        """初始化向量存储"""

    async def add_entry(self, entry: Entry):
        """添加条目到向量索引"""

    async def search(self, query: str, top_k: int = 10) -> List[Tuple[str, float]]:
        """向量检索"""

    def get_doc_vector(self, knowledge_id: int) -> Optional[np.ndarray]:
        """根据 knowledge_id 取回已存储的文档级向量（M9 新增）
        用于 get_related 关联推荐：取出条目的 embedding 后做相似搜索"""

    def search_doc(self, vector: np.ndarray, k: int) -> List[Tuple[int, float]]:
        """文档级向量搜索"""

    def save_index(self):
        """保存索引到磁盘"""

    def load_index(self):
        """从磁盘加载索引"""
```

**索引文件**:
- 活跃索引位于由 runtime snapshot 指向的 `vectors/generations/<generation-id>/`；其中的
  index、metadata 和 manifest 必须先 stage/validate，再通过 pointer CAS 发布为 `READY`。
- 根平铺 `doc_vectors.*` / `chunk_vectors.*` 仅可能作为历史组件遗留存在；R4 产品读取和写入
  不会将其作为 fallback。

**Embedding 合同**: Provider、model、endpoint fingerprint 与 resolved dimension 由 generation
binding 固定；并不假定固定模型或 1536 维。

---

## 关键依赖与配置

### 依赖库

- `python-frontmatter`: YAML Front Matter 解析
- `sqlite3`: SQLite 数据库（Python 标准库）
- `hnswlib`: HNSW 向量索引
- `numpy`: 向量操作（get_doc_vector 返回 np.ndarray）
- `jieba`: 中文分词（FTS5 查询）

### 配置项

存储路径不再由调用方拼接 `.data` 或从独立的 `storage.*_dir` 配置读取。
在产品路径中，bundled `config/config.yaml` 与唯一可编辑的
`%USERPROFILE%\\.pkv\\config.yaml` 构造一个不可变 `Config` snapshot：

```python
from src.utils.config import Config

config = Config()
vault_dir = config.vault_dir
db_path = config.db_path
vector_dir = config.vector_index_dir
log_dir = config.log_dir
tmp_dir = config.tmp_dir
```

有效 data root 的选择顺序为 `PKV_DATA_ROOT` → 用户配置
`storage.data_root` → `%USERPROFILE%\\.pkv\\data`。上述全部子路径由同一
snapshot 的 `RuntimeLayout` 派生并被 containment 校验；不要在用户配置中把
`vault_dir`、`db_path` 或 `vector_index_dir` 当成第二套产品布局。
`<data-root>/config/local.yaml` 仅保存 PKV 管理的无密钥 runtime snapshot（数据库/
Embedding 合同等），不是可编辑用户配置，也不包含 Provider 密钥。

---

## 数据模型

### Entry 数据类

所有存储操作的核心数据结构:

```python
@dataclass
class Entry:
    # 基础元数据
    title: str
    source_type: str
    source_url: Optional[str]
    archived_at: Optional[str]

    # 内容分析
    tags: list
    keywords: list
    abstract: str
    summary_one_sentence: str
    summary_100_words: str

    # 检索配置
    search_strategy: str
    word_count: int

    # 关联信息
    related_docs: list

    # 个人标注
    reading_status: str
    rating: int
    notes: str

    # 正文内容
    content: str
```

详细规范: [docs/specs/models/Entry数据模型规范.md](../../docs/specs/models/Entry数据模型规范.md)

---

## 关键设计

### 1. 双重存储策略

```
Q0: ingress identity / private request spool / task-fenced temporary assets（无内容写、无 Provider）
    ↓
Q1′: Markdown + SQLite + operation proof + durable AI handoff（唯一内容 writer）
    ↓
Q2: usage/reservation → DerivationPatch 回送 Q1′ → staged/validated generation pointer
```

**优势**:
- Markdown 可直接编辑、Git 版本控制
- SQLite 快速查询、FTS5 全文检索
- 向量索引支持语义搜索
- vector 是从已提交 source 派生，但 rebuild/public repair 必须经独立 lifecycle，不能绕过 Q1′ 或 runtime confirmation

Q0 的 request payload 与临时 asset 都不是 Vault 内容：短 admission lease 先创建
`runtime/r4/ingress/<task-id>/<owner-fence>/assets/`，随后慢速 parser 只持有该 task 的 claim-fenced
temporary-image grant。它不能写 Markdown、SQLite、vector、日志、Config 或共享 `tmp/`；URL 原始网页
只在内存处理，Q1′ core commit 或 Q0 terminal rejection 后安全地 best-effort 清理该 workspace。文件
附件的长期归档语义尚未在此合同中定义。

### 2. FTS5 中文分词

SQLite FTS5 不支持中文分词，需手动 jieba 分词:

```python
from src.utils.text_utils import TextProcessor

# 查询时必须手动分词
query = "Claude Code 使用指南"
tokens = TextProcessor.tokenize_chinese(query)  # "Claude Code 使用 指南"

# 传入 FTS5
cursor.execute(
    "SELECT * FROM knowledge_fts WHERE knowledge_fts MATCH ?",
    (tokens,)
)
```

### 3. knowledge_id 命名规范

所有表使用 `knowledge_id` 作为主键或外键，而非通用的 `id`:

```sql
CREATE TABLE knowledge_items (
    knowledge_id TEXT PRIMARY KEY,  -- 而非 id
    title TEXT NOT NULL,
    ...
);

CREATE TABLE knowledge_item_tags (
    knowledge_id TEXT NOT NULL,     -- 外键
    tag_id TEXT NOT NULL,
    FOREIGN KEY (knowledge_id) REFERENCES knowledge_items(knowledge_id)
);
```

**动机**: 领域驱动设计，避免通用 `id` 造成的混淆。

详见: [docs/history/issues/SCHEMA_MIGRATION_PLAN.md](../../docs/history/issues/SCHEMA_MIGRATION_PLAN.md)

### 4. MCP 集成 (M8+M9)

Storage 层为 MCP Server 提供核心数据访问:
- `SQLiteStore` 单例被 MCP tools/resources 共享（通过 `server.py` 的 `get_sqlite_store()`）
- `MarkdownStore` 单例用于读取条目全文
- `VectorStore.get_doc_vector()` 方法专为 `get_related` Tool 设计

---

## 测试与质量

### 单元测试

```powershell
# 运行存储层测试
.\scripts\run-test.ps1 -Direct -DataRoot .data-test\storage-unit -Command @("pytest", "tests/unit", "-k", "store or storage", "-v")

# 测试 Markdown 存储
.\scripts\run-test.ps1 -Direct -DataRoot .data-test\storage-markdown -Command @("pytest", "tests/unit/test_markdown_store.py", "-v")

# 测试 SQLite 存储
.\scripts\run-test.ps1 -Direct -DataRoot .data-test\storage-sqlite -Command @("pytest", "tests/unit/test_sqlite_store_queries.py", "tests/unit/test_sqlite_store_additional.py", "tests/unit/test_sqlite_store_management.py", "tests/unit/test_sqlite_store_initialize_compat.py", "-v")

# 测试向量存储
.\scripts\run-test.ps1 -Direct -DataRoot .data-test\storage-vector -Command @("pytest", "tests/unit/test_vector_store_safety.py", "tests/unit/test_vector_retriever_contract.py", "-v")
```

### 跨存储协调测试

```powershell
# 测试跨 Markdown / SQLite / Vector 的终态协调
.\scripts\run-test.ps1 -Direct -DataRoot .data-test\storage-coordinator -Command @("pytest", "tests/unit/test_storage_coordinator.py", "-v")
```

工作流和检索层的跨模块存储交互分别由对应 integration suite 覆盖；仓库当前没有
`tests/integration/test_storage_integration.py`。

### 数据一致性测试

在隔离 fault-injection tests 中验证 Q1′ proof 的 Markdown/SQLite revision 一致，以及 Q2
的 staged generation 仅在 validate + pointer CAS 后被读取；这不是用户可执行的重建流程。

W3 Store 专项命令：

```powershell
.\scripts\run-test.ps1 -Direct -DataRoot .data-test\w3-store-final-v3 pytest tests/unit/test_w3_outcome_store.py -vv --tb=short
```

2026-10-25 此命令为 **65 passed**（exit 0），覆盖八类输入 mutation 的三处 SQL 故障切点、
no-op、完整 pointer CAS、review/receipt 原子性与重放、独立 WAL writer 下的 snapshot、
原始字段损坏拒绝及伪 schema 只读门禁。逐 nodeid 日志保存在
`.data-test/w3-outcome-store-evidence/reports/final-65-passed.log`；集成范围及剩余边界见
[W3 实施记录](../../docs/overview/W3-Outcome一致性与版本审批实施记录-2026-10.md)。
随后将 snapshot barrier 的成功断言移到 SQLite trace callback 外，避免 callback 吞掉异常；
同一文件的 `test_read_bundle_uses_one_snapshot_across_independent_writer` 在
`.data-test/w3-store-snapshot-final` 单独重跑 **1 passed**，日志为
`.data-test/w3-outcome-store-evidence/reports/snapshot-oracle-1-passed.log`，不是新增第 66 个 case。
此处不预先宣称正在进行的跨模块全量回归通过，也不代表对用户 Vault 执行了迁移。

---

## 常见问题 (FAQ)

### Q1: 如何从 Markdown 完全重建索引？

当前有一个受限 public generation rebuild：`pkv embedding inspect`、`pkv embedding
rebuild --force`，以及 MCP 的 `inspect_embedding_rebuild` / `plan_embedding_rebuild` /
`execute_embedding_rebuild`。它只经 `pkv_kernel.embedding` 的 `inspect → plan → confirm →
execute` 合同运行，使用独立 token policy、fenced private stage 和 pointer CAS；它不公开
Q2 task resume/retry、flat-vector fallback 或任意目录操作。不要直接调用 `clear_all()`、
`save_entry()` 或 `add_entry()` 操作用户数据根。

### Q2: FTS5 查询为什么返回空结果？

可能原因:
1. **未手动分词**: FTS5 查询必须使用 `TextProcessor.tokenize_chinese()`
2. **触发器未触发**: 检查 `knowledge_fts` 虚拟表是否有数据
3. **MATCH 语法错误**: 使用 `MATCH` 而非 `LIKE` 或 `=`

正确用法:
```python
query = "Claude Code"
tokens = TextProcessor.tokenize_chinese(query)
cursor.execute("SELECT * FROM knowledge_fts WHERE knowledge_fts MATCH ?", (tokens,))
```

### Q3: 向量索引占用多少磁盘空间？

估算公式:
```
文档级索引大小 ≈ 条目数 x 1536 维 x 4 字节 ≈ 6 KB/条目
分块级索引大小 ≈ 条目数 x 平均分块数 x 6 KB
```

示例: 10,000 条目 -> 约 60 MB（文档级）+ 240 MB（分块级）

### Q4: 如何迁移现有数据？

参考迁移指南: [docs/history/issues/SCHEMA_MIGRATION_PLAN.md](../../docs/history/issues/SCHEMA_MIGRATION_PLAN.md)

---

## 相关文件清单

### 核心文件

| 文件 | 说明 |
|------|------|
| `__init__.py` | 存储层模块入口 |
| `markdown_store.py` | Markdown 主存储 (Entry 数据类) |
| `sqlite_store.py` | SQLite 索引层 (FTS5) |
| `vector_store.py` | 向量语义层 (hnswlib) -- 含 `get_doc_vector()` |
| `migration_manager.py` | 数据库增量迁移管理器 |
| `outcome_store.py` | P1-B Topic/Evidence/Claim/research brief SQLite authority |
| `outcome_migration.py` | 014 原始输入 backfill、只读结构和绑定校验 |
| `../outcomes/identity.py` | input/publication/review receipt 的冻结 canonical hash |

### 测试文件

| 文件 | 说明 |
|------|------|
| `tests/unit/test_markdown_store.py` | Markdown 存储测试 |
| `tests/unit/test_sqlite_store_queries.py` 等 | SQLite 投影、查询和初始化兼容性测试 |
| `tests/unit/test_vector_store_safety.py` | 向量索引一致性与只读安全测试 |
| `tests/unit/test_storage_coordinator.py` | 跨 Markdown / SQLite / Vector 终态协调测试 |
| `tests/unit/test_w3_outcome_store.py` | Outcome 输入事务、版本 CAS、receipt 与 snapshot 故障矩阵 |

### 文档

| 文件 | 说明 |
|------|------|
| [docs/specs/models/Entry数据模型规范.md](../../docs/specs/models/Entry数据模型规范.md) | Entry 数据类规范 |
| [docs/specs/interfaces/Storage接口规范.md](../../docs/specs/interfaces/Storage接口规范.md) | 三层存储架构设计 |
| [docs/specs/database/SQLite_Schema完整规范.md](../../docs/specs/database/SQLite_Schema完整规范.md) | 数据库 Schema 详细设计 |
| [docs/specs/models/数据规范.md](../../docs/specs/models/数据规范.md) | Markdown Front Matter 规范 |

---

## 变更记录 (Changelog)

### 2026-02-19 00:58 (M9)
- `vector_store.py` 新增 `get_doc_vector(knowledge_id)` 方法
- 用于 MCP `get_related` Tool 的关联推荐功能
- 利用 hnswlib 原生 `get_items()` 从内存索引中读取向量
- 更新文档体现 MCP 集成和新增接口

### 2026-02-16
- 生成模块级 CLAUDE.md 文档
- 添加导航面包屑
- 补充 FTS5 中文分词和 knowledge_id 命名说明

### 2026-02-15 (M5.1)
- 修复 SQLite 配置字段名不匹配问题
- 完成 Schema 迁移到 `knowledge_id` 命名

### 2026-02-10 (M1)
- 完成三层存储架构实现
- 完成 FTS5 全文检索集成
- 完成向量索引集成

---

**模块维护者**: AI Agent
**最后更新**: 2026-10-25

*本文档由 Claude Code 自动生成*
