# MCP 服务模块

[根目录](../../CLAUDE.md) > [src](..) > **mcp**

---

## 模块职责

**MCP Server**: 将 Personal Knowledge Vault 作为 MCP (Model Context Protocol) 服务暴露给 AI Agent（Claude Code、Cursor 等），使 AI 能够直接搜索、检索、归档和浏览知识库。

### 核心能力

- **14 个 Tool**: 12 只读 + 2 写入
- **9 个 Resource**: 条目全文/元数据、精确 chunk、时间字段、关系边、标签与统计
- **3 个 Prompt 模板**: 搜索总结/知识问答/思想磨砺
- **安全加固**: URL DNS/连接目标/重定向/子资源 SSRF 重校验 + 文本长度验证
- **发布传输**: M13 Developer Preview 仅支持 stdio；HTTP/Bearer 不在发布面

三个探索 Tool 继续使用 `partial-v1` 合同并返回 `implementation_level=partial`。默认自动化只使用合成数据和 `.data-test` 隔离根，不连接真实 Provider、不读取真实 API key 或真实 Vault。

---

## 入口与启动

### 启动 MCP Server

```powershell
# stdio 模式（Claude Code / Cursor 本地集成；默认连接用户知识库）
.\scripts\run-windows.ps1 python -m src.mcp.server

# MCP Inspector 可视化调试（强制隔离到 .data-test）
npx @modelcontextprotocol/inspector powershell.exe -ExecutionPolicy Bypass -Command "& '.\scripts\run-test.ps1' -DataRoot '.data-test\mcp-inspector' -Direct -Command @('python','-m','src.mcp.server')"

# 自定义日志级别
.\scripts\run-windows.ps1 python -m src.mcp.server --log-level DEBUG
```

`--transport` 只接受 `stdio`。任何非 stdio 值都会在读取应用配置、bootstrap 数据目录或绑定端口之前 fail-closed；不存在可部署的 HTTP/Bearer 路径。

### Claude Code 集成配置

```json
{
  "mcpServers": {
    "pkv": {
      "command": "powershell.exe",
      "args": [
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        "E:\\repos\\personal\\personal-knowledge-vault\\scripts\\run-windows.ps1",
        "python",
        "-m",
        "src.mcp.server"
      ],
      "cwd": "E:\\repos\\personal\\personal-knowledge-vault"
    }
  }
}
```

Windows 客户端必须通过 `run-windows.ps1` 固定使用 `py311-private`；Provider 配置继续由工作目录下 Git 忽略的 `config/local.yaml` 提供。

---

## 对外接口

### Tools (14 个)

#### 只读 Tool (readOnlyHint=True)

| Tool | 参数 | 返回 | 说明 |
|------|------|------|------|
| `search_knowledge` | `query`, `strategy?`, `top_k?`, `source_type?`, `tag?` | `{status, strategy, total, results[], issues[]}` | 搜索知识库，支持 auto/bm25/vector/hybrid 五态响应 |
| `get_entry` | `knowledge_id` | `{knowledge_id, title, tags, content, ...}` | 获取条目完整内容(含 Markdown 全文) |
| `list_tags` | (无) | `{total_tags, tags[{name, count}]}` | 列出所有标签及计数 |
| `list_entries` | `page?`, `per_page?`, `sort_by?`, `sort_order?`, `source_type?`, `tag?` | `{total, page, entries[]}` | 分页浏览条目列表 |
| `get_stats` | (无) | `{total_entries, source_types, ...}` | 知识库统计信息 |
| `get_related` | `knowledge_id`, `limit?` | `{total, results[{knowledge_id, title, score}]}` | 基于向量相似度的关联推荐 |
| `query_subgraph` | `knowledge_id`, `depth?`, `relation_types?`, `max_nodes?` | `{seed_knowledge_id, nodes[], edges[], grouped_edges}` | 受限多跳关系子图查询 |
| `explain_relation` | `source_knowledge_id`, `target_knowledge_id`, `relation_types?`, `max_depth?` | `{found, summary, path[], evidence_items[]}` | 解释两个条目之间为何相关 |
| `collect_evidence` | `question`, `top_k?`, `relation_max_depth?` | `{seed_knowledge_id, summary, evidence[]}` | 聚合证据包；chunk 证据含稳定 citation locator |
| `find_bridges` | `seed_knowledge_id`, `top_k?`, `max_depth?` | `{items[], limitation_notes[]}` | 发现显式关系子图中的桥接候选及逐跳 evidence path（partial-v1） |
| `timeline_of` | `topic`, `top_k?`, `sort_order?` | `{items[], inferred_time_field}` | 按 `event_time > published_at > archived_at` 重建弱时间线；无持久时间时以 `unavailable` + entry locator 诚实降级（partial-v1） |
| `contrast` | `topic_a`, `topic_b`, `top_k?` | `{shared_tags, only_a_tags, only_b_tags, ...}` | 基于候选表面字段与来源 provenance 做主题对比（partial-v1） |

#### 写入 Tool

| Tool | 参数 | 返回 | 说明 |
|------|------|------|------|
| `archive_url` | `url` | `{success, knowledge_id, title, ...}` | 归档网页 (含 SSRF 防护) |
| `archive_text` | `text`, `title?` | `{success, knowledge_id, title, ...}` | 归档纯文本 (含长度验证) |

### Resources (9 个)

| URI | 说明 | 返回格式 |
|-----|------|----------|
| `pkv://entries/{knowledge_id}` | 条目 Markdown 全文 | `text/markdown` |
| `pkv://entries/{knowledge_id}/metadata` | 条目元数据 | `application/json` |
| `pkv://entries/{knowledge_id}/chunks/{chunk_id}` | 按持久 ID 精确读取 chunk | `application/json` |
| `pkv://entries/{knowledge_id}/chunk-index/{chunk_index}` | 按条目内序号精确读取 chunk | `application/json` |
| `pkv://entries/{knowledge_id}/metadata/{field_name}` | 读取持久时间字段；仅允许 timeline 支持字段 | `application/json` |
| `pkv://relations/{relation_id}` | 按持久 ID 读取关系边 | `application/json` |
| `pkv://relations/by-edge/{source_id}/{target_id}/{relation_type}/{source_type}` | 按唯一边字段读取关系 | `application/json` |
| `pkv://tags` | 标签列表 | `application/json` |
| `pkv://stats` | 统计信息 | `application/json` |

精确 citation 不使用 URI fragment。Tool 返回的 chunk、timeline field 和
relation locator 必须可直接传给 FastMCP `read_resource`。时间字段 Resource
只允许 `event_time`、`published_at`、`published_time`、`publish_time` 和
`archived_at`；legacy alias 仅在对应字段确实持久存在于 Markdown
frontmatter 时可读，transient 检索 metadata 不会伪装成持久引用。
没有可持久读取时间字段的 timeline item 不生成 metadata-field locator：
`time_value`/`time_source_field` 为空，`time_source`/`time_precision` 为
`unavailable`，并引用可读的 `pkv://entries/{id}`。

所有 MCP 公开序列化均把 `source_url` 视为不可信输入。Windows/POSIX/UNC
绝对路径及 `file:` URI 会被清空，`source`/`citation_source` 回退到可读
entry Resource；嵌套 relation evidence 中的本地引用也会递归脱敏。

entry、chunk 与 frontmatter metadata-field Resource 在读取/返回内容前会先
解析父 entry 的 canonical 路径（包含 symlink 解析），并严格证明目标是 vault
根目录内的普通文件。
越界、UNC、目录、缺失文件、symlink escape、无效/缺失 ID 和 loader 异常均
以不含底层路径的 MCP 错误拒绝，不返回可被误判为成功的 Markdown 错误页。
`get_entry` 正文与 `collect_evidence` 的文档/chunk 候选复用同一 guard；
越界 chunk 文本不得通过检索结果进入公开响应。
`timeline_of` / `contrast` 同样排除无法由 entry Resource 回读的越界候选，
不得生成必然不可解析的 entry fallback locator。

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
- `src.ai`: production Provider factory（Embedding / Chat）与 DeepSeek-compatible 摘要客户端

### 配置项

MCP Server 共享 `config/config.yaml` 与 Git 忽略的 `config/local.yaml`。LLM 与 Embedding 配置只从 YAML 读取：

```yaml
ai:
  llm:
    api_key: ""
    base_url: "https://api.deepseek.com/v1"
    model: "deepseek-chat"
  embedding:
    api_key: ""
    base_url: "https://api.openai.com/v1"
    model: "text-embedding-3-small"
    dim: auto
```

`DATA_DIR` / `DB_PATH` / `LOG_LEVEL` 仅用于进程级运行隔离。`PKV_MCP_AUTH_TOKEN` 不属于当前配置合同；M13 不发布 HTTP transport 或 Bearer 认证。

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
    ├── validate_url_security_result() # 入口格式与主机预检
    └── SafeFetcher
        ├── 每跳重新解析 DNS 并拒绝非公网地址
        ├── 连接已验证的固定 IP，同时保持原 Host/SNI/证书 hostname
        ├── 每次 redirect 重新验证
        └── 页面子资源继续走同一安全抓取器

archive_text(text)
    └── validate_text_length()  # 最大 100,000 字符

transport
    └── stdio only              # 非 stdio 在 bootstrap/bind 前拒绝
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

用例数以当前受控收集结果为准，不在模块文档中维护静态总数。

### 运行测试

```powershell
# Layer 1: 单元测试 (最快)
.\scripts\run-test.ps1 -Direct -DataRoot .data-test\mcp-layer1 -Command @("pytest", "tests/unit", "-k", "mcp", "-v")

# Layer 2: 进程内集成测试
.\scripts\run-test.ps1 -Direct -DataRoot .data-test\mcp-layer2 -Command @("pytest", "tests/integration", "-k", "mcp", "-v")

# Layer 3: stdio 黑盒测试 (最慢,启动子进程)
.\scripts\run-test.ps1 -Direct -DataRoot .data-test\mcp-layer3 -Command @("pytest", "tests/blackbox/test_mcp_blackbox.py", "-v")

# 全部 MCP 测试
.\scripts\run-test.ps1 -Direct -DataRoot .data-test\mcp-all -Command @("pytest", "tests/unit", "tests/integration", "tests/blackbox", "tests/e2e", "-k", "mcp", "-v")
```

---

## 常见问题 (FAQ)

### Q1: stdio 模式下为什么日志不能输出到 stdout?

stdio 模式下 stdout 被 MCP JSON-RPC 协议占用,日志必须走 stderr。Server 已自动处理,所有日志输出到 `sys.stderr` 和 `pkv.log` 文件。

### Q2: 如何调试 MCP Tool 返回值?

推荐使用 MCP Inspector:
```powershell
npx @modelcontextprotocol/inspector powershell.exe -ExecutionPolicy Bypass -Command "& '.\scripts\run-test.ps1' -DataRoot '.data-test\mcp-inspector' -Direct -Command @('python','-m','src.mcp.server')"
```
在浏览器中可视化调用每个 Tool/Resource/Prompt。该命令把读写操作限制在 `.data-test\mcp-inspector`。

### Q3: archive_url 支持哪些 URL?

- 支持: `http://` 和 `https://` 协议的公网 URL
- 拒绝: `ftp://`, `file://`, `javascript:` 等非 HTTP 协议
- 拒绝: 127.0.0.0/8, 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, localhost, *.local, *.internal

### Q4: 如何启用 HTTP 或 Bearer?

M13 不能启用。Developer Preview 只支持由本地 MCP Client 管理生命周期的 stdio；HTTP transport、监听端口、远程 URL 与 Bearer Token 都没有发布合同。若未来路线明确纳入 HTTP，必须重新设计并验证真实 transport 认证后才能恢复对应文档。

### Q5: get_related 没有可用结果怎么办?

先检查五态 `status` 和 `issues`，不能只看 `results`：

1. `no_hits`：请求成功，但知识库没有足够的相似内容。
2. `degraded`：仍有可用部分结果，同时按 `issues` 处理索引或元数据问题。
3. `invalid`：修正 knowledge ID / 参数。
4. `error`：向量索引或元数据不可用；按稳定 issue code 排查。默认离线验证不读取真实 key、真实 Provider 或真实 Vault。

---

## 相关文件清单

### 核心文件

| 文件 | 说明 |
|------|------|
| `__init__.py` | 模块入口,描述模块结构 |
| `__main__.py` | 支持 `python -m src.mcp.server` 启动 |
| `server.py` | FastMCP 主入口,单例管理,注册子模块 |
| `tools.py` | 14 个 Tool handler 实现 |
| `resources.py` | 9 个 Resource handler 实现 |
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

### 2026-07-29 (Phase B citation Resource 与透明性复核)
- chunk、metadata field 与 relation locator 改为真实注册、可直接读取的 Resource URI。
- 固定离线评测逐项执行 `read_resource`，并验证 bridge 邻接/断连子图、
  semantic score provenance、timeline 物理字段和 contrast provenance。
- Phase B 新增公开响应递归剔除本机绝对路径；无 URL 时 `source` 回退 entry Resource。
- `find_bridges` 公开子图截断状态、节点/边限制及完整评分输入，但仍保持 partial。
- timeline 无持久时间字段时公开 `unavailable` 语义并回退可读 entry
  Resource，不再生成不存在的 `archived_at` field locator。
- Tool 与 Resource 统一清空本地路径型 `source_url`（含盘符、UNC、`file:`），
  并递归脱敏 relation evidence。
- entry/frontmatter Resource 增加 canonical vault boundary guard；伪成功错误
  文本改为受控 MCP 错误，固定评测同时执行正向可读与越界拒绝预检。

### 2026-07-29 (Phase B citation 合同收口)
- `collect_evidence` chunk 证据新增稳定 citation source/locator。
- `find_bridges` candidate 新增逐跳 `evidence_path`。
- `timeline_of` item 新增 source 与时间字段 locator。
- `contrast` 新增对比维度候选—来源 provenance。
- 三个探索 Tool 仍为 partial，并保留既有限制和证据来源声明。

### 2026-02-19 00:58 (M8+M9)
- 创建 MCP 模块 CLAUDE.md 文档
- M8: 实现 5 个只读 Tool + 4 个 Resource
- M9: 实现 3 个写入/关联 Tool + 3 个 Prompt 模板
- M9 历史原型包含 Bearer 设计；M13 W2 已从运行入口和发布文档移除 HTTP/Bearer，并加强 URL 全链路 SSRF 重校验
- M9: 三层测试体系 (203 tests)
- VectorStore 新增 `get_doc_vector()` 方法(支持 get_related)

---

**模块维护者**: AI Agent
**最后更新**: 2026-02-19 00:58:06

*本文档由 Claude Code 自动生成*
