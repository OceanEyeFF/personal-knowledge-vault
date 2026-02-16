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

# AI 服务配置
ai:
  deepseek:
    model: "deepseek-chat"             # DeepSeek 模型
    api_base: "https://api.deepseek.com/v1"
    temperature: 0.7                   # 生成温度
    max_tokens: 2000                   # 最大 token 数

  openai:
    embedding_model: "text-embedding-3-small"  # Embedding 模型
    embedding_dimensions: 1536         # 向量维度

# 检索配置
retrieval:
  bm25_k1: 1.5                         # BM25 参数 k1
  bm25_b: 0.75                         # BM25 参数 b
  vector_top_k: 10                     # 向量检索返回数量
  hybrid_rrf_k: 60                     # 混合检索 RRF 参数
  query_router_threshold: 10           # 查询路由阈值 (tokens)

# 日志配置
logging:
  level: "INFO"                        # 日志级别
  format: "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
  file: ".data/logs/pkv.log"           # 日志文件路径
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

#### search.yaml

**用途**: 搜索知识库的工作流定义

**步骤**:
1. `query_router` - 查询路由 (选择检索策略)
2. `retrieve` - 执行检索
3. `format_results` - 格式化结果

```yaml
name: search
description: "在知识库中搜索内容"
steps:
  - id: route
    type: query_router

  - id: retrieve
    type: retrieve

  - id: format
    type: format_results
```

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

## 环境变量配置

### .env (生产环境)

**用途**: 生产环境敏感配置

**配置项**:
```env
# DeepSeek API
DEEPSEEK_API_KEY=sk-your-deepseek-key

# OpenAI API
OPENAI_API_KEY=sk-your-openai-key

# 数据库路径 (可选,默认使用 config.yaml)
DB_PATH=.data/db/knowledge_vault.db

# 日志级别 (可选)
LOG_LEVEL=INFO
```

**创建方式**:
```powershell
copy .env.example .env
notepad .env
```

---

### .env.test (测试环境)

**用途**: 测试环境配置

**配置项**:
```env
# 数据库路径 (使用测试专用目录)
DB_PATH=.data-test/db/knowledge_vault.db

# 测试用 API Keys (可选)
DEEPSEEK_API_KEY=sk-test-your-key
OPENAI_API_KEY=sk-test-your-key

# 日志级别 (DEBUG 获取详细日志)
LOG_LEVEL=DEBUG
```

**创建方式**:
```powershell
copy .env.test.example .env.test
notepad .env.test
```

**使用方式**:
```powershell
# 自动加载 (通过 run-test.ps1)
.\scripts\run-test.ps1 <command>
```

---

## 配置优先级

**优先级从高到低**:
1. 环境变量 (`$env:DB_PATH`)
2. `.env` 文件
3. `config.yaml` 文件
4. 代码默认值

**示例**:

```python
from src.utils.config import Config

# 1. 优先读取环境变量 DB_PATH
# 2. 如果不存在,读取 .env 文件
# 3. 如果不存在,读取 config.yaml
# 4. 如果都不存在,使用默认值

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
- API Keys 是否设置
- 数据目录是否可创建

---

## 常见问题 (FAQ)

### Q1: 如何修改配置?

**方法 1: 编辑 config.yaml**

```yaml
# 修改 DeepSeek 模型
ai:
  deepseek:
    model: "deepseek-coder"  # 改为 Coder 模型
```

**方法 2: 使用 CLI**

```bash
python -m src.main config set ai.temperature 0.8
```

**方法 3: 环境变量**

```powershell
$env:DB_PATH = ".data-custom/db/knowledge_vault.db"
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
# 方法 1: 使用测试脚本
.\scripts\run-test.ps1 <command>

# 方法 2: 手动设置环境变量
$env:DB_PATH = ".data-test/db/knowledge_vault.db"
python -m src.main <command>
```

---

## 相关文件

| 文件 | 说明 |
|------|------|
| `config.yaml` | 主配置文件 |
| `workflows/archive-url.yaml` | 归档工作流配置 |
| `workflows/search.yaml` | 搜索工作流配置 |
| `custom_dict.txt` | jieba 自定义词典 |
| `../.env.example` | 环境变量模板 (生产) |
| `../.env.test.example` | 环境变量模板 (测试) |

---

## 变更记录 (Changelog)

### 2026-02-16 18:51
- 生成 Config 模块 CLAUDE.md 文档
- 补充配置优先级和环境变量说明

### 2026-02-14 (M1)
- 完成主配置文件 config.yaml
- 完成工作流配置文件
- 完成自定义词典

---

**模块维护者**: AI Agent
**最后更新**: 2026-02-16 18:51:32

*本文档由 Claude Code 自动生成*
