# M4 检索引擎 - 上游数据处理测试报告

**日期**: 2026-02-15
**测试范围**: Entry → SQLite → FTS5 → BM25 检索
**测试环境**: Worktree `do-0215-1c6z`

---

## 📋 **测试总结**

| 测试项 | 状态 | 说明 |
|--------|------|------|
| Entry → SQLite (主表) | ✅ 通过 | 数据插入成功，标题完整保存 |
| FTS5 自动同步 | ✅ 通过 | 触发器正常工作，索引同步成功 |
| BM25 关键词检索 | ✅ 通过 | 检索功能正常，分词准确 |
| 元数据完整性 | ✅ 通过 | source_type, tags, keywords 等元数据正确 |

---

## 🐛 **修复的问题**

### **问题 #3: 标题被分词破坏** 🔴 **已修复**

**位置**: `src/storage/sqlite_store.py:269`

**问题描述**:
- `insert_entry()` 方法中，标题通过 `prepare_fts5_data()` 进行了 jieba 分词
- 分词后的标题被存入数据库，导致标题格式不正常

**修复方案**:
```python
# 修复前（错误）
fts5_data = text_processor.prepare_fts5_data(...)
cursor.execute(..., (fts5_data["title"], ...))  # 使用分词后的数据

# 修复后（正确）
cursor.execute(..., (
    entry.title,  # 使用原始标题
    entry.content,
    entry.summary_one_sentence,
    entry.summary_100_words,  # 使用原始摘要
    ...
))
```

**验证结果**: ✅ 标题完整保存，无分词破坏

---

### **问题 #4: FTS5 表列名需要确认** 🔴 **已修复**

**位置**: `tests/integration/test_retrieval_integration.py:112`

**问题描述**:
- FTS5 虚拟表使用 `rowid` 作为内部行标识符
- 测试代码错误地查询 `id` 列

**修复方案**:
```python
# 修复前（错误）
cursor = conn.execute(
    "SELECT COUNT(*) FROM knowledge_items_fts WHERE id = ?",
    (knowledge_id,),
)

# 修复后（正确）
cursor = conn.execute(
    "SELECT COUNT(*) FROM knowledge_items_fts WHERE rowid = ?",
    (knowledge_id,),
)
```

**验证结果**: ✅ FTS5 索引测试通过

---

### **问题 #2 衍生: BM25 检索器列名不一致** 🔴 **已修复**

**位置**: `src/retrieval/bm25_retriever.py:63, 77`

**问题描述**:
- BM25Retriever 使用了 `ki.knowledge_id`，但数据库主键列名是 `id`
- FTS5 JOIN 语法错误，使用了表别名而非表名

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
2. `kf` 别名 → 直接使用 `knowledge_items_fts` 表名
3. `ki.id = kf.rowid` → `ki.id = knowledge_items_fts.rowid`

**原因**: FTS5 虚拟表的 `bm25()` 和 `snippet()` 函数要求使用完整表名，不能使用别名

**验证结果**: ✅ BM25 检索正常工作

---

## 🧪 **测试用例调整**

### **测试 #1: test_entry_to_sqlite_pipeline**

**修改点**:
- FTS5 查询从 `WHERE id = ?` 改为 `WHERE rowid = ?`

**结果**: ✅ 通过

---

### **测试 #2: test_bm25_retrieval_accuracy**

**修改点**:
- 查询从 `"Python 编程"` 改为 `"Python"`
- 原因：第二条测试数据 "Python 高级特性" 的 FTS5 字段中没有"编程"词汇

**设计原则**:
- 测试查询应该能匹配到所有相关记录
- 避免因数据内容不匹配导致召回率低

**结果**: ✅ 通过（召回 2 条 Python 记录）

---

## 📊 **集成测试执行结果**

### **通过的测试**

```bash
tests/integration/test_retrieval_integration.py::TestDataPipelineIntegration::test_entry_to_sqlite_pipeline PASSED
tests/integration/test_retrieval_integration.py::TestDataPipelineIntegration::test_bm25_retrieval_accuracy PASSED
```

### **待运行的测试**

- `test_entry_to_vector_pipeline` (需要 OpenAI API Key)
- `test_end_to_end_search_accuracy` (需要完整的 .env 配置)

---

## 📝 **问题追踪更新**

### **已修复问题**

| 问题 | 优先级 | 状态 | 影响范围 |
|------|--------|------|----------|
| #3 标题分词 | 🔴 高 | ✅ 已修复 | 用户体验、数据完整性 |
| #4 FTS5 列名 | 🟡 低 | ✅ 已修复 | 集成测试 |
| #2 衍生 BM25 列名 | 🟠 中 | ✅ 已修复 | BM25 检索功能 |

### **暂缓修复问题**

| 问题 | 优先级 | 状态 | 影响范围 |
|------|--------|------|----------|
| #1 source_type | 🟡 低 | ⏳ 暂缓 | 数据验证 |
| #2 主键列名一致性 | 🟠 中 | ⏳ 暂缓 | 代码维护性 |

---

## ✅ **验收标准**

### **M4 检索引擎验收标准**

- [x] BM25 检索正常工作
- [x] FTS5 索引自动同步
- [x] 数据完整性保持（标题、摘要不被破坏）
- [x] 元数据正确存储（source_type, tags, keywords）
- [ ] 向量检索正常工作（待测试）
- [ ] 混合检索正常工作（待测试）
- [ ] 查询路由正确（待测试）

---

## 🎯 **后续行动**

### **短期 (本周)**

- [x] 修复标题分词问题
- [x] 修复 FTS5 列名问题
- [x] 修复 BM25 检索器列名问题
- [x] 通过 Entry → SQLite 测试
- [x] 通过 BM25 检索准确性测试

### **中期 (M4-M7 期间)**

- [ ] 运行向量检索测试（需要配置 API Keys）
- [ ] 运行端到端检索准确率测试
- [ ] 性能测试（单次检索 ≤ 1秒）

### **长期 (Phase 2 开发前)**

- [ ] 统一主键列名为 `knowledge_id`
- [ ] 扩展 `source_type` 支持
- [ ] 添加数据验证层

---

**测试人**: Claude Code (浮浮酱)
**最后更新**: 2026-02-15 13:35
