# Scripts 运维脚本

[根目录](../CLAUDE.md) > **scripts**

---

## 模块职责

**运维与自动化**:提供环境搭建、数据备份恢复、数据库迁移、测试环境管理等运维脚本。

### 核心理念

- **自动化优先**: 一键完成环境搭建和数据管理
- **安全第一**: 备份/恢复/迁移前强制确认
- **测试隔离**: 自动化运行路径锁定到 `.data-test`；Python guard 不是文件系统或 OS sandbox
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
- 首次仅创建不覆盖的用户配置 `%USERPROFILE%\.pkv\config.yaml`
- 不创建数据库、向量索引或数据根；运行时初始化必须先 `inspect`、再审阅 `setup` 计划并显式确认
- 发现旧 checkout `config/local.yaml` 或 `.data/` 时只提示并保留；不读取内容、不复制、不删除、不迁移

**优势**:
- 避免 Python 3.13 兼容性问题
- 环境隔离更彻底
- 可以方便地切换 Python 版本

**当前运行时布局**:

- 唯一可编辑的用户配置是 `%USERPROFILE%\.pkv\config.yaml`。
- 默认数据根是 `%USERPROFILE%\.pkv\data`；用户配置中的 `storage.data_root` 可选择其他根，进程级 `PKV_DATA_ROOT` 优先级更高。
- `<data-root>/config/local.yaml` 是 PKV 写入的无密钥运行时快照，不是第二份用户配置，不能放 API Key、Cookie 或其他敏感值。
- 旧 checkout 路径不是自动迁移来源。迁移前必须先展示影响并获得用户确认；当前安装脚本不执行迁移。

---

#### test-conda.ps1

**用途**: 在 Conda 环境中运行验证测试

**运行方式**:
```powershell
.\scripts\test-conda.ps1
```

**功能**:
- 检查 Conda 环境及 Python 3.11 合同，并执行 `pip check`
- 通过 `run-test.ps1` 运行 smoke、收集契约、完整离线、MCP 覆盖率或 Windows P0 套件
- 全部 pytest 运行保持在新建的 `.data-test/conda-*` 根中
- 显示测试结果

默认环境与 `setup-conda.ps1` 相同，为 `py311-private`。若使用
`setup-test-conda.ps1` 创建的独立测试环境，必须显式传入
`-EnvironmentName`。pytest 的临时目录与 cache 完全由 `run-test.ps1`
在所选 `.data-test` 根内管理；`test-conda.ps1` 不传 `--basetemp` 或
`cache_dir` 覆盖。`-Suite MCP` 是 `src.mcp >=95%` 的显式 Windows 覆盖率门禁；
它与仅验证默认收集和完整离线源码兼容性的 `-Suite P0` 分开报告。

**适用场景**:
- 首次安装后验证
- 更新依赖后验证
- 故障排查

---

### 测试环境管理脚本

#### run-test.ps1 (重要!)

**用途**: 默认自动化入口。使用隔离路径运行 PKV CLI、pytest，或经 FT7 运行受保护的仓库 Direct Python。

**运行方式**:
```powershell
# 查看测试环境统计
.\scripts\run-test.ps1 stats

# 在测试环境离线搜索
.\scripts\run-test.ps1 search "AI" --strategy bm25

# 查看测试环境统计
.\scripts\run-test.ps1 stats

# 列出测试环境条目
.\scripts\run-test.ps1 list

# 直接运行 pytest（仍使用同一套测试路径隔离）
.\scripts\run-test.ps1 -Direct -Command @("pytest", "tests\unit", "-q")

# 运行仓库 Direct Python（只允许仓库 -m module 或 .py）
.\scripts\run-test.ps1 -Direct -DataRoot .data-test\rebuild-dev -Command @("python", "scripts\rebuild-dev-vault.py", "--root", ".data-test/rebuild-dev", "--check-only", "--json")
```

**功能**:
- 在当前进程直接设置完整测试路径覆盖，不读取 `.env.test`
- 支持 `-DataRoot` 创建彼此隔离的测试场景
- 默认 CLI、MCP 离线子进程由 `tests/offline_entrypoint.py` 启动；pytest 由同一入口的 `pytest` 目标在 pytest/plugin 导入前建立 G0，根 `tests/conftest.py` 再维持逐用例隔离
- Direct Python（FT7）只允许显式 test-safe target：pytest、`setup-test-db.py`、`rebuild-dev-vault.py`、受控 consistency checker、`src.cli.commands`、`src.mcp.server`、`src.utils.verify_setup` 和固定 MCP 评测；其他仓库脚本/模块（含 build helper）在创建 DataRoot 前拒绝，`-c`、stdin 和解释器 flags 同样拒绝；同进程 `runpy` 在产品导入前清理 live/secret/proxy，并安装 base-only Config、网络及子进程 guard
- 隔离测试数据到 `.data-test/` 目录
- 自动创建测试目录结构
- 显示测试环境状态(绿色提示)

**关键特性**:
- 默认运行路径不指向生产数据 (`.data/`)，并 fail-closed 拒绝危险目标
- 可验证受支持的离线 CAT-0 功能
- 支持离线 CLI 子命令；需要网络或真实数据的命令仍受 user-only gate 阻塞
- FT7 是 Python 进程内 guard，不是 OS sandbox；非 Python Direct 仍须经 wrapper 启动，但不属于 Python G0、不保证离线，需单独审查
- `setup-test-db.py` 的输出必须精确位于所选 `DATA_DIR`（默认 `DB_PATH`）
- `migrate.py` 以及已停用的原始回填/初始化入口被包装器 fail-closed 拒绝并返回 exit 2；真实迁移仍受 U1/G8/FT5 user-only gate 阻塞，尚未执行真实数据迁移

**AI 安全规范**:
- 所有 AI 协作测试**必须**使用此脚本
- 禁止直接操作生产数据

详见: [.ai-safety-rules.md](../.ai-safety-rules.md)

---

#### 已停用的原始维护入口（R3.1 fence）

`backfill_chunks.py`、`backfill_relations.py`、`init_db.py` 与 `migrate.py`
不再是当前产品操作入口。无论裸跑还是经 `run-test.ps1` 的 Direct Python 调用，它们都会在
加载 `Config`、打开数据根、执行迁移或发起网络请求之前返回 exit 2。它们不能替代
`inspect → plan → confirm → execute`，也不能绕过单写者 lease。

其中少量模块级兼容 helper 仍被隔离的合成 fixture 测试调用；这不是给 Wrapper、CLI、MCP
或用户数据根提供的 API。需要未来维护动作时，应以独立 lifecycle plan 明确范围、影响、备份和确认。

`check_chunk_index_consistency.py` 是唯一保留的遗留诊断入口：它只以 SQLite `mode=ro` 打开
一个已存在且安全的数据库；缺失、链接/替换或不可读数据库均返回 exit 2，绝不新建数据库或索引。

---

#### rebuild-dev-vault.py

**用途**: 只在本次 wrapper `-DataRoot` 选中的 `DATA_DIR` 内执行合成开发 Vault 的重建或只读健康检查；不得指向其他 `.data-test` 场景。脚本在第三方/产品 import 前要求 FT7 runtime attestation，裸 Python 启动会 fail-closed。两种模式都必须经 `run-test.ps1` 的 Direct Python/FT7 入口：

```powershell
.\scripts\run-test.ps1 -Direct -DataRoot .data-test\rebuild-dev -Command @("python", "scripts\rebuild-dev-vault.py", "--root", ".data-test/rebuild-dev", "--json")
.\scripts\run-test.ps1 -Direct -DataRoot .data-test\rebuild-dev -Command @("python", "scripts\rebuild-dev-vault.py", "--root", ".data-test/rebuild-dev", "--check-only", "--json")
```

这只是合成开发库演练，不替代旧版真实快照迁移或生产迁移验证。

---

#### build-internal-package.ps1（P1-A 内部自测封包）

**用途**：仅为维护者生成 `dist/internal/` 下的 PyInstaller onedir + ZIP，并可用 `-Smoke`
在合成 `.data-test` 根上执行外置包的 CLI BM25 与 MCP stdio initialize 自测：

```powershell
.\scripts\build-internal-package.ps1
.\scripts\build-internal-package.ps1 -Smoke
```

它是 **INTERNAL TEST ONLY** 工具：无 installer、绝不写 `dist/release/`、不会替代或弱化
`build-release.ps1` 的 clean-checkout/reproducibility/hold 合同。生成包内含构建时间、Git
revision/dirty 状态、Python/依赖摘要，并在构建后拒绝 `local.yaml`、凭据、Vault、日志、数据库
及测试 fixture；冻结的 CArchive/PYZ 与 ZIP 会递归、有界地检查内容和成员元数据，且 ZIP 只接受
可完整验证的规范物理布局。`-Smoke` 仍必须经 `run-test.ps1`；包从仓库外临时目录启动，数据始终留在
`.data-test`，且结论只可称为 `INTERNAL SELF-TEST PASSED`。仓库外 workspace
清理经 `internal-package-workspace.ps1` 逐项 no-follow 审计与叶到根删除；遇到 junction/
symlink/reparse 或竞态变化必须失败并保留现场。

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

> **历史路径提示**：本节的仓库 `.data/`、`.data-backup/` 描述的是保留的旧 checkout 运维脚本，
> 不构成当前默认运行时布局，也不会被 `setup-conda.ps1` 自动接管或迁移。任何旧目录迁移都须先
> 展示影响、保留原目录并由用户明确确认。

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

#### migrate.py（已停用的 Python 脚本）

**用途**: 历史数据库 Schema 增量迁移实现；当前入口已 fail-closed。

**当前门禁**:

- `run-test.ps1` 与裸 `migrate.py` 均明确拒绝执行（exit 2），不得把它包装成当前自动化命令。
- 真实旧库/生产迁移仍受 U1/G8/FT5 user-only gate 阻塞；截至当前尚未执行任何真实数据迁移。
- 门禁交付后也只能由用户在明确授权、备份和脱敏旧版夹具验证完成后，按专用迁移 Runbook 操作；AI/自动化不执行。

**历史功能（非当前命令）**:
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
   - ...（当前链的末端为 010_add_storage_operation_commits.sql，v1.2.4）
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
| `004_add_chat_sessions.sql` | 1.1.1 | AI 对话会话表 |
| `005_add_review_system.sql` | 1.1.2 | 审核系统表 |
| `006_add_relations_foundation.sql` | 1.2.0 | Phase A 关系层基础表 |
| `007_add_timeline_time_fields.sql` | 1.2.1 | 真实事件/发布时间字段 |
| `008_align_fts_contract.sql` | 1.2.2 | FTS 表与触发器合同对齐 |
| `009_repair_fts_storage_contract.sql` | 1.2.3 | FTS 存储合同修复 |
| `010_add_storage_operation_commits.sql` | 1.2.4 | 跨存储操作提交凭据 |

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

#### `%USERPROFILE%\.pkv\config.yaml`

用户/生产运行的服务、模型和密钥只配置在 `%USERPROFILE%\.pkv\config.yaml`。默认自动化不读取该文件，
而由 `run-test.ps1` 与 offline entrypoint 安装 base-only Config。`PKV_DATA_ROOT` 和
`PKV_LOG_LEVEL` 是正式的进程级覆盖；其中数据根覆盖优先于该配置内的 `storage.data_root`。

`<data-root>/config/local.yaml` 仅是 PKV 管理、无敏感字段的运行时快照。它用于验证当前数据库/
Embedding 构建合同，不能替代或覆盖用户配置。

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

# 2. 编辑唯一的本机配置
notepad "%USERPROFILE%\.pkv\config.yaml"

# 3. 无副作用检查配置、数据根与待执行工作
.\scripts\run-windows.ps1 python -m src.cli.commands inspect

# 4. 只展示初始化计划；仍不写入数据根
.\scripts\run-windows.ps1 python -m src.cli.commands setup

# 5. 审阅上一步的影响后，由用户使用其 PLAN_ID 明确确认。
#    --allow-network 只授权计划中的最小 Provider 连通性探测，可能联网或产生费用。
.\scripts\run-windows.ps1 python -m src.cli.commands setup --apply --confirm <PLAN_ID> --allow-network
```

`setup-conda.ps1` 不创建 `%USERPROFILE%\.pkv\data`，也不会接管 checkout 中遗留的
`config/local.yaml` 或 `.data/`。若发现旧目录，先保留它们并取得用户确认的独立迁移方案。

---

### 场景 2: 测试新功能

```powershell
# 1. 确认测试路径
.\scripts\run-test.ps1 config show

# 2. 在测试环境测试
.\scripts\run-test.ps1 stats
.\scripts\run-test.ps1 search "测试" --strategy bm25

# 3. 再次确认命令仍指向测试路径；AI 不读取生产 .data/ 作对比
.\scripts\run-test.ps1 config show
git status --short
```

---

### 场景 3: 数据库升级

空的 `.data-test\migration` 只能证明 fresh install，不能证明旧库升级兼容，也不能作为生产升级门禁。当前 `run-test.ps1` 会对 `migrate.py` 返回 exit 2，因此不存在可由 AI/自动化执行的迁移命令。

真实旧库/生产迁移须等待 U1/G8/FT5 user-only gate 完整交付；届时也必须由用户在明确授权、备份和脱敏旧版夹具验证后按专用 Runbook 执行。当前尚未执行真实数据迁移，AI 不读取 `.data/` 验证。

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
默认 CLI：通过 run-windows.ps1 执行 tests/offline_entrypoint.py cli <CLI-subcommand>
pytest Direct：转交 tests/offline_entrypoint.py pytest，在 pytest/plugin 导入前建立 G0，再加载根 tests/conftest.py
Python Direct：执行 tests/offline_entrypoint.py python，再由同进程 runpy 运行仓库目标
非 Python Direct：仍经 wrapper，但原样执行且不属于 Python G0、不保证离线
    ↓
离线路径使用 base-only Config；产品导入前清理 live/secret/proxy 并安装相应 guard
    ↓
操作 .data-test/ 数据(隔离)
    ↓
生产数据 .data/ 未受影响 ✓
```

### 数据库迁移数据流

```
AI/自动化
    ↓
run-test.ps1 识别 migrate.py
    ↓
fail-closed，exit 2（不读取、不迁移）

真实旧库/生产路径
    ↓
U1/G8/FT5 user-only gate 尚未交付
    ↓
保持阻塞；当前尚未执行真实数据迁移
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
| `rebuild-dev-vault.py` | 开发专用轻量重建（隔离根清理/迁移/确定性种子/健康检查，P1） |
| `migrate.py` | 已停用的原始迁移入口（fail-closed） |
| `init_db.py` | 已停用的原始初始化入口（fail-closed） |

### 迁移脚本

| 文件 | 版本 | 说明 |
|------|------|------|
| `migrations/001_initial_schema.sql` | 1.0.0 | M1 初始 Schema |
| `migrations/002_add_cli_tables.sql` | 1.1.0 | M6 CLI 统计表 |
| `migrations/004_add_chat_sessions.sql` | 1.1.1 | AI 对话会话表 |
| `migrations/005_add_review_system.sql` | 1.1.2 | 审核系统表 |
| `migrations/006_add_relations_foundation.sql` | 1.2.0 | Phase A 关系层基础表 |
| `migrations/007_add_timeline_time_fields.sql` | 1.2.1 | 真实事件/发布时间字段 |
| `migrations/008_align_fts_contract.sql` | 1.2.2 | FTS 表与触发器合同对齐 |
| `migrations/009_repair_fts_storage_contract.sql` | 1.2.3 | FTS 存储合同修复 |
| `migrations/010_add_storage_operation_commits.sql` | 1.2.4 | 跨存储操作提交凭据 |
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
**最后更新**: 2026-08-13

*本文档由 Claude Code 自动生成*
