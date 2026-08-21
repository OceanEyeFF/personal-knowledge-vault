# Personal Knowledge Vault - 快速开始

> **当前用途**：PKV 是个人知识数据库，管理互联网信源、历史新闻和过往文章。CLI、MCP 与未来 GUI 共用一个 PKV 数据根；外部应用（例如 `个人文章`）只是调用方。

**最后更新**：2026-08-21
**适用对象**：本仓库的内部个人使用者与开发者
**不适用**：正式发布、PyPI 安装、历史 release candidate 升级或自动迁移

---

## 1. 当前运行合同

| 项目 | 当前规则 |
|---|---|
| 可编辑配置 | `%USERPROFILE%\.pkv\config.yaml`，仅此一份；可含 Provider Key/Cookie |
| 默认数据根 | `%USERPROFILE%\.pkv\data` |
| 数据根优先级 | `PKV_DATA_ROOT` → `storage.data_root` → 默认数据根 |
| 日志级别覆盖 | `PKV_LOG_LEVEL`；与 `PKV_DATA_ROOT` 是仅有的正式环境变量 |
| runtime snapshot | `<data-root>/config/local.yaml`；PKV 管理、无密钥、不可编辑，也不参与业务配置 merge |
| 首次初始化 | `inspect` → `setup` 计划 → 用户确认的 `setup --apply --confirm PLAN_ID --allow-network` |
| 并发 | 同一数据根允许多个读操作，但一次只允许一个写操作；冲突返回 `write_busy` |

历史 checkout 的 `config/local.yaml`、`.data/`，以及历史 `%LOCALAPPDATA%` held candidate 都不是当前产品数据根。本指南不读取内容、不复制、不删除、不迁移这些路径；若需要处理它们，先查看影响与备份策略，再由用户确认专门方案。

---

## 2. 安装开发环境

推荐 Windows + Conda + Python 3.11：

```powershell
# 在源码 checkout 根目录执行。该脚本只准备开发依赖，
# 不会初始化/迁移用户数据根，也不会接管旧 checkout 目录。
.\scripts\setup-conda.ps1
conda activate py311-private
```

或在已有 Python 3.11+ 虚拟环境中安装：

```powershell
python -m pip install -r requirements.txt
```

依赖安装可能访问 Conda/PyPI 镜像；这不是 PKV Provider 调用。

---

## 3. 准备唯一用户配置

用户只需编辑 `%USERPROFILE%\.pkv\config.yaml`。下面的 PowerShell 片段只会在该文件不存在时复制无密钥模板，已有文件不会被覆盖：

```powershell
$pkvProfile = Join-Path $env:USERPROFILE '.pkv'
$userConfig = Join-Path $pkvProfile 'config.yaml'
New-Item -ItemType Directory -Path $pkvProfile -Force | Out-Null

if (-not (Test-Path -LiteralPath $userConfig)) {
    try {
        [System.IO.File]::Copy(
            (Resolve-Path -LiteralPath .\config\config.yaml).Path,
            $userConfig,
            $false  # overwrite = false；并发创建时绝不覆盖另一份配置
        )
    } catch [System.IO.IOException] {
        if (-not (Test-Path -LiteralPath $userConfig)) { throw }
    }
}

notepad $userConfig
```

首次 setup 必须能验证 LLM 与 Embedding Provider，因此在配置中填入可用服务（真实 Key 只留在此文件）：

```yaml
ai:
  llm:
    base_url: "https://your-llm.example/v1"
    api_key: "<local-only>"
    model: "<llm-model>"
  embedding:
    base_url: "https://your-embedding.example/v1"
    api_key: "<local-only>"
    model: "<embedding-model>"
    dim: 1536

# 可选；未设置时使用 %USERPROFILE%\.pkv\data。
# storage:
#   data_root: "D:\\PKV\\data"
```

如果只想让当前终端临时使用另一个根：

```powershell
$env:PKV_DATA_ROOT = 'D:\PKV\data'
$env:PKV_LOG_LEVEL = 'DEBUG'
```

不要通过 `DATA_DIR`、`DB_PATH`、`VAULT_DIR`、`VECTOR_DIR`、`LOG_DIR` 或 `TMP_DIR` 配置产品；它们只为隔离测试内部兼容而保留。也不要在 `<data-root>/config/local.yaml` 写入 Key、Cookie、endpoint 或业务配置。

---

## 4. 第一次运行：检查、计划、确认执行

所有用户数据写入都从只读检查开始：

```powershell
# 只读：显示 root、readiness、风险、现有状态和计划。
python -m src.main inspect

# 仍只读：显示 setup 动作、影响范围、PLAN_ID、网络与备份/保留说明。
python -m src.main setup
```

首次 fresh setup 的计划会包含 LLM/Embedding 的最小健康探测、创建新数据库以及写入无密钥 runtime snapshot。健康探测会访问你配置的 endpoint，可能产生网络流量和 Provider 费用。确认目标根、计划影响、`PLAN_ID`、`requires_network` 后，再由用户执行：

```powershell
$planId = '从上一条 setup 输出复制的 PLAN_ID'
python -m src.main setup --apply --confirm $planId --allow-network

# 只读复核，预期进入 READY。
python -m src.main inspect
```

`--apply --confirm PLAN_ID --allow-network` 是一次特定计划的授权，不能复用。如果配置、数据根或 runtime 状态改变，重新 `inspect` / `setup` 取得新 ID。`setup` 遇到非空、不完整或已有数据的根时不会把它当 fresh install；使用 `repair` 先查看只读计划，不要自行删除数据库或向量目录。

---

## 5. 使用 CLI 与 MCP

在 `READY` 后，源码 checkout 的 CLI 入口是：

```powershell
python -m src.main --help
python -m src.main archive "https://example.com/article" --skip-sharpen
python -m src.main archive-text "一条本地笔记" --title "示例笔记"
python -m src.main search "关键词" --strategy bm25
python -m src.main related 1 --format json
python -m src.main stats
```

MCP 只支持 stdio：

```powershell
python -m src.mcp
```

真实 URL archive、AI 分析、Embedding、向量/混合检索均可能联网和收费。读操作可以并行；写操作发生冲突时，CLI/MCP 会返回可恢复的 `write_busy`，应等待再试。外部 GUI 仅能通过 `pkv_kernel` 使用这一数据根，不能从 GUI import `src.*`。

---

## 6. 验证开发 checkout（隔离且离线）

默认验证不能读取用户 profile、真实 Vault、真实 Key 或真实 Provider：

```powershell
.\scripts\test-conda.ps1

.\scripts\run-test.ps1 -Direct -DataRoot .data-test\quickstart -Command @(
  "python", "-m", "pytest", "-q"
)
```

这只验证合成 `.data-test` 根与离线合同；不等于真实数据、Provider 连通性或迁移已获验证。

---

## 7. 常见问题

### 配置还没写好

先编辑 `%USERPROFILE%\.pkv\config.yaml`，然后重新运行 `python -m src.main inspect`。不要把 Key 作为命令行参数传入，也不要使用 runtime snapshot 当作第二份配置。

### `setup` 没有执行写入

这是设计行为。`setup` 默认只展示计划；检查输出的 `PLAN_ID` 和 `requires_network`，再显式传入 `--apply --confirm PLAN_ID --allow-network`。

### 发现旧 `config/local.yaml` 或 `.data/`

保留它们，不要复制、删除或指向新 root。当前流程不会自动接管旧目录；需要处理时先做只读 `inspect`，展示影响范围和备份/保留计划，等待用户确认。

### 向量检索提示 drift 或需要重建

Embedding endpoint、模型或维度是索引契约。不要手工删除 `<data-root>/vectors`；先运行 `inspect`，再查看 `repair` / 后续 Embedding 生命周期计划的影响、网络和确认要求。

---

下一步请阅读 [使用手册](使用手册.md) 与 [用户配置与运行数据布局 ADR](../overview/ADR-用户配置与运行数据布局-2026-08.md)。
