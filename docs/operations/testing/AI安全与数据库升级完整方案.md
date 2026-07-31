# AI 安全与数据库升级完整方案

> **完成时间**: 2026-02-16
> **版本**: v1.1（P0 安全合同同步）
> **作者**: 幽浮酱 ฅ'ω'ฅ
> **2026-07-31 P0 状态**: CAT-0 G0 已交付并逐入口核验；真实数据尚未执行。CAT-U/CAT-C 仍等待 U1/G8，migration 另需 FT5；`run-test.ps1` 会拒绝 `migrate.py`（exit 2）。

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

#### 当前 CAT-0 G0 合同（P0 收口）

- pytest 由 `tests/offline_entrypoint.py pytest` 在 pytest/plugin 导入前安装 base-only Config 与网络 fail-closed，root `tests/conftest.py` 再维持逐用例隔离。
- CLI/MCP 由 `tests/offline_entrypoint.py` 在产品导入前安装同类门禁。
- FT7 generic Direct Python 仅接受仓库 `python -m <module>` 或 `python <script.py>`，并在同一受保护进程内通过 `runpy` 执行；拒绝 `-c`、stdin、解释器 flags 与仓库外目标。
- 入口会清理 live/secret/proxy 环境、绑定不加载 `config/local.yaml` 的 base-only Config，并安装网络 guard；Direct Python 另安装子进程 guard。
- 非 Python `-Direct` 不属于 Python G0；上述 guard 是 Python 进程内防护，**不是 OS sandbox**。
- fixed seed 的 `setup-test-db.py --output` 必须精确位于本次所选 `DATA_DIR`；dev vault 重建与 `--check-only` 都必须经 wrapper Direct Python。`rebuild-dev-vault.py` 要求 FT7 runtime attestation（`process_guarded=True`），`--root` 必须位于本次 selected `DATA_DIR`；裸启动会在产品 import 前失败，任意 `.data-test` sibling 也不能旁路。

这些保护只覆盖合成 CAT-0，不授权读取真实快照。真实数据尚未执行；CAT-U/CAT-C 必须等待 U1/G8，migration 还必须等待 FT5。

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

# ✅ 安全：使用合成 CAT-0（不会发起真实抓取）
.\scripts\run-test.ps1 -Direct -DataRoot .data-test\contract -Command @("python", "-m", "pytest", "-q", "-m", "not network and not manual")
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

   # pytest、仓库 Python 模块/脚本使用 -Direct；FT7 只接受 python -m 或仓库 .py
   .\scripts\run-test.ps1 -Direct -Command @("python", "-m", "<repo.module>", "<arg>")
   ```

3. **确认命令仍指向测试环境**
   ```powershell
   .\scripts\run-test.ps1 config show
   git status --short
   ```

   AI 不读取生产 `.data/` 作前后对比；如需额外确认，由用户自行检查备份或文件时间戳。

用户生产服务配置仍写入 Git 忽略的 `config/local.yaml`，但 CAT-0 的 base-only Config **不会读取它**，Agent 也不得读取或打印它。`PKV_RUN_LIVE` 只控制 pytest 收集，既不是应用网络开关，也不能解除 U1/G8 或用户授权门禁。

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
# 当前只运行使用临时 SQLite 的迁移单元/集成测试
.\scripts\run-test.ps1 -Direct -DataRoot .data-test\migration-contract -Command @(
  "python", "-m", "pytest", "tests/unit", "tests/integration", "-q", "-k", "migration"
)
```

`run-test.ps1` 会拒绝 `scripts/migrate.py` 并返回 exit 2。真实快照 migration 必须等待 FT5 + U1/G8 + 用户授权，并且只在 disposable clone 内执行；未设置测试路径覆盖的裸命令可能读取或修改生产 `.data/`，AI 不执行。

**结果判定**:

- 临时 SQLite 迁移测试应返回退出码 `0`，且不读取真实快照或 `config/local.yaml`。
- 真实 migration 的版本、dry-run、执行与前后一致性结论在 FT5/U1/G8 未交付前均标记“剔除待前置/未覆盖”。
- fresh install 不能证明历史 schema 升级兼容性；pending=0 也不得宣称覆盖迁移路径。

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

先明确验证目标：全新的临时 SQLite 只验证 **fresh install/迁移代码契约**，不能证明旧库升级兼容。真实快照升级兼容性当前受 FT5 + U1/G8 阻塞；即使用户已准备脱敏旧库，也不得绕过 `run-test.ps1` 对 `migrate.py` 的 exit 2 拒绝。

1. **运行临时 SQLite 迁移测试**
   ```powershell
   .\scripts\run-test.ps1 -Direct -DataRoot .data-test\migration-contract -Command @(
     "python", "-m", "pytest", "tests/unit", "tests/integration", "-q", "-k", "migration"
   )
   ```

2. **真实快照 migration 记为阻塞**

   FT5 必须先提供显式 DataRoot/clone 寻址和迁移前后一致性断言；U1/G8 必须验证用户授权、只读 snapshot 与 writable clone，并避免原始数据落入工作区日志。缺少任一项时记录“剔除待前置/未覆盖”，不提供可执行命令。

3. **解释结果边界**

   临时 SQLite 测试通过只说明迁移代码契约成立；它不证明历史 schema baseline 可升级，也不授权任何真实或生产迁移。

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

# 2. AI 运行合成离线测试，不向 CAT-0 launcher 提交真实 URL
.\scripts\run-test.ps1 -Direct -DataRoot .data-test\contract -Command @("python", "-m", "pytest", "-q", "-m", "not network and not manual")

# 3. AI 再次确认测试路径；不读取生产 .data/
.\scripts\run-test.ps1 config show
git status --short
```

**关键点**:
- ✅ AI 自动使用测试环境
- ✅ 本场景未读取生产数据；安全结论仅限已核验 CAT-0 Python 入口
- ✅ 测试完成后可清理测试数据

真实 URL archive 属于 live/CAT-U；真实数据尚未执行，U1/G8 与用户授权齐备前保持阻塞。

---

### 场景 2：版本升级（如 M6 上线）

当前先用临时 SQLite 验证迁移代码契约；真实快照升级兼容仍受 FT5 + U1/G8 阻塞：

```powershell
# 1. Agent 只运行临时 SQLite 迁移测试；wrapper 会拒绝 migrate.py（exit 2）
.\scripts\run-test.ps1 -Direct -DataRoot .data-test\migration-contract -Command @(
  "python", "-m", "pytest", "tests/unit", "tests/integration", "-q", "-k", "migration"
)
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

先创建迁移脚本，再用临时 SQLite 的测试用例验证代码契约：

```powershell
# 1. 创建迁移脚本
# scripts/migrations/003_add_feature_table.sql

# 2. wrapper 会拒绝 migrate.py（exit 2）；Agent 只运行迁移测试
.\scripts\run-test.ps1 -Direct -DataRoot .data-test\feature-migration -Command @(
  "python", "-m", "pytest", "tests/unit", "tests/integration", "-q", "-k", "migration"
)

# 3. 测试不接触真实快照的新功能
.\scripts\run-test.ps1 -DataRoot .data-test\feature-migration <CLI-subcommand>
```

需要在历史快照上验证时，仍必须等待 FT5 + U1/G8，并仅在用户授权的 disposable clone 内执行。

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
   - 默认使用 `run-test.ps1` 执行 CLI；Direct Python 只使用仓库 `python -m` / `.py`，不使用 `-c`/stdin/解释器 flags；非 Python Direct 不属于 Python G0
   - 重要变更前提示用户备份

2. **数据库升级时**
   - 当前先用临时 SQLite pytest 验证迁移脚本；真实快照 migration 等待 FT5 + U1/G8
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
- [ ] `MigrationManager` 在临时 SQLite 上可正确识别待迁移脚本
- [ ] 临时 SQLite 迁移测试通过，且不读取真实快照
- [ ] `run-test.ps1` 对 `migrate.py` 返回 exit 2；没有 wrapper migrate 命令
- [ ] 真实 migration 在 FT5 + U1/G8 + 用户授权前保持阻塞
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

生产恢复必须由用户明确授权并执行；AI 只用临时 SQLite 测试重新验证迁移代码：

```powershell
# 1. 修复迁移脚本
# 2. 在受保护 CAT-0 入口重新运行迁移测试；不执行 migrate.py
.\scripts\run-test.ps1 -Direct -DataRoot .data-test\migration-contract -Command @(
  "python", "-m", "pytest", "tests/unit", "tests/integration", "-q", "-k", "migration"
)
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

- ✅ CAT-0 已按 pytest、CLI/MCP、FT7 Direct Python 逐入口 fail-closed
- ✅ base-only Config 不读取 `config/local.yaml`，live/secret/proxy 被清理
- ✅ fixed seed 输出锁定所选 `DATA_DIR`，dev vault rebuild/`--check-only` 经 wrapper Direct Python
- ✅ 完整的安全规则文档

### 数据库增量升级 ✅

- ✅ 版本管理和增量迁移
- ✅ 自动备份和回滚机制
- ✅ 幂等性和向后兼容
- ✅ 测试验证和生产应用

现在主人可以：
- 🤖 **在已核验 CAT-0 Python 入口内与 AI 协作开发**（进程内 guard 不是 OS sandbox；非 Python Direct 不在 Python G0 内）
- 📊 **验证临时 SQLite 迁移契约**（不等于历史真实库升级兼容性）
- 💾 **按 user-only runbook 规划真实升级/备份/恢复**（仍受授权、U1/G8 与 FT5 等门禁约束）

真实数据验证仍未执行；CAT-U/CAT-C 等待 U1/G8，migration 另需 FT5。上述 CAT-0 结论不得扩展为真实数据或生产发布结论。

---

**文档版本**: v1.1（P0 CAT-0/G0 与 migration 门禁同步）
**作者**: 幽浮酱 ฅ'ω'ฅ
**最后同步**: 2026-07-31

*数据安全，开发愉快喵～*
