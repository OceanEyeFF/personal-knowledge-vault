# 数据库 Schema 迁移计划

**日期**: 2026-02-15
**目标**: 修复问题 #1 (source_type 扩展) 和 #2 (列名统一为 knowledge_id)

---

## 📋 **迁移目标**

### **问题 #1: 扩展 source_type 允许值**

**当前约束**:
```sql
CHECK(source_type IN ('wechat', 'zhihu', 'bilibili', 'generic', 'personal'))
```

**新约束** (添加 'webpage', 'article', 'document'):
```sql
CHECK(source_type IN ('wechat', 'zhihu', 'bilibili', 'webpage', 'article', 'document', 'generic', 'personal'))
```

### **问题 #2: 统一列名为 knowledge_id**

**需要重命名的列**:
- `knowledge_items.id` → `knowledge_items.knowledge_id`
- 所有外键引用也需要更新

**影响范围**:
- 主表: `knowledge_items`
- 外键表: `content_chunks`, `knowledge_tags`, `video_timestamps`
- FTS5 表: `knowledge_items_fts` (使用 `content_rowid`)

---

## 🔄 **迁移策略**

SQLite 不支持直接 `ALTER TABLE RENAME COLUMN`（在旧版本），需要：

1. 创建新表结构
2. 复制数据
3. 删除旧表
4. 重命名新表

**关键步骤**:
1. ✅ 备份现有数据（如果有）
2. ✅ 创建新 Schema
3. ✅ 更新所有代码引用
4. ✅ 更新 FTS5 触发器
5. ✅ 运行测试验证

---

## 📝 **具体修改**

### **Step 1: 更新 Schema**

**文件**: `src/storage/sqlite_store.py`

#### **1.1 修改 knowledge_items 表**

```sql
CREATE TABLE IF NOT EXISTS knowledge_items (
    knowledge_id INTEGER PRIMARY KEY AUTOINCREMENT,  -- 改名！
    title TEXT NOT NULL,
    content TEXT,
    summary_one_sentence TEXT,
    summary_100_words TEXT,
    keywords TEXT,
    tags TEXT,
    outline TEXT,
    source_type TEXT NOT NULL CHECK(source_type IN (
        'wechat', 'zhihu', 'bilibili',
        'webpage', 'article', 'document',  -- 新增！
        'generic', 'personal'
    )),
    source_url TEXT UNIQUE,
    search_strategy TEXT CHECK(search_strategy IN ('keyword', 'hybrid', 'vector', 'structured')),
    file_path TEXT NOT NULL UNIQUE,
    word_count INTEGER DEFAULT 0,
    archived_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
```

#### **1.2 修改外键表**

**content_chunks**:
```sql
CREATE TABLE IF NOT EXISTS content_chunks (
    chunk_id INTEGER PRIMARY KEY AUTOINCREMENT,  -- 更明确的名称
    knowledge_id INTEGER NOT NULL,
    chunk_index INTEGER NOT NULL,
    chunk_text TEXT NOT NULL,
    context_before TEXT,
    context_after TEXT,
    section_title TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (knowledge_id) REFERENCES knowledge_items(knowledge_id) ON DELETE CASCADE,
    UNIQUE(knowledge_id, chunk_index)
)
```

**knowledge_tags**:
```sql
CREATE TABLE IF NOT EXISTS knowledge_tags (
    knowledge_id INTEGER NOT NULL,
    tag_id INTEGER NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (knowledge_id, tag_id),
    FOREIGN KEY (knowledge_id) REFERENCES knowledge_items(knowledge_id) ON DELETE CASCADE,
    FOREIGN KEY (tag_id) REFERENCES tags(tag_id) ON DELETE CASCADE
)
```

**tags** 表也需要改名:
```sql
CREATE TABLE IF NOT EXISTS tags (
    tag_id INTEGER PRIMARY KEY AUTOINCREMENT,  -- 改名！
    name TEXT NOT NULL UNIQUE,
    tag_group TEXT,
    count INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
```

**video_timestamps**:
```sql
CREATE TABLE IF NOT EXISTS video_timestamps (
    timestamp_id INTEGER PRIMARY KEY AUTOINCREMENT,  -- 更明确的名称
    knowledge_id INTEGER NOT NULL,
    timestamp_seconds INTEGER NOT NULL,
    segment_text TEXT NOT NULL,
    chapter_title TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (knowledge_id) REFERENCES knowledge_items(knowledge_id) ON DELETE CASCADE,
    UNIQUE(knowledge_id, timestamp_seconds)
)
```

#### **1.3 修改 FTS5 虚拟表**

```sql
CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_items_fts USING fts5(
    title,
    summary_100_words,
    keywords,
    tags,
    content=knowledge_items,
    content_rowid=knowledge_id  -- 改名！
)
```

#### **1.4 修改触发器**

```sql
-- 插入触发器
CREATE TRIGGER IF NOT EXISTS knowledge_items_ai AFTER INSERT ON knowledge_items BEGIN
    INSERT INTO knowledge_items_fts(rowid, title, summary_100_words, keywords, tags)
    VALUES (new.knowledge_id, new.title, new.summary_100_words, new.keywords, new.tags);
END

-- 删除触发器
CREATE TRIGGER IF NOT EXISTS knowledge_items_ad AFTER DELETE ON knowledge_items BEGIN
    DELETE FROM knowledge_items_fts WHERE rowid = old.knowledge_id;
END

-- 更新触发器
CREATE TRIGGER IF NOT EXISTS knowledge_items_au AFTER UPDATE ON knowledge_items BEGIN
    DELETE FROM knowledge_items_fts WHERE rowid = old.knowledge_id;
    INSERT INTO knowledge_items_fts(rowid, title, summary_100_words, keywords, tags)
    VALUES (new.knowledge_id, new.title, new.summary_100_words, new.keywords, new.tags);
END
```

---

### **Step 2: 更新代码引用**

#### **2.1 SQLiteStore**

**查询语句**:
- `SELECT * FROM knowledge_items WHERE id = ?` → `WHERE knowledge_id = ?`
- `SELECT id FROM tags WHERE name = ?` → `SELECT tag_id FROM tags WHERE name = ?`
- 所有 `cursor.lastrowid` 返回的都是正确的 ID

#### **2.2 BM25Retriever**

**查询语句**:
```python
SELECT
    ki.knowledge_id,  # 保持不变
    ki.title,
    ...
FROM knowledge_items ki
JOIN knowledge_items_fts ON ki.knowledge_id = knowledge_items_fts.rowid
WHERE knowledge_items_fts MATCH ?
```

#### **2.3 VectorRetriever**

**查询语句**:
```python
SELECT
    knowledge_id,  # 保持不变
    title,
    ...
FROM knowledge_items
WHERE knowledge_id = ?
```

#### **2.4 其他检索器**

HybridRetriever 和 QueryRouter 不直接执行 SQL，无需修改。

---

### **Step 3: 更新索引**

**文件**: `src/storage/sqlite_store.py` 的 `_create_indexes()`

```python
indexes = [
    # knowledge_items 索引
    "CREATE INDEX IF NOT EXISTS idx_source_url ON knowledge_items(source_url)",
    "CREATE INDEX IF NOT EXISTS idx_source_type ON knowledge_items(source_type)",
    "CREATE INDEX IF NOT EXISTS idx_archived_at ON knowledge_items(archived_at)",
    "CREATE INDEX IF NOT EXISTS idx_search_strategy ON knowledge_items(search_strategy)",
    "CREATE INDEX IF NOT EXISTS idx_file_path ON knowledge_items(file_path)",
    # content_chunks 索引
    "CREATE INDEX IF NOT EXISTS idx_knowledge_chunk ON content_chunks(knowledge_id, chunk_index)",
    "CREATE INDEX IF NOT EXISTS idx_knowledge_id ON content_chunks(knowledge_id)",
    # tags 索引
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_tag_name ON tags(name)",
    "CREATE INDEX IF NOT EXISTS idx_tag_group ON tags(tag_group)",
    # knowledge_tags 索引
    "CREATE INDEX IF NOT EXISTS idx_kt_knowledge_id ON knowledge_tags(knowledge_id)",
    "CREATE INDEX IF NOT EXISTS idx_kt_tag_id ON knowledge_tags(tag_id)",
    # video_timestamps 索引
    "CREATE INDEX IF NOT EXISTS idx_knowledge_timestamp ON video_timestamps(knowledge_id, timestamp_seconds)",
    "CREATE INDEX IF NOT EXISTS idx_vt_knowledge_id ON video_timestamps(knowledge_id)",
]
```

---

## ✅ **验证清单**

### **Schema 验证**
- [ ] knowledge_items 表主键改为 knowledge_id
- [ ] source_type 约束包含新值
- [ ] 所有外键正确引用 knowledge_id
- [ ] FTS5 表使用 content_rowid=knowledge_id
- [ ] 触发器使用正确的列名

### **代码验证**
- [ ] SQLiteStore 所有查询使用 knowledge_id
- [ ] BM25Retriever 查询正确
- [ ] VectorRetriever 查询正确
- [ ] tags 表查询使用 tag_id

### **测试验证**
- [ ] 所有单元测试通过
- [ ] 所有集成测试通过
- [ ] BM25 检索功能正常
- [ ] FTS5 索引同步正常

---

## 🎯 **执行顺序**

1. ✅ 更新 `src/storage/sqlite_store.py` 的 Schema
2. ✅ 更新 `src/retrieval/bm25_retriever.py` 的查询
3. ✅ 更新 `src/retrieval/vector_retriever.py` 的查询
4. ✅ 运行所有测试验证
5. ✅ 更新文档

---

**规划人**: Claude Code (浮浮酱)
**最后更新**: 2026-02-15 14:30
