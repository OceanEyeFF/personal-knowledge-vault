# 上游数据处理问题 - 待修复列表

**日期**: 2026-02-15
**优先级**: 低-中等（非阻塞）

---

## ✅ **已修复的问题**

### 问题 #3: 标题被 jieba 分词破坏 ✅ **已修复**

**修复位置**: `src/storage/sqlite_store.py:269`

**修复内容**:
- 将 `insert_entry()` 方法改为使用原始数据
- 不再对标题、摘要、关键词进行分词
- FTS5 索引由触发器自动同步并分词

**修复前**:
```python
fts5_data = text_processor.prepare_fts5_data(...)
cursor.execute(..., (fts5_data["title"], ...))
```

**修复后**:
```python
cursor.execute(..., (entry.title, ...))  # 使用原始数据
```

**验证结果**: ✅ 标题测试通过，数据完整性恢复

---

## ⏳ **待修复的问题（非紧急）**

### 问题 #1: source_type 约束不完整

**优先级**: 低
**影响**: 轻微（已有 workaround）

**问题描述**:
- SQLite Schema 限制 `source_type IN ('wechat', 'zhihu', 'bilibili', 'generic', 'personal')`
- 文档中可能使用了 `'webpage'` 等其他值

**当前解决方案**:
- 暂时统一使用 `'generic'` 覆盖所有通用网页
- 后续根据需要添加新的 `source_type`

**建议未来优化**:
1. 扩展允许的 `source_type` 列表
2. 添加 Entry 验证逻辑
3. 更新 API 文档

**优先级原因**: 不影响核心功能，有 workaround

---

### 问题 #2: 主键列名不一致 (knowledge_id vs id)

**优先级**: 中等
**影响**: 代码可读性、维护性

**问题描述**:
- SQLite Schema 中主键列名是 `id`
- 代码中多处使用 `knowledge_id` 引用
- FTS5 表的主键列名也可能不一致

**当前状态**: 暂不修复（涉及数据库重建）

**未来修复方案**:
1. **方案 A**：全局重命名列为 `knowledge_id`
   - 需要修改 Schema
   - 需要数据迁移脚本
   - 影响范围大

2. **方案 B**：代码中统一使用 `id`
   - 不需要修改数据库
   - 需要全局搜索替换代码
   - 可读性稍差

3. **方案 C**：使用别名查询
   ```sql
   SELECT id AS knowledge_id FROM knowledge_items
   ```

**推荐**: 方案 A（下一个大版本迁移时统一修复）

**优先级原因**:
- 不影响核心功能
- 修复成本高（数据库重建）
- 可以在下一个 major version 时统一处理

---

### 问题 #4: FTS5 表列名需要确认 ✅ **已解决**

**修复位置**: `tests/integration/test_retrieval_integration.py:112`

**修复内容**:
- FTS5 虚拟表使用 `rowid` 作为内部行标识符
- 不能直接查询 `id` 列（FTS5 虚拟表没有该列）
- 通过 `content_rowid=id` 关联到主表的 `id`

**修复前**:
```python
cursor = conn.execute(
    "SELECT COUNT(*) FROM knowledge_items_fts WHERE id = ?",  # ❌ 错误
    (knowledge_id,),
)
```

**修复后**:
```python
cursor = conn.execute(
    "SELECT COUNT(*) FROM knowledge_items_fts WHERE rowid = ?",  # ✅ 正确
    (knowledge_id,),
)
```

**验证结果**: ✅ FTS5 索引测试通过，触发器同步正常

---

## 📊 **问题统计**

| 问题 | 优先级 | 状态 | 影响范围 |
|------|--------|------|----------|
| #3 标题分词 | 🔴 高 | ✅ 已修复 | 用户体验、数据完整性 |
| #4 FTS5 列名 | 🟡 低 | ✅ 已修复 | 集成测试 |
| #1 source_type | 🟡 低 | ⏳ 暂缓 | 数据验证 |
| #2 列名一致性 | 🟠 中 | ⏳ 暂缓 | 代码维护性 |

---

## 📝 **修复时间线建议**

### 立即 (本周)
- ✅ 修复问题 #3（已完成）
- ✅ 修复问题 #4（已完成）

### 短期 (M4-M7 期间)
- 无紧急问题

### 中期 (Phase 2 开发前)
- 设计数据库迁移方案
- 统一列名为 `knowledge_id`

### 长期 (v2.0)
- 扩展 `source_type` 支持
- 添加数据验证层
- 完善 API 文档

---

**维护者**: Claude Code (浮浮酱)
**最后更新**: 2026-02-15
