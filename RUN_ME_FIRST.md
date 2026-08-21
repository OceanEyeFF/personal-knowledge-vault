# 🚀 PKV：从这里开始

> 本指南面向当前的内部个人使用。PKV 是管理互联网信源、历史新闻和过往文章的无头 Core；CLI、MCP 和未来 GUI 共享同一数据根。`个人文章` 等应用是调用方，不是 PKV 数据目录。

> **当前边界**：本仓库提供 headless Kernel、CLI 与 MCP stdio。桌面 GUI 位于独立的 `pkv-GUI` 仓库；外部 Wrapper 只可依赖 `pkv_kernel`，不能导入 `src.*`。不发布 PyPI，也不把历史 held test candidate 当作正式 release。默认自动化始终离线，只使用 `.data-test` 合成数据，绝不读取真实 Vault、真实配置或 Provider 凭据。

---

## 先记住这三个规则

1. 唯一可编辑的用户配置是 `%USERPROFILE%\.pkv\config.yaml`；它可以保存 Provider Key 与 Cookie，不能提交到仓库或粘贴到日志。
2. 数据根按 `PKV_DATA_ROOT` → `storage.data_root` → `%USERPROFILE%\.pkv\data` 解析。只支持 `PKV_DATA_ROOT` 与 `PKV_LOG_LEVEL` 两个正式环境覆盖。
3. `<data-root>/config/local.yaml`（Windows 上为 `<data-root>\config\local.yaml`）是 PKV 写入的无密钥 runtime snapshot，用来校验数据库和 Embedding 契约；**不要编辑、复制或把 Key 写入其中**。

旧 checkout 中的 `config/local.yaml` 与 `.data/` 可能仍在磁盘上。它们是历史布局：本指南、安装脚本和首次初始化不读取内容、不复制、不删除、不迁移这些路径。若日后需要处理旧数据，先运行 `inspect`，查看影响与备份策略，再等待用户单独确认迁移方案。

---

## 1. 准备环境

推荐 Windows PowerShell + Conda（Python 3.11）：

```powershell
# 在仓库根目录运行；它安装开发环境，不会初始化或迁移用户数据。
.\scripts\setup-conda.ps1
conda activate py311-private
```

也可以在已激活的 Python 3.11+ 虚拟环境中安装依赖：

```powershell
python -m pip install -r requirements.txt
```

安装依赖本身可能访问包源；这与后续 Provider 健康探测是两件事。

---

## 2. 创建并编辑唯一用户配置

以下命令只在配置不存在时从无密钥默认模板创建它，已有配置绝不覆盖。请由用户在自己的 PowerShell 中执行；默认测试和 AI 自动化不执行这组命令。

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

在首次初始化前，填写可用的 LLM 与 Embedding Provider。初始化计划会对两者做最小健康探测；没有可用配置时，`setup` 只会告诉你需要补充配置，不会创建数据库。

```yaml
ai:
  llm:
    base_url: "https://your-llm.example/v1"
    api_key: "<仅保存在本机用户配置中>"
    model: "<llm-model>"
  embedding:
    base_url: "https://your-embedding.example/v1"
    api_key: "<仅保存在本机用户配置中>"
    model: "<embedding-model>"
    dim: 1536

# 可选：持久化地改用另一个单一数据根；所有子目录都从它派生。
# storage:
#   data_root: "D:\\PKV\\data"
```

临时换根只使用当前 PowerShell 会话的正式覆盖：

```powershell
$env:PKV_DATA_ROOT = 'D:\PKV\data'
$env:PKV_LOG_LEVEL = 'DEBUG'
```

环境变量优先于 `storage.data_root`，关闭该 PowerShell 后会失效。不要设置 `DATA_DIR`、`DB_PATH`、`VAULT_DIR`、`VECTOR_DIR`、`LOG_DIR` 或 `TMP_DIR`；它们仅是隔离测试内部兼容 seam，不是产品配置。

---

## 3. 首次初始化：先检查，再计划，再确认

不要手工创建数据库、向量目录或 runtime snapshot。用户在配置完成后按此顺序运行：

```powershell
# 只读：显示当前 root、readiness、风险和可执行计划；不会创建文件。
python -m src.main inspect

# 仍然只读：展示 setup 计划、影响范围、是否需要网络和 PLAN_ID。
python -m src.main setup
```

仔细检查输出中的目标数据根、影响范围、`PLAN_ID`、备份/保留说明与 `requires_network`。首次 fresh setup 通常会探测 LLM 与 Embedding endpoint，可能产生网络流量和 Provider 费用。只有确认这些影响后，才把刚刚输出的 ID 代入下面的变量执行：

```powershell
$planId = '从上一条 setup 输出复制的 PLAN_ID'
python -m src.main setup --apply --confirm $planId --allow-network

# 再次只读检查；应确认 runtime 已达到 READY。
python -m src.main inspect
```

`--apply`、`--confirm PLAN_ID` 与（计划需要时的）`--allow-network` 缺一不可。配置或数据根在确认后发生变化时，计划会过期；重新 `inspect` / `setup`，不要复用旧 ID。已有数据、异常快照或旧目录不是 fresh setup 的目标，应通过 `repair` 的只读计划和单独确认处理。

---

## 4. 日常入口

当 `inspect` 显示 `READY` 后，源码 checkout 可以使用：

```powershell
python -m src.main --help
python -m src.main archive "https://example.com/article" --skip-sharpen
python -m src.main search "关键词" --strategy bm25
python -m src.main stats

# MCP 只支持 stdio。
python -m src.mcp
```

URL 归档、AI 分析、Embedding 与向量/混合检索可能联网并产生 Provider 费用。读操作可以并行；同一数据根不允许两个写操作并行，遇到 `write_busy` 时等待当前写操作完成后再试。

---

## 5. 验证开发环境，不碰用户数据

以下命令仅验证仓库的离线合成环境；它不能证明真实 Provider 连通性，也不会初始化 `%USERPROFILE%\.pkv\data`：

```powershell
.\scripts\test-conda.ps1

.\scripts\run-test.ps1 -Direct -DataRoot .data-test\verify-setup -Command @(
  "python", "src\\utils\\verify_setup.py"
)
```

如需更多细节，请读：

- [快速开始](docs/operations/QUICKSTART.md)
- [开发环境搭建](docs/operations/开发环境搭建.md)
- [使用手册](docs/operations/使用手册.md)
- [用户配置与运行数据布局 ADR](docs/overview/ADR-用户配置与运行数据布局-2026-08.md)

---

*本文档描述当前个人运行布局和显式生命周期边界；它不授权真实迁移、自动接管旧目录、发布或远端操作。*
