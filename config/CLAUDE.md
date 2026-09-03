# Config 配置模块

[根目录](../CLAUDE.md) > **config**

---

## 模块职责

**配置管理**:提供系统配置文件、工作流配置、自定义词典等配置资源。

### 配置类型

- **主配置** (`config.yaml`): 系统全局配置
- **工作流配置** (`workflows/*.yaml`): 工作流定义
- **自定义词典** (`custom_dict.txt`): jieba 分词自定义词典

---

## 配置文件清单

### 主配置文件

#### config.yaml

**用途**: 系统全局配置

**关键配置项**:

```yaml
# 存储配置
storage: {}
# 运行时只使用一个数据根：默认 %USERPROFILE%/.pkv/data；用户可在
# %USERPROFILE%/.pkv/config.yaml 中设置 storage.data_root。数据库、Vault、
# vectors、logs、tmp 一律从最终根派生，不是独立业务配置。

# AI 服务默认配置；本机私有覆盖只写入 %USERPROFILE%/.pkv/config.yaml
ai:
  llm:
    provider: "openai_compatible"
    api_key: ""                       # 默认必须为空，私钥不得提交
    base_url: "https://api.deepseek.com/v1"
    model: "deepseek-chat"
    temperature: 0.7
    max_tokens: 2000
    timeout_seconds: 30
    max_retries: 2

  embedding:
    provider: "openai_compatible"
    api_key: ""                       # 默认必须为空，私钥不得提交
    base_url: "https://api.openai.com/v1"
    model: "text-embedding-3-small"
    dim: 1536                           # 也可在用户 config.yaml 中设为 auto
    timeout_seconds: 30
    max_retries: 3

  # R4 内部自动 AI 生命周期；默认关闭。启用必须由用户一次确认当前
  # policy_sha256，并配置至少一个 token hard cap。没有价格卡时只记录 token，
  # 不推测价格；price card/金额上限是可选的第二层控制。
  automation:
    schema_version: 1
    enabled: false
    authorization:
      policy_sha256: null
    token_budget:
      timezone: "UTC"                  # IANA timezone
      daily_total_tokens: null
      monthly_total_tokens: null
    retry:
      max_attempts: 2

# 检索配置
retrieval:
  bm25:
    k1: 1.5
    b: 0.75
  vector:
    top_k: 10
    ef_search: 50
  hybrid:
    bm25_weight: 0.4
    vector_weight: 0.6
  strategy_thresholds:
    keyword_max_length: 2000
    vector_min_length: 5000

# 日志配置
logging:
  level: "INFO"
  format: "[%(asctime)s] [%(levelname)s] %(name)s: %(message)s"
  date_format: "%Y-%m-%d %H:%M:%S"
  file:
    enabled: true
    # 路径固定由数据根派生为 <data-root>/logs/pkv.log
    max_bytes: 10485760
    backup_count: 5
  console:
    enabled: true
    colorize: true
```

**读取方式**:

```python
from src.utils.config import Config

config = Config()
vault_dir = config.vault_dir
db_path = config.db_path
```

---

### 工作流配置 (workflows/)

运行时只加载这里真实存在、带 `schema_version` 且通过严格 schema 校验的 YAML。当前仅支持 `archive-url.yaml` 与 `archive-text.yaml`；不存在内嵌 step 回退，配置无效时会在执行任何 step 前 fail-closed。

#### archive-url.yaml

**用途**: 归档网页内容的工作流定义

**遗留 WorkflowEngine 配置**:

`archive-url.yaml` 是版本化 WorkflowEngine 的兼容/characterization 合同，不是 R4 公开
archive 的执行配置。真实 CLI/MCP `archive_url` 由 `KnowledgeApplication` 直接进入
`R4IngressLifecycle` 的 Q0 admission/PreparedDocument，随后由 Q1′/Q2 完成内容提交和派生。
该路径不执行 YAML 声明的 `ai_analyze` / `store_entry` step list。

因此，YAML 的 schema 与兼容 step 行为仍必须由各自测试维护，但不得把它们的直接
Markdown/SQLite/vector 写入当成 R4 的产品行为或 source acceptance 证据。

---

#### archive-text.yaml (M9 新增)

**用途**: 归档纯文本内容的工作流定义 (MCP `archive_text` Tool 专用)

**与 archive-url 的区别（仅 WorkflowEngine 兼容合同）**:
- `archive-text` 的 YAML 不含 URL fetch step；`archive-url` 含 `fetch_content`
- 它们保留旧 Engine 的 `Entry`/step 行为，不能代表真实 R4 CLI/MCP archive 的调用链

真实 R4 `archive_text` 由 `KnowledgeApplication` 直接将字面文本送入 Q0；路径形状文本不读取
本地文件，之后由 Q1′/Q2 处理 core mutation、AI patch、usage/reservation 和 generation。
`archive-text.yaml` 的严格 schema 仍受测试保护，但它不控制这条公开 R4 路径。

---

#### 检索配置边界

`search.yaml` 不受支持，也不应创建。搜索由 `src/retrieval/` 及 CLI/MCP adapter 直接执行；GUI 的 M13 发布合同只保证 BM25。向量/混合策略按需构造 Embedding Provider，缺少或无效 Provider 配置必须返回显式错误，不能降格为空结果。

---

### 自定义词典

#### custom_dict.txt

**用途**: jieba 中文分词自定义词典

**格式**:
```
词语 词频 词性
```

**示例**:
```
Claude 1000 n
DeepSeek 1000 n
工作流 500 n
检索引擎 300 n
向量索引 300 n
```

**加载方式**:

```python
import jieba

jieba.load_userdict("config/custom_dict.txt")
```

---

## 用户配置、数据根与运行快照

配置由三个明确平面组成，不能混用：

1. bundled defaults：只读 `<resources>/config/config.yaml`，随源码/wheel 提供，绝不含私钥；
2. 用户业务配置：唯一可编辑文件 `%USERPROFILE%\.pkv\config.yaml`，可包含 Provider Key、Cookie、模型和 `storage.data_root`；
3. 内部运行快照：`<data-root>\config\local.yaml`，仅由 PKV 管理、不得含 key/cookie/auth/token 类字段，且**不参与业务配置合并**。

首次使用时只需在用户 profile 创建配置：

```powershell
New-Item -ItemType Directory -Force "$env:USERPROFILE\.pkv"
notepad "$env:USERPROFILE\.pkv\config.yaml"
```

示例：

```yaml
storage:
  # 可选；未设置时默认 %USERPROFILE%/.pkv/data
  data_root: "D:/PKV/data"

ai:
  llm:
    api_key: "..."
    model: "deepseek-chat"
  embedding:
    api_key: "..."
    model: "text-embedding-3-small"
    dim: auto
```

`Config.update_user_config()` 是产品写入入口。`update_local_config()` 和
`Config.local_config_path` 保留一个 API-major 的弃用兼容，但都指向上述用户
配置，绝不会写 data-root 内的 runtime `local.yaml`。

R4 的 `ai.automation` 也只属于用户业务配置：它保存是否启用、无密钥的 policy
approval hash、token 配额、时区和可选 price-card reference。它不会存入 API key，也不会
进入 `<data-root>/config/local.yaml`；后者只保存 PKV 管理的 generation binding/runtime
state。自动化 policy inspect 是纯本地检查，不创建 Provider、网络连接、数据根或日志。

项目不加载 `.env`，旧 `PKV_LLM_*` / `PKV_EMBD_*` 变量不会覆盖 YAML。

### 两阶段数据根解析与环境变量白名单

解析不写文件，顺序固定为：

```text
bundled defaults + 用户 config.yaml
  → PKV_DATA_ROOT（若设置）
  → 用户 storage.data_root
  → 用户 storage.data_dir（只读弃用兼容）
  → %USERPROFILE%/.pkv/data
```

产品正式支持的环境变量仅有：

- `PKV_DATA_ROOT`：覆盖本次进程的数据根；
- `PKV_LOG_LEVEL`：覆盖本次进程的日志级别。

`DATA_DIR`、`DB_PATH`、`VAULT_DIR`、`VECTOR_DIR`、`LOG_DIR`、`TMP_DIR` 与
`LOG_LEVEL` 只在 `PKV_TEST_OFFLINE=1` 的隔离测试注入中兼容，不能作为产品
配置或部署方式。即使在测试中，所有子路径仍必须 containment 于一个 data root。

用户配置路径独立于数据根，不受 data-root containment 验证；二者各自拒绝
symbolic link、junction/reparse point、硬链接和不可判定路径。运行快照读取时
发现敏感字段会 fail-closed，错误不会回显值。

日常测试直接使用：

```powershell
.\scripts\run-test.ps1 <CLI-subcommand>

# pytest、Python 脚本等非 CLI 命令必须使用 -Direct 与显式 -Command 数组
.\scripts\run-test.ps1 -Direct -Command @("<executable>", "<arg1>", "<arg2>")
```

脚本会设置完整隔离路径，不读取 `.env.test`。

---

## 配置优先级

**业务配置优先级从高到低**:
1. `%USERPROFILE%\.pkv\config.yaml`
2. bundled `config/config.yaml`
3. 代码默认值

`<data-root>/config/local.yaml` 不在该列表中；它是内部 runtime contract
snapshot。数据根只有 `PKV_DATA_ROOT` 与用户 `storage.data_root` 两个产品
输入，结果会冻结在每个 Config/Application snapshot 内。

普通 `Config.update_user_config()` 和 Kernel reload 只能在同一
`data_root_identity` 内发布新 snapshot。试图设置不同的
`storage.data_root` 会在写入前以 `data_root_switch_required` 拒绝；手工编辑
后 reload 也会拒绝。数据根移动、旧目录保留/备份和 Embedding 重建属于
`inspect → plan → confirm → execute` 生命周期，不是普通配置刷新。

生命周期计划会额外保存用户配置源的进程私有、不可逆 revision：它只用于在
inspect 与 execute 之间检测外部文件编辑（包括 Provider Key 更换），不会解析、
合并、输出或记录配置内容。普通已经开始的归档任务仍继续使用其捕获的不可变
Config snapshot，不会被这项检查中途换配。

**示例**:

```python
from src.utils.config import Config

config = Config()
db_path = config.db_path
```

---

## 配置验证

### 验证配置完整性

```python
from src.utils.verify_setup import verify_config

verify_config()
```

**检查项**:
- config.yaml 是否存在
- 必需配置项是否完整
- Provider-backed 能力所需字段是否合法；默认离线/BM25 验证不要求真实 API Key
- 数据目录是否可创建

---

## 常见问题 (FAQ)

### Q1: 如何修改配置?

**方法 1: 编辑用户 `%USERPROFILE%\.pkv\config.yaml`（推荐）**

```yaml
# 修改 OpenAI-compatible LLM 模型
ai:
  llm:
    model: "deepseek-coder"
```

**方法 2: 使用 CLI**

```bash
.\scripts\run-windows.ps1 python -m src.cli.commands config set ai.llm.temperature 0.8
```

**方法 3: 临时覆盖数据根**

```powershell
$env:PKV_DATA_ROOT = "D:\PKV\temporary-data"
```

---

### Q2: 如何添加自定义词?

编辑 `config/custom_dict.txt`:

```
我的自定义词 1000 n
另一个词 500 v
```

然后重启程序(jieba 会自动加载)。

---

### Q3: 如何切换到测试环境?

```powershell
# 方法 1: 运行 PKV CLI 子命令
.\scripts\run-test.ps1 <CLI-subcommand>

# 方法 2: 运行 pytest 等非 CLI 命令
.\scripts\run-test.ps1 -Direct -DataRoot .data-test\manual -Command @("pytest", "tests/integration/", "-v")
```

---

## 相关文件

| 文件 | 说明 |
|------|------|
| `config/config.yaml` | bundled 主配置默认值 |
| `%USERPROFILE%\.pkv\config.yaml` | 唯一用户可编辑的业务配置，可含凭据 |
| `<data-root>/config/local.yaml` | PKV 内部 runtime snapshot；不得含凭据，不参与业务 merge |
| `workflows/archive-url.yaml` | 归档网页工作流配置 |
| `workflows/archive-text.yaml` | 归档文本工作流配置 (M9 新增) |
| `custom_dict.txt` | jieba 自定义词典 |

---

## 变更记录 (Changelog)

### 2026-09-03 (R4)
- 明确 `ai.automation` 只配置无密钥 policy authorization、token hard cap、retry 与可选
  reviewed price-card reference；无 price card 时只记录 token，不估价。
- workflow YAML 是遗留 Engine 的兼容合同；真实 R4 public archive 不执行它的 step list。
  R4 不新增 public rebuild/resume 配置项。

### 2026-08-20 (R1)
- 配置拆为 bundled defaults、`%USERPROFILE%/.pkv/config.yaml` 用户业务配置和 data-root runtime snapshot 三个平面
- 统一默认数据根为 `%USERPROFILE%/.pkv/data`；仅 `PKV_DATA_ROOT` / `PKV_LOG_LEVEL` 是产品环境变量
- 保留旧 `data_dir` 与 `local_config` API 的受控弃用兼容，不自动移动任何历史目录

### 2026-02-19 00:58 (M9)
- 新增 `workflows/archive-text.yaml` 工作流配置 (MCP archive_text 专用)
- 历史原型曾记录 HTTP Bearer 配置；M13 W2 已将 HTTP/Bearer 从发布面和运行入口移除
- 当前发布配置只保留 2 个真实、版本化的归档工作流

### 2026-02-16 18:51
- 生成 Config 模块 CLAUDE.md 文档
- 补充配置优先级和环境变量说明

### 2026-02-14 (M1)
- 完成主配置文件 config.yaml
- 完成工作流配置文件
- 完成自定义词典

---

**模块维护者**: AI Agent
**最后更新**: 2026-09-03

*本文档由 Claude Code 自动生成*
