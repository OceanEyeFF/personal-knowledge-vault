# 数据库迁移脚本目录

> **用途**: 存放所有数据库 Schema 迁移脚本 ฅ'ω'ฅ

---

## 目录结构

```
scripts/migrations/
├── README.md                      # 本文件
├── 001_initial_schema.sql         # v1.0.0 初始 Schema（M1）
├── 002_add_cli_tables.sql         # v1.1.0 新增 CLI 表（M6）
├── 004_add_chat_sessions.sql      # v1.1.1 新增 AI 对话会话表（M12）
├── 005_add_review_system.sql      # v1.1.2 新增审核系统表
├── 006_add_relations_foundation.sql # v1.2.0 新增关系层基础表（Phase A）
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

未设置路径覆盖时，以下裸命令会读取或修改生产 `.data/`，仅供用户明确授权后的生产维护；AI 不执行。这些命令只说明 `migrate.py` 的人工维护接口，不是可复制的 Agent/默认自动化流程。

> **当前自动化边界**：`run-test.ps1` 会显式拒绝 `scripts/migrate.py` 并以 exit 2 结束；FT7 的 Direct Python 入口只覆盖仓库内 `-m`/`.py` 的 CAT-0 离线任务，不会绕过这条迁移 denylist，也不构成真实数据授权。默认自动化迁移验证仅运行使用临时 SQLite 的单元/集成测试。真实快照迁移尚未执行，仍需先完成 FT5，并同时满足 U1/G8 与用户明确授权；届时只能迁移一次性可写 clone，原始快照始终只读。

### 1. 检查待迁移脚本

```powershell
# 人工 API 示例（非 Agent/默认自动化流程）：查看当前数据库版本和待迁移脚本
.\scripts\run-windows.ps1 python scripts/migrate.py --dry-run
```

### 2. 执行迁移

```powershell
# 人工 API 示例：交互式迁移
.\scripts\run-windows.ps1 python scripts/migrate.py

# 人工 API 示例：无交互生产迁移（仅限完成门禁并获授权的用户维护流程）
.\scripts\run-windows.ps1 python scripts/migrate.py --auto

# 人工 API 示例：健康检查（只读）
.\scripts\run-windows.ps1 python scripts/migrate.py --health-check
```

### 3. 在测试环境验证

```powershell
# 默认自动化只运行由 pytest 创建并回收临时 SQLite 的迁移测试。
.\scripts\run-test.ps1 -Direct -Command @("pytest", "tests/unit/test_migration_health_check.py", "tests/unit/test_migration_manager_additional.py", "tests/unit/test_migration_manager_runtime.py", "tests/unit/test_migration_manager_versions.py", "tests/integration/test_relations_migration.py", "tests/integration/test_review_migration.py")
```

---

## 版本管理

### 当前版本

| 版本号 | 脚本文件 | 说明 | 日期 |
|--------|----------|------|------|
| 1.0.0 | 001_initial_schema.sql | M1 初始 Schema | 2026-02-14 |
| 1.1.0 | 002_add_cli_tables.sql | M6 CLI 统计表 | 2026-02-16 |
| 1.1.1 | 004_add_chat_sessions.sql | M12 AI 对话会话表 | 2026-02-20 |
| 1.1.2 | 005_add_review_system.sql | 审核系统表 | 2026-02-28 |
| 1.2.0 | 006_add_relations_foundation.sql | Phase A 关系层基础表 | 2026-03-09 |

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

每个迁移脚本提交前，默认自动化必须在 pytest 创建的一次性临时 SQLite 上验证。
空库测试只能证明 fresh install，不能证明历史库升级。真实快照升级兼容验证当前尚未执行；
只有完成 FT5、满足 U1/G8 且获得用户明确授权后，才可从只读快照制作一次性可写 clone 进行验证。

1. **验证迁移管理器与临时旧版 fixture**

   ```powershell
   .\scripts\run-test.ps1 -Direct -Command @("pytest", "tests/unit/test_migration_manager_additional.py", "tests/unit/test_migration_manager_runtime.py", "tests/unit/test_migration_manager_versions.py", "tests/integration/test_relations_migration.py", "tests/integration/test_review_migration.py")
   ```

2. **验证迁移管理器重复启动安全**

   在临时 SQLite 测试中第二次调用管理器；它只验证“已登记版本不会再次执行”，不能作为 SQL 幂等性证据。

   如果迁移声明 SQL 本身可重复执行，必须另写专门测试，在一次性临时数据库上
   直接执行目标迁移 SQL 两次，并断言第二次成功及 Schema/数据未重复；不能用
   `MigrationManager` 的 pending-version 短路替代。

3. **验证数据完整性**

   在临时 SQLite 测试内对比升级前后行数、关系、FTS、索引与 schema version，而不只是命令退出码。

4. **测试应用功能**

   仅对已填充的普通 disposable 测试数据根运行纯离线 CLI 回归；这不等同于真实快照迁移验收：

   ```powershell
   .\scripts\run-test.ps1 -DataRoot .data-test\migration stats
   .\scripts\run-test.ps1 -DataRoot .data-test\migration list
   .\scripts\run-test.ps1 -DataRoot .data-test\migration search "测试" --strategy bm25
   ```

---

## 相关文档

- [数据库迁移指南.md](../../docs/数据库迁移指南.md) - 完整迁移流程文档
- [维护指南.md](../../docs/维护指南.md) - 数据库日常维护

---

**最后更新**: 2026-02-16
**维护者**: 幽浮酱 ฅ'ω'ฅ
