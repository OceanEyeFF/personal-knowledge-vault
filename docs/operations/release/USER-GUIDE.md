# Personal Knowledge Vault 0.8.1 用户指南

本文档随 `Personal Knowledge Vault 0.8.1` Windows x86-64 unsigned test candidate 提供。当前产物固定声明 `artifact_kind=test_candidate`、`artifact_status=test-candidate-on-compliance-hold`、`release_eligible=false`，只用于 W4 功能 E2E 和受控评估，不是正式 release，也不应接入生产 Vault。候选包无需安装 Python 或 Conda；请勿把仓库源码运行说明当作本候选包的安装步骤。

## 1. 支持范围

本候选面向 Windows x86-64、离线优先、fresh-install 场景，包含以下入口：

- `app\pkv-gui.exe`：桌面 GUI
- `app\pkv.exe`：CLI
- `app\pkv-mcp.exe`：MCP stdio server

运行环境下限为 Windows 10 1809 x86-64 或 Windows 11 x86-64，以及 Windows PowerShell 5.1 或更新版本。安装、卸载和下面的运维命令应先通过：

```powershell
if (-not [Environment]::Is64BitOperatingSystem) {
  throw "PKV 0.8.1 requires x86-64 Windows"
}
if ($PSVersionTable.PSVersion -lt [version]'5.1') {
  throw "PKV 0.8.1 requires Windows PowerShell 5.1 or newer"
}
```

默认浏览、离线文本归档、BM25 搜索和 MCP 能力发现不需要 Provider。Chat、摘要、向量与混合检索需要用户自行配置正常的 OpenAI-compatible Provider。

本候选不包含 MCP HTTP transport 或 Bearer 合同，不支持历史数据库原地升级，也不代表真实用户 Vault 已完成质量验收。

候选的安装和启动成功不等于 W4 GUI Artifact 验收。可采信的 GUI W4 evidence 必须来自解锁、可交互的 Windows 桌面与 UI Automation；headless、Windows service session 或已断开的远程桌面不能替代该环境。

## 2. 受控候选安装

当前 W4 输入的 canonical test candidate 名为：

```text
PersonalKnowledgeVault-0.8.1-windows-x86_64.zip
```

构建同时生成同名的 `.zip.sha256` 与 `.provenance.json` sidecar。先验证 ZIP 身份、hash 与合规 hold 状态：

```powershell
$pkvZip = 'D:\path\to\PersonalKnowledgeVault-0.8.1-windows-x86_64.zip'
$pkvHashParts = (Get-Content -LiteralPath "$pkvZip.sha256" -Raw).Trim() -split '\s+', 2
if ($pkvHashParts.Count -ne 2 -or $pkvHashParts[1] -ne (Split-Path -Leaf $pkvZip)) {
  throw "PKV SHA-256 sidecar names a different Artifact"
}
$pkvExpected = $pkvHashParts[0].ToLowerInvariant()
$pkvActual = (Get-FileHash -LiteralPath $pkvZip -Algorithm SHA256).Hash.ToLowerInvariant()
if ($pkvActual -ne $pkvExpected) {
  throw "PKV Artifact SHA-256 mismatch"
}
$pkvProvenancePath = Join-Path (Split-Path -Parent $pkvZip) `
  'PersonalKnowledgeVault-0.8.1-windows-x86_64.provenance.json'
$pkvProvenance = Get-Content -LiteralPath $pkvProvenancePath -Raw -Encoding UTF8 |
  ConvertFrom-Json
$pkvExpectedBlockers = @(
  'conda-native-license-materials-and-spdx'
  'html2text-gpl-compliance'
  'native-msvc-license-and-provenance'
  'qt-corresponding-source-location'
  'qt-linkage-and-replacement-not-proven'
  'qt-module-license-audit'
  'qt-notice-placeholders'
)
if (
  $pkvProvenance.schema_version -ne 'pkv.artifact-provenance.v1' -or
  $pkvProvenance.version -ne '0.8.1' -or
  $pkvProvenance.artifact_file -ne (Split-Path -Leaf $pkvZip) -or
  ([string]$pkvProvenance.artifact_sha256).ToLowerInvariant() -ne $pkvActual -or
  $pkvProvenance.artifact_kind -ne 'test_candidate' -or
  $pkvProvenance.artifact_status -ne 'test-candidate-on-compliance-hold' -or
  [bool]$pkvProvenance.release_eligible -or
  ((@($pkvProvenance.release_blockers) | ConvertTo-Json -Compress) -cne
    ($pkvExpectedBlockers | ConvertTo-Json -Compress))
) {
  throw "PKV provenance does not bind the expected held 0.8.1 test candidate"
}
```

七个 blocker 按 UTF-8 顺序写入 provenance：`conda-native-license-materials-and-spdx`、`html2text-gpl-compliance`、`native-msvc-license-and-provenance`、`qt-corresponding-source-location`、`qt-linkage-and-replacement-not-proven`、`qt-module-license-audit`、`qt-notice-placeholders`。其中 `html2text-gpl-compliance` 只有在 `combined-work-licensing-decision`、`corresponding-source-scope-and-persistent-location`、`spdx-license-expression`、`whole-work-license-and-notices` 四项 machine-readable requirement 全部闭合后才能解除。任一 blocker 未关闭时，即使 W4 的 10 个功能场景全部通过，`functional_verified` 也只表示功能验证完成，最终决策仍必须是 `hold`，不能据此把候选重命名、复制或宣传为 release。

仅在隔离数据根和受控评估环境中继续安装：

1. 将 ZIP 完整解压到一个临时目录。不要直接从 ZIP 浏览器中运行脚本或程序。
2. 进入唯一顶层目录 `PersonalKnowledgeVault-0.8.1-windows-x86_64`；该目录应同时包含 `Install.ps1`、`Uninstall.ps1`、`app\`、`payload-manifest.json` 和本文件。
3. 在 PowerShell 中运行：

```powershell
powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass `
  -File .\Install.ps1 `
  -AllowComplianceHoldTestCandidate `
  -ComplianceHoldConfirmation 'W4-TEST-CANDIDATE-NOT-FOR-DISTRIBUTION'
```

这两个候选 override 参数及确认 token 必须同时精确提供；完整参数合同是
`-AllowComplianceHoldTestCandidate -ComplianceHoldConfirmation W4-TEST-CANDIDATE-NOT-FOR-DISTRIBUTION`。
缺失、拼写不同或大小写不同都会 fail-closed。它们只授权 W4/受控评估安装当前
compliance-held test candidate，不授予分发权限；对真正的 release Artifact 反而禁止提供这两个参数。

安装脚本会先按 `payload-manifest.json` 校验文件大小和 SHA-256，再执行当前用户安装。默认程序目录为：

```text
%LOCALAPPDATA%\Programs\PersonalKnowledgeVault
```

需要把受控评估与默认程序目录隔离时，可在上述安装命令中追加
`-InstallRoot "$env:LOCALAPPDATA\Programs\PersonalKnowledgeVault-W4"`。`-InstallRoot` 必须是
`%LOCALAPPDATA%\Programs` 的严格子目录；指向该目录本身或其外部都会 fail-closed。后续启动
示例中的 `$pkvProgramRoot` 必须改成同一个自定义根，卸载时也必须向 `Uninstall.ps1` 传入
同一个值。不要把自定义程序目录放在源码仓库、候选解压目录、`PKV_DATA_ROOT` 或现有 Vault 内。

成功时脚本向 stdout 输出一行 JSON，`status` 为 `installed`；重复安装完全相同的 `0.8.1` payload 时为 `already_installed`。校验失败时不要手工跳过检查或复制单个文件，请重新取得并完整解压候选包。

本候选不承诺 portable 运行。受支持的已安装入口是 `Install.ps1` 校验并复制后的三个 EXE，不要把临时解压目录当作长期程序目录。

## 3. 首次启动

安装后先为受控评估设置一个不存在或专用的绝对数据根，再从 PowerShell 启动：

```powershell
$pkvProgramRoot = Join-Path $env:LOCALAPPDATA 'Programs\PersonalKnowledgeVault'
$env:PKV_DATA_ROOT = 'D:\PKV-0.8.1-Candidate-Data'

# GUI
& "$pkvProgramRoot\app\pkv-gui.exe"

# CLI 版本与帮助
& "$pkvProgramRoot\app\pkv.exe" --version
& "$pkvProgramRoot\app\pkv.exe" --help
```

首次实际启动会在 `PKV_DATA_ROOT` 指向的隔离根中创建所需目录和 fresh 数据库。当前候选不得指向生产 Vault；程序目录与用户数据目录相互独立，不要把用户配置或 Vault 放进 `app\` 或 `_internal\`。下表中的默认根仅用于说明程序合同，不改变本候选应隔离评估的要求。

## 4. Bundled 资源与用户数据

| 类型 | 默认位置 | 规则 |
|---|---|---|
| 程序与 bundled 只读资源 | `%LOCALAPPDATA%\Programs\PersonalKnowledgeVault` | 由安装清单管理；包含三个入口、基础配置、workflows、migrations、Qt/native 依赖、许可证与 manifest，不应手工修改 |
| 用户数据根 | `%LOCALAPPDATA%\PersonalKnowledgeVault` | 所有用户可写状态必须位于此根内；默认卸载保留 |
| 本机私有配置 | `%LOCALAPPDATA%\PersonalKnowledgeVault\config\local.yaml` | 可包含 Provider key；不要分享或提交到版本库 |
| GUI 设置 | `%LOCALAPPDATA%\PersonalKnowledgeVault\config\ui.ini` | GUI 本机状态 |
| SQLite | `%LOCALAPPDATA%\PersonalKnowledgeVault\db\knowledge_vault.db` | 必需索引与会话数据；运行时不要手工编辑 |
| Markdown Vault | `%LOCALAPPDATA%\PersonalKnowledgeVault\vault` | 主存储 |
| 向量索引 | `%LOCALAPPDATA%\PersonalKnowledgeVault\vectors` | 可修复辅助索引；与 embedding 模型和维度绑定 |
| 日志 | `%LOCALAPPDATA%\PersonalKnowledgeVault\logs` | 故障排查入口 |
| 临时/运行状态 | `%LOCALAPPDATA%\PersonalKnowledgeVault\tmp`、`runtime` | 不应作为备份事实源 |
| 内部备份目录 | `%LOCALAPPDATA%\PersonalKnowledgeVault\backups` | 供受控数据库操作使用；不能代替完整的用户冷备份 |

高级隔离场景可在启动每个 PKV 进程前设置绝对的 `PKV_DATA_ROOT`：

```powershell
$env:PKV_DATA_ROOT = 'D:\PKV-Data-0.8.1'
& "$env:LOCALAPPDATA\Programs\PersonalKnowledgeVault\app\pkv-gui.exe"
```

设置后，`config\local.yaml`、DB、Vault、vectors、logs、tmp、backups 与 runtime 均相对于该根。GUI、CLI 与 MCP 必须使用同一个值；不要在运行中切换。卸载器只识别并可选择删除默认 `%LOCALAPPDATA%\PersonalKnowledgeVault`，不会接管自定义根；自定义根必须由用户另行备份和管理。

## 5. 首次配置

BM25、浏览与 MCP stdio discovery 可保持无 key 运行。需要 Provider-backed 能力时，创建或编辑：

```powershell
$pkvDataRoot = if ($env:PKV_DATA_ROOT) {
  [System.IO.Path]::GetFullPath($env:PKV_DATA_ROOT)
} else {
  Join-Path $env:LOCALAPPDATA 'PersonalKnowledgeVault'
}
$pkvLocalConfig = Join-Path $pkvDataRoot 'config\local.yaml'
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $pkvLocalConfig) | Out-Null
notepad.exe $pkvLocalConfig
```

最小示例：

```yaml
ai:
  llm:
    provider: openai_compatible
    api_key: "replace-with-your-key"
    base_url: "https://api.deepseek.com/v1"
    model: "deepseek-chat"
  embedding:
    provider: openai_compatible
    api_key: "replace-with-your-key"
    base_url: "https://api.openai.com/v1"
    model: "text-embedding-3-small"
    dim: 1536
```

注意：

- 不使用 `.env`、Provider 环境变量或仓库内 `config\local.yaml` 作为候选配置源。
- 更换 embedding endpoint、model 或 `dim` 后，旧向量索引不再兼容，必须在受控流程中重建；不要直接改 metadata 规避检查。
- Chat 的自动验收使用候选 payload 之外的 deterministic loopback harness；该 harness 不是用户功能且不会随候选包提供。正常使用 Chat 必须配置真实 Provider。

## 6. 常用 CLI

```powershell
$pkv = Join-Path $env:LOCALAPPDATA 'Programs\PersonalKnowledgeVault\app\pkv.exe'

# 离线 BM25
& $pkv search "关键词" --strategy bm25

# 浏览与统计
& $pkv list --limit 20
& $pkv stats

# Provider 配置有效时才使用向量/混合检索
& $pkv search "语义查询" --strategy vector
& $pkv search "混合查询" --strategy hybrid
```

公开检索终态区分 `success`、`no_hits`、`invalid`、`error` 与 `degraded`。Provider 缺失或失败不应被理解成“没有命中”。

## 7. MCP stdio 接入

MCP 入口只支持 stdio：

受支持的命令形态为 `pkv-mcp.exe --transport stdio`；以下示例再显式收紧日志级别：

```powershell
$pkvMcp = Join-Path $env:LOCALAPPDATA 'Programs\PersonalKnowledgeVault\app\pkv-mcp.exe'
& $pkvMcp --transport stdio --log-level WARNING
```

在 MCP 客户端配置中使用可执行文件的绝对路径，并把参数作为数组传递。例如：

```json
{
  "mcpServers": {
    "personal-knowledge-vault": {
      "command": "C:\\Users\\<你的用户名>\\AppData\\Local\\Programs\\PersonalKnowledgeVault\\app\\pkv-mcp.exe",
      "args": ["--transport", "stdio", "--log-level", "WARNING"]
    }
  }
}
```

候选能力表面为 14 Tools、2 个静态 Resources、7 个 Resource Templates 与 3 Prompts，总 Resource surface 为 9。客户端若分别提供 `resources/list` 和 `resources/templates/list`，应观察到 `2 + 7`，而不是要求单次 `resources/list` 返回 9 条。

MCP stdout 只用于协议数据。不要用会向 stdout 插入 banner 或日志的 shell wrapper 包裹 `pkv-mcp.exe`。HTTP/streamable-http 与 Bearer Token 均不在 0.8.1 候选能力边界内。

## 8. 冷备份与恢复

0.8.1 没有承诺在线备份或跨版本恢复。完整备份必须在 GUI、CLI、MCP 及其子进程全部退出后，对整个用户数据根制作冷备份。以下示例针对默认数据根；如使用 `PKV_DATA_ROOT`，应把 `$pkvDataRoot` 改为实际的绝对路径。

备份示例：

```powershell
$pkvDataRoot = Join-Path $env:LOCALAPPDATA 'PersonalKnowledgeVault'
$pkvBackupDirectory = 'X:\EncryptedBackups'

if (-not (Get-Command tar.exe -CommandType Application -ErrorAction SilentlyContinue)) {
  throw "Cold backup requires tar.exe; no fallback copy will be attempted"
}

if (-not (Test-Path -LiteralPath $pkvDataRoot -PathType Container)) {
  throw "PKV user data root does not exist: $pkvDataRoot"
}
if (-not (Test-Path -LiteralPath $pkvBackupDirectory -PathType Container)) {
  throw "Choose an existing protected backup directory: $pkvBackupDirectory"
}
$pkvStamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$pkvArchive = Join-Path $pkvBackupDirectory "PKV-0.8.1-$pkvStamp.tar"
$pkvMetadata = "$pkvArchive.metadata.json"
if ((Test-Path -LiteralPath $pkvArchive) -or (Test-Path -LiteralPath $pkvMetadata)) {
  throw "Backup destination already exists"
}

$pkvDataParent = Split-Path -Parent $pkvDataRoot
$pkvDataLeaf = Split-Path -Leaf $pkvDataRoot
& tar.exe -C $pkvDataParent -cf $pkvArchive $pkvDataLeaf
if ($LASTEXITCODE -ne 0) {
  Remove-Item -LiteralPath $pkvArchive -Force -ErrorAction SilentlyContinue
  throw "tar backup failed with exit code $LASTEXITCODE"
}
$pkvArchiveHash = (Get-FileHash -LiteralPath $pkvArchive -Algorithm SHA256).Hash.ToLowerInvariant()
$pkvBackupInfo = [ordered]@{
  schema_version = 'pkv.user-cold-backup.v1'
  app_version = '0.8.1'
  root_name = $pkvDataLeaf
  archive_file = Split-Path -Leaf $pkvArchive
  archive_sha256 = $pkvArchiveHash
}
[System.IO.File]::WriteAllText(
  $pkvMetadata,
  (ConvertTo-Json -InputObject $pkvBackupInfo -Compress) + "`n",
  [System.Text.UTF8Encoding]::new($false)
)
```

`X:\EncryptedBackups` 必须替换为用户控制的加密或等价受保护位置。备份可能包含 API key、Cookie、聊天内容和完整 Vault；`.tar` 与 `.metadata.json` 必须成对保留，不应放入公开同步目录。

恢复只支持把同一 `0.8.1` 冷备份恢复到不存在的默认数据根；命令先验证版本、archive SHA-256 与成员路径，再在临时目录解包并发布，不会合并或覆盖现有数据：

```powershell
$pkvArchive = 'X:\EncryptedBackups\PKV-0.8.1-yyyyMMdd-HHmmss.tar'
$pkvMetadata = "$pkvArchive.metadata.json"
$pkvDataRoot = Join-Path $env:LOCALAPPDATA 'PersonalKnowledgeVault'

if (-not (Get-Command tar.exe -CommandType Application -ErrorAction SilentlyContinue)) {
  throw "Cold restore requires tar.exe; no fallback extraction will be attempted"
}

if (Test-Path -LiteralPath $pkvDataRoot) {
  throw "Refusing to overwrite existing PKV user data: $pkvDataRoot"
}
$pkvBackupInfo = Get-Content -LiteralPath $pkvMetadata -Raw -Encoding UTF8 | ConvertFrom-Json
if (
  $pkvBackupInfo.schema_version -ne 'pkv.user-cold-backup.v1' -or
  $pkvBackupInfo.app_version -ne '0.8.1' -or
  $pkvBackupInfo.root_name -ne 'PersonalKnowledgeVault'
) {
  throw "Backup metadata is not a default-root PKV 0.8.1 cold backup"
}
$pkvActualHash = (Get-FileHash -LiteralPath $pkvArchive -Algorithm SHA256).Hash.ToLowerInvariant()
if ($pkvActualHash -ne ([string]$pkvBackupInfo.archive_sha256).ToLowerInvariant()) {
  throw "Backup archive SHA-256 mismatch"
}

$pkvMembers = @(& tar.exe -tf $pkvArchive)
if ($LASTEXITCODE -ne 0 -or $pkvMembers.Count -eq 0) {
  throw "Cannot list backup archive"
}
foreach ($pkvMember in $pkvMembers) {
  $pkvNormalized = ([string]$pkvMember).Replace('\', '/').TrimEnd('/')
  if (
    ($pkvNormalized -ne 'PersonalKnowledgeVault' -and
      -not $pkvNormalized.StartsWith('PersonalKnowledgeVault/', [System.StringComparison]::Ordinal)) -or
    $pkvNormalized -match '(^|/)\.\.(/|$)'
  ) {
    throw "Unsafe backup member: $pkvMember"
  }
}

$pkvRestoreStage = Join-Path $env:LOCALAPPDATA ('.pkv-restore-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $pkvRestoreStage -ErrorAction Stop | Out-Null
$pkvRestorePublished = $false
try {
  & tar.exe -C $pkvRestoreStage -xf $pkvArchive
  if ($LASTEXITCODE -ne 0) {
    throw "tar restore failed with exit code $LASTEXITCODE"
  }
  $pkvStagedRoot = Join-Path $pkvRestoreStage 'PersonalKnowledgeVault'
  if (-not (Test-Path -LiteralPath $pkvStagedRoot -PathType Container)) {
    throw "Backup archive does not contain the expected data root"
  }
  $pkvUnsafeLink = Get-ChildItem -LiteralPath $pkvStagedRoot -Recurse -Force -ErrorAction Stop |
    Where-Object { ($_.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0 } |
    Select-Object -First 1
  if ($pkvUnsafeLink) {
    throw "Backup contains a forbidden link/reparse point: $($pkvUnsafeLink.FullName)"
  }
  if (Test-Path -LiteralPath $pkvDataRoot) {
    throw "Restore target appeared while validating the backup: $pkvDataRoot"
  }
  # Both paths are under LOCALAPPDATA. Directory.Move refuses an existing target,
  # so a concurrent creator cannot turn this publish into a nested merge.
  [System.IO.Directory]::Move($pkvStagedRoot, $pkvDataRoot)
  $pkvRestorePublished = $true
} catch {
  Write-Warning "Restore failed; staging is retained for manual inspection: $pkvRestoreStage"
  throw
} finally {
  if ($pkvRestorePublished -and (Test-Path -LiteralPath $pkvRestoreStage)) {
    Remove-Item -LiteralPath $pkvRestoreStage -Recurse -Force
  }
}
```

失败时 staging 可能包含完整敏感数据；记录路径、限制访问并保留供人工核验，确认原因后再由用户显式清理。恢复后首次启动若报告数据库版本或 schema 不兼容，请保留现场并停止；不要修改 `schema_version`、删除表或把异常库伪装成 fresh。

## 9. 升级拒绝边界

0.8.1 是 fresh-install Developer Preview，不承诺从历史版本原地升级：

- 当现有程序目录具有合法 `install-state.json` 且其版本不同，`Install.ps1` 输出 `status=upgrade_unsupported` 并以退出码 `20`（exit code 20）拒绝。缺失或非法 install state、manifest 漂移等情况以通用安装失败和退出码 `1` fail-closed；两类拒绝都不会迁移用户数据。
- 运行时会拒绝旧版、未来版、损坏或 schema 漂移的数据库，不把它们当作 fresh 数据库继续运行。
- 卸载旧程序并不会把旧数据转换成 0.8.1 数据。需要保留旧数据时先做冷备份，并把它视为独立资料，而不是可直接恢复的 0.8.1 备份。
- 不要使用源码仓库中的迁移脚本对候选安装的用户数据做未经本版本承诺的升级。

## 10. 卸载

关闭 GUI、CLI 与 MCP 后，从已安装程序目录运行：

```powershell
$pkvProgramRoot = Join-Path $env:LOCALAPPDATA 'Programs\PersonalKnowledgeVault'
powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass `
  -File "$pkvProgramRoot\Uninstall.ps1"
```

默认只删除由 manifest 管理的程序文件，并保留 `%LOCALAPPDATA%\PersonalKnowledgeVault`。卸载前若程序目录被修改、加入额外文件或 manifest 校验失败，脚本会 fail-closed；不要用强制删除掩盖原因。

只有在已完成备份且确定永久删除全部用户数据时，才使用双重确认：

```powershell
$pkvProgramRoot = Join-Path $env:LOCALAPPDATA 'Programs\PersonalKnowledgeVault'
powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass `
  -File "$pkvProgramRoot\Uninstall.ps1" `
  -DeleteUserData `
  -ConfirmDataDeletion DELETE-PKV-USER-DATA
```

该操作不可恢复，会删除配置、数据库、Vault、向量、日志、备份和运行状态。

## 11. 故障排查

### 安装校验失败

- 重新完整取得并解压候选 ZIP。
- 确认从 ZIP 顶层运行 `Install.ps1`，不要移动单个 DLL、EXE 或 manifest。
- 不要编辑 `payload-manifest.json` 绕过 size/hash 检查。

### 启动报告 bundled resource 或 DLL 缺失

- 三个已安装入口位于 `app\`，但都依赖同目录的 `_internal\`；不要把单个 EXE 移出 `app\`，也不要从其他安装复制单个 DLL。
- 同版本 installer 不提供自动修复，payload 已缺失或变更时 uninstaller 也会拒绝。停止所有 PKV 进程、保留用户数据根和错误输出，不要强制覆盖或删除程序目录；重新取得完整候选包进行校验，并在人工确认损坏范围后处理。

### 数据库升级、未来版本或损坏错误

- 停止所有 PKV 进程并保留 `%LOCALAPPDATA%\PersonalKnowledgeVault` 现场。
- 查看 `logs\` 中的日志与 CLI/MCP stderr；记录稳定错误码。
- 不要删除数据库、版本记录或 Vault 文件尝试“修复”。0.8.1 不提供历史库原地升级。

### BM25 可用，但 Chat/向量/混合失败

- 检查 `config\local.yaml` 的 `ai.llm` 或 `ai.embedding` 配置。
- Provider failure 应显示为 error/degraded，而不是 no-hits；不要把 Provider 失败误判成知识库为空。
- 默认离线能力不验证真实 Provider 可用性、额度或第三方服务 SLA。

### MCP 客户端无法连接

- 确认客户端直接启动安装后的 `app\pkv-mcp.exe`，transport 为 `stdio`。
- 确认 command 使用绝对路径，反斜杠已按客户端配置格式正确转义。
- MCP stdout 不得混入 shell banner；诊断日志应走 stderr。

## 12. 已知限制

- `find_bridges`、`timeline_of`、`contrast` 为 `partial-v1`，公开响应继续声明 `implementation_level=partial`；调用方必须读取每次响应的 `limitation_notes`，不能只看 `found/items`：
  - `find_bridges` 只组合受限的显式关系子图、局部图桥接信号与标题/摘要/tags 轻量重合；子图截断时，候选集合和“未发现”结论都不代表全图。
  - `timeline_of` 只按 `event_time > published_at > archived_at` 使用可持久读取的结构化时间，不做正文事件时间抽取；缺少可靠时间的 item 会标记 `time_source/time_precision=unavailable`。
  - `contrast` 只组合检索候选的表层字段与跨主题显式关系路径信号，不是争议、因果、补充等完整语义对比。
- 候选中的 GUI 搜索只保证 BM25。Vector/Hybrid 是 CLI/MCP 的显式 Provider-backed 能力。
- Workflow 只支持候选包内版本化的 `archive-url.yaml` 与 `archive-text.yaml`；不支持 `search.yaml` workflow。
- MCP 仅 stdio；HTTP transport、Bearer、历史库原地升级、真实快照质量结论与正式稳定版 SLA 均未承诺。
- 候选包不包含测试 fixture、真实数据、API key、local config、外置 Chat loopback harness 或 fake provider。

## 13. 构建、合规状态与材料

候选 ZIP 顶层应同时包含：

- `build-info.json`：版本、源码 revision、构建指纹、工具链摘要，以及与 provenance 一致的 `artifact_kind`、`artifact_status`、`release_eligible`、`release_blockers`
- `dependency-manifest.json`：锁定依赖，以及同一 `artifact_status`、`release_eligible`、`release_blockers`
- `sbom.cdx.json`：CycloneDX SBOM；`metadata.properties` 以 `pkv:artifact-status`、`pkv:release-eligible` 和逐项 `pkv:release-blocker` 公开同一 hold 状态
- `payload-manifest.json`：payload 文件、大小和 SHA-256；安装与卸载据此 fail-closed
- `LICENSE`、`THIRD-PARTY-NOTICES.txt`、`licenses\index.json` 与 `licenses\` 中被索引的原始材料：许可证合同
- `USER-GUIDE.md`：本文件

这些文件属于 Artifact 合同。缺失、无法解析、hash 不匹配、版本不是 `0.8.1`，或合规状态与 provenance 不一致时，不应安装或继续验收。源码树 smoke、W2 handoff 或 W3-T0 的合成 preflight 不能替代安装后 Artifact 证据。

维护者只能从 clean checkout 和锁定构建环境使用以下单一入口生成候选或 release Artifact：

```powershell
powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass `
  -File .\scripts\build-release.ps1
```

命令会执行 PyInstaller A/B 构建并要求两个 unsigned ZIP 的 SHA-256 完全一致。当前七个合规 blocker 非空，因此 exit code `0` 只表示 test candidate 可复现；产品 ZIP 与 sidecar 只能输出到：

```text
dist\candidate\PersonalKnowledgeVault-0.8.1-windows-x86_64.zip
dist\candidate\PersonalKnowledgeVault-0.8.1-windows-x86_64.zip.sha256
dist\candidate\PersonalKnowledgeVault-0.8.1-windows-x86_64.provenance.json
```

对应源码材料固定输出到：

```text
dist\compliance-sources\html2text-2020.1.16.tar.gz
dist\compliance-sources\html2text-2020.1.16.tar.gz.sha256
dist\compliance-sources\manifest.json
dist\compliance-sources\provenance.json
```

W4 使用的外置 deterministic harness 保持在 `dist\e2e-harness\`，不得进入产品 payload。只有合规权威状态同时满足 `release_eligible=true` 与 `release_blockers=[]` 时，产品 Artifact 才可声明 `artifact_kind=release` 并路由到 `dist\release\`；否则 `dist\release\` 必须不存在。
