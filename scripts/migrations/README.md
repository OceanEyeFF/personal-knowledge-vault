# 数据库迁移脚本目录

> **用途**: 存放所有数据库 Schema 迁移脚本 ฅ'ω'ฅ

---

## 目录结构

```
scripts/migrations/
├── README.md                      # 本文件
├── 001_initial_schema.sql         # v1.0.0 初始 Schema（M1）
├── 002_add_cli_tables.sql         # v1.1.0 新增 CLI 表（M6）
└── (未来的迁移脚本...)
```

---

## 迁移脚本命名规范

- **格式**: `{序号}_{描述}.sql`
- **序号**: 3 位数字（001, 002, 003...）
- **描述**: 蛇形命名法（snake_case），简洁明了

**示例**:
```
001_initial_schema.sql            # 初始 Schema
002_add_cli_tables.sql            # 新增 CLI 相关表
003_optimize_fts_indexes.sql      # 优化 FTS 索引
004_add_user_preferences.sql      # 新增用户偏好设置
```

---

## 迁移脚本模板

每个迁移脚本必须包含以下部分：

```sql
-- Migration: {文件名}
-- Version: {版本号}
-- Description: {变更描述}
-- Author: {作者}
-- Date: {日期}

-- ========================================
-- 向上迁移（Upgrade）
-- ========================================

-- 1. Schema 变更
CREATE TABLE IF NOT EXISTS new_table (...);
ALTER TABLE existing_table ADD COLUMN new_column TEXT DEFAULT '';

-- 2. 索引变更
CREATE INDEX IF NOT EXISTS idx_new ON new_table(field);

-- 3. 数据迁移（如果需要）
UPDATE existing_table SET new_column = 'default' WHERE new_column IS NULL;

-- 4. 更新版本号
INSERT INTO schema_version (version, description)
VALUES ('{版本号}', '{变更描述}');

-- ========================================
-- 向下迁移（Rollback）- 可选
-- ========================================

-- 如果需要回滚，执行以下 SQL：
-- DROP TABLE IF EXISTS new_table;
-- DELETE FROM schema_version WHERE version = '{版本号}';
```

---

## 使用方法

### 1. 检查待迁移脚本

```powershell
# 查看当前数据库版本和待迁移脚本
python scripts/migrate.py --dry-run
```

### 2. 执行迁移

```powershell
# 交互式迁移（推荐）
python scripts/migrate.py

# 自动迁移（用于 CI/CD）
python scripts/migrate.py --auto
```

### 3. 在测试环境验证

```powershell
# 设置测试环境
$env:DB_PATH = ".data-test/db/knowledge_vault.db"

# 执行迁移
python scripts/migrate.py --auto

# 验证结果
.\scripts\run-test.ps1 stats
```

---

## 版本管理

### 当前版本

| 版本号 | 脚本文件 | 说明 | 日期 |
|--------|----------|------|------|
| 1.0.0 | 001_initial_schema.sql | M1 初始 Schema | 2026-02-14 |
| 1.1.0 | 002_add_cli_tables.sql | M6 CLI 统计表 | 2026-02-16 |

### 版本号规范

使用语义化版本号：`主版本.次版本.修订版本`

- **主版本**: 不兼容的 Schema 变更（如删除表）
- **次版本**: 向后兼容的功能新增（如新增表）
- **修订版本**: 向后兼容的问题修复（如索引优化）

---

## 编写指南

### ✅ 推荐做法

1. **幂等性**: 使用 `IF NOT EXISTS`/`IF EXISTS`
2. **向后兼容**: 新增列时设置默认值
3. **事务性**: 使用 `BEGIN`/`COMMIT`（对于复杂迁移）
4. **文档化**: 详细注释每个变更
5. **可回滚**: 提供回滚 SQL

### ❌ 避免

1. ❌ 删除列（SQLite 不支持，且会丢失数据）
2. ❌ 修改列类型（需要重建表，风险高）
3. ❌ 不检查存在性（重复执行会报错）
4. ❌ 硬编码数据（使用配置或参数）

---

## 测试要求

每个迁移脚本提交前必须：

1. **在测试环境验证**
   ```powershell
   $env:DB_PATH = ".data-test/db/knowledge_vault.db"
   python scripts/migrate.py --auto
   ```

2. **测试幂等性**（重复执行应成功）
   ```powershell
   python scripts/migrate.py --auto  # 第一次
   python scripts/migrate.py --auto  # 第二次（应该跳过已应用的迁移）
   ```

3. **验证数据完整性**
   ```powershell
   .\scripts\run-test.ps1 stats
   .\scripts\run-test.ps1 list
   ```

4. **测试应用功能**
   ```powershell
   .\scripts\run-test.ps1 archive "https://example.com"
   .\scripts\run-test.ps1 search "测试"
   ```

---

## 相关文档

- [数据库迁移指南.md](../../docs/数据库迁移指南.md) - 完整迁移流程文档
- [维护指南.md](../../docs/维护指南.md) - 数据库日常维护

---

**最后更新**: 2026-02-16
**维护者**: 幽浮酱 ฅ'ω'ฅ
