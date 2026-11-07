# MCP 服务模块

[根目录](../../CLAUDE.md) > [src](..) > **mcp**

---

## 模块职责

**MCP Server**: 将 Personal Knowledge Vault 作为 MCP (Model Context Protocol) 服务暴露给 AI Agent（Claude Code、Cursor 等），使 AI 能够直接搜索、检索、归档和浏览知识库。

### 核心能力

- **41 个 Tool**: 23 只读 + 18 写入（W4 v2 增量，当前范围验收完成）
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

`--transport` 只接受 `stdio`。任何非 stdio 值都会在读取应用配置、检查数据根或绑定端口之前 fail-closed；不存在可部署的 HTTP/Bearer 路径。

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

Windows 客户端必须通过 `run-windows.ps1` 固定使用 `py311-private`；用户可编辑的 Provider 配置位于 `%USERPROFILE%\.pkv\config.yaml`。正式产品环境变量白名单**仅**为 `PKV_DATA_ROOT` 与 `PKV_LOG_LEVEL`，它们覆盖对应运行设置；不以环境变量传递 Provider key。`<data-root>/config/local.yaml` 是无密钥的 runtime snapshot，不是用户配置来源。

---

## 对外接口

### Tools (41 个)

#### 只读 Tool (readOnlyHint=True)

| Tool | 参数 | 返回 | 说明 |
|------|------|------|------|
| `get_runtime_status` | (无) | `{status, readiness, inspection, plan, issues}` | 未就绪数据根仍可只读检查；不初始化/迁移/修复/Provider 探测 |
| `inspect_embedding_rebuild` | (无) | `{terminal, inspection, plan: null, issues}` | 只读检查 generation binding 与独立手动重建预算；零写、零 Provider、零 task drain |
| `plan_embedding_rebuild` | `force?` | `{terminal, inspection, plan, issues}` | 生成安全、无路径/正文/凭据的 public rebuild 计划；`ready` generation 需精确 `force=true` |
| `inspect_embedding_rebuild_v2` | 无 | v2 envelope，result 为 inspection DTO | 当前预算期间、ledger schema 与升级影响；零写、零 lease、零 Provider |
| `plan_embedding_rebuild_v2` | `force=false` | v2 envelope，result 为 rebuild plan DTO | 网络计划绑定当前期间及升级影响，默认不授权网络 |
| `inspect_embedding_operation_v2` | `operation_id` | v2 envelope，result 为 operation DTO | 只读复核 proof、pointer 与账本事实，不在读路径对齐状态 |
| `plan_embedding_publication_v2` | `operation_id` | v2 envelope，result 为 publication plan DTO | 显式 publish-only/完成对齐计划；零 Provider、零新预留 |
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
| `get_outcome` | `topic_id` | `{status, operation, result:{topic,evidence,claims,brief}}` | 零写入地重验 evidence binding；drift 显示为 `stale/unavailable` 与 brief 的 effective `needs_review` |
| `suggest_outcome_evidence` | `topic_id`, `query`, `limit?` | `{strategy:"bm25",status,suggestions,issues}` | 仅用严格 BM25 提供待显式 attach 的 entry locator；不构造 QueryRouter、Provider 或 vector generation |
| `get_outcome_v2` | `topic_id` | v2 envelope，result 为 `OutcomeBundleV2` | 同一输入 snapshot 的 Topic/Evidence/Claim/brief 与精确 review confirmation |
| `get_outcome_research_brief_v2` | `topic_id=null`, `outcome_id=null` | v2 envelope，result 为 `ResearchBriefRecordV2` 或 null | 必须且只能提供一个 selector；不存在合法 selector 返回 null，零写入 |

#### 写入 Tool

| Tool | 参数 | 返回 | 说明 |
|------|------|------|------|
| `archive_url` | `url` | `{success, knowledge_id, title, ...}` | 归档网页 (含 SSRF 防护) |
| `archive_text` | `text`, `title?` | `{success, knowledge_id, title, ...}` | 归档纯文本 (含长度验证) |
| `execute_embedding_rebuild` | `plan_id`, `approved`, `allow_network`, `force?` | `{terminal, inspection, plan, execution, issues}` | 重新核验同一 public plan 后执行一次 fenced generation rebuild；显式确认才允许构造 Provider |
| `execute_embedding_rebuild_v2` | `plan_id`, `approved=false`, `allow_network=false`, `force=false` | v2 envelope，result 为 execution DTO | 精确重算确认后在 lease 内接纳；Provider 在 lease 外，完整 stage 封存后发布 |
| `execute_embedding_publication_v2` | `operation_id`, `plan_id`, `approved=false` | v2 envelope，result 为 execution DTO | 原 operation + 当前确认只发布完整封存产物或对齐完成；不接收 force/network 参数 |
| `create_outcome_topic` | `title`, `question` | public Topic DTO | 创建一次明确 inquiry；不自动检索、联网或调用 Provider |
| `attach_outcome_evidence` / `detach_outcome_evidence` | canonical locator / `evidence_id` | public Evidence DTO / stable id | 显式绑定或解除 entry/chunk/relation evidence；绝不自动重绑来源 |
| `create_outcome_claim` / `revise_outcome_claim` | statement、state、revision | public Claim DTO | 记录用户明确的 Claim；无有效 evidence 时外显为 `insufficient` |
| `link_outcome_claim_evidence` / `unlink_outcome_claim_evidence` | claim/evidence id、polarity | public Claim DTO | 显式维护 support/counter 边；跨 Topic 关系 fail-closed |
| `render_outcome_research_brief` | `topic_id` | public brief DTO | current binding 下发布 immutable Markdown revision，再提交 SQLite pointer |
| `approve_outcome_research_brief` / `return_outcome_research_brief` | `topic_id` | public brief DTO，附弃用信息 | 旧 v1 签名与成功语义保留；不具有调用方的精确版本确认保证，替代入口为下列 v2 Tool |
| `render_outcome_research_brief_v2` | `topic_id` | v2 envelope，result 为 `ResearchBriefRecordV2` | 使用持久 renderer 合同发布绑定当前输入的 immutable revision |
| `approve_outcome_research_brief_v2` | `outcome_id`, `expected_revision`, `content_sha256`, `expected_input_revision` | v2 envelope，result 为 `ResearchBriefReviewResultV2` | 精确四元组批准，state 与 review receipt 同事务；同动作精确重放幂等 |
| `return_outcome_research_brief_v2` | 同四元组，必填 `reason_code` | v2 envelope，result 为 `ResearchBriefReviewResultV2` | 原因限 `needs_revision / insufficient_evidence / other`；不同原因重放拒绝 |

两种 archive 写入 Tool 均只进入 `KnowledgeApplication` 的 R4 Q0 → Q1′ → Q2 内部生命周期：
Q0/Q1′ 未完成时返回稳定 processing 投影，只有 `core_committed` 后才称文档已保存；
Q2 的 retry/budget/authorization 状态保持可操作的 degraded 信息。它们不暴露 resume 或后台
daemon，也不直接调用 Storage/AI lifecycle。

P1-B outcome Tool 同样只投影公开 Kernel façade。它们读取或保存 SQLite-authoritative 的
结构状态和相对 Markdown pointer，Evidence 只保留 canonical locator/captured digest/limitation；
不返回 source body、私有路径、raw URL/credential、Store、lease 或 Provider。读 Tool 不写入；
mutation 在 Application 的 root writer lease 下审计执行，且不构造 LLM、Embedding 或 QueryRouter。

### W4 Embedding v2 协议（已完成范围验收）

SDK `0.10.0` / API `1.0.0` 保留全部 v1 表面，按冻结合同 §6 增加 Embedding v2 的
4 个只读与 2 个写入 Tool；总计 41 Tool（23 只读 + 18 写入）。`kernel.embedding-rebuild.v2`
已实现并声明，W4 范围验收完成，ER/MIG 证据见 [W4 主记录](../../docs/overview/W4-Embedding继续发布与预算恢复实施记录-2026-10.md)，
不能由历史 W3/MCP coverage 数字推导 W4 已完成。W5 major 切换与旧 Outcome 拒绝尚未实施。

六个新 Tool 仅调用 `pkv_kernel.embedding`；inputSchema 封闭为
`additionalProperties=false`，原始 JSON 在 FastMCP 转换前校验。布尔值不接受数值或字符串，
plan ID 为 64 位小写十六进制，operation ID 为 `embedding-rebuild-[0-9a-f]{32}`。
execute 在当前 Config 重算 plan，精确匹配后创建同一 plan 的确认，不把 operation ID 当授权。
这些 Tool 走独立 lifecycle 合同，不扩大普通业务 Tool 或 Outcome 的 READY 访问边界。

响应使用 `pkv.lifecycle-response.v2`，operation 固定为当前 Tool 名。成功 result 中的
completion-pending 警告也保留在 issues；不能将 pointer 已提交误报为需重新生成。A 争锁返回
`write_busy`；完整 C 封存后 D 争锁返回 `embedding_publication_pending`，safe details 说明原
operation、awaiting_publish、Provider completed 和 publish-only 要求。v1 Tool 保留旧形状，
在 `issues[0].details` 透传这些信息；继续操作只能转用 v2 lookup/plan/execute。

Ledger 3 升级是明确确认 mutation 的一部分，inspect/plan 不升级；legacy 1/2 的 v1 写入
返回 `embedding_ledger_upgrade_required`。publish-only 在 lease 内验证 grant 2 和完整 sealed
proof，沿用原 reservation/usage，不构造 Provider、不新增预算；缺失/不可验证 proof 不自动重试。

### W3 v2 协议与兼容期

W3 的历史兼容 build 为 SDK `0.9.0` / API `1.0.0`，当时只新增 Outcome v2 capability。
当前 W4 build 与 Embedding 增量见上节，全部 v1 capability 保留，未进行 W5 major 切换。
上述五个 Outcome v2 Tool 只调用 `pkv_kernel.outcomes` 的对应 `*_v2` 函数，
不让客户端提供 Config、路径、Store 或 lease，也不读取 latest 补齐 review 四元组。
其中两个 get 的 `readOnlyHint=true`，三个 mutation 为 false；五者仍要求 READY，
不扩大未就绪或 DEGRADED 根的 Outcome 访问权限。

原始 JSON 参数在 FastMCP 转换前校验，五个 inputSchema 均为 `additionalProperties=false`。
标识须满足 `[a-z][a-z0-9_-]{0,95}`，摘要须为 64 位小写十六进制，两项 revision
为 `1..2**63-1` 的严格整数，不接受 bool、float 或数字字符串；reason 为三值枚举。
缺必填项、额外字段、双/空 selector 返回 `outcome_invalid`，不进入 SDK 或写入。
已合法的字符串原样传递，包括字符串 `"null"`；不把它 JSON 解码成空 selector。

CLI 与 MCP 统一使用
`{schema:"pkv.lifecycle-response.v2",status,terminal,operation,result,issues}`。
status/terminal 为 success 或 error；operation 固定为对应 MCP Tool 名（CLI 亦然），
成功 result 是公开 DTO 或合法 lookup 的 null，失败 result 为 null。issues 仅含稳定
code/message/stage/recoverable 等冻结字段，不回显输入或异常原文；MCP 的结构化 content
与 JSON 文本承载相同对象。只读 lookup 不迁移旧 schema、不创建历史 review 凭据；
`legacy_unverified` 或 stale binding 必须显式重新生成/审阅。

旧无版本 approve/return Tool 仍可按原语义成功，响应额外带固定
`deprecation={replacement:"对应 *_v2 Tool",removal_api_major:2}`，SDK 同时发出
`DeprecationWarning`。这不是 major-2 拒绝 stub；R-04 仍有旧入口 `known_gap`，
须由 W5 的 major 切换和旧调用拒绝关闭，不能凭新入口通过宣称已关闭。
详见 [v2 冻结合同](../../docs/specs/interfaces/生命周期一致性v2接口冻结合同-2026-10.md)
与 [W3 实施主记录](../../docs/overview/W3-Outcome一致性与版本审批实施记录-2026-10.md)。

`execute_embedding_rebuild` 是独立的 `pkv_kernel.embedding` 前台维护路径，不是 Q2 resume：
它只从已经提交的 Markdown + SQLite projection 生成新的向量 generation，Provider 调用位于
root writer lease 之外，且不会重新抓取、重写正文、修改 `ai.automation` 或暴露 task drain/
retry 接口。独立 `ai.embedding_rebuild` token hard cap 缺失或耗尽时，在 Provider/stage 前返回
`embedding_rebuild_authorization_required` 或 `embedding_budget_paused`。

共享数据根实行**一写多读**：CLI、MCP 与未来 GUI 可并行读取，但不允许两个写操作
并行。若写入 Tool 发现其他应用持有数据根 write lease，它返回稳定 `write_busy`。
既有 archive 响应形状如下；Outcome v2 使用上节统一 error envelope 和对应 issue：

```json
{
  "success": false,
  "terminal": "error",
  "error_code": "write_busy",
  "retryable": true,
  "issues": [{"code": "write_busy", "recoverable": true}]
}
```

客户端可在短暂退避后重试；不得把 `write_busy` 误报为归档成功、空结果或在 adapter
层自行并行重放写入。

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
- `src.application.KnowledgeApplication`: 同一 validated config 下的 Store、
  Retrieval、Workflow、Provider 与领域服务 composition root
- `src.storage` / `src.retrieval` / `src.workflow` / `src.processors` / `src.ai`:
  application 的内部依赖；MCP handler 不直接构造它们

### 配置项

MCP Server 读取 bundled `config/config.yaml` 与唯一用户配置 `%USERPROFILE%\.pkv\config.yaml`。LLM 与 Embedding 配置只从 YAML 读取；环境变量只覆盖已发布的少量运行设置，不传递 Provider key：

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

正式环境变量白名单仅为 `PKV_DATA_ROOT` / `PKV_LOG_LEVEL`。`DATA_DIR`、`DB_PATH`
和 `LOG_LEVEL` 仅在 `PKV_TEST_OFFLINE=1` 时作为测试进程隔离注入 seam，不能作为用户
产品配置或 Provider 凭据来源。`PKV_MCP_AUTH_TOKEN` 不属于当前配置合同；M13 不发布
HTTP transport 或 Bearer 认证。

### 工作流配置

写入 Tool 使用的工作流:
- `archive_url` -> `config/workflows/archive-url.yaml` (fetch + analyze + store)
- `archive_text` -> `config/workflows/archive-text.yaml` (analyze + store, 跳过 fetch)

---

## 架构设计

### Application 生命周期

MCP 启动只解析 Config 并执行只读 `inspect_runtime()`，绝不调用历史 mutating
bootstrap、迁移、恢复或 Provider probe。未 READY 时仍保留 stdio 会话与
`get_runtime_status`；普通业务 Tool/Resource（包括全部 Outcome v1/v2）以稳定 readiness
issue 拒绝，不创建数据库、runtime snapshot、Provider 或文件日志。既有
`inspect_embedding_rebuild` / `plan_embedding_rebuild` 及 Embedding v2 inspect/plan 工具走独立
只读维护入口，按其 lifecycle 合同取得当前同根 Config，不装配 Application；这一窄例外不授予
Outcome 读取或写入权限。

首次 READY 的后端请求才以启动时捕获的不可变 Config snapshot 配置一次
`KnowledgeApplication`。Server 中的 `get_sqlite_store()`、`get_markdown_store()`
和 `get_query_router()` 是已注册 handler 的兼容访问器，全部委托给这个共享
application，不能成为重新自行装配后端的入口。后续 Tool 不传显式 Config 重新
创建 application；它们复用同一 Store、Retriever、Workflow 与 Provider 图。
Vector 检索按需打开已有索引，而不是每个 Tool 调用重新建索引。

### 异步策略

```
只读 Tool  →  async def + anyio.to_thread.run_sync(_impl)
               └── 将同步 SQLite/文件 I/O 包装到线程池

archive 写入 Tool → async def + await application.archive_url/archive_text(...)
               └── application 保持 Workflow 原生 async,无需 threadpool

P1-B 同步写入 Tool → async def + anyio.to_thread.run_sync(_impl)
               └── 只经 `pkv_kernel.outcomes`，由 Application 取得 writer lease

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
| **Layer 3** | `tests/blackbox/test_mcp_blackbox.py` / `test_r4_mcp_fullflow.py` / `test_outcome_mcp_blackbox.py` | stdio 子进程黑盒测试（含 R4 archive → restart → vector/hybrid，以及 P1-B Topic → review → fresh-process read） |

用例数以当前受控收集结果为准，不在模块文档中维护静态总数。

### 运行测试

```powershell
# Layer 1: 单元测试 (最快)
.\scripts\run-test.ps1 -Direct -DataRoot .data-test\mcp-layer1 -Command @("pytest", "tests/unit", "-k", "mcp", "-v")

# Layer 2: 进程内集成测试
.\scripts\run-test.ps1 -Direct -DataRoot .data-test\mcp-layer2 -Command @("pytest", "tests/integration", "-k", "mcp", "-v")

# Layer 3: stdio 黑盒测试 (最慢,启动子进程)
.\scripts\run-test.ps1 -Direct -DataRoot .data-test\mcp-layer3 -Command @("pytest", "tests/blackbox/test_mcp_blackbox.py", "tests/blackbox/test_r4_mcp_fullflow.py", "tests/blackbox/test_outcome_mcp_blackbox.py", "tests/blackbox/test_w3_outcome_mcp_blackbox.py", "-v")

# 正式 MCP 覆盖率门禁：固定版本化目标集、离线 selector 与 95% 阈值
.\scripts\test-conda.ps1 -EnvironmentName py311-private -Suite MCP
```

`-k mcp` 仅适合本地临时定位，不是覆盖率 gate：它会随着其他测试名称变化而扩大或缩小。
`-Suite MCP` 的目标集固定覆盖 unit、integration、blackbox 与 E2E MCP 用例，并经
`run-test.ps1` 为本次运行建立新的 `.data-test/conda-*/mcp-coverage` 根。

W3 新旧 Outcome CLI/MCP 四文件组件合跑为 **21 passed / 230.03s，exit 0**，见
[组件日志](../../.data-test/w3-adapter-20261025-full/pytest.log)。新 MCP 主节点是
`tests/blackbox/test_w3_outcome_mcp_blackbox.py::test_outcome_mcp_v2_exact_review_replay_restart_and_v1_deprecation`，
覆盖旧确认拒绝、exact receipt replay、退回后拒绝审批、重启读取及旧 Tool 弃用；同文件
`test_outcome_mcp_v2_rejects_coercion_missing_and_extra_fields_without_writes` 覆盖 22 个
无效请求、合法 `"null"` selector 和零写入。该次组件运行包含启动错误 stage 微调前后的
进程；最终同树结果、默认全量、MCP coverage 与 Phase C 状态只按
[W3 主记录](../../docs/overview/W3-Outcome一致性与版本审批实施记录-2026-10.md) 登记。
组件计数不能替代这些门禁，也不是 Artifact/release 验收。

---

## 常见问题 (FAQ)

### Q1: stdio 模式下为什么日志不能输出到 stdout?

stdio 模式下 stdout 被 MCP JSON-RPC 协议占用，日志必须走 stderr。Server 已自动处理；
`pkv.log` 使用延迟 handler，只有当前写入 Tool 已持有同一 data-root 的 writer lease
时才会创建或追加。READY 读取与 status-only 启动都保持 stderr-only，避免日志把读
操作伪装成数据根写入。

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
| `tools.py` | 41 个 Tool handler 实现 |
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
| `tests/blackbox/test_r4_mcp_fullflow.py` | 真正 `archive_text`、关闭 server 后重启并 vector/hybrid 命中的 R4 source blackbox |
| `tests/blackbox/test_outcome_mcp_blackbox.py` | 真 stdio Topic → Evidence → Claim → research brief → restart read 的 P1-B blackbox |
| `tests/blackbox/test_w3_outcome_mcp_blackbox.py` | W3 v2 exact review/replay、strict scalar/extra fields、旧 v1 弃用和重启读取 |

### 配置文件

| 文件 | 说明 |
|------|------|
| `config/workflows/archive-url.yaml` | archive_url 使用的工作流 |
| `config/workflows/archive-text.yaml` | archive_text 使用的工作流 (M9 新增) |

---

## 变更记录 (Changelog)

### 2026-10-26（W4 Embedding v2 已完成范围验收）
- 在既有 35 Tool 上兼容增加 4 个只读与 2 个写入 Tool，当前共 41（23 只读 + 18 写入）。
- 新入口采用封闭原始 JSON 校验与 v2 envelope；v1 保留旧形状并透传 pending code/details。
  继续发布使用原 operation、完整 sealed proof 与当前确认，零 Provider、零新 reservation。
- SDK `0.10.0` / API `1.0.0`，`kernel.embedding-rebuild.v2` 已声明；整体门禁结果见 W4 主记录，W5 与 release hold 不变。

### 2026-10-25（W3 Outcome v2 兼容准备）
- 增加 2 个只读与 3 个写入 v2 Tool，当前共 35 Tool（19 只读 + 16 写入）；原 30 个保留。
- 新入口采用封闭参数、精确版本确认与统一 v2 envelope；旧无版本 review 已弃用但仍可执行，
  R-04 旧入口缺口保留至 W5。SDK `0.9.0` / API `1.0.0`，仅新增 Outcome v2 capability。
- 记录组件黑盒证据及 W3 主记录链接；未以该组件通过数宣称全量、coverage、Phase C 或发布通过。

### 2026-09-04 (P1-B traceable outcomes)
- 新增 10 个 outcome 写入 Tool 与 2 个只读 Tool，全部只通过 `pkv_kernel.outcomes`。
- Topic/Evidence/Claim/research brief 使用 SQLite authority 与 immutable Markdown projection；
  source drift 只显示 stale/needs_review，不会改绑或调用 Provider。
- 本条只表述 source-level MCP adapter/blackbox acceptance，不解除 Artifact/release compliance hold。

### 2026-09-04 (R4-P1 public Embedding rebuild)
- 新增 public inspect/plan/execute 三个 stdio Tool；它们通过窄的 Kernel façade，而不绕过
  Application/R4、Storage、Provider 或 writer lease 合同。
- `execute_embedding_rebuild` 使用独立 token ledger 与 fenced private stage；Provider 保持在
  root writer lease 外。该条目仅描述 source-level public adapter，不表示 Artifact/release 通过。

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
**最后更新**: 2026-10-26（W4 Embedding v2 已完成范围验收；历史证据保持原语境）

*本文档由 Claude Code 自动生成*
