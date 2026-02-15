# M4 检索引擎 - 上游数据处理问题报告

**日期**: 2026-02-15
**版本**: M4 开发阶段
**测试范围**: 数据入库流程 (Entry → SQLite → FTS5 → Vector)

---

## 🐛 **发现的问题**

### **问题 #1: source_type 约束不完整** ⚠️ 中等优先级

**位置**: `src/storage/sqlite_store.py:90`

**问题描述**:
- SQLite Schema 的 `source_type` 字段有 CHECK 约束：
  ```sql
  CHECK(source_type IN ('wechat', 'zhihu', 'bilibili', 'generic', 'personal'))
  ```
- 但测试和文档中可能使用了 `'webpage'` 等其他值

**影响**:
- 集成测试失败
- 可能导致运行时插入错误

**建议修复**:
1. **统一约束列表**：确保所有代码和文档使用一致的 `source_type` 值
2. **添加验证**：在 Entry 创建时验证 `source_type` 是否合法
3. **文档更新**：在 API 文档中明确列出所有允许的值

---

### **问题 #2: 主键列名不一致** ⚠️ 中等优先级

**位置**: `src/storage/sqlite_store.py:82`

**问题描述**:
- SQLite Schema 中主键列名是 `id`
- 但代码中多处使用 `knowledge_id` 来引用
- FTS5 表和索引也可能使用不同的列名

**影响**:
- 代码可读性差
- 容易产生 SQL 错误
- 维护困难

**建议修复**:
1. **统一列名**：全局搜索替换，统一使用 `knowledge_id` 或 `id`
2. **代码审查**：检查所有 SQL 查询中的列名
3. **添加别名**：如果需要兼容，可以在查询中使用 `AS knowledge_id`

**示例**:
```python
# 现状
conn.execute("SELECT * FROM knowledge_items WHERE id = ?", (knowledge_id,))

# 建议改为
conn.execute("SELECT * FROM knowledge_items WHERE knowledge_id = ?", (knowledge_id,))
```

---

### **问题 #3: 标题被分词处理（CRITICAL）** 🔴 **高优先级**

**位置**: `src/storage/sqlite_store.py:269`

**问题描述**:
- `insert_entry()` 方法中，标题通过 `prepare_fts5_data()` 进行了 jieba 分词
- 分词后的标题被存入数据库，导致标题格式不正常

**复现**:
```python
# 原始标题
entry.title = "分布式系统的 CAP 定理"

# 存储后从数据库读取
row["title"] = "分布式系统 的   CAP   定理"
#                       ↑↑  ↑↑   ↑↑  多余空格
```

**影响**:
- **严重影响用户体验**：搜索结果标题显示异常
- **检索结果不准确**：标题匹配失效
- **元数据损坏**：原始标题无法恢复

**根本原因**:
- `prepare_fts5_data()` 的设计初衷是为 FTS5 准备分词数据
- 但 `insert_entry()` 直接将分词后的数据存入主表，而非仅用于 FTS5 表

**建议修复方案**:

**方案 A（推荐）：分离主表和 FTS5 表的数据**
```python
def insert_entry(self, entry: Entry, file_path: str) -> int:
    # 1. 主表存储原始数据（不分词）
    cursor = conn.execute("""
        INSERT INTO knowledge_items (
            title, content, summary_one_sentence, ...
        ) VALUES (?, ?, ?, ...)
    """, (
        entry.title,  # 原始标题，不分词
        entry.content,
        entry.summary_one_sentence,
        ...
    ))

    # 2. FTS5 表由触发器自动同步（已分词）
    # 触发器中调用 prepare_fts5_data() 进行分词
```

**方案 B：修改 prepare_fts5_data() 的行为**
```python
def prepare_fts5_data(self, title: str, summary: str, ...) -> dict:
    """仅对内容分词，不修改标题"""
    return {
        "title": title,  # 保持原样
        "summary_100_words": self.tokenize_chinese(summary),  # 分词
        ...
    }
```

**推荐方案 A**，因为：
1. 符合数据库设计原则（主表存原始数据）
2. FTS5 表已经有自动同步触发器
3. 不需要修改 `prepare_fts5_data()` 的语义

---

## 📊 **测试结果总结**

| 测试项 | 状态 | 说明 |
|--------|------|------|
| Entry → Markdown | ✅ 通过 | 文件保存正常 |
| Entry → SQLite (主表) | ⚠️ 部分通过 | 数据插入成功，但标题被分词 |
| FTS5 自动同步 | ⏳ 待验证 | 修复问题 #3 后重新测试 |
| 元数据完整性 | ⚠️ 部分通过 | source_type 需验证 |

---

## 🎯 **修复优先级**

1. **高优先级** 🔴：问题 #3（标题分词）- 影响用户体验
2. **中等优先级** ⚠️：问题 #2（列名一致性）- 影响代码维护
3. **中等优先级** ⚠️：问题 #1（source_type 约束）- 影响数据验证

---

## 📝 **后续行动**

### 立即行动
- [ ] 修复问题 #3：修改 `insert_entry()` 使用原始标题
- [ ] 重新运行集成测试验证修复效果

### 短期行动
- [ ] 修复问题 #2：统一主键列名为 `knowledge_id`
- [ ] 修复问题 #1：验证和统一 `source_type` 值

### 长期行动
- [ ] 添加数据完整性测试
- [ ] 添加 Entry 验证逻辑
- [ ] 更新 API 文档

---

**报告人**: Claude Code (浮浮酱)
**测试环境**: Worktree `do-0215-1c6z`
