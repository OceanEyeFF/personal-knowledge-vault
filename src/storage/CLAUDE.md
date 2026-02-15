# Storage 模块

[根目录](../../CLAUDE.md) > [src](..) > **storage**

---

## 模块职责

**三层存储架构**：提供 Markdown 主存储、SQLite 索引层、向量语义层的统一接口。

### 核心理念

- **Markdown 主存储**: 人类可读，Git 友好，数据主权
- **SQLite 索引层**: 元数据索引 + FTS5 全文检索
- **向量语义层**: hnswlib HNSW 算法，语义检索
- **数据一致性**: 所有辅助存储可从 Markdown 完全重建

---

## 入口与启动

### 初始化存储

```python
from pathlib import Path
from src.storage.markdown_store import MarkdownStore
from src.storage.sqlite_store import SQLiteStore
from src.storage.vector_store import VectorStore

# 初始化三层存储
vault_dir = Path(".data/vault")
db_path = Path(".data/db/knowledge_vault.db")
vector_dir = Path(".data/vectors")

md_store = MarkdownStore(vault_dir)
sql_store = SQLiteStore(db_path)
vec_store = VectorStore(vector_dir)

# 初始化数据库 Schema
sql_store.initialize()
```

### 存储一个条目

```python
from src.storage.markdown_store import Entry

entry = Entry(
    title="示例文章",
    source_type="wechat",
    source_url="https://mp.weixin.qq.com/xxx",
    content="# 正文内容...",
    tags=["技术", "AI"],
    keywords=["Claude", "Code"],
    abstract="这是一篇关于 Claude Code 的文章",
    summary_one_sentence="介绍 Claude Code 的核心功能",
    summary_100_words="详细介绍...",
    search_strategy="keyword",
)

# 1. 保存到 Markdown
md_path = md_store.save(entry)

# 2. 保存到 SQLite（元数据 + FTS5）
sql_store.save_entry(entry)

# 3. 保存到向量索引
await vec_store.add_entry(entry)
```

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
路径: .data/vault/2026/02/20260216-claude-code-intro.md
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
```

**Schema 设计**:

核心表:
- `knowledge_items`: 知识条目主表（knowledge_id 主键）
- `tags`: 标签表（tag_id 主键）
- `keywords`: 关键词表（keyword_id 主键）
- `knowledge_item_tags`: 多对多关联表
- `knowledge_item_keywords`: 多对多关联表
- `knowledge_fts`: FTS5 虚拟表（全文检索）

详细设计: [docs/refactor/SQLite_Schema完整规范.md](../../docs/refactor/SQLite_Schema完整规范.md)

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

    def save_index(self):
        """保存索引到磁盘"""

    def load_index(self):
        """从磁盘加载索引"""
```

**索引文件**:
- `doc_vectors.idx`: 文档级向量索引（hnswlib 格式）
- `chunk_vectors.idx`: 分块级向量索引
- `doc_vectors_metadata.json`: knowledge_id <-> vector_id 映射
- `chunk_vectors_metadata.json`: chunk_id <-> vector_id 映射

**Embedding 提供者**: OpenAI `text-embedding-3-small` (1536 维)

---

## 关键依赖与配置

### 依赖库

- `python-frontmatter`: YAML Front Matter 解析
- `sqlite3`: SQLite 数据库（Python 标准库）
- `hnswlib`: HNSW 向量索引
- `jieba`: 中文分词（FTS5 查询）

### 配置项

在 `config/config.yaml` 中:

```yaml
storage:
  vault_dir: ".data/vault"          # Markdown 存储目录
  db_path: ".data/db/knowledge_vault.db"  # SQLite 数据库路径
  vector_index_dir: ".data/vectors" # 向量索引目录
  log_dir: ".data/logs"             # 日志目录
  tmp_dir: ".data/tmp"              # 临时文件目录
```

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

详细规范: [docs/refactor/Entry数据模型规范.md](../../docs/refactor/Entry数据模型规范.md)

---

## 关键设计

### 1. 双重存储策略

```
主存储层: Markdown + YAML Front Matter
    ↓ (同步写入)
索引层: SQLite (元数据 + FTS5)
    ↓ (异步写入)
语义层: hnswlib (向量索引)
```

**优势**:
- Markdown 可直接编辑、Git 版本控制
- SQLite 快速查询、FTS5 全文检索
- 向量索引支持语义搜索
- 所有辅助存储可从 Markdown 重建

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

详见: [docs/issues/SCHEMA_MIGRATION_PLAN.md](../../docs/issues/SCHEMA_MIGRATION_PLAN.md)

---

## 测试与质量

### 单元测试

```bash
# 运行存储层测试
python -m pytest tests/unit/test_storage_*.py -v

# 测试 Markdown 存储
python -m pytest tests/unit/test_markdown_store.py -v

# 测试 SQLite 存储
python -m pytest tests/unit/test_sqlite_store.py -v

# 测试向量存储
python -m pytest tests/unit/test_vector_store.py -v
```

### 集成测试

```bash
# 测试三层存储协同
python -m pytest tests/integration/test_storage_integration.py -v
```

### 数据一致性测试

验证从 Markdown 重建索引的能力:

```python
# 1. 清空 SQLite 和向量索引
sql_store.clear_all()
vec_store.clear_all()

# 2. 从 Markdown 重建
for md_file in md_store.list_all():
    entry = md_store.load(md_file)
    sql_store.save_entry(entry)
    await vec_store.add_entry(entry)

# 3. 验证一致性
assert sql_store.count() == len(md_store.list_all())
```

---

## 常见问题 (FAQ)

### Q1: 如何从 Markdown 完全重建索引？

```python
from src.storage import MarkdownStore, SQLiteStore, VectorStore

md_store = MarkdownStore(Path(".data/vault"))
sql_store = SQLiteStore(Path(".data/db/knowledge_vault.db"))
vec_store = VectorStore(Path(".data/vectors"))

# 重建 SQLite
sql_store.initialize()  # 重建表结构
for md_file in md_store.list_all():
    entry = md_store.load(md_file)
    sql_store.save_entry(entry)

# 重建向量索引
vec_store.clear_all()
for md_file in md_store.list_all():
    entry = md_store.load(md_file)
    await vec_store.add_entry(entry)
```

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
文档级索引大小 ≈ 条目数 × 1536 维 × 4 字节 ≈ 6 KB/条目
分块级索引大小 ≈ 条目数 × 平均分块数 × 6 KB
```

示例: 10,000 条目 → 约 60 MB（文档级）+ 240 MB（分块级）

### Q4: 如何迁移现有数据？

参考迁移指南: [docs/issues/SCHEMA_MIGRATION_PLAN.md](../../docs/issues/SCHEMA_MIGRATION_PLAN.md)

---

## 相关文件清单

### 核心文件

| 文件 | 说明 |
|------|------|
| `__init__.py` | 存储层模块入口 |
| `markdown_store.py` | Markdown 主存储 (Entry 数据类) |
| `sqlite_store.py` | SQLite 索引层 (FTS5) |
| `vector_store.py` | 向量语义层 (hnswlib) |

### 测试文件

| 文件 | 说明 |
|------|------|
| `tests/unit/test_markdown_store.py` | Markdown 存储测试 |
| `tests/unit/test_sqlite_store.py` | SQLite 存储测试 |
| `tests/unit/test_vector_store.py` | 向量存储测试 |
| `tests/integration/test_storage_integration.py` | 三层存储集成测试 |

### 文档

| 文件 | 说明 |
|------|------|
| [docs/refactor/Entry数据模型规范.md](../../docs/refactor/Entry数据模型规范.md) | Entry 数据类规范 |
| [docs/refactor/Storage接口规范.md](../../docs/refactor/Storage接口规范.md) | 三层存储架构设计 |
| [docs/refactor/SQLite_Schema完整规范.md](../../docs/refactor/SQLite_Schema完整规范.md) | 数据库 Schema 详细设计 |
| [docs/design/数据规范.md](../../docs/design/数据规范.md) | Markdown Front Matter 规范 |

---

## 变更记录 (Changelog)

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
**最后更新**: 2026-02-16 01:53:22

*本文档由 Claude Code 自动生成*
