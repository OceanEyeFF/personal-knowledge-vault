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
.data/

# 备份数据（AI 不应修改）
.data-backup/

# 本机敏感配置（AI 不应读取或打印）
config/local.yaml

# 兼容性防泄漏兜底；应用配置不再使用 .env
.env*
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
# AI/自动化直接查看测试配置，不裸跑会读取默认数据库统计的检测脚本
.\scripts\run-test.ps1 config show
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
   .\scripts\run-test.ps1 config show
   ```

2. **如果需要测试，切换到测试环境**
   ```powershell
   # run-test.ps1 自动把数据库、Vault、向量、日志和临时目录
   # 全部指向 .data-test/，无需创建或加载 .env.test。
   .\scripts\run-test.ps1 <CLI-subcommand>

   # pytest、Python 脚本等非 CLI 命令必须显式使用 -Direct 和 -Command 字符串数组
   .\scripts\run-test.ps1 -Direct -Command @("<executable>", "<arg1>", "<arg2>")
   ```

3. **确认命令仍指向测试环境**
   ```powershell
   .\scripts\run-test.ps1 config show
   git status --short
   ```

   AI 不读取生产 `.data/` 作前后对比；如需额外确认，由用户自行检查备份或文件时间戳。

应用服务配置统一写入 Git 忽略的 `config/local.yaml`，使用
`ai.llm.*` 与 `ai.embedding.*` 键。真实服务测试可临时设置
`PKV_RUN_LIVE=1`；它只是运行开关，不承载服务地址、模型或密钥。

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
    db_path=Path(".data-test/migration/db/knowledge_vault.db"),
    migrations_dir=Path("scripts/migrations")
)

# 获取当前版本
version = manager.get_current_version()  # "1.0.0"

# 检查待迁移脚本
pending = manager.get_pending_migrations()  # [(version, path), ...]

# 隔离测试库禁止触发固定读取生产 .data/ 的自动备份
manager.apply_all_pending(auto_backup=False)
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
# AI/自动化默认只在隔离测试路径运行；migrate.py 是非 CLI 命令，必须使用 -Direct 和显式 -Command 数组
.\scripts\run-test.ps1 -Direct -DataRoot .data-test\migration -Command @("python", "scripts\migrate.py", "--version")
.\scripts\run-test.ps1 -Direct -DataRoot .data-test\migration -Command @("python", "scripts\migrate.py", "--dry-run")
# 当前自动备份脚本固定读取生产 .data/；可丢弃测试数据必须禁用自动备份
.\scripts\run-test.ps1 -Direct -DataRoot .data-test\migration -Command @("python", "scripts\migrate.py", "--auto", "--no-backup")
```

未设置测试路径覆盖的裸命令会读取或修改生产 `.data/`。只有用户明确授权生产维护时，才由用户执行；AI 不执行，也不建议跳过备份。

**结果判定**:

- `--dry-run` 应返回退出码 `0`，并只报告当前版本与待执行迁移，不写数据库。
- `--auto --no-backup` 应返回退出码 `0`；该组合不会等待交互确认，也不会调用生产 `.data/` 备份脚本。
- 待执行迁移的数量和文件名取决于旧版测试夹具与当前迁移目录，不固定为某一个脚本。
- 完成后继续使用同一 `.data-test\migration` 根目录运行 `stats` 与 `list`。

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

先明确验证目标：全新的 `.data-test\migration` 空目录只验证 **fresh install**（从零建库到最新版本），不能证明旧库升级兼容。要验证升级兼容性，应先把已脱敏的旧版本数据库夹具复制到 `.data-test\migration\db\knowledge_vault.db`；场景依赖 Vault 或向量数据时，也应把对应测试夹具复制到同一数据根目录。不要让测试命令直接指向生产 `.data/`。

1. **检查当前版本**
   ```powershell
   .\scripts\run-test.ps1 -Direct -DataRoot .data-test\migration -Command @("python", "scripts\migrate.py", "--version")
   # 输出: 当前数据库版本: 1.0.0
   ```

2. **查看待迁移脚本（Dry-run）**
   ```powershell
   .\scripts\run-test.ps1 -Direct -DataRoot .data-test\migration -Command @("python", "scripts\migrate.py", "--dry-run")
   ```

3. **在测试环境验证迁移**

   使用同一个测试数据根目录执行：

   ```powershell
   # migrate.py 不是常规 CLI 子命令，需用显式 -Command 数组；测试时还必须避免固定读取生产 .data/ 的自动备份
   .\scripts\run-test.ps1 -Direct -DataRoot .data-test\migration -Command @("python", "scripts\migrate.py", "--auto", "--no-backup")

   # 验证结果
   .\scripts\run-test.ps1 -DataRoot .data-test\migration stats
   .\scripts\run-test.ps1 -DataRoot .data-test\migration list
   ```

   验证完成后关闭这个测试窗口，不能在其中继续执行生产迁移。

4. **用户明确授权后，在新开的 PowerShell 窗口备份生产数据**

   以下步骤开始操作生产 `.data/`，只由用户执行；AI 到此停止。用户首先确认六项测试路径变量均不存在：

   ```powershell
   $pathVariables = "DATA_DIR", "DB_PATH", "VAULT_DIR", "VECTOR_DIR", "LOG_DIR", "TMP_DIR"
   $remaining = $pathVariables | Where-Object { Test-Path "Env:$_" }
   if ($remaining) {
       throw "检测到路径环境变量，请关闭此窗口并新开 PowerShell：$($remaining -join ', ')"
   }

   .\scripts\backup-data.ps1 -Message "v1.1.0 升级前备份"
   ```

5. **用户明确授权后，在刚刚校验通过的新窗口执行生产环境迁移**
   ```powershell
   # 以下命令只由用户执行；AI 不执行
   .\scripts\run-windows.ps1 python scripts/migrate.py
   # 输入 YES 确认
   ```

6. **由用户验证升级结果**
   ```powershell
   # 以下命令读取生产 .data/，AI 不执行
   .\scripts\run-windows.ps1 python -m src.cli.commands stats
   .\scripts\run-windows.ps1 python -m src.cli.commands list --limit 10
   ```

---

## 目录结构总览

```
personal-knowledge-vault/
├── .ai-safety-rules.md               # AI 安全规则
├── .claudeignore                      # Claude 忽略文件
├── config/
│   ├── config.yaml                    # 可提交的默认配置
│   └── local.yaml                     # Git 忽略的本机应用配置
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
| **环境隔离** | `run-test.ps1` 自动设置完整测试路径 | 测试数据写入 `.data-test/` |
| **自动提示** | `.ai-safety-rules.md` 指导 AI | AI 默认推荐测试环境 |
| **数据保护** | `.claudeignore` 限制访问 | AI 无法读取生产数据 |
| **环境检测** | `check-environment.ps1` | 快速验证当前环境 |

### 数据库升级功能

| 特性 | 实现方式 | 效果 |
|------|---------|------|
| **版本管理** | `schema_version` 表 | 记录每次升级历史 |
| **增量升级** | 按序执行待迁移脚本 | 支持跨版本升级（如 1.0.0 → 1.5.0） |
| **自动备份** | 生产迁移时调用 `backup-data.ps1` | 仅由用户授权的生产 runbook 使用；测试迁移加 `--no-backup` |
| **幂等性** | `IF NOT EXISTS` / `IF EXISTS` | 重复执行安全 |
| **测试验证** | 测试环境先验证 | 降低生产风险 |

---

## 使用场景

### 场景 1：AI 协作开发新功能

```powershell
# 1. AI 确认测试路径
.\scripts\run-test.ps1 config show

# 2. AI 推荐在测试环境测试
.\scripts\run-test.ps1 archive "https://example.com"

# 3. AI 再次确认测试路径；不读取生产 .data/
.\scripts\run-test.ps1 config show
git status --short
```

**关键点**:
- ✅ AI 自动使用测试环境
- ✅ 生产数据完全隔离
- ✅ 测试完成后可清理测试数据

---

### 场景 2：版本升级（如 M6 上线）

在隔离测试数据根目录中验证升级：

```powershell
# 1. migrate.py 为非 CLI 命令，需用显式 -Command 数组；测试迁移禁用会读取生产 .data/ 的自动备份
.\scripts\run-test.ps1 -Direct -DataRoot .data-test\migration -Command @("python", "scripts\migrate.py", "--auto", "--no-backup")
```

测试通过后，如需操作生产数据，必须取得用户明确授权。以下步骤只由用户执行，AI 不执行：

```powershell
$pathVariables = "DATA_DIR", "DB_PATH", "VAULT_DIR", "VECTOR_DIR", "LOG_DIR", "TMP_DIR"
$remaining = $pathVariables | Where-Object { Test-Path "Env:$_" }
if ($remaining) {
    throw "检测到路径环境变量，请关闭此窗口并新开 PowerShell：$($remaining -join ', ')"
}

# 2. 备份生产数据
.\scripts\backup-data.ps1 -Message "M6 升级前备份"

# 3. 以下生产升级与验证只由用户执行；AI 不执行、不读取 .data/
.\scripts\run-windows.ps1 python scripts/migrate.py

# 4. 验证
.\scripts\run-windows.ps1 python -m src.main stats
```

**关键点**:
- ✅ 测试环境先验证
- ✅ 自动备份数据
- ✅ 增量升级，老数据兼容

---

### 场景 3：开发新功能需要新表

先创建迁移脚本，再在隔离测试数据根目录中验证：

```powershell
# 1. 创建迁移脚本
# scripts/migrations/003_add_feature_table.sql

# 2. 在测试环境验证；使用 -Direct 和显式 -Command 数组，并禁用会读取生产 .data/ 的自动备份
.\scripts\run-test.ps1 -Direct -DataRoot .data-test\feature-migration -Command @("python", "scripts\migrate.py", "--auto", "--no-backup")

# 3. 测试新功能
.\scripts\run-test.ps1 -DataRoot .data-test\feature-migration <CLI-subcommand>
```

验证完成后，如需应用到生产环境，必须取得用户明确授权。以下步骤只由用户执行，AI 不执行：

```powershell
$pathVariables = "DATA_DIR", "DB_PATH", "VAULT_DIR", "VECTOR_DIR", "LOG_DIR", "TMP_DIR"
$remaining = $pathVariables | Where-Object { Test-Path "Env:$_" }
if ($remaining) {
    throw "检测到路径环境变量，请关闭此窗口并新开 PowerShell：$($remaining -join ', ')"
}

# 4. 以下生产升级只由用户执行；AI 不执行
.\scripts\run-windows.ps1 python scripts/migrate.py
```

**关键点**:
- ✅ 迁移脚本版本化管理
- ✅ 测试环境充分验证
- ✅ 生产环境平滑升级

---

## 最佳实践

### ✅ 推荐做法

1. **AI 协作时**
   - 使用 `run-test.ps1 config show` 检查测试路径，不裸跑会读取默认数据库的检测命令
   - 默认使用 `run-test.ps1` 执行 CLI；pytest/Python 等非 CLI 命令使用 `-Direct -Command @("executable", "arg", ...)`
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
   - AI 不应读取或打印 `config/local.yaml`（可能包含 API 密钥）
   - 服务地址、模型和密钥不得通过旧环境变量配置
   - AI 不应直接修改 `.data/` 目录
   - AI 不应跳过安全检查

2. **数据库升级**
   - 避免删除列（SQLite 不支持且会丢失数据）
   - 避免修改列类型（需要重建表，风险高）
   - 大数据量迁移时使用批量处理

3. **版本控制**
   - `config/local.yaml` 不应提交到 Git（已忽略）
   - `.env*` 保持在忽略规则中，仅作为防泄漏兜底
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
- [ ] 测试迁移使用 `--no-backup`，未触发生产 `.data/` 备份
- [ ] 生产备份仅由用户在授权 runbook 中单独验证
- [ ] 迁移后数据完整性验证通过

---

## 故障排查

### 问题 1：AI 意外操作生产数据

**检查**:
```powershell
.\scripts\run-test.ps1 config show
```

**解决**:

停止所有可写命令并保留现场。恢复会替换生产 `.data/`，必须取得用户明确授权并由用户按生产恢复 runbook 执行；AI 不运行 `restore-data.ps1`。

---

### 问题 2：迁移执行失败

**检查**:
```powershell
# 只查看隔离测试日志
Get-Content .data-test\migration\logs\pkv.log | Select-String "migration"
```

**解决**:

生产恢复必须由用户明确授权并执行；AI 只在隔离测试路径重新验证迁移：

```powershell
# 1. 修复迁移脚本
# 2. 在测试环境重新验证；使用 -Direct 和显式 -Command 数组，并禁用会读取生产 .data/ 的自动备份
.\scripts\run-test.ps1 -Direct -DataRoot .data-test\migration -Command @("python", "scripts\migrate.py", "--auto", "--no-backup")
```

需要恢复备份或重新执行生产迁移时，必须取得用户明确授权，并由用户重新执行备份、恢复与路径检查；AI 不执行。

---

### 问题 3：版本号混乱

**检查**:
```sql
sqlite3 .data-test/migration/db/knowledge_vault.db
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
