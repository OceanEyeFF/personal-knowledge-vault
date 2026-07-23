# Scripts 运维脚本

[根目录](../CLAUDE.md) > **scripts**

---

## 模块职责

**运维与自动化**:提供环境搭建、数据备份恢复、数据库迁移、测试环境管理等运维脚本。

### 核心理念

- **自动化优先**: 一键完成环境搭建和数据管理
- **安全第一**: 备份/恢复/迁移前强制确认
- **测试隔离**: 测试环境与生产环境完全隔离
- **增量升级**: 数据库版本化管理,支持增量迁移

---

## 脚本清单

### 环境搭建脚本

#### setup-conda.ps1 (推荐)

**用途**: 使用 Conda 创建 Python 3.11 环境并安装依赖

**运行方式**:
```powershell
.\scripts\setup-conda.ps1
```

**功能**:
- 检查 Conda 是否安装
- 创建 Python 3.11 Conda 环境 (`py311-private`)
- 通过 `conda run` 固定环境并升级 pip
- 安装所有依赖包
- 验证关键依赖
- 创建 Git 忽略的 `config/local.yaml`
- 创建数据目录结构

**优势**:
- 避免 Python 3.13 兼容性问题
- 环境隔离更彻底
- 可以方便地切换 Python 版本

---

#### test-conda.ps1

**用途**: 在 Conda 环境中运行验证测试

**运行方式**:
```powershell
.\scripts\test-conda.ps1
```

**功能**:
- 检查 `py311-private` 环境是否存在
- 通过 `run-windows.ps1` 固定 Conda 环境与 UTF-8
- 运行 `src/utils/verify_setup.py` 验证脚本
- 显示测试结果

**适用场景**:
- 首次安装后验证
- 更新依赖后验证
- 故障排查

---

### 测试环境管理脚本

#### run-test.ps1 (重要!)

**用途**: 使用隔离的测试数据路径运行 PKV CLI；pytest、Python 等非 CLI 命令必须使用 `-Direct -Command @(...)`（不影响生产数据）

**运行方式**:
```powershell
# 在测试环境归档网页
.\scripts\run-test.ps1 archive "https://example.com"

# 在测试环境搜索
.\scripts\run-test.ps1 search "AI"

# 查看测试环境统计
.\scripts\run-test.ps1 stats

# 列出测试环境条目
.\scripts\run-test.ps1 list

# 直接运行 pytest（仍使用同一套测试路径隔离）
.\scripts\run-test.ps1 -Direct -Command @("pytest", "tests\unit", "-q")

# 直接运行迁移工具并指定独立测试场景
.\scripts\run-test.ps1 -Direct -DataRoot .data-test\migration -Command @("python", "scripts\migrate.py", "--dry-run")
```

**功能**:
- 在当前进程直接设置完整测试路径覆盖，不读取 `.env.test`
- 支持 `-DataRoot` 创建彼此隔离的测试场景
- 默认模式将参数传给 `python -m src.cli.commands`；非 CLI 命令必须显式使用 `-Direct -Command @(...)`
- 隔离测试数据到 `.data-test/` 目录
- 自动创建测试目录结构
- 显示测试环境状态(绿色提示)

**关键特性**:
- 完全隔离生产数据(`.data/`)
- 可以安全测试新功能
- 支持所有 CLI 命令

**AI 安全规范**:
- 所有 AI 协作测试**必须**使用此脚本
- 禁止直接操作生产数据

详见: [.ai-safety-rules.md](../.ai-safety-rules.md)

---

#### check-environment.ps1

**用途**: 检测当前使用的数据库环境(生产/测试)

**运行方式**:
```powershell
# AI/自动化：改用测试配置查看命令，避免读取默认生产数据库统计
.\scripts\run-test.ps1 config show

# 用户主动检查当前默认数据库时才裸跑；需要明确授权，AI 不执行
.\scripts\check-environment.ps1
```

**输出示例**:

```
========================================
 环境检测
========================================

当前数据库路径（进程级测试覆盖）:
  <仓库路径>\.data-test\db\knowledge_vault.db
  ✓ 测试环境

数据库统计:
  总条目数: 0

建议:
  ✓ 使用 .\scripts\run-test.ps1 进行测试
  ✓ 重要变更前先备份: .\scripts\backup-data.ps1
```

**使用场景**:
- 验证当前使用的数据库
- AI 协作前确认环境
- 故障排查

---

### 数据备份与恢复脚本

本节脚本会读取或替换生产 `.data/`，只供用户明确授权后的人工 runbook；AI 不执行。

#### backup-data.ps1

**用途**: 备份生产数据到 `.data-backup/` 目录

**运行方式**:
```powershell
# 手动备份
.\scripts\backup-data.ps1

# 带说明的备份
.\scripts\backup-data.ps1 -Message "重要更新前的备份"
```

**功能**:
- 完整备份 `.data/` 目录
- 生成备份信息文件(`backup-info.txt`):
  - 时间戳
  - 备份大小
  - 文件数量
  - 备份说明
- 显示最近的 5 个备份
- 自动计算备份大小和文件数

**备份目录结构**:
```
.data-backup/
└── 20260216-143000/
    ├── backup-info.txt
    └── .data/
        ├── db/
        ├── vectors/
        ├── vault/
        └── logs/
```

**最佳实践**:
- 重要变更前先备份
- 数据库 Schema 迁移前必须备份
- 定期清理旧备份(手动)

---

#### restore-data.ps1

**用途**: 从备份恢复数据

**运行方式**:
```powershell
# 交互式选择备份恢复
.\scripts\restore-data.ps1

# 恢复指定时间戳的备份
.\scripts\restore-data.ps1 -BackupTimestamp "20260216-143000"
```

**功能**:
- 列出所有可用备份(含详细信息)
- 交互式选择备份版本
- 安全确认机制(需输入 `YES`)
- 自动验证恢复结果

**交互流程**:
```
可用备份:
  [1] 20260216-143000 (45.3 MB, 1250 files) - "重要更新前的备份"
  [2] 20260216-120000 (42.1 MB, 1200 files)

请选择要恢复的备份 [1-2]: 1

⚠️ 警告: 恢复操作将完全替换当前 .data/ 目录!

请输入 YES 确认恢复: YES

恢复中...
✓ 恢复完成
```

**警告**:
- 恢复操作会**完全替换**当前 `.data/` 目录
- 建议先备份当前数据再恢复

---

### 数据库迁移脚本

#### migrate.py (Python 脚本)

**用途**: 数据库 Schema 增量迁移工具

**运行方式**:
```bash
# 以下裸命令使用当前配置；未设置路径覆盖时会读取或修改生产 .data/。
# 仅供用户明确授权后的生产维护，AI 不执行。

# 查看当前数据库版本
python scripts/migrate.py --version

# 检查待迁移脚本(不执行)
python scripts/migrate.py --dry-run

# 交互式升级(推荐)
python scripts/migrate.py

# 自动升级(无需确认)
python scripts/migrate.py --auto

# 跳过自动备份(不推荐)
python scripts/migrate.py --no-backup
```

**功能**:
- 获取当前数据库版本(从 `schema_version` 表)
- 扫描 `scripts/migrations/` 目录下的待执行脚本
- 语义化版本号比较(如 `1.0.0` < `1.1.0`)
- 自动备份(可选)
- 执行迁移并记录到 `schema_version` 表
- 显示迁移日志

**迁移流程**:
```
1. 检查当前版本: 1.0.0
2. 扫描待迁移脚本:
   - 002_add_cli_tables.sql (v1.1.0)
3. 自动备份到 .data-backup/
4. 执行迁移脚本
5. 更新 schema_version 表
6. 完成!
```

**安全机制**:
- 默认自动备份(可用 `--no-backup` 跳过)
- 交互式确认(除非使用 `--auto`)
- 失败自动回滚(如果可能)

详见: [docs/operations/数据库迁移指南.md](../docs/operations/数据库迁移指南.md)

---

#### migrations/ 目录

**用途**: 存放数据库迁移脚本

**命名规范**:
```
格式: {编号}_{描述}.sql
示例: 001_initial_schema.sql
      002_add_cli_tables.sql
      003_add_new_feature.sql
```

**脚本结构**:
```sql
-- Version: 1.1.0
-- Description: 添加 CLI 统计表

-- 向上迁移
CREATE TABLE IF NOT EXISTS cli_stats (
    stat_id TEXT PRIMARY KEY,
    command TEXT NOT NULL,
    executed_at TEXT NOT NULL
);

-- 向下迁移(注释)
-- DROP TABLE IF EXISTS cli_stats;
```

**关键要求**:
- 脚本必须**幂等**(可重复执行)
- 必须**向后兼容**(不破坏现有数据)
- 必须包含版本号注释(`-- Version: x.y.z`)
- 建议包含向下迁移 SQL(注释形式)

**现有迁移脚本**:

| 脚本 | 版本 | 说明 |
|------|------|------|
| `001_initial_schema.sql` | 1.0.0 | M1 初始 Schema (5 张表) |
| `002_add_cli_tables.sql` | 1.1.0 | M6 CLI 统计表 |

详见: [scripts/migrations/README.md](./migrations/README.md)

---

### Legacy 脚本 (已归档)

以下脚本已归档到 `scripts/legacy/` 目录:

- `setup.ps1` / `setup.bat` - venv 安装脚本 (不推荐,Python 3.13 兼容性问题)
- `test.ps1` / `test.bat` - venv 测试脚本

详见: [scripts/legacy/README.md](./legacy/README.md)

---

## 关键依赖与配置

### PowerShell 脚本依赖

- Windows PowerShell 5.1+ 或 PowerShell Core 7+
- 执行策略: 需允许运行脚本 (临时: `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass`)
- `.ps1` 文件保存为 UTF-8 BOM，避免 Windows PowerShell 5.1 将中文源码按系统代码页误读

### Python 脚本依赖

- Python 3.11+ (推荐使用 Conda 环境)
- 依赖: `src/` 模块 (自动导入)

### 配置文件

#### config/local.yaml

应用服务、模型和密钥统一配置在 Git 忽略的 `config/local.yaml`。测试数据路径由 `run-test.ps1` 在进程内覆盖，与本机服务配置相互独立。

---

## 常见问题 (FAQ)

### Q1: PowerShell 提示"无法加载脚本"

**错误信息**:
```
无法加载文件 xxx.ps1,因为在此系统上禁止运行脚本
```

**解决方案**:
```powershell
# 临时允许运行脚本(仅当前会话)
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass

# 然后运行脚本
.\scripts\setup-conda.ps1
```

---

### Q2: 如何清理测试环境数据?

```powershell
# stats 只用于查看指定测试场景的数据，不执行清理；以下以 batch-import 为例
.\scripts\run-test.ps1 -DataRoot .data-test\batch-import stats
```

当前不提供可直接复制执行的递归删除命令。清理必须通过专用流程完成：先规范化并核对目标绝对路径确实位于仓库 `.data-test` 下，再递归拒绝 junction、symlink 等 reparse point，最后才允许删除对应场景子目录。没有具备这些检查的清理工具时，保留 Git 已忽略的测试数据并由用户人工核对。

---

### Q3: 数据库迁移失败如何回滚?

恢复会替换生产 `.data/`，必须取得用户明确授权并由用户执行；AI 不运行以下命令。

1. **如果自动备份存在**:
```powershell
.\scripts\restore-data.ps1
# 选择迁移前的备份
```

2. **如果没有备份**:
- 迁移脚本中的"向下迁移"SQL 可以手动执行
- 或从 Git 历史恢复数据库文件

**最佳实践**: 迁移前**必须**先备份!

---

### Q4: 如何定期清理旧备份?

```powershell
# 列出所有备份(按日期排序)
Get-ChildItem .data-backup | Sort-Object LastWriteTime

# 保留最近 5 个备份,删除其他
Get-ChildItem .data-backup | Sort-Object LastWriteTime -Descending | Select-Object -Skip 5 | Remove-Item -Recurse -Force
```

**建议**: 保留至少 3-5 个备份

---

## 使用场景

### 场景 1: 首次安装

```powershell
# 1. 安装 Conda 环境
.\scripts\setup-conda.ps1

# 2. 编辑本机配置
notepad config\local.yaml

# 4. 验证安装
.\scripts\test-conda.ps1

# 4. 初始化默认数据库（生产操作；仅由用户明确授权后执行，AI 不执行）
.\scripts\run-windows.ps1 python scripts/migrate.py
```

---

### 场景 2: 测试新功能

```powershell
# 1. 确认测试路径
.\scripts\run-test.ps1 config show

# 2. 在测试环境测试
.\scripts\run-test.ps1 archive "https://example.com"
.\scripts\run-test.ps1 search "测试"

# 3. 再次确认命令仍指向测试路径；AI 不读取生产 .data/ 作对比
.\scripts\run-test.ps1 config show
git status --short
```

---

### 场景 3: 数据库升级

空的 `.data-test\migration` 只验证 **fresh install**，不能证明旧库升级兼容，也不能作为生产升级门禁。验证升级兼容性前，先将已脱敏的旧版本 DB/Schema 夹具放到 `.data-test\migration\db\knowledge_vault.db`；迁移依赖 Vault 或向量数据时，也要把对应测试夹具放入同一数据根目录。不要让测试命令直接指向生产 `.data/`。

```powershell
# 1. 在测试环境验证迁移
.\scripts\run-test.ps1 -Direct -DataRoot .data-test\migration -Command @("python", "scripts\migrate.py", "--dry-run")

# 2. 执行测试环境迁移；当前自动备份脚本固定读取生产 .data/，测试时必须禁用
.\scripts\run-test.ps1 -Direct -DataRoot .data-test\migration -Command @("python", "scripts\migrate.py", "--auto", "--no-backup")

# 3. 验证测试环境
.\scripts\run-test.ps1 -DataRoot .data-test\migration stats

# 4. 以下均为生产操作：必须由用户明确授权并执行，AI 到此停止
.\scripts\backup-data.ps1 -Message "升级到 v1.2.0 前备份"
.\scripts\run-windows.ps1 python scripts/migrate.py

# 5. 生产验证同样由用户执行；AI 不读取 .data/
.\scripts\run-windows.ps1 python -m src.cli.commands stats
```

---

### 场景 4: 数据恢复

```powershell
# 1. 以下恢复流程会操作生产数据：必须由用户明确授权并执行，AI 不执行
# 列出可用备份
.\scripts\restore-data.ps1

# 2. 选择要恢复的备份并确认

# 3. 生产验证由用户执行；AI 不读取 .data/
.\scripts\run-windows.ps1 python -m src.cli.commands stats
.\scripts\run-windows.ps1 python -m src.cli.commands list --limit 5
```

---

## 数据流

### 测试环境数据流

```
.\scripts\run-test.ps1 <CLI-subcommand>
    ↓
校验 DataRoot 不指向 .data/
    ↓
设置 DATA_DIR/DB_PATH/VAULT_DIR/VECTOR_DIR/LOG_DIR/TMP_DIR
    ↓
创建 .data-test/ 目录结构
    ↓
默认模式：通过 run-windows.ps1 执行 python -m src.cli.commands <CLI-subcommand>
Direct 模式：.\scripts\run-test.ps1 -Direct -Command @("<executable>", "<arg>", "...")
             通过 run-windows.ps1 原样执行 <executable> <args>
    ↓
Config 合并 YAML 应用配置与当前进程的隔离路径覆盖；输出不得泄露密钥
    ↓
操作 .data-test/ 数据(隔离)
    ↓
生产数据 .data/ 未受影响 ✓
```

### 数据库迁移数据流

```
测试路径（AI/自动化默认）：
.\scripts\run-test.ps1 -Direct -DataRoot .data-test\migration -Command @("python", "scripts\migrate.py", "--auto", "--no-backup")
    ↓
读取并迁移 .data-test\migration\db；禁用当前会读取生产 .data/ 的自动备份
    ↓
使用同一 DataRoot 运行 stats/list 验证

生产路径（必须由用户明确授权并执行，AI 不执行）：
.\scripts\run-windows.ps1 python scripts/migrate.py
    ↓
MigrationManager.get_pending_migrations()
  → 读取生产 schema_version 并扫描 scripts/migrations/*.sql
    ↓
自动备份 .data/ → .data-backup/{timestamp}/
    ↓
逐个执行迁移脚本
  → 执行 SQL
  → 记录到 schema_version 表
    ↓
生产 stats/list 验证同样只由用户执行
```

---

## 相关文件清单

### PowerShell 脚本

| 文件 | 用途 |
|------|------|
| `setup-conda.ps1` | Conda 环境自动安装 |
| `test-conda.ps1` | Conda 环境验证测试 |
| `run-test.ps1` | 测试环境运行脚本 |
| `check-environment.ps1` | 环境检测脚本 |
| `backup-data.ps1` | 数据备份脚本 |
| `restore-data.ps1` | 数据恢复脚本 |

### Python 脚本

| 文件 | 用途 |
|------|------|
| `migrate.py` | 数据库迁移工具 |
| `init_db.py` | 数据库初始化(Legacy) |

### 迁移脚本

| 文件 | 版本 | 说明 |
|------|------|------|
| `migrations/001_initial_schema.sql` | 1.0.0 | M1 初始 Schema |
| `migrations/002_add_cli_tables.sql` | 1.1.0 | M6 CLI 统计表 |
| `migrations/README.md` | - | 迁移脚本说明 |

### 文档

| 文件 | 说明 |
|------|------|
| [scripts/README.md](./README.md) | 脚本详细说明 |
| [docs/operations/testing/测试环境隔离指南.md](../docs/operations/testing/测试环境隔离指南.md) | 测试环境完整文档 |
| [docs/operations/testing/测试环境快速开始.md](../docs/operations/testing/测试环境快速开始.md) | 3 分钟入门 |
| [docs/operations/数据库迁移指南.md](../docs/operations/数据库迁移指南.md) | 数据库迁移完整指南 |
| [docs/operations/testing/AI安全与数据库升级完整方案.md](../docs/operations/testing/AI安全与数据库升级完整方案.md) | AI 安全 + 数据库升级总结 |

---

## 变更记录 (Changelog)

### 2026-02-16 18:51
- 生成 Scripts 模块 CLAUDE.md 文档
- 添加导航面包屑
- 补充数据库迁移和测试环境管理脚本说明

### 2026-02-16 (v0.6.1)
- 新增数据库迁移工具 (`migrate.py`)
- 新增测试环境管理脚本 (`run-test.ps1`, `check-environment.ps1`)
- 新增备份恢复脚本 (`backup-data.ps1`, `restore-data.ps1`)

### 2026-02-14 (v0.1.0)
- 新增 Conda 安装脚本 (`setup-conda.ps1`, `test-conda.ps1`)
- 归档 Legacy venv 脚本到 `scripts/legacy/`

---

**模块维护者**: AI Agent
**最后更新**: 2026-02-16 18:51:32

*本文档由 Claude Code 自动生成*
