# MCP 服务模块

[根目录](../../CLAUDE.md) > [src](..) > **mcp**

---

## 模块职责

**MCP Server**: 将 Personal Knowledge Vault 作为 MCP (Model Context Protocol) 服务暴露给 AI Agent（Claude Code、Cursor 等），使 AI 能够直接搜索、检索、归档和浏览知识库。

### 核心能力

- **14 个 Tool**: 12 只读 + 2 写入
- **4 个 Resource**: 条目全文/元数据/标签列表/统计信息
- **3 个 Prompt 模板**: 搜索总结/知识问答/思想磨砺
- **安全加固**: SSRF 防护 + 文本长度验证 + Bearer Token 认证
- **双传输**: stdio (本地集成) + streamable-http (远程访问)

---

## 入口与启动

### 启动 MCP Server

```bash
# stdio 模式（Claude Code / Cursor 本地集成）
python -m src.mcp.server

# HTTP 模式（远程访问）
python -m src.mcp.server --transport streamable-http --port 3000

# MCP Inspector 可视化调试
npx @modelcontextprotocol/inspector python -m src.mcp.server

# 自定义日志级别
python -m src.mcp.server --log-level DEBUG
```

### Claude Code 集成配置

```json
{
  "mcpServers": {
    "pkv": {
      "command": "python",
      "args": ["-m", "src.mcp.server"],
      "cwd": "/path/to/personal-knowledge-vault"
    }
  }
}
```

---

## 对外接口

### Tools (14 个)

#### 只读 Tool (readOnlyHint=True)

| Tool | 参数 | 返回 | 说明 |
|------|------|------|------|
| `search_knowledge` | `query`, `strategy?`, `top_k?`, `source_type?`, `tag?` | `{total, strategy_used, results[]}` | 搜索知识库,支持 auto/bm25/vector/hybrid 策略 |
| `get_entry` | `knowledge_id` | `{knowledge_id, title, tags, content, ...}` | 获取条目完整内容(含 Markdown 全文) |
| `list_tags` | (无) | `{total_tags, tags[{name, count}]}` | 列出所有标签及计数 |
| `list_entries` | `page?`, `per_page?`, `sort_by?`, `sort_order?`, `source_type?`, `tag?` | `{total, page, entries[]}` | 分页浏览条目列表 |
| `get_stats` | (无) | `{total_entries, source_types, ...}` | 知识库统计信息 |
| `get_related` | `knowledge_id`, `limit?` | `{total, results[{knowledge_id, title, score}]}` | 基于向量相似度的关联推荐 |
| `query_subgraph` | `knowledge_id`, `depth?`, `relation_types?`, `max_nodes?` | `{seed_knowledge_id, nodes[], edges[], grouped_edges}` | 受限多跳关系子图查询 |
| `explain_relation` | `source_knowledge_id`, `target_knowledge_id`, `relation_types?`, `max_depth?` | `{found, summary, path[], evidence_items[]}` | 解释两个条目之间为何相关 |
| `collect_evidence` | `question`, `top_k?`, `relation_max_depth?` | `{seed_knowledge_id, summary, evidence[]}` | 聚合文档级证据包 |
| `find_bridges` | `seed_knowledge_id`, `top_k?`, `max_depth?` | `{items[], limitation_notes[]}` | 发现显式关系子图中的桥接候选（partial） |
| `timeline_of` | `topic`, `top_k?`, `sort_order?` | `{items[], inferred_time_field}` | 按 `archived_at` 重建弱时间线（partial） |
| `contrast` | `topic_a`, `topic_b`, `top_k?` | `{shared_tags, only_a_tags, only_b_tags, ...}` | 基于检索候选表面字段做主题对比（partial） |

#### 写入 Tool

| Tool | 参数 | 返回 | 说明 |
|------|------|------|------|
| `archive_url` | `url` | `{success, knowledge_id, title, ...}` | 归档网页 (含 SSRF 防护) |
| `archive_text` | `text`, `title?` | `{success, knowledge_id, title, ...}` | 归档纯文本 (含长度验证) |

### Resources (4 个)

| URI | 说明 | 返回格式 |
|-----|------|----------|
| `pkv://entries/{knowledge_id}` | 条目 Markdown 全文 | `text/markdown` |
| `pkv://entries/{knowledge_id}/metadata` | 条目元数据 | `application/json` |
| `pkv://tags` | 标签列表 | `application/json` |
| `pkv://stats` | 统计信息 | `application/json` |

### Prompts (3 个)

| Prompt | 参数 | 说明 |
|--------|------|------|
| `search_and_summarize` | `query`, `context?` | 搜索知识库并总结结果 |
| `knowledge_qa` | `question` | 基于知识库的智能问答 |
| `idea_sharpen` | `content`, `entry_id?` | 对知识条目进行思想磨砺 |

---

## 关键依赖与配置

### 依赖库

- `mcp` (FastMCP): MCP Server 框架
- `anyio`: 异步 I/O 桥接(同步操作包装为线程池任务)
- `src.storage`: SQLiteStore, MarkdownStore, VectorStore
- `src.retrieval`: QueryRouter, BM25/Vector/Hybrid Retriever
- `src.workflow`: WorkflowEngine (写入 Tool 使用)
- `src.processors`: TextFallbackProcessor (archive_text 使用)
- `src.ai`: OpenAIClient (Embedding), DeepSeekClient (摘要)

### 配置项

MCP Server 共享主配置 `config/config.yaml` 和环境变量:

```bash
# 必需
DEEPSEEK_API_KEY=sk-...    # AI 摘要/标签
OPENAI_API_KEY=sk-...       # Embedding 向量

# 可选
DB_PATH=.data/db/knowledge_vault.db  # 数据库路径
LOG_LEVEL=INFO                       # 日志级别
PKV_MCP_AUTH_TOKEN=my-secret-token   # HTTP Bearer Token (仅 HTTP 模式需要)
```

### 工作流配置

写入 Tool 使用的工作流:
- `archive_url` -> `config/workflows/archive-url.yaml` (fetch + analyze + store)
- `archive_text` -> `config/workflows/archive-text.yaml` (analyze + store, 跳过 fetch)

---

## 架构设计

### 单例管理

Server 生命周期内复用以下对象(延迟初始化):

```python
# server.py 中的单例工厂
get_sqlite_store()   # SQLiteStore 单例
get_markdown_store() # MarkdownStore 单例
get_query_router()   # QueryRouter 单例(含 BM25 + HybridRetriever + VectorStore)
```

**为什么不能每次请求重建**: VectorRetriever 需加载 hnswlib 索引文件到内存(~1-3s),重复创建浪费资源。

### 异步策略

```
只读 Tool  →  async def + anyio.to_thread.run_sync(_impl)
               └── 将同步 SQLite/文件 I/O 包装到线程池

写入 Tool  →  async def + await engine.execute_async(...)
               └── WorkflowEngine 原生 async,无需 threadpool

Resource   →  async def + anyio.to_thread.run_sync(_impl)
               └── 同只读 Tool

Prompt     →  同步 def (返回 str,无 I/O)
```

### 安全层

```
archive_url(url)
    ├── validate_url()          # 格式验证: http/https, 有效 netloc
    ├── is_private_ip()         # SSRF 防护: 拒绝内网地址
    └── validate_url_security() # 综合验证

archive_text(text)
    └── validate_text_length()  # 最大 100,000 字符

HTTP 传输
    └── validate_http_auth()    # Bearer Token 验证
        └── PKV_MCP_AUTH_TOKEN 环境变量 (未配置则拒绝所有)
```

---

## 数据模型

### Tool 返回值规范

搜索结果序列化(`serialize_search_result`):
- `SearchResult.highlight` -> `abstract` (SearchResult 无 abstract 属性)
- `SearchResult.metadata["tags"]` 逗号字符串 -> `tags` 列表

条目摘要序列化(`serialize_entry_summary`):
- `summary_one_sentence` -> `abstract` (DB 无 abstract 列)
- `tags`/`keywords` 逗号字符串 -> 列表

### 参数约束

- `top_k`: clamp(1, 50)
- `per_page`: clamp(1, 100)
- `limit` (get_related): clamp(1, 20)
- `text` (archive_text): max 100,000 字符

---

## 测试与质量

### 三层测试体系

| 层级 | 文件 | 测试数量 | 说明 |
|------|------|----------|------|
| **Layer 1** | `tests/unit/test_mcp_tools.py` | ~40 | Tool handler 单元测试 (Mock 隔离) |
| **Layer 1** | `tests/unit/test_mcp_resources.py` | ~15 | Resource handler 单元测试 |
| **Layer 1** | `tests/unit/test_mcp_prompts.py` | ~15 | Prompt 模板参数和输出测试 |
| **Layer 1** | `tests/unit/test_mcp_security.py` | ~30 | 安全验证函数测试 (URL/IP/文本/Auth) |
| **Layer 2** | `tests/integration/test_mcp_functional.py` | ~50 | FastMCP 进程内集成测试 |
| **Layer 2** | `tests/integration/test_mcp_integration.py` | ~15 | 真实 SQLiteStore 集成测试 |
| **Layer 3** | `tests/blackbox/test_mcp_blackbox.py` | ~40 | stdio 子进程黑盒测试 (JSON-RPC) |

**总计**: 约 203 个测试用例

### 运行测试

```bash
# Layer 1: 单元测试 (最快)
python -m pytest tests/unit/test_mcp_*.py -v

# Layer 2: 进程内集成测试
python -m pytest tests/integration/test_mcp_*.py -v

# Layer 3: stdio 黑盒测试 (最慢,启动子进程)
python -m pytest tests/blackbox/test_mcp_blackbox.py -v

# 全部 MCP 测试
python -m pytest tests/unit/test_mcp_*.py tests/integration/test_mcp_*.py tests/blackbox/test_mcp_blackbox.py -v
```

---

## 常见问题 (FAQ)

### Q1: stdio 模式下为什么日志不能输出到 stdout?

stdio 模式下 stdout 被 MCP JSON-RPC 协议占用,日志必须走 stderr。Server 已自动处理,所有日志输出到 `sys.stderr` 和 `pkv.log` 文件。

### Q2: 如何调试 MCP Tool 返回值?

推荐使用 MCP Inspector:
```bash
npx @modelcontextprotocol/inspector python -m src.mcp.server
```
在浏览器中可视化调用每个 Tool/Resource/Prompt。

### Q3: archive_url 支持哪些 URL?

- 支持: `http://` 和 `https://` 协议的公网 URL
- 拒绝: `ftp://`, `file://`, `javascript:` 等非 HTTP 协议
- 拒绝: 127.0.0.0/8, 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, localhost, *.local, *.internal

### Q4: HTTP 模式如何配置认证?

```bash
# 1. 设置环境变量
export PKV_MCP_AUTH_TOKEN="my-secret-token-here"

# 2. 启动 HTTP 模式
python -m src.mcp.server --transport streamable-http --port 3000

# 3. 客户端请求时携带 Bearer Token
Authorization: Bearer my-secret-token-here
```

未配置 `PKV_MCP_AUTH_TOKEN` 时,所有 HTTP 请求将被拒绝(安全默认原则)。

### Q5: get_related 返回空结果怎么办?

可能原因:
1. 该条目归档时未生成向量索引(需要 OpenAI API Key)
2. 知识库条目太少,无足够相似内容
3. hnswlib 索引文件不存在或损坏

---

## 相关文件清单

### 核心文件

| 文件 | 说明 |
|------|------|
| `__init__.py` | 模块入口,描述模块结构 |
| `__main__.py` | 支持 `python -m src.mcp.server` 启动 |
| `server.py` | FastMCP 主入口,单例管理,注册子模块 |
| `tools.py` | 14 个 Tool handler 实现 |
| `resources.py` | 4 个 Resource handler 实现 |
| `prompts.py` | 3 个 Prompt 模板实现 |
| `utils.py` | 辅助工具(序列化/安全验证/参数约束) |

### 测试文件

| 文件 | 说明 |
|------|------|
| `tests/unit/test_mcp_tools.py` | Tool handler 单元测试 |
| `tests/unit/test_mcp_resources.py` | Resource handler 单元测试 |
| `tests/unit/test_mcp_prompts.py` | Prompt 模板单元测试 |
| `tests/unit/test_mcp_security.py` | 安全验证函数单元测试 |
| `tests/integration/test_mcp_functional.py` | 进程内功能测试 (Layer 2) |
| `tests/integration/test_mcp_integration.py` | 真实 SQLiteStore 集成测试 |
| `tests/blackbox/test_mcp_blackbox.py` | stdio 协议级黑盒测试 (Layer 3) |

### 配置文件

| 文件 | 说明 |
|------|------|
| `config/workflows/archive-url.yaml` | archive_url 使用的工作流 |
| `config/workflows/archive-text.yaml` | archive_text 使用的工作流 (M9 新增) |

---

## 变更记录 (Changelog)

### 2026-02-19 00:58 (M8+M9)
- 创建 MCP 模块 CLAUDE.md 文档
- M8: 实现 5 个只读 Tool + 4 个 Resource
- M9: 实现 3 个写入/关联 Tool + 3 个 Prompt 模板
- M9: SSRF 防护 + 文本长度验证 + Bearer Token 认证
- M9: 三层测试体系 (203 tests)
- VectorStore 新增 `get_doc_vector()` 方法(支持 get_related)

---

**模块维护者**: AI Agent
**最后更新**: 2026-02-19 00:58:06

*本文档由 Claude Code 自动生成*
