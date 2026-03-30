# SQLite Schema 完整规范

> **版本**: 1.0
> **创建日期**: 2026-02-15
> **文件位置**: `src/storage/sqlite_store.py`
> **作用**: 定义知识库的关系型数据存储结构和索引策略

---

## 📋 数据库表结构

### 1. knowledge_items (主知识表)

**作用**: 存储知识条目的元数据和内容

**CREATE TABLE 语句**:
```sql
CREATE TABLE IF NOT EXISTS knowledge_items (
    knowledge_id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    content TEXT,
    summary_one_sentence TEXT,
    summary_100_words TEXT,
    keywords TEXT,
    tags TEXT,
    outline TEXT,
    source_type TEXT NOT NULL CHECK(source_type IN ('wechat', 'zhihu', 'bilibili', 'webpage', 'article', 'document', 'generic', 'personal', 'ai_chat', 'text', 'test')),
    source_url TEXT UNIQUE,
    search_strategy TEXT CHECK(search_strategy IN ('keyword', 'hybrid', 'vector', 'structured')),
    file_path TEXT NOT NULL UNIQUE,
    word_count INTEGER DEFAULT 0,
    event_time TIMESTAMP,
    published_at TIMESTAMP,
    archived_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
```

#### 字段定义

| 字段名 | 类型 | 约束 | 默认值 | 说明 |
|--------|------|------|--------|------|
| `knowledge_id` | INTEGER | PRIMARY KEY AUTOINCREMENT | 自增 | **主键** - 知识条目唯一标识 |
| `title` | TEXT | NOT NULL | 无 | 标题（原始文本，不分词） |
| `content` | TEXT | 可选 | NULL | 正文内容（原始文本） |
| `summary_one_sentence` | TEXT | 可选 | NULL | 一句话摘要 |
| `summary_100_words` | TEXT | 可选 | NULL | 100 字摘要 |
| `keywords` | TEXT | 可选 | NULL | 关键词（逗号分隔字符串） |
| `tags` | TEXT | 可选 | NULL | 标签（逗号分隔字符串） |
| `outline` | TEXT | 可选 | NULL | 大纲（未使用） |
| `source_type` | TEXT | NOT NULL + CHECK | 无 | 来源类型（枚举约束） |
| `source_url` | TEXT | UNIQUE | NULL | 来源 URL（唯一约束） |
| `search_strategy` | TEXT | CHECK | NULL | 检索策略（枚举约束） |
| `file_path` | TEXT | NOT NULL + UNIQUE | 无 | Markdown 文件路径（唯一约束） |
| `word_count` | INTEGER | DEFAULT 0 | 0 | 字数统计 |
| `event_time` | TIMESTAMP | 可选 | NULL | 真实事件时间；`timeline_of` 首选时间源 |
| `published_at` | TIMESTAMP | 可选 | NULL | 来源发布时间；缺少 `event_time` 时回退使用 |
| `archived_at` | TIMESTAMP | DEFAULT CURRENT_TIMESTAMP | 当前时间 | 归档时间 |
| `updated_at` | TIMESTAMP | DEFAULT CURRENT_TIMESTAMP | 当前时间 | 更新时间（未实现自动更新） |

#### 约束详解

**CHECK 约束**:
```sql
-- source_type 枚举（M5.1 更新：添加 ai_chat, text, test）
CHECK(source_type IN ('wechat', 'zhihu', 'bilibili', 'webpage', 'article', 'document', 'generic', 'personal', 'ai_chat', 'text', 'test'))

-- search_strategy 枚举
CHECK(search_strategy IN ('keyword', 'hybrid', 'vector', 'structured'))
```

**UNIQUE 约束**:
- `source_url` - 确保同一 URL 不会重复归档
- `file_path` - 确保 Markdown 文件路径唯一

#### 索引

```sql
CREATE INDEX IF NOT EXISTS idx_source_url ON knowledge_items(source_url);
CREATE INDEX IF NOT EXISTS idx_source_type ON knowledge_items(source_type);
CREATE INDEX IF NOT EXISTS idx_event_time ON knowledge_items(event_time);
CREATE INDEX IF NOT EXISTS idx_published_at ON knowledge_items(published_at);
CREATE INDEX IF NOT EXISTS idx_archived_at ON knowledge_items(archived_at);
CREATE INDEX IF NOT EXISTS idx_search_strategy ON knowledge_items(search_strategy);
CREATE INDEX IF NOT EXISTS idx_file_path ON knowledge_items(file_path);
```

#### 时间字段语义与回退规则

- `event_time`：条目正文或结构化元数据中表达的真实事件发生时间。
- `published_at`：来源页面/文档的发布时间。
- `archived_at`：系统把内容落库的时间。
- `timeline_of` 的排序取值优先级固定为 `event_time > published_at > archived_at`。
- 当前 schema 仅存储单个规范化时间值，不做多值时间数组建模；多时间冲突由上游抽取阶段先行裁决。

---

### 2. content_chunks (长文本分块表)

**作用**: 存储长文本的分块数据（用于向量检索）

**CREATE TABLE 语句**:
```sql
CREATE TABLE IF NOT EXISTS content_chunks (
    chunk_id INTEGER PRIMARY KEY AUTOINCREMENT,
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

#### 字段定义

| 字段名 | 类型 | 约束 | 默认值 | 说明 |
|--------|------|------|--------|------|
| `chunk_id` | INTEGER | PRIMARY KEY AUTOINCREMENT | 自增 | **主键** - 分块唯一标识 |
| `knowledge_id` | INTEGER | NOT NULL + FOREIGN KEY | 无 | 关联的知识条目 ID |
| `chunk_index` | INTEGER | NOT NULL + UNIQUE | 无 | 分块序号（从 0 开始） |
| `chunk_text` | TEXT | NOT NULL | 无 | 分块内容 |
| `context_before` | TEXT | 可选 | NULL | 前文上下文（未使用） |
| `context_after` | TEXT | 可选 | NULL | 后文上下文（未使用） |
| `section_title` | TEXT | 可选 | NULL | 所属章节标题（未使用） |
| `created_at` | TIMESTAMP | DEFAULT CURRENT_TIMESTAMP | 当前时间 | 创建时间 |

#### 约束详解

**FOREIGN KEY**:
```sql
FOREIGN KEY (knowledge_id) REFERENCES knowledge_items(knowledge_id) ON DELETE CASCADE
```
- 级联删除：删除知识条目时自动删除所有分块

**UNIQUE 约束**:
```sql
UNIQUE(knowledge_id, chunk_index)
```
- 确保同一条目的分块序号唯一

#### 索引

```sql
CREATE INDEX IF NOT EXISTS idx_knowledge_chunk ON content_chunks(knowledge_id, chunk_index);
CREATE INDEX IF NOT EXISTS idx_knowledge_id ON content_chunks(knowledge_id);
```

---

### 3. tags (标签表)

**作用**: 存储标签及其统计信息

**CREATE TABLE 语句**:
```sql
CREATE TABLE IF NOT EXISTS tags (
    tag_id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    tag_group TEXT,
    count INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
```

#### 字段定义

| 字段名 | 类型 | 约束 | 默认值 | 说明 |
|--------|------|------|--------|------|
| `tag_id` | INTEGER | PRIMARY KEY AUTOINCREMENT | 自增 | **主键** - 标签唯一标识 |
| `name` | TEXT | NOT NULL + UNIQUE | 无 | 标签名称（唯一） |
| `tag_group` | TEXT | 可选 | NULL | 标签分组（未使用） |
| `count` | INTEGER | DEFAULT 0 | 0 | 引用计数 |
| `created_at` | TIMESTAMP | DEFAULT CURRENT_TIMESTAMP | 当前时间 | 创建时间 |

#### 约束详解

**UNIQUE 约束**:
```sql
CREATE UNIQUE INDEX IF NOT EXISTS idx_tag_name ON tags(name);
```
- 确保标签名称唯一

#### 索引

```sql
CREATE UNIQUE INDEX IF NOT EXISTS idx_tag_name ON tags(name);
CREATE INDEX IF NOT EXISTS idx_tag_group ON tags(tag_group);
```

---

### 4. knowledge_tags (知识-标签关联表)

**作用**: 多对多关系表，关联知识条目和标签

**CREATE TABLE 语句**:
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

#### 字段定义

| 字段名 | 类型 | 约束 | 默认值 | 说明 |
|--------|------|------|--------|------|
| `knowledge_id` | INTEGER | NOT NULL + FOREIGN KEY + PRIMARY KEY | 无 | 知识条目 ID |
| `tag_id` | INTEGER | NOT NULL + FOREIGN KEY + PRIMARY KEY | 无 | 标签 ID |
| `created_at` | TIMESTAMP | DEFAULT CURRENT_TIMESTAMP | 当前时间 | 创建时间 |

#### 约束详解

**复合主键**:
```sql
PRIMARY KEY (knowledge_id, tag_id)
```
- 确保同一条目和标签的关联唯一

**FOREIGN KEY**:
```sql
FOREIGN KEY (knowledge_id) REFERENCES knowledge_items(knowledge_id) ON DELETE CASCADE
FOREIGN KEY (tag_id) REFERENCES tags(tag_id) ON DELETE CASCADE
```
- 双向级联删除

#### 索引

```sql
CREATE INDEX IF NOT EXISTS idx_kt_knowledge_id ON knowledge_tags(knowledge_id);
CREATE INDEX IF NOT EXISTS idx_kt_tag_id ON knowledge_tags(tag_id);
```

---

### 5. video_timestamps (视频时间轴表)

**作用**: 存储视频内容的时间戳和章节信息（Phase 2 功能）

**CREATE TABLE 语句**:
```sql
CREATE TABLE IF NOT EXISTS video_timestamps (
    timestamp_id INTEGER PRIMARY KEY AUTOINCREMENT,
    knowledge_id INTEGER NOT NULL,
    timestamp_seconds INTEGER NOT NULL,
    segment_text TEXT NOT NULL,
    chapter_title TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (knowledge_id) REFERENCES knowledge_items(knowledge_id) ON DELETE CASCADE,
    UNIQUE(knowledge_id, timestamp_seconds)
)
```

#### 字段定义

| 字段名 | 类型 | 约束 | 默认值 | 说明 |
|--------|------|------|--------|------|
| `timestamp_id` | INTEGER | PRIMARY KEY AUTOINCREMENT | 自增 | **主键** - 时间戳唯一标识 |
| `knowledge_id` | INTEGER | NOT NULL + FOREIGN KEY | 无 | 关联的知识条目 ID |
| `timestamp_seconds` | INTEGER | NOT NULL + UNIQUE | 无 | 时间戳（秒） |
| `segment_text` | TEXT | NOT NULL | 无 | 该时间点的文本内容 |
| `chapter_title` | TEXT | 可选 | NULL | 章节标题 |
| `created_at` | TIMESTAMP | DEFAULT CURRENT_TIMESTAMP | 当前时间 | 创建时间 |

#### 约束详解

**UNIQUE 约束**:
```sql
UNIQUE(knowledge_id, timestamp_seconds)
```
- 确保同一视频的时间戳唯一

**FOREIGN KEY**:
```sql
FOREIGN KEY (knowledge_id) REFERENCES knowledge_items(knowledge_id) ON DELETE CASCADE
```
- 级联删除

#### 索引

```sql
CREATE INDEX IF NOT EXISTS idx_knowledge_timestamp ON video_timestamps(knowledge_id, timestamp_seconds);
CREATE INDEX IF NOT EXISTS idx_vt_knowledge_id ON video_timestamps(knowledge_id);
```

---

## 🔍 FTS5 全文搜索虚拟表

### knowledge_items_fts (FTS5 虚拟表)

**作用**: 提供中文全文检索能力（BM25 算法）

**CREATE VIRTUAL TABLE 语句**:
```sql
CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_items_fts USING fts5(
    title,
    summary_100_words,
    keywords,
    tags,
    content=knowledge_items,
    content_rowid=knowledge_id
)
```

#### 配置说明

| 配置项 | 值 | 说明 |
|--------|------|------|
| `content` | `knowledge_items` | 指定实际存储数据的表 |
| `content_rowid` | `knowledge_id` | 指定行 ID 对应的列 |

#### 索引字段

- `title` - 标题（分词后）
- `summary_100_words` - 摘要（分词后）
- `keywords` - 关键词（分词后）
- `tags` - 标签（分词后）

**注意**: 不索引 `content` 字段（正文），以提高性能和减少存储

---

### 自动同步触发器

#### INSERT 触发器

```sql
CREATE TRIGGER IF NOT EXISTS knowledge_items_ai AFTER INSERT ON knowledge_items BEGIN
    INSERT INTO knowledge_items_fts(rowid, title, summary_100_words, keywords, tags)
    VALUES (new.knowledge_id, new.title, new.summary_100_words, new.keywords, new.tags);
END
```

#### DELETE 触发器

```sql
CREATE TRIGGER IF NOT EXISTS knowledge_items_ad AFTER DELETE ON knowledge_items BEGIN
    DELETE FROM knowledge_items_fts WHERE rowid = old.knowledge_id;
END
```

#### UPDATE 触发器

```sql
CREATE TRIGGER IF NOT EXISTS knowledge_items_au AFTER UPDATE ON knowledge_items BEGIN
    DELETE FROM knowledge_items_fts WHERE rowid = old.knowledge_id;
    INSERT INTO knowledge_items_fts(rowid, title, summary_100_words, keywords, tags)
    VALUES (new.knowledge_id, new.title, new.summary_100_words, new.keywords, new.tags);
END
```

**实际实现细节**:
- 触发器插入原始数据后，`insert_entry()` 方法会删除并重新插入**分词后的数据**
- 分词通过 `TextProcessor.prepare_fts5_data()` 完成（使用 jieba）

---

## 🗂️ 数据库配置

### PRAGMA 设置

```sql
PRAGMA foreign_keys = ON;  -- 启用外键约束
```

### Row Factory

```python
conn.row_factory = sqlite3.Row  # 使用字典模式访问列
```

### 完整性检查

```sql
PRAGMA foreign_key_check;  -- 检查外键约束
PRAGMA integrity_check;    -- 检查数据库完整性
```

---

## 📊 命名规范

### 主键命名

**当前规范**: 使用 **领域特定名称** 作为主键

| 表名 | 主键列名 | 类型 |
|------|---------|------|
| `knowledge_items` | `knowledge_id` | INTEGER |
| `content_chunks` | `chunk_id` | INTEGER |
| `tags` | `tag_id` | INTEGER |
| `video_timestamps` | `timestamp_id` | INTEGER |

**优点**:
- 语义清晰，一眼就知道是什么 ID
- 避免在 JOIN 时列名冲突

**缺点**:
- 代码中需要记住每个表的主键名
- 部分代码可能期望 `id` 字段（兼容性问题）

---

### 外键命名

**规范**: 外键列名直接使用被引用表的主键名

**示例**:
```sql
-- content_chunks 引用 knowledge_items
FOREIGN KEY (knowledge_id) REFERENCES knowledge_items(knowledge_id)

-- knowledge_tags 引用 knowledge_items 和 tags
FOREIGN KEY (knowledge_id) REFERENCES knowledge_items(knowledge_id)
FOREIGN KEY (tag_id) REFERENCES tags(tag_id)
```

---

### 时间戳字段命名

**规范**: 使用 `_at` 后缀表示时间戳

| 字段名 | 说明 |
|--------|------|
| `created_at` | 创建时间 |
| `updated_at` | 更新时间 |
| `archived_at` | 归档时间 |

**数据类型**: `TIMESTAMP`

**默认值**: `DEFAULT CURRENT_TIMESTAMP`

---

### 索引命名

**规范**: `idx_{表名缩写}_{列名}` 或 `idx_{列名}_{列名}`

**示例**:
```sql
CREATE INDEX IF NOT EXISTS idx_source_url ON knowledge_items(source_url);
CREATE INDEX IF NOT EXISTS idx_kt_knowledge_id ON knowledge_tags(knowledge_id);
CREATE INDEX IF NOT EXISTS idx_knowledge_chunk ON content_chunks(knowledge_id, chunk_index);
```

---

## ⚠️ 已知问题和改进建议

### 问题 1: `knowledge_id` vs `id` 命名不一致

**问题描述**:
- 数据库使用 `knowledge_id` 作为主键
- 部分代码可能期望 `id` 字段
- 向量存储 API 使用 `doc_id` 作为参数名

**影响范围**: 中等 - 需要在代码中保持一致性

**优先级**: 中

**修复计划**: 已有迁移计划（见 `docs/history/issues/SCHEMA_MIGRATION_PLAN.md`）

---

### 问题 2: `keywords` 字段数据类型不一致

**问题描述**:
- 数据库存储为 `TEXT`（逗号分隔字符串）
- Entry 对象中为 `list`
- 需要在 `insert_entry()` 中手动转换

**当前代码**:
```python
# src/storage/sqlite_store.py:274
",".join(entry.tags) if isinstance(entry.tags, list) else entry.tags
```

**影响范围**: 低 - 已有转换逻辑，但代码不优雅

**优先级**: 低

**建议修复**:
- 方案 A: 统一为逗号分隔字符串（Entry 中也使用 `str`）
- 方案 B: 数据库使用 JSON 类型存储列表

---

### 问题 3: `updated_at` 字段未自动更新

**问题描述**:
- `updated_at` 字段存在但没有自动更新机制
- SQLite 不支持 `ON UPDATE CURRENT_TIMESTAMP`

**影响范围**: 低 - 目前没有更新知识条目的功能

**优先级**: 低

**建议修复**: 实现 UPDATE 触发器自动更新 `updated_at`

**示例代码**:
```sql
CREATE TRIGGER IF NOT EXISTS update_timestamp AFTER UPDATE ON knowledge_items
FOR EACH ROW
BEGIN
    UPDATE knowledge_items SET updated_at = CURRENT_TIMESTAMP WHERE knowledge_id = NEW.knowledge_id;
END;
```

---

### 问题 4: `outline` 字段未使用

**问题描述**:
- `knowledge_items` 表定义了 `outline` 字段
- Entry 数据类没有对应字段
- 从未被写入或读取

**影响范围**: 低 - 仅占用存储空间

**优先级**: 低

**建议**: 移除该字段或实现大纲提取功能

---

### 问题 5: FTS5 分词逻辑复杂

**问题描述**:
- 触发器自动插入原始数据
- 代码手动删除并重新插入分词后的数据
- 逻辑绕弯，容易出错

**当前实现** (`src/storage/sqlite_store.py:285-307`):
```python
# 删除触发器自动插入的原始数据
conn.execute("DELETE FROM knowledge_items_fts WHERE rowid = ?", (knowledge_id,))

# 插入分词后的数据
conn.execute("""
    INSERT INTO knowledge_items_fts(rowid, title, summary_100_words, keywords, tags)
    VALUES (?, ?, ?, ?, ?)
""", (knowledge_id, fts5_data["title"], fts5_data["summary_100_words"], ...))
```

**影响范围**: 中 - 性能开销（额外的 DELETE + INSERT）

**优先级**: 中

**建议修复**:
- 方案 A: 禁用触发器，手动管理 FTS5 同步
- 方案 B: 在触发器中调用自定义分词函数（SQLite 扩展）

---

### ✅ 问题 6: `source_type` 枚举值与 Entry 不匹配（已修复）

**修复时间**: 2026-02-16 (M5.1)

**数据库枚举（修复后）**:
```sql
CHECK(source_type IN ('wechat', 'zhihu', 'bilibili', 'webpage', 'article', 'document', 'generic', 'personal', 'ai_chat', 'text', 'test'))
```

**Entry 实际使用**:
- `wechat` ✅
- `zhihu` ✅
- `ai_chat` ✅ (已添加)
- `text` ✅ (已添加)
- `test` ✅ (已添加，用于测试)

**修复方法**:
- 修改 `src/storage/sqlite_store.py:91` 的 CHECK 约束
- 删除现有测试数据库，让系统重建（SQLite 不支持直接修改 CHECK 约束）

**验证状态**: ✅ 所有单元测试通过 (122/122)

---

## 📝 Schema 迁移记录

### 当前版本: 1.0

**初始版本** - 包含以下表：
- `knowledge_items`
- `content_chunks`
- `tags`
- `knowledge_tags`
- `video_timestamps`
- `knowledge_items_fts` (FTS5 虚拟表)

### 待迁移问题

参见 `docs/history/issues/SCHEMA_MIGRATION_PLAN.md`：
- [ ] 统一主键命名（`knowledge_id` vs `id`）
- [x] 更新 `source_type` 枚举值（已完成 M5.1）
- [ ] 移除未使用的 `outline` 字段
- [ ] 添加 `updated_at` 自动更新触发器

---

## 🎯 总结

### 设计优点

✅ 清晰的外键约束和级联删除
✅ 合理的索引设计
✅ FTS5 全文检索支持
✅ 领域驱动的命名规范（`knowledge_id`, `tag_id`）
✅ 完整性检查机制
✅ `source_type` 枚举完整（已包含 ai_chat, text, test）

### 需要改进

⚠️ FTS5 分词逻辑复杂（触发器 + 手动覆盖）
⚠️ 部分字段未使用（`outline`, `updated_at`）
⚠️ 数据类型不一致（`keywords` 列表 vs 字符串）
⚠️ 缺少 Schema 版本管理机制

---

**文档维护者**: AI Agent (猫娘 幽浮喵)
**最后更新**: 2026-02-16
