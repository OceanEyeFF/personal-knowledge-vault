# Storage 接口规范（简化版）

> **版本**: 1.0
> **创建日期**: 2026-02-15
> **文件位置**: `src/storage/`
> **作用**: 知识库存储层的核心接口

---

## 📦 三大存储后端

| 存储后端 | 文件 | 作用 | 数据格式 |
|---------|------|------|---------|
| **MarkdownStore** | `markdown_store.py` | 人类可读的主存储 | YAML Front Matter + Markdown |
| **SQLiteStore** | `sqlite_store.py` | 元数据索引和 FTS5 全文搜索 | SQLite 数据库 |
| **VectorStore** | `vector_store.py` | 向量索引（语义搜索） | hnswlib 索引文件 |

---

## 🔧 MarkdownStore

### 核心方法

#### save(entry, subdir) → Path

```python
def save(self, entry: Entry, subdir: Optional[str] = None) -> Path:
    """保存知识条目为 Markdown 文件"""
```

**输入**:
- `entry: Entry` - 知识条目对象
- `subdir: str` - 子目录（默认使用 `entry.source_type`）

**输出**:
- `Path` - 保存的文件路径

**文件命名**:
```python
# 1. 安全化标题
safe_title = TextProcessor.sanitize_filename(entry.title)

# 2. 生成文件名
filename = f"{safe_title}.md"

# 3. 如果文件已存在，添加时间戳
if file_path.exists():
    filename = f"{safe_title}-{timestamp}.md"
```

**文件结构**:
```markdown
---
title: "示例标题"
source_type: "wechat"
tags: ["AI", "知识管理"]
...
---

# 正文内容

这里是 Markdown 格式的正文...
```

---

#### load(file_path) → Entry

```python
def load(self, file_path: Path) -> Entry:
    """加载 Markdown 文件"""
```

**解析过程**:
1. 读取文件（使用 `frontmatter` 库）
2. 提取 YAML Front Matter → 元数据
3. 提取 Markdown Body → 正文
4. 构建 Entry 对象

---

## 🔧 SQLiteStore

### 核心方法

#### initialize()

```python
def initialize(self):
    """初始化数据库 Schema"""
```

**执行步骤**:
1. 创建主表（`knowledge_items`, `tags`, `content_chunks` 等）
2. 创建索引
3. 创建 FTS5 虚拟表和触发器
4. 验证完整性

---

#### insert_entry(entry, file_path) → int

```python
def insert_entry(self, entry: Entry, file_path: str) -> int:
    """插入知识条目"""
```

**返回值**: `knowledge_id` (主键)

**特殊处理**:
```python
# 1. 插入主表（原始数据）
cursor.execute("INSERT INTO knowledge_items (...) VALUES (...)", (...))
knowledge_id = cursor.lastrowid

# 2. 手动更新 FTS5 表（分词后的数据）
fts5_data = self.text_processor.prepare_fts5_data(...)
conn.execute("DELETE FROM knowledge_items_fts WHERE rowid = ?", (knowledge_id,))
conn.execute("INSERT INTO knowledge_items_fts (...) VALUES (...)", (...))

# 3. 插入标签关联
self._insert_tags(conn, knowledge_id, entry.tags)
```

**注意**:
- ⚠️ FTS5 同步逻辑：先删除触发器自动插入的原始数据，再插入分词后的数据

---

#### query_by_id(knowledge_id) → Dict

```python
def query_by_id(self, knowledge_id: int) -> Optional[Dict[str, Any]]:
    """根据 ID 查询知识条目"""
```

**返回值**: 字典形式的条目数据（`sqlite3.Row` 转换）

---

## 🔧 VectorStore

### 核心方法

#### add_doc_vector(knowledge_id, vector) → int

```python
def add_doc_vector(self, knowledge_id: int, vector: np.ndarray) -> int:
    """添加文档级向量"""
```

**输入**:
- `knowledge_id: int` - 知识条目 ID
- `vector: np.ndarray` - 当前 Embedding 模型真实维度的向量

**输出**:
- `int` - 内部向量 ID（hnswlib label）

**数据流**:
```python
# 1. 生成内部 ID
internal_id = self._next_id
self._next_id += 1

# 2. 添加到 hnswlib 索引
self.index.add_items(vector, internal_id)

# 3. 保存双向映射
self._id_mapping[internal_id] = knowledge_id
self._reverse_mapping[knowledge_id] = internal_id
```

---

#### add_entry(entry, knowledge_id) → List[int]

```python
def add_entry(self, entry: Entry, knowledge_id: int) -> List[int]:
    """添加 Entry（自动分块向量化）"""
```

**处理流程**:
1. 提取正文内容
2. 使用 `Embedder.embed_chunks()` 分块向量化
3. 逐个添加分块向量到索引
4. 返回向量 ID 列表

---

#### search(query_vector, top_k) → List[Tuple[int, float]]

```python
def search(self, query_vector: np.ndarray, top_k: int = 10) -> List[Tuple[int, float]]:
    """向量检索"""
```

**返回值**: `[(knowledge_id, similarity_score), ...]`

**实现**:
```python
# 1. hnswlib 近似最近邻搜索
labels, distances = self.index.knn_query(query_vector, k=top_k)

# 2. 转换内部 ID → knowledge_id
results = []
for label, distance in zip(labels[0], distances[0]):
    knowledge_id = self._id_mapping.get(label)
    similarity = 1.0 / (1.0 + distance)  # 距离 → 相似度
    results.append((knowledge_id, similarity))
```

---

## 🔗 三者协作

### StoreStep 中的使用

```python
# 1. Markdown 存储
file_path = markdown_store.save(entry, subdir="wechat")

# 2. SQLite 存储
knowledge_id = sqlite_store.insert_entry(entry, file_path)

# 3. 向量存储
vector_ids = vector_store.add_entry(entry, knowledge_id)

# 返回结果
return {
    "file_path": file_path,
    "knowledge_id": knowledge_id,
    "vector_ids": vector_ids
}
```

### 数据关联

```
Entry (对象)
    ↓
┌─────────────┬──────────────┬─────────────┐
↓             ↓              ↓             ↓
Markdown      SQLite        Vector       ID映射
.md 文件      knowledge_id  internal_id  双向
(主存储)      (元数据)      (向量)       关联
```

---

## ⚠️ 关键问题

### 问题 1: `knowledge_id` 命名不一致

**问题**: 数据库使用 `knowledge_id`，但部分代码期望 `id`

**优先级**: 中

---

### 问题 2: `keywords` 字段类型不一致

**问题**: Entry 中为 `list`，SQLite 中为逗号分隔字符串

**优先级**: 低

---

### 问题 3: 向量存储缺少持久化

**问题**: ID 映射需要手动保存/加载

**优先级**: 中

---

## 🎯 总结

### 三层存储架构

| 层次 | 后端 | 特点 |
|------|------|------|
| **主存储** | MarkdownStore | 人类可读、Git 友好 |
| **索引层** | SQLiteStore | 快速查询、FTS5 全文搜索 |
| **语义层** | VectorStore | 向量检索、语义搜索 |

### 核心方法

| 后端 | 核心方法 | 返回值 |
|------|---------|--------|
| MarkdownStore | `save()` | `file_path` |
| SQLiteStore | `insert_entry()` | `knowledge_id` |
| VectorStore | `add_entry()` | `vector_ids` |

---

**文档维护者**: AI Agent
**最后更新**: 2026-02-15
