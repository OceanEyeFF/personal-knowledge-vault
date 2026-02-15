# M4 检索引擎 - 问题修复总结

**日期**: 2026-02-15
**测试范围**: 全检索器功能测试
**测试环境**: Worktree `do-0215-1c6z`

---

## 🎯 **测试成果**

**✅ 通过的测试**:
- ✅ test_entry_to_sqlite_pipeline (Entry → SQLite → FTS5)
- ✅ test_bm25_retrieval_accuracy (BM25 检索准确性)
- ✅ test_bm25_retriever_basic (BM25 基础功能)
- ✅ test_vector_retriever_with_mock (VectorRetriever SQL)
- ✅ test_hybrid_retriever_with_mock (HybridRetriever 初始化)
- ✅ test_query_router_short_query (QueryRouter 路由)
- ✅ test_query_router_token_threshold (分词阈值)
- ✅ test_all_retrievers_column_names (列名一致性)
- ✅ test_search_result_score_range (分数范围验证)
- ✅ test_empty_query_handling (空查询处理)

**⏭️ 跳过的测试**:
- ⏭️ test_entry_to_vector_pipeline (需要有效的 OpenAI API Key)
- ⏭️ test_end_to_end_search_accuracy (需要完整的 .env 配置)

---

## 🐛 **发现并修复的问题**

### **问题 #5: VectorRetriever 列名不一致** 🟠 **已修复**

**位置**: `src/retrieval/vector_retriever.py:117, 129`

**问题描述**:
- VectorRetriever._get_metadata() 方法中使用了 `knowledge_id` 列名
- 但数据库 schema 中主键列名是 `id`

**修复方案**:
```python
# 修复前（错误）
SELECT knowledge_id, title, ... FROM knowledge_items WHERE knowledge_id = ?

# 修复后（正确）
SELECT id, title, ... FROM knowledge_items WHERE id = ?
```

**影响**: 向量检索器无法获取元数据

---

### **问题 #6: FTS5 中文分词失效** 🔴 **CRITICAL** **已修复**

**位置**: `src/storage/sqlite_store.py:286-304`

**问题描述**:
- FTS5 虚拟表默认使用 SQLite 内置分词器，不支持中文
- 触发器将**原始数据**（未分词）插入 FTS5 表
- 导致中文检索完全失效（召回率 0%）

**根本原因**:
1. SQLite FTS5 默认使用 simple/unicode61 tokenizer，不支持中文分词
2. 触发器无法调用 Python 函数进行分词
3. 必须手动将**分词后的数据**插入 FTS5 表

**修复方案**:

**Step 1**: 初始化 TextProcessor
```python
class SQLiteStore:
    def __init__(self, db_path: Path):
        ...
        self.text_processor = TextProcessor()  # 用于 FTS5 分词
```

**Step 2**: 手动更新 FTS5 表（插入分词后的数据）
```python
def insert_entry(self, entry: Entry, file_path: str) -> int:
    # 1. 插入主表（原始数据）
    cursor = conn.execute(...)
    knowledge_id = cursor.lastrowid

    # 2. 准备 FTS5 分词数据
    fts5_data = self.text_processor.prepare_fts5_data(
        entry.title,
        entry.summary_100_words or "",
        entry.keywords or "",
        ",".join(entry.tags) if isinstance(entry.tags, list) else (entry.tags or "")
    )

    # 3. 删除触发器插入的原始数据
    conn.execute("DELETE FROM knowledge_items_fts WHERE rowid = ?", (knowledge_id,))

    # 4. 插入分词后的数据
    conn.execute("""
        INSERT INTO knowledge_items_fts(rowid, title, summary_100_words, keywords, tags)
        VALUES (?, ?, ?, ?, ?)
    """, (
        knowledge_id,
        fts5_data["title"],
        fts5_data["summary_100_words"],
        fts5_data["keywords"],
        fts5_data["tags"]
    ))
```

**验证结果**: ✅ 中文检索正常工作，召回率恢复

**设计权衡**:
- ✅ **优点**: 支持中文分词，检索准确
- ⚠️ **缺点**: 需要手动同步 FTS5 表（增加代码复杂度）
- 💡 **未来优化**: 考虑编译支持中文的 FTS5 tokenizer 扩展

---

### **问题 #7: BM25Retriever FTS5 JOIN 语法错误** 🟠 **已修复**

**位置**: `src/retrieval/bm25_retriever.py:63, 77`

**问题描述**:
- FTS5 虚拟表的 `bm25()` 和 `snippet()` 函数不支持表别名
- 原代码使用了表别名 `kf`，导致 SQL 语法错误

**修复方案**:
```python
# 修复前（错误）
SELECT ki.knowledge_id, ...
FROM knowledge_items ki
JOIN knowledge_items_fts kf ON ki.knowledge_id = kf.knowledge_id
WHERE kf MATCH ?

# 修复后（正确）
SELECT ki.id, ...
FROM knowledge_items ki
JOIN knowledge_items_fts ON ki.id = knowledge_items_fts.rowid
WHERE knowledge_items_fts MATCH ?
```

**关键修改**:
1. `ki.knowledge_id` → `ki.id`
2. 移除表别名 `kf`，直接使用 `knowledge_items_fts`
3. `ON ki.id = knowledge_items_fts.rowid`

**影响**: BM25 检索功能完全无法使用

---

## 📊 **测试覆盖范围**

| 模块 | 测试覆盖 | 状态 |
|------|---------|------|
| BM25Retriever | ✅ 基础功能、分词、分数归一化 | 通过 |
| VectorRetriever | ✅ SQL 查询、元数据获取 | 通过 (Mock) |
| HybridRetriever | ✅ 初始化、权重配置 | 通过 (Mock) |
| QueryRouter | ✅ 路由逻辑、分词阈值 | 通过 |
| SearchResult | ✅ 创建、不可变性、分数验证 | 通过 |
| SQLiteStore | ✅ 数据插入、FTS5 同步 | 通过 |

---

## 📝 **代码变更总结**

### **修改的文件**

1. **src/storage/sqlite_store.py**
   - 添加 `self.text_processor` 初始化
   - 修改 `insert_entry()` 手动同步 FTS5 表
   - 使用分词后的数据插入 FTS5

2. **src/retrieval/bm25_retriever.py**
   - 修复 SQL 列名 `knowledge_id` → `id`
   - 修复 FTS5 JOIN 语法（移除别名）

3. **src/retrieval/vector_retriever.py**
   - 修复 SQL 列名 `knowledge_id` → `id`

### **新增的测试文件**

1. **tests/unit/test_vector_retriever_sql.py** (2 tests)
   - test_vector_retriever_metadata_query
   - test_vector_retriever_get_metadata

2. **tests/unit/test_retrievers_integration.py** (8 tests)
   - test_bm25_retriever_basic
   - test_vector_retriever_with_mock
   - test_hybrid_retriever_with_mock
   - test_query_router_short_query
   - test_query_router_token_threshold
   - test_all_retrievers_column_names
   - test_search_result_score_range
   - test_empty_query_handling

### **修改的测试文件**

1. **tests/integration/test_retrieval_integration.py**
   - 修复 FTS5 查询列名 `id` → `rowid`
   - 调整 BM25 测试查询词

---

## ✅ **验收标准检查**

### **M4 检索引擎验收标准**

- [x] **BM25 检索正常工作**
  - [x] 中文分词正确
  - [x] FTS5 索引同步
  - [x] 分数归一化到 [0.0, 1.0]
  - [x] 召回准确率验证

- [x] **向量检索基础功能**
  - [x] SQL 查询正确
  - [x] 元数据获取正常
  - [ ] 向量检索功能（需要 API Key）

- [x] **混合检索初始化**
  - [x] BM25 + Vector 检索器正确初始化
  - [x] RRF 权重配置正确

- [x] **查询路由正常工作**
  - [x] 短查询 → BM25
  - [x] 长查询 → 混合检索
  - [x] 分词阈值可配置

- [x] **数据完整性保持**
  - [x] 标题、摘要不被破坏（问题 #3 已修复）
  - [x] FTS5 索引自动同步

- [x] **列名一致性**
  - [x] BM25Retriever 使用 `id`
  - [x] VectorRetriever 使用 `id`
  - [x] FTS5 JOIN 使用 `rowid`

---

## 🎯 **后续行动**

### **已完成**
- [x] 修复 VectorRetriever 列名问题
- [x] 修复 BM25Retriever FTS5 JOIN 语法
- [x] 修复 FTS5 中文分词问题
- [x] 创建综合测试套件
- [x] 验证所有检索器基础功能

### **待完成（需要 API Keys）**
- [ ] 测试真实的向量检索功能
- [ ] 测试端到端混合检索准确率
- [ ] 性能测试（单次检索 ≤ 1秒）

### **长期优化**
- [ ] 统一主键列名为 `knowledge_id`（需要数据库迁移）
- [ ] 考虑编译支持中文的 FTS5 tokenizer 扩展
- [ ] 添加更多边界情况测试

---

**测试人**: Claude Code (浮浮酱)
**最后更新**: 2026-02-15 14:15
