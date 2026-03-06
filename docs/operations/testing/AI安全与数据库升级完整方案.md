# AI 安全与数据库升级完整方案

> **完成时间**: 2026-02-16
> **版本**: v1.0
> **作者**: 幽浮酱 ฅ'ω'ฅ

---

## 概述

本文档汇总了两个重要功能的完整实现：

1. **AI 调试安全测试功能** - 确保 AI 协作时自动使用测试环境
2. **数据库增量升级方案** - 实现老数据库平滑升级到新版本

---

## 第一部分：AI 调试安全测试功能 🤖

### 背景与目标

**问题**: AI 助手（如 Claude Code）在协作时可能误操作生产数据

**解决方案**: 通过安全规则、环境检测和自动提示，确保 AI 默认使用测试环境

---

### 实现内容

#### 1. AI 安全规则文档

**文件**: [.ai-safety-rules.md](../../../.ai-safety-rules.md)

**核心内容**:
- 🔒 **生产数据保护**: AI 禁止直接操作 `.data/` 目录
- ✅ **强制使用测试环境**: 所有测试必须使用 `run-test.ps1`
- ⚠️ **备份要求**: 重要变更前必须提示备份
- 📋 **典型场景处理**: 测试新功能、修改 Schema、运行集成测试等

**关键场景示例**:

```powershell
# ❌ 危险：直接操作生产环境
python -m src.main archive "https://example.com"

# ✅ 安全：使用测试环境
.\scripts\run-test.ps1 archive "https://example.com"
```

---

#### 2. Claude 忽略文件配置

**文件**: [.claudeignore](../../../.claudeignore)

**内容**:
```
# 生产数据目录（AI 不应直接读取或修改）
.data/db/
.data/vault/
.data/vectors/

# 备份数据（AI 不应修改）
.data-backup/

# 敏感配置文件（AI 不应读取 API Keys）
.env
```

**作用**: 防止 AI 意外访问敏感数据

---

#### 3. 环境检测脚本

**文件**: [scripts/check-environment.ps1](../../../scripts/check-environment.ps1)

**功能**:
- 检测当前使用的数据库路径（生产 vs 测试）
- 显示数据库统计信息
- 给出安全建议

**使用方式**:
```powershell
.\scripts\check-environment.ps1
```

**输出示例**:
```
========================================
 PKV 环境检测
========================================

[环境变量模式]
  DB_PATH = .data-test/db/knowledge_vault.db
  状态: ✓ 测试环境

========================================
数据库统计:

  总条目数: 1
  数据库大小: 24 KB

========================================
✓ 当前为测试环境，可以安全测试

提示: 使用 .\scripts\run-test.ps1 运行命令
```

---

### 使用流程

#### AI 协作时的标准流程

1. **检测环境**
   ```powershell
   .\scripts\check-environment.ps1
   ```

2. **如果需要测试，切换到测试环境**
   ```powershell
   # 创建测试配置（如果没有）
   if (!(Test-Path .env.test)) { copy .env.test.example .env.test }

   # 使用测试环境执行命令
   .\scripts\run-test.ps1 <command>
   ```

3. **验证生产数据未受影响**
   ```powershell
   python -m src.main stats  # 应显示原有数据量
   ```

---

## 第二部分：数据库增量升级方案 📊

### 背景与目标

**问题**: 新功能上线后，老数据库无法使用，需要完整重建（不现实）

**解决方案**: 实现增量迁移机制，自动检测版本并执行升级脚本

---

### 实现内容

#### 1. 迁移管理器

**文件**: [src/storage/migration_manager.py](../../../src/storage/migration_manager.py)

**核心功能**:
- ✅ 获取当前数据库版本
- ✅ 扫描待执行的迁移脚本
- ✅ 自动执行迁移（支持自动备份）
- ✅ 版本号比较（支持语义化版本）

**API 示例**:
```python
from src.storage.migration_manager import MigrationManager
from pathlib import Path

# 初始化
manager = MigrationManager(
    db_path=Path(".data/db/knowledge_vault.db"),
    migrations_dir=Path("scripts/migrations")
)

# 获取当前版本
version = manager.get_current_version()  # "1.0.0"

# 检查待迁移脚本
pending = manager.get_pending_migrations()  # [(version, path), ...]

# 执行所有待迁移脚本
manager.apply_all_pending(auto_backup=True)
```

---

#### 2. 迁移脚本

**目录**: [scripts/migrations/](../../../scripts/migrations/)

**已创建的迁移**:

| 版本 | 脚本文件 | 说明 |
|------|----------|------|
| 1.0.0 | 001_initial_schema.sql | M1 初始 Schema + 版本管理表 |
| 1.1.0 | 002_add_cli_tables.sql | M6 CLI 统计表 + 用户偏好设置 |

**脚本结构示例**:
```sql
-- Migration: 002_add_cli_tables.sql
-- Version: 1.1.0
-- Description: 新增 CLI 使用统计表

-- ========================================
-- 向上迁移（Upgrade）
-- ========================================

CREATE TABLE IF NOT EXISTS cli_command_history (
    command_id INTEGER PRIMARY KEY AUTOINCREMENT,
    command TEXT NOT NULL,
    ...
);

INSERT INTO schema_version (version, description)
VALUES ('1.1.0', '新增 CLI 使用统计表（M6）');

-- ========================================
-- 向下迁移（Rollback）
-- ========================================

-- DROP TABLE IF EXISTS cli_command_history;
-- DELETE FROM schema_version WHERE version = '1.1.0';
```

---

#### 3. 命令行迁移工具

**文件**: [scripts/migrate.py](../../../scripts/migrate.py)

**用法**:
```powershell
# 交互式升级（推荐）
python scripts/migrate.py

# 自动升级（无需确认）
python scripts/migrate.py --auto

# 仅检查待迁移脚本（不执行）
python scripts/migrate.py --dry-run

# 查看当前版本
python scripts/migrate.py --version

# 跳过自动备份（不推荐）
python scripts/migrate.py --auto --no-backup
```

**输出示例**:
```
======================================================================
 PKV 数据库迁移工具
======================================================================

数据库路径: E:\gitee\personal-knowledge-vault\.data\db\knowledge_vault.db
迁移脚本目录: E:\gitee\personal-knowledge-vault\scripts\migrations

当前版本: 1.0.0

待执行的迁移: 1

  • 002_add_cli_tables.sql
    版本: v1.1.0
    说明: 新增 CLI 使用统计表（M6 CLI 入口与交互界面）

======================================================================
 ⚠️  警告：数据库迁移操作
======================================================================

  即将执行数据库 Schema 变更！
  每个迁移前会自动备份到 .data-backup/

是否继续执行迁移？(输入 YES 继续，其他任意键取消): YES

======================================================================
 开始迁移
======================================================================

✓ 迁移成功: 002_add_cli_tables.sql

======================================================================
 迁移完成 ✓
======================================================================

成功执行 1 个迁移脚本

建议: 运行以下命令验证数据完整性
  python -m src.main stats
  python -m src.main list --limit 10
```

---

#### 4. Schema 版本管理表

**表结构**:
```sql
CREATE TABLE schema_version (
    version_id INTEGER PRIMARY KEY AUTOINCREMENT,
    version TEXT NOT NULL UNIQUE,           -- 版本号（如 "1.0.0"）
    description TEXT,                       -- 变更描述
    applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    applied_by TEXT                         -- 执行者（可选）
);
```

**数据示例**:
```sql
SELECT * FROM schema_version;

-- 输出:
-- version_id | version | description                    | applied_at
-- -----------|---------|--------------------------------|-------------------
-- 1          | 1.0.0   | 初始 Schema - M1 基础设施层      | 2026-02-14 12:00:00
-- 2          | 1.1.0   | 新增 CLI 使用统计表（M6）        | 2026-02-16 15:00:00
```

---

### 使用流程

#### 完整升级流程

1. **检查当前版本**
   ```powershell
   python scripts/migrate.py --version
   # 输出: 当前数据库版本: 1.0.0
   ```

2. **查看待迁移脚本（Dry-run）**
   ```powershell
   python scripts/migrate.py --dry-run
   ```

3. **在测试环境验证迁移**
   ```powershell
   # 设置测试环境
   $env:DB_PATH = ".data-test/db/knowledge_vault.db"

   # 执行迁移
   python scripts/migrate.py --auto

   # 验证结果
   .\scripts\run-test.ps1 stats
   .\scripts\run-test.ps1 list
   ```

4. **备份生产数据**
   ```powershell
   .\scripts\backup-data.ps1 -Message "v1.1.0 升级前备份"
   ```

5. **执行生产环境迁移**
   ```powershell
   python scripts/migrate.py
   # 输入 YES 确认
   ```

6. **验证升级结果**
   ```powershell
   python -m src.main stats
   python -m src.main list --limit 10
   ```

---

## 目录结构总览

```
personal-knowledge-vault/
├── .ai-safety-rules.md               # AI 安全规则
├── .claudeignore                      # Claude 忽略文件
├── .env.test.example                  # 测试环境配置模板
│
├── scripts/
│   ├── check-environment.ps1          # 环境检测脚本
│   ├── migrate.py                     # 迁移命令行工具
│   ├── run-test.ps1                   # 测试环境执行脚本
│   ├── backup-data.ps1                # 数据备份脚本
│   ├── restore-data.ps1               # 数据恢复脚本
│   └── migrations/
│       ├── README.md                  # 迁移脚本说明
│       ├── 001_initial_schema.sql     # v1.0.0 初始 Schema
│       └── 002_add_cli_tables.sql     # v1.1.0 CLI 统计表
│
├── src/
│   └── storage/
│       └── migration_manager.py       # 迁移管理器
│
└── docs/
    ├── 测试环境隔离指南.md              # 测试环境完整文档
    ├── 测试环境快速开始.md              # 测试环境入门
    ├── 测试环境演示.md                  # 完整演示脚本
    ├── 数据库迁移指南.md                # 迁移完整文档
    └── AI安全与数据库升级完整方案.md    # 本文件
```

---

## 关键特性

### AI 安全测试功能

| 特性 | 实现方式 | 效果 |
|------|---------|------|
| **环境隔离** | `.env.test` + `DB_PATH` 覆盖 | 测试数据写入 `.data-test/` |
| **自动提示** | `.ai-safety-rules.md` 指导 AI | AI 默认推荐测试环境 |
| **数据保护** | `.claudeignore` 限制访问 | AI 无法读取生产数据 |
| **环境检测** | `check-environment.ps1` | 快速验证当前环境 |

### 数据库升级功能

| 特性 | 实现方式 | 效果 |
|------|---------|------|
| **版本管理** | `schema_version` 表 | 记录每次升级历史 |
| **增量升级** | 按序执行待迁移脚本 | 支持跨版本升级（如 1.0.0 → 1.5.0） |
| **自动备份** | 每次迁移前调用 `backup-data.ps1` | 数据安全有保障 |
| **幂等性** | `IF NOT EXISTS` / `IF EXISTS` | 重复执行安全 |
| **测试验证** | 测试环境先验证 | 降低生产风险 |

---

## 使用场景

### 场景 1：AI 协作开发新功能

```powershell
# 1. AI 建议检测环境
.\scripts\check-environment.ps1

# 2. AI 推荐在测试环境测试
.\scripts\run-test.ps1 archive "https://example.com"

# 3. AI 验证生产数据未受影响
python -m src.main stats
```

**关键点**:
- ✅ AI 自动使用测试环境
- ✅ 生产数据完全隔离
- ✅ 测试完成后可清理测试数据

---

### 场景 2：版本升级（如 M6 上线）

```powershell
# 1. 在测试环境验证升级
$env:DB_PATH = ".data-test/db/knowledge_vault.db"
python scripts/migrate.py --auto

# 2. 备份生产数据
.\scripts\backup-data.ps1 -Message "M6 升级前备份"

# 3. 执行生产环境升级
python scripts/migrate.py

# 4. 验证
python -m src.main stats
```

**关键点**:
- ✅ 测试环境先验证
- ✅ 自动备份数据
- ✅ 增量升级，老数据兼容

---

### 场景 3：开发新功能需要新表

```powershell
# 1. 创建迁移脚本
# scripts/migrations/003_add_feature_table.sql

# 2. 在测试环境验证
$env:DB_PATH = ".data-test/db/knowledge_vault.db"
python scripts/migrate.py --auto

# 3. 测试新功能
.\scripts\run-test.ps1 <new-feature-command>

# 4. 应用到生产环境
python scripts/migrate.py
```

**关键点**:
- ✅ 迁移脚本版本化管理
- ✅ 测试环境充分验证
- ✅ 生产环境平滑升级

---

## 最佳实践

### ✅ 推荐做法

1. **AI 协作时**
   - 始终通过 `check-environment.ps1` 检测环境
   - 默认使用 `run-test.ps1` 执行命令
   - 重要变更前提示用户备份

2. **数据库升级时**
   - 测试环境先验证迁移脚本
   - 生产环境升级前必须备份
   - 使用语义化版本号（主.次.修订）

3. **编写迁移脚本时**
   - 使用 `IF NOT EXISTS` / `IF EXISTS`（幂等性）
   - 新增列时设置默认值（向后兼容）
   - 提供回滚 SQL（可选）

### ⚠️ 注意事项

1. **AI 协作**
   - AI 不应读取 `.env`（包含 API Keys）
   - AI 不应直接修改 `.data/` 目录
   - AI 不应跳过安全检查

2. **数据库升级**
   - 避免删除列（SQLite 不支持且会丢失数据）
   - 避免修改列类型（需要重建表，风险高）
   - 大数据量迁移时使用批量处理

3. **版本控制**
   - `.env.test` 不应提交到 Git（已忽略）
   - `.data-test/` 不应提交到 Git（已忽略）
   - `.data-backup/` 不应提交到 Git（已忽略）

---

## 验证清单

### AI 安全测试功能验证

- [ ] `.ai-safety-rules.md` 文件存在
- [ ] `.claudeignore` 文件存在
- [ ] `check-environment.ps1` 可正常运行
- [ ] `run-test.ps1` 可正确隔离测试数据
- [ ] AI 协作时默认推荐测试环境

### 数据库升级功能验证

- [ ] `migration_manager.py` 可正常导入
- [ ] `migrate.py` 可正确识别待迁移脚本
- [ ] 测试环境可成功执行迁移
- [ ] 迁移前自动备份生效
- [ ] 迁移后数据完整性验证通过

---

## 故障排查

### 问题 1：AI 意外操作生产数据

**检查**:
```powershell
.\scripts\check-environment.ps1
```

**解决**:
```powershell
# 从备份恢复
.\scripts\restore-data.ps1
```

---

### 问题 2：迁移执行失败

**检查**:
```powershell
# 查看日志
cat .data/logs/pkv.log | Select-String "migration"
```

**解决**:
```powershell
# 1. 从备份恢复
.\scripts\restore-data.ps1

# 2. 修复迁移脚本
# 3. 在测试环境重新验证
$env:DB_PATH = ".data-test/db/knowledge_vault.db"
python scripts/migrate.py --auto

# 4. 重新执行生产环境迁移
```

---

### 问题 3：版本号混乱

**检查**:
```sql
sqlite3 .data/db/knowledge_vault.db
SELECT * FROM schema_version ORDER BY version_id DESC;
.quit
```

**解决**:
```sql
-- 删除错误的版本记录
DELETE FROM schema_version WHERE version = 'wrong_version';

-- 手动插入正确版本
INSERT INTO schema_version (version, description)
VALUES ('correct_version', '修正版本号');
```

---

## 相关文档

### AI 安全测试

- [.ai-safety-rules.md](../../../.ai-safety-rules.md) - AI 安全规则
- [测试环境隔离指南.md](./测试环境隔离指南.md) - 完整测试环境文档
- [测试环境快速开始.md](./测试环境快速开始.md) - 3 分钟入门
- [测试环境演示.md](./测试环境演示.md) - 完整演示脚本

### 数据库升级

- [数据库迁移指南.md](../数据库迁移指南.md) - 完整迁移流程文档
- [scripts/migrations/README.md](../../../scripts/migrations/README.md) - 迁移脚本说明
- [维护指南.md](../维护指南.md) - 数据库日常维护

---

## 总结

浮浮酱已经完成了两个重要功能的实现喵～ o(*￣︶￣*)o

### AI 安全测试功能 ✅

- ✅ AI 协作时自动使用测试环境
- ✅ 生产数据完全隔离保护
- ✅ 环境检测和自动提示
- ✅ 完整的安全规则文档

### 数据库增量升级 ✅

- ✅ 版本管理和增量迁移
- ✅ 自动备份和回滚机制
- ✅ 幂等性和向后兼容
- ✅ 测试验证和生产应用

现在主人可以：
- 🤖 **安全地与 AI 协作开发**（不担心误操作生产数据）
- 📊 **平滑地升级数据库**（老数据自动兼容新功能）
- 💾 **随时备份恢复数据**（数据安全有保障）

---

**文档版本**: v1.0
**作者**: 幽浮酱 ฅ'ω'ฅ
**完成时间**: 2026-02-16

*数据安全，开发愉快喵～*
