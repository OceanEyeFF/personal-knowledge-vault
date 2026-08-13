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
storage:
  vault_dir: ".data/vault"             # Markdown 存储目录
  db_path: ".data/db/knowledge_vault.db"  # SQLite 数据库路径
  vector_index_dir: ".data/vectors"    # 向量索引目录
  log_dir: ".data/logs"                # 日志目录
  tmp_dir: ".data/tmp"                 # 临时文件目录

# AI 服务默认配置；本机私有覆盖只写入被 Git 忽略的 config/local.yaml
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
    dim: 1536                           # 也可在 local.yaml 中设为 auto
    timeout_seconds: 30
    max_retries: 3

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
    path: ".data/logs/pkv.log"
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

**步骤**:
1. `fetch_content` - 获取网页内容
2. `ai_analyze` - AI 分析 (摘要/标签/关键词)
3. `store_entry` - 存储到三层存储

```yaml
name: archive-url
description: "归档网页内容到知识库"
steps:
  - id: fetch
    type: fetch_content
    config:
      timeout: 30

  - id: analyze
    type: ai_analyze
    config:
      use_deepseek: true

  - id: store
    type: store_entry
    config:
      generate_embedding: true
```

---

#### archive-text.yaml (M9 新增)

**用途**: 归档纯文本内容的工作流定义 (MCP `archive_text` Tool 专用)

**与 archive-url 的区别**:
- 跳过 `fetch_content` 步骤（无需抓取,文本由 MCP Tool 层传入）
- 跳过 `idea_sharpen` 步骤（MCP 场景无终端交互能力）
- Entry 由 MCP Tool 层调用 `TextFallbackProcessor` 预构建

**步骤**:
1. `ai_analyze` - AI 分析 (摘要/标签),失败时使用 TextFallbackProcessor 的默认摘要
2. `store_entry` - 存储到三层存储 (Markdown + SQLite + Vector)

```yaml
name: archive-text
description: "归档纯文本内容工作流（MCP 专用）"
steps:
  - id: ai_analyze
    type: ai_analyze
    config:
      model: deepseek-chat
      max_tokens: 2000
      temperature: 0.7
      tasks:
        - summarize
        - extract_tags
    on_error: continue      # AI 失败则跳过

  - id: store_entry
    type: store_entry
    config:
      targets:
        - markdown
        - sqlite
        - vector_index
    on_error: fail
```

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

## 本机配置与运行隔离

### `config/local.yaml`（本机私有配置）

应用服务、模型、密钥和处理器凭据只写入 Git 忽略的 `config/local.yaml`：

```powershell
Copy-Item config\config.yaml config\local.yaml
notepad config\local.yaml
```

主要键路径：

```yaml
ai:
  llm:
    provider: "openai_compatible"
    api_key: ""
    base_url: "https://api.deepseek.com/v1"
    model: "deepseek-chat"
  embedding:
    provider: "openai_compatible"
    api_key: ""
    base_url: "https://api.openai.com/v1"
    model: "text-embedding-3-small"
    dim: auto
```

项目不加载 `.env`，旧的 `PKV_LLM_*` / `PKV_EMBD_*` 变量不会覆盖 YAML。

### 进程级运行覆盖

环境变量仅用于一次性运行隔离：`DATA_DIR`、`DB_PATH`、`VAULT_DIR`、`VECTOR_DIR`、`LOG_DIR`、`TMP_DIR`、`LOG_LEVEL`。设置 `DATA_DIR` 时，未显式覆盖的其余数据路径会从该目录派生。

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
1. `config/local.yaml`
2. `config/config.yaml`
3. 代码默认值

存储路径可由上述进程级运行覆盖临时替换，但它们不是业务配置来源。

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

**方法 1: 编辑本机 `config/local.yaml`（推荐）**

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

**方法 3: 临时隔离数据路径**

```powershell
$env:DATA_DIR = ".data-custom"
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
| `config.yaml` | 主配置文件 |
| `local.yaml` | Git 忽略的本机私有覆盖配置 |
| `workflows/archive-url.yaml` | 归档网页工作流配置 |
| `workflows/archive-text.yaml` | 归档文本工作流配置 (M9 新增) |
| `custom_dict.txt` | jieba 自定义词典 |

---

## 变更记录 (Changelog)

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
**最后更新**: 2026-08-13

*本文档由 Claude Code 自动生成*
