# Scripts 目录说明

这个目录包含了 Personal Knowledge Vault 的自动化脚本喵～ ฅ'ω'ฅ

## 📋 脚本列表

### Conda 自动化安装 🌟

#### `setup-test-conda.ps1` - Windows Py311 测试环境

**用途**: 新建一个只用于离线测试的 Python 3.11 Conda 环境。创建环境和安装依赖
需要访问已配置的 Conda/Python 包源；URL processors 使用 urllib3 DNS-pinned
SafeFetcher，不需要浏览器 runtime。脚本不会创建 `config/local.yaml`，也不会创建或读取生产 `.data/`。

```powershell
.\scripts\setup-test-conda.ps1
.\scripts\test-conda.ps1 -EnvironmentName pkv-test-py311 -Suite P0
```

环境基础包由根目录的 `environment.test.yml` 声明；项目依赖安装完成后必须通过
`python -m pip check`。如果目标环境名已存在，脚本会拒绝覆盖或删除，需改用新的
`-EnvironmentName`。

Windows `P0` 预检依次验证默认收集和完整离线套件。MCP 95% 覆盖率是独立的
Windows 专项门禁，不作为 P0 的兼容性结论；使用
`-Suite MCP` 显式执行。也可以使用 `-Suite Smoke`、`-Suite Contract` 或
`-Suite Offline` 缩小范围。所有 pytest 命令都排除 `manual` 与 `network`；项目
运行路径、pytest 临时目录和 cache 目录均位于每次新建的 `.data-test/conda-*` 目录。

---

#### `setup-conda.ps1` - Conda 本地开发环境脚本 ⭐⭐推荐

**用途**: 使用 Conda 创建 Python 3.11 环境并安装依赖

**运行方式**:

```powershell
# 在 PowerShell 中运行
.\scripts\setup-conda.ps1
```

**功能**:

- ✅ 检查 Conda 是否安装
- ✅ 创建 Python 3.11 Conda 环境 (`py311-private`)
- ✅ 通过 `conda run` 固定目标环境
- ✅ 升级 pip
- ✅ 安装所有依赖包
- ✅ 验证关键依赖
- ✅ 首次仅创建不覆盖的用户配置 `%USERPROFILE%\.pkv\config.yaml`
- ✅ 引导用户先执行只读 `inspect`、再审阅 `setup` 计划
- ✅ 检测旧 checkout `config/local.yaml` / `.data` 时只提示并保留，绝不自动迁移

**运行时边界**:

- 默认数据根为 `%USERPROFILE%\.pkv\data`。`PKV_DATA_ROOT` 可在进程级覆盖用户配置中的 `storage.data_root`。
- `<data-root>/config/local.yaml` 是 PKV 维护的无密钥运行时快照，不是可编辑配置。
- 本脚本不创建数据库、向量索引或数据根；任何旧目录迁移必须先展示影响、保留原目录并取得用户确认。

**优势**:

- ✨ 避免 Python 3.13 兼容性问题
- ✨ 环境隔离更彻底
- ✨ 可以方便地切换 Python 版本

---

#### `test-conda.ps1` - Conda 测试脚本

**用途**: 在指定 Conda 环境中运行 smoke、收集契约、离线全套、MCP 覆盖率或 Windows P0 预检

**运行方式**:

```powershell
.\scripts\test-conda.ps1
.\scripts\test-conda.ps1 -EnvironmentName py311-private -Suite P0
.\scripts\test-conda.ps1 -EnvironmentName py311-private -Suite MCP
```

**功能**:

- ✅ 检查 Conda 环境是否存在
- ✅ 强制 Python 3.11 并执行 `pip check`
- ✅ 通过 `run-test.ps1` 使用显式目标环境和隔离路径
- ✅ 默认环境与 `setup-conda.ps1` 一致，为 `py311-private`；
  `setup-test-conda.ps1` 创建的测试环境仍须显式传入 `-EnvironmentName`
- ✅ 默认运行纯离线基础语法 smoke；可选择 Windows P0 或 MCP 覆盖率门禁
- ✅ 排除 manual/network，测试数据只写入 `.data-test/`
- ✅ pytest 的临时目录和 cache 仅由 `run-test.ps1` 管理，调用方不传
  `--basetemp` 或 `cache_dir`
- ✅ 显示测试结果

---

## 🚀 快速开始

### 第一次使用

1. **确保已安装 Conda**:
   - 如果没有，请安装 [Miniconda](https://docs.conda.io/en/latest/miniconda.html) 或 Anaconda
   - 下载地址: <https://docs.conda.io/en/latest/miniconda.html>

2. **运行 Conda 安装脚本**:

   ```powershell
   .\scripts\setup-conda.ps1
   ```

3. **编辑唯一的本机配置**:

   ```powershell
   notepad "%USERPROFILE%\.pkv\config.yaml"
   # 填入 ai.llm.api_key 和 ai.embedding.api_key
   ```

4. **无副作用检查配置、数据根与待执行工作**:

   ```powershell
   .\scripts\run-windows.ps1 python -m src.cli.commands inspect
   ```

5. **先审阅初始化计划；仅在用户确认后才执行**:

   ```powershell
   .\scripts\run-windows.ps1 python -m src.cli.commands setup
   # --allow-network 只授权计划中的最小 Provider 连通性探测，可能联网或产生费用。
   .\scripts\run-windows.ps1 python -m src.cli.commands setup --apply --confirm <PLAN_ID> --allow-network
   ```

`setup-conda.ps1` 不会创建 `%USERPROFILE%\.pkv\data`，也不读取内容、不复制、不删除、不迁移 checkout
遗留的 `config/local.yaml` / `.data`。需要迁移时，先保留旧目录并按用户确认的单独方案处理。

### Legacy 方案 (不推荐)

如果无法使用 Conda，可以查看 [legacy/README.md](legacy/README.md) 了解传统 venv 安装方式。

⚠️ **注意**: Legacy 方案在 Python 3.13 环境下可能遇到兼容性问题。

---

### 测试环境管理 🧪

#### `run-test.ps1` - 测试环境运行脚本

**用途**: 默认自动化入口。使用隔离路径运行 PKV CLI、pytest，或经 FT7 运行受保护的仓库 Direct Python。

**运行方式**:

```powershell
# 查看测试环境统计
.\scripts\run-test.ps1 stats

# 在测试环境离线搜索
.\scripts\run-test.ps1 search "AI" --strategy bm25

# 查看测试环境统计
.\scripts\run-test.ps1 stats

# 直接运行 pytest；仍隔离数据库、Vault、向量、日志和临时目录
.\scripts\run-test.ps1 -Direct -Command @("pytest", "tests\unit\test_text_utils.py", "-q")
```

**功能**:

- ✅ 由脚本直接设置进程级测试路径，不读取 `%USERPROFILE%\.pkv\config.yaml`
- ✅ pytest 先由 `tests/offline_entrypoint.py pytest` 在 pytest/plugin 导入前建立 G0，再由根 `tests/conftest.py` 维持逐用例隔离，CLI/MCP 离线子进程由 `tests/offline_entrypoint.py` 启动
- ✅ Direct Python（FT7）仅接受 pytest 或 `scripts/CLAUDE.md` 所列的显式离线测试 allowlist；仓库内位置本身不足以获准，其他模块/脚本、`-c`、stdin 与解释器 flags 均会拒绝
- ✅ FT7 通过同进程 `runpy` 在产品导入前清理 live/secret/proxy、安装 base-only Config、网络 guard 与子进程 guard
- ✅ 隔离测试数据到 `.data-test/` 目录
- ✅ 自动创建测试目录结构
- ✅ 拒绝将测试数据目录指向生产 `.data/`
- ✅ 通过 `run-windows.ps1` 固定 Conda 环境与 UTF-8 编码
- ✅ 显示测试环境状态

**命令分派**:

- 默认模式经 `tests/offline_entrypoint.py cli` 启动 PKV CLI；联网型 `archive` 不属于默认离线示例。
- pytest Direct 会规范化为 `tests/offline_entrypoint.py pytest`，在 pytest/plugin 导入前建立 G0，再加载根 conftest。
- Python Direct 会改写为 `tests/offline_entrypoint.py python ...`，并按 FT7 规则在同一解释器中执行。
- 非 Python Direct 仍须经 wrapper 启动，但属于原样命令、不在 Python G0 内，也不保证离线；使用前必须单独审查边界与副作用。
- 两种模式都会先设置 `DATA_DIR`、`DB_PATH`、`VAULT_DIR`、`VECTOR_DIR`、`LOG_DIR`、`TMP_DIR`，并拒绝 `.data/`、junction 与符号链接目标。

FT7 是 Python 进程内 guard，不是 OS sandbox。`scripts/setup-test-db.py` 只能通过该入口运行，输出必须精确位于本次 `-DataRoot` 对应的 `DATA_DIR`（默认 `DB_PATH`）。`migrate.py` 与已停用的原始回填/初始化入口会 fail-closed 并返回 exit 2；真实迁移仍受 U1/G8/FT5 user-only gate 阻塞，且尚未执行真实数据迁移。

**使用场景**:

- 在受控 `.data-test` 路径内验证离线新功能（不是 OS sandbox）
- 验证数据库变更
- 开发调试

---

#### 已停用的原始维护入口（R3.1 fence）

`backfill_chunks.py`、`backfill_relations.py`、`init_db.py` 和 `migrate.py` 不再是 PKV
产品入口。它们的裸脚本调用会在读取 `Config` 或打开数据根之前以 exit 2 拒绝；
`run-test.ps1 -Direct` 也会在创建测试运行时路径之前拒绝它们。不要用它们绕过
`inspect → plan → confirm → execute` 或单写者 lease。

少量模块级 helper 仅为 `.data-test` 下的合成 fixture 保留，不是 Wrapper/CLI/MCP 或用户数据根的
支持接口。`check_chunk_index_consistency.py` 则只保留为诊断：它以 SQLite `mode=ro` 打开已存在
数据库，缺失或不安全数据库会 fail-closed（exit 2），绝不会创建 DB 或索引。

---

#### `build-internal-package.ps1` — INTERNAL TEST ONLY onedir + ZIP

**用途**：快速生成仅供维护者本机合成数据自测的 PyInstaller `onedir` 目录和 ZIP。它是
后 M13 P1-A 的内部封包入口，和严格的 `build-release.ps1` 完全分离：不会生成 installer，
不会写入 `dist/release/`，也绝不能被描述为发布或候选发布。

```powershell
# 只构建；输出始终位于 Git 忽略的 dist\internal\
.\scripts\build-internal-package.ps1

# 构建后执行内部自测：自动生成合成 DB，并从仓库外临时工作目录启动解压后的包
.\scripts\build-internal-package.ps1 -Smoke
```

每个包都含有 `INTERNAL-TEST-ONLY.txt` 和 `internal-build-info.json`。后者记录 UTC 构建时间、
Git revision/dirty 状态、Python/平台信息及 `requirements.txt` 声明依赖的版本摘要。构建前会
核验 runtime allowlist；构建后会 fail-closed 拒绝 checkout 遗留的 `config/local.yaml`、`.env`、凭据痕迹、
Vault、数据库/索引、日志及测试 fixture，并以有界递归解析检查冻结 EXE 内的 PyInstaller
CArchive/PYZ、嵌套 ZIP 的解压内容和成员元数据；ZIP 还会拒绝 SFX/拼接、链接、加密、
非规范 Windows 路径及无法完整验证的物理布局。正式 `scripts/build-release.ps1` 与
`scripts/build_release.py` 不受此入口影响。

`-Smoke` 的数据只由 `scripts/run-test.ps1` 创建在单独的 `.data-test\internal-package-*` 根中，
不会读取 `local.yaml` 或真实 Provider 凭据。它从仓库外解压后的 headless onedir 依次检查
CLI `--help`、CLI BM25 和 MCP stdio `initialize`；唯一可作出的结论文字是
`INTERNAL SELF-TEST PASSED`。默认临时外部工作目录会清理；传入 `-KeepSmokeWorkspace` 仅用于
人工排查。自动清理使用逐项 no-follow 审计和叶到根删除；如出现 junction/
symlink/reparse 或目录竞态，会保留 workspace 并失败。

---

#### `rebuild-dev-vault.py` - 开发专用轻量重建入口（P1）

**用途**: 在安全隔离根上执行 可受控清理 → 数据库迁移 → 确定性最小种子 → 健康检查 的完整重建流程，幂等可重复。

**安全契约**:

- 默认根目录为仓库内 `.data-test/rebuild-dev`，绝不隐式指向生产 `.data/`。
- 脚本在导入第三方/产品代码前要求 FT7 runtime attestation；裸 Python 启动会 fail-closed。
- `--root` 必须位于本次 wrapper `-DataRoot` 选中的 `DATA_DIR` 内，不能指向其他 `.data-test` 场景；`.data`、仓库其他目录、仓库外路径、文件系统根、用户主目录等危险目标一律拒绝，无任何旁路开关。
- 危险目标拒绝为纯字符串判断（不解析、不 stat 被拒绝路径）。
- 清理前递归检查 junction / 符号链接 / 硬链接 / 内部挂载点；迁移始终 `--no-backup` 语义（`auto_backup=False`），不会调用读取生产 `.data` 的备份脚本。
- **内部链接安全门**：对已通过边界校验的已存在 root，在任何内容读取（iterdir / manifest / DB）之前执行只读递归内部链接扫描；各读取、清理、迁移、seed 与 manifest 阶段还会复核根目录身份并重新扫描。发现链接/挂载点、根身份变化或扫描无法完整遍历（权限/IO 错误）立即拒绝（exit 2）。执行期间必须由本脚本独占该 root，不支持其他进程并发改名或替换目录。
- **fail-closed**：通过版本化 `rebuild-manifest.json` 识别本脚本完整生成的 root；校验严格 manifest 类型、SQLite quick/foreign-key 检查与标签计数、核心表/FTS 触发器、FTS rowid 与分词后检索字段同步、schema/pending、SQLite 条目与 Markdown 路径/正文/核心 frontmatter 一一对应，以及五个标准目录。任一缺失或漂移均拒绝（exit 1），不写入、不清理，必须显式 `--force` 才能重建。

**运行方式**:

```powershell
# 首次重建或按相同参数检查默认根（wrapper 预建的空标准目录可直接使用）
.\scripts\run-test.ps1 -Direct -DataRoot .data-test\rebuild-dev -Command @("python", "scripts\rebuild-dev-vault.py", "--root", ".data-test/rebuild-dev")

# 指定隔离根并强制完整重建（受控清理）
.\scripts\run-test.ps1 -Direct -DataRoot .data-test\rebuild-dev -Command @("python", "scripts\rebuild-dev-vault.py", "--root", ".data-test/rebuild-dev", "--force")

# 仅健康检查；脚本只读，wrapper 可能预建受控空 scaffold
.\scripts\run-test.ps1 -Direct -DataRoot .data-test\rebuild-dev -Command @("python", "scripts\rebuild-dev-vault.py", "--root", ".data-test/rebuild-dev", "--check-only", "--json")

# 机器可读结果契约（exit 0/1/2）
.\scripts\run-test.ps1 -Direct -DataRoot .data-test\rebuild-dev -Command @("python", "scripts\rebuild-dev-vault.py", "--root", ".data-test/rebuild-dev", "--json")
```

**行为**（同一根重复执行）:

- `--force` 保留已经过链接扫描的 `tmp/` 运行时内容，避免删除当前 wrapper/Conda 正在使用的命令载荷；其余受管内容仍按合同清理重建。

1. 根目录已有内容、未传 `--force` → 先做完整 fail-closed 校验；本次 `--seed` / `--count` / `--no-seed` 还必须与 manifest 一致，通过才报告 `up_to_date`。自定义参数重跑时必须重复传入；如需变更则使用 `--force`。
2. 根目录为空、不存在，或仅含 wrapper 预建的空 scaffold（五个标准目录及 `reports`/`runtime`）→ 迁移 11 个脚本至 v1.2.6 + 生成默认 3 条确定性种子 + 健康检查 + 写入 manifest。
3. 传 `--force` → 受控清理（先校验链接）后完整重建（仅限已通过边界校验的 `.data-test` 专用子目录）。
4. `--check-only` → 必须同样经 `run-test.ps1` 的 Direct Python/FT7 入口；SQLite `mode=ro` 只读，对不存在或不完整的 DB 必须失败（exit 1），不会创建数据库，也不把迁移脚本合法误当 vault 健康。包装器只会预建受控空 scaffold。
5. `--no-seed` → 可验证：manifest 记录 `seeded=false, seed_count=0`，重复运行仍 `up_to_date`。

**离线测试**: `tests/unit/test_rebuild_dev_vault.py`（受控根隔离 / 幂等 / 危险目标拒绝 / 结果契约 / 候选根解析路径监控 / 主存储与 schema 完整性）。测试重建均使用 `.data-test` 下受控临时子目录，外部临时目录仅用于验证“必须被拒绝”。

---

#### `backup-data.ps1` - 数据备份脚本

**用途**: 备份生产数据到 `.data-backup/` 目录

**运行方式**:

```powershell
# 手动备份
.\scripts\backup-data.ps1

# 带说明的备份
.\scripts\backup-data.ps1 -Message "重要更新前的备份"
```

**功能**:

- ✅ 完整备份 `.data/` 目录
- ✅ 生成备份信息文件（时间戳、大小、说明）
- ✅ 显示最近的 5 个备份
- ✅ 自动计算备份大小和文件数

**最佳实践**:

- 重要变更前先备份
- 定期清理旧备份（手动）

---

#### `restore-data.ps1` - 数据恢复脚本

**用途**: 从备份恢复数据

**运行方式**:

```powershell
# 交互式选择备份恢复
.\scripts\restore-data.ps1

# 恢复指定时间戳的备份
.\scripts\restore-data.ps1 -BackupTimestamp "20260216-143000"
```

**功能**:

- ✅ 列出所有可用备份（含详细信息）
- ✅ 交互式选择备份版本
- ✅ 安全确认机制（需输入 YES）
- ✅ 自动验证恢复结果

**警告**:

- ⚠️ 恢复操作会**完全替换**当前 `.data/` 目录
- ⚠️ 建议先备份当前数据再恢复

如需并行隔离多个测试场景，可显式指定数据根目录：

```powershell
.\scripts\run-test.ps1 -DataRoot .data-test\feature-a stats
```

用户/生产运行的服务、模型和密钥只来自 `%USERPROFILE%\.pkv\config.yaml`；默认自动化不会读取该文件，
而由 offline entrypoint 安装 base-only Config。`<data-root>/config/local.yaml` 仅为无密钥运行时快照。

---

## ⚠️ 常见问题

### Q1: PowerShell 提示"无法加载脚本"

**错误信息**:

```
无法加载文件 xxx.ps1，因为在此系统上禁止运行脚本
```

**解决方案**:

```powershell
# 临时允许运行脚本（仅当前会话）
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass

# 或使用 CMD 运行 .bat 脚本
.\scripts\legacy\setup.bat
```

---

### Q2: 虚拟环境激活失败

**解决方案**:

```powershell
# 手动激活虚拟环境
.\.venv\Scripts\Activate.ps1   # PowerShell
.\.venv\Scripts\activate.bat    # CMD
```

---

### Q3: Python 3.13 兼容性问题（lxml、greenlet 编译失败）

**错误信息**: `fatal error C1189: #error: "this header requires Py_BUILD_CORE define"`

**原因**: Python 3.13 太新，部分包还没有提供预编译版本

**推荐解决方案**: 使用 Conda 创建 Python 3.11 环境

```powershell
.\scripts\setup-conda.ps1
```

**或手动降级 Python**:

1. 卸载 Python 3.13
2. 安装 Python 3.11
3. 重新运行 `.\scripts\legacy\setup.ps1`

---

### Q4: hnswlib 安装失败

**原因**: 需要 C++ 编译器

**Windows 解决方案**:

1. 安装 [Visual Studio Build Tools](https://visualstudio.microsoft.com/visual-cpp-build-tools/)
2. 或安装 [Visual Studio Community](https://visualstudio.microsoft.com/)（选择"使用 C++ 的桌面开发"工作负载）

---

## 📝 手动安装步骤

如果自动脚本失败，可以手动执行以下步骤：

```powershell
# 1. 创建虚拟环境
python -m venv .venv

# 2. 激活虚拟环境
.\.venv\Scripts\Activate.ps1

# 3. 升级 pip
python -m pip install --upgrade pip

# 4. 安装依赖
python -m pip install -r requirements.txt

# 5. 首次手动创建唯一用户配置（存在时不要覆盖）
$pkvConfigPath = "$env:USERPROFILE\.pkv\config.yaml"
if (-not (Test-Path -LiteralPath $pkvConfigPath)) {
    New-Item -ItemType Directory -Force "$env:USERPROFILE\.pkv"
    Copy-Item config\config.yaml $pkvConfigPath
}
#    填写 Provider 配置；不要把密钥放入 <data-root>\config\local.yaml

# 6. 先检查，再审阅初始化计划；setup 默认不写入数据根
.\scripts\run-windows.ps1 python -m src.cli.commands inspect
.\scripts\run-windows.ps1 python -m src.cli.commands setup
#    仅在审阅输出后，由用户以 PLAN_ID 显式确认 --apply
```

---

## 💡 提示

- 🔧 首次安装可能需要 3-5 分钟
- 📦 Conda 环境名称: `py311-private` (Python 3.11)
- 🔑 API Key 只放在 `%USERPROFILE%\.pkv\config.yaml`；`<data-root>/config/local.yaml` 绝不保存密钥
- 📝 运行测试确保一切正常
- 🌟 推荐通过 `scripts/run-windows.ps1` 运行命令
- 🔤 PowerShell 脚本使用 UTF-8 BOM 以兼容 Windows PowerShell 5.1；编辑时不要移除 BOM

---

**作者**: 幽浮酱 ฅ'ω'ฅ
**日期**: 2026-02-14
