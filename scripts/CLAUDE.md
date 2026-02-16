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
- 创建 Python 3.11 Conda 环境 (`pkv-py311`)
- 激活环境并升级 pip
- 安装所有依赖包
- 验证关键依赖
- 创建 `.env` 配置文件(从 `.env.example` 复制)
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
- 检查 `pkv-py311` 环境是否存在
- 激活 Conda 环境
- 运行 `src/utils/verify_setup.py` 验证脚本
- 显示测试结果

**适用场景**:
- 首次安装后验证
- 更新依赖后验证
- 故障排查

---

### 测试环境管理脚本

#### run-test.ps1 (重要!)

**用途**: 使用隔离的测试数据库运行 PKV 命令(不影响生产数据)

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
```

**功能**:
- 自动加载 `.env.test` 测试配置
- 设置环境变量: `$env:DB_PATH = ".data-test/db/knowledge_vault.db"`
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
.\scripts\check-environment.ps1
```

**输出示例**:

```
========================================
 环境检测
========================================

当前数据库路径（默认配置）:
  E:\gitee\personal-knowledge-vault\.data\db\knowledge_vault.db
  ⚠️  生产环境

数据库统计:
  总条目数: 125
  最后更新: 2026-02-16 12:30:00

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

详见: [docs/数据库迁移指南.md](../docs/数据库迁移指南.md)

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

### Python 脚本依赖

- Python 3.11+ (推荐使用 Conda 环境)
- 依赖: `src/` 模块 (自动导入)

### 配置文件

#### .env.test.example

**用途**: 测试环境配置模板

**使用方式**:
```powershell
# 复制模板文件
copy .env.test.example .env.test

# 编辑配置
notepad .env.test
```

**配置项**:
```env
# 数据库路径(使用测试专用目录)
DB_PATH=.data-test/db/knowledge_vault.db

# 测试用 API Keys(可选)
DEEPSEEK_API_KEY=sk-test-your-key
OPENAI_API_KEY=sk-test-your-key

# 日志级别(DEBUG 获取详细日志)
LOG_LEVEL=DEBUG
```

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
# 方法 1: 删除测试目录
Remove-Item -Recurse -Force .data-test

# 方法 2: 运行清理命令
.\scripts\run-test.ps1 stats  # 先查看统计
# 然后手动删除
```

---

### Q3: 数据库迁移失败如何回滚?

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

# 2. 激活环境
conda activate pkv-py311

# 3. 编辑配置
notepad .env

# 4. 验证安装
.\scripts\test-conda.ps1

# 5. 初始化数据库
python scripts/migrate.py
```

---

### 场景 2: 测试新功能

```powershell
# 1. 确认当前环境
.\scripts\check-environment.ps1

# 2. 在测试环境测试
.\scripts\run-test.ps1 archive "https://example.com"
.\scripts\run-test.ps1 search "测试"

# 3. 验证生产数据未受影响
python -m src.main stats
```

---

### 场景 3: 数据库升级

```powershell
# 1. 备份生产数据
.\scripts\backup-data.ps1 -Message "升级到 v1.2.0 前备份"

# 2. 在测试环境验证迁移
$env:DB_PATH = ".data-test/db/knowledge_vault.db"
python scripts/migrate.py --dry-run

# 3. 执行测试环境迁移
python scripts/migrate.py --auto

# 4. 验证测试环境
.\scripts\run-test.ps1 stats

# 5. 执行生产环境迁移(谨慎!)
Remove-Item env:DB_PATH
python scripts/migrate.py

# 6. 验证生产环境
python -m src.main stats
```

---

### 场景 4: 数据恢复

```powershell
# 1. 列出可用备份
.\scripts\restore-data.ps1

# 2. 选择要恢复的备份并确认

# 3. 验证恢复结果
python -m src.main stats
python -m src.main list --limit 5
```

---

## 数据流

### 测试环境数据流

```
.\scripts\run-test.ps1 <command>
    ↓
加载 .env.test (测试配置)
    ↓
设置 $env:DB_PATH = ".data-test/db/knowledge_vault.db"
    ↓
创建 .data-test/ 目录结构
    ↓
执行 python -m src.main <command>
    ↓
Config 读取 $env:DB_PATH
    ↓
操作 .data-test/ 数据(隔离)
    ↓
生产数据 .data/ 未受影响 ✓
```

### 数据库迁移数据流

```
python scripts/migrate.py
    ↓
MigrationManager.get_current_version()
  → 读取 schema_version 表
    ↓
MigrationManager.get_pending_migrations()
  → 扫描 scripts/migrations/*.sql
  → 比较版本号
    ↓
自动备份 .data/ → .data-backup/{timestamp}/
    ↓
逐个执行迁移脚本
  → 执行 SQL
  → 记录到 schema_version 表
    ↓
显示迁移日志
    ↓
完成 ✓
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
| [docs/测试环境隔离指南.md](../docs/测试环境隔离指南.md) | 测试环境完整文档 |
| [docs/测试环境快速开始.md](../docs/测试环境快速开始.md) | 3 分钟入门 |
| [docs/数据库迁移指南.md](../docs/数据库迁移指南.md) | 数据库迁移完整指南 |
| [docs/AI安全与数据库升级完整方案.md](../docs/AI安全与数据库升级完整方案.md) | AI 安全 + 数据库升级总结 |

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
