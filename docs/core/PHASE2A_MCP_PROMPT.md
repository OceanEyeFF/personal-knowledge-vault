# Personal Knowledge Vault - Phase 2A 开发 Prompt

> MCP 服务开发执行指令（M8 + M9）
>
> **版本**: 1.0
> **创建日期**: 2026-02-18
> **适用对象**: Claude Code、CodeX 等 AI 开发工具
> **前置条件**: Phase 1 (v0.6.1) 已全部完成
> **总览文档**: [PHASE2_DEV_PROMPT.md](./PHASE2_DEV_PROMPT.md)

---

## 🎯 Phase 2A 目标

将 Personal Knowledge Vault 作为 **MCP Server** 暴露给 AI Agent（Claude Code、Cursor 等），
使 AI 能够直接搜索、检索、归档和浏览知识库。

**交付版本**: v0.7.0-alpha（M8）→ v0.7.0（M9）

---

## 📚 必读文档

- [PHASE2_DEV_PROMPT.md](./PHASE2_DEV_PROMPT.md) - Phase 2 总览（约束 + 原则 + 里程碑）
- [MCP_SERVICE_DESIGN.md](../design/MCP_SERVICE_DESIGN.md) - **MCP 服务完整技术设计**（必读！）
- [WorkflowEngine接口规范.md](../refactor/WorkflowEngine接口规范.md) - workflow engine API
- [Storage接口规范.md](../refactor/Storage接口规范.md) - SQLiteStore / MarkdownStore API
- [Retrieval检索引擎规范.md](../refactor/Retrieval检索引擎规范.md) - 检索引擎 API

---

## ⚙️ 技术基础（已验证）

- **SDK**: `mcp[cli]>=1.6.0`，正确导入：`from mcp.server.fastmcp import FastMCP`
  - `FastMCP` 是官方 SDK 的高级封装，不是 `MCPServer`（底层类）
  - 独立增强版：`fastmcp>=2.0.0`，导入：`from fastmcp import FastMCP`
  - **Tool 注解类**：`from mcp.types import ToolAnnotations`（注意：参数驼峰命名，如 `readOnlyHint=True`）
- **注册方式**: 模块级 `@mcp.tool()` / `@mcp.resource()` / `@mcp.prompt()` 装饰器
- **异步策略**（⚠️ 重要，与 FastAPI 不同）：
  - FastMCP 的同步 `def` handler **直接在 asyncio 事件循环调用**，任何 I/O 都会冻结服务器
  - Tool/Resource handler **必须使用 `async def` + `anyio.to_thread.run_sync()`**
  - 例外：`WorkflowEngine.execute_async()` 是原生 async，可直接 `await`，无需 threadpool
  - Prompt handler 只做字符串模板，可用同步 `def`
- **传输方式**: `mcp.run(transport="stdio")` 或 `mcp.run(transport="streamable-http")`
- **新增依赖**:
  ```txt
  mcp[cli]>=1.6.0   # MCP Python SDK（含 FastMCP 和 CLI 调试工具），2026-02 最新 v1.26.0
  anyio>=4.0.0      # FastMCP 自身依赖，用于 anyio.to_thread.run_sync()
  ```

---

## 🏗️ Milestone 8: MCP 只读服务 (v0.7.0-alpha)

**目标**: 搭建 MCP Server 框架，实现所有只读查询能力

**技术方案**: 详见 [MCP_SERVICE_DESIGN.md](../design/MCP_SERVICE_DESIGN.md) 第 4 节

### Tool 详细设计与可行性

##### Tool 1: `search_knowledge` — 搜索知识库 ✅ 低成本

```python
@mcp.tool(annotations=ToolAnnotations(title="搜索知识库", readOnlyHint=True))
async def search_knowledge(query: str, strategy: str = "auto", top_k: int = 5) -> dict:
    return await anyio.to_thread.run_sync(lambda: _do_search_knowledge(query, strategy, top_k))
```

| 项目 | 说明 |
|------|------|
| **调用链** | `QueryRouter.search(query, limit=top_k)` — 已有 sync API |
| **实现成本** | 约 15 行，直接调用现有 `QueryRouter` |
| **客户端兼容** | ✅ Claude Code / Cursor 均支持带参数的 Tool 调用 |
| **风险** | 无，核心路径已在 CLI 中验证过 |

##### Tool 2: `get_entry` — 获取条目详情 ✅ 低成本

```python
@mcp.tool(annotations=ToolAnnotations(title="获取知识条目", readOnlyHint=True))
async def get_entry(knowledge_id: str) -> dict:
    return await anyio.to_thread.run_sync(lambda: _do_get_entry(knowledge_id))
```

| 项目 | 说明 |
|------|------|
| **调用链** | `SQLiteStore.query_by_id(int(knowledge_id))` + `MarkdownStore.load(file_path)` — 均已有（注意：DB 中 `knowledge_id` 是 INTEGER，MCP 层传入 str 需 `int()` 转换） |
| **实现成本** | 约 20 行，查元数据 + 读 Markdown 全文 |
| **风险** | 无 |

##### Tool 3: `list_tags` — 列出标签 ⚠️ 需要新增 SQLiteStore 方法

```python
@mcp.tool(annotations=ToolAnnotations(title="列出知识库标签", readOnlyHint=True))
async def list_tags() -> dict:
    return await anyio.to_thread.run_sync(_do_list_tags)
```

| 项目 | 说明 |
|------|------|
| **调用链** | `SQLiteStore.get_all_tags_with_count()` — **需新增** |
| **新增 SQL** | `SELECT t.name, COUNT(*) as cnt FROM tags t JOIN knowledge_tags kt ON ... GROUP BY t.name ORDER BY cnt DESC` |
| **实现成本** | SQLiteStore 新增约 15 行 + Tool handler 约 10 行 |
| **风险** | 低，标准 SQL 聚合查询 |

##### Tool 4: `list_entries` — 浏览条目列表 ⚠️ 需要新增 SQLiteStore 方法

```python
@mcp.tool(annotations=ToolAnnotations(title="浏览知识条目列表", readOnlyHint=True))
async def list_entries(page: int = 1, per_page: int = 20, source_type: str = "") -> dict:
    return await anyio.to_thread.run_sync(lambda: _do_list_entries(page, per_page, source_type))
```

| 项目 | 说明 |
|------|------|
| **调用链** | `SQLiteStore.list_entries(limit, offset, ...)` + `SQLiteStore.count_entries(...)` — **均需新增** |
| **新增 SQL** | `SELECT ... FROM knowledge_items WHERE ... ORDER BY archived_at DESC LIMIT ? OFFSET ?` |
| **实现成本** | SQLiteStore 新增约 30 行 + Tool handler 约 15 行 |
| **风险** | 低，标准分页查询 |

##### Tool 5: `get_stats` — 知识库统计 ⚠️ 需要新增 SQLiteStore 方法

```python
@mcp.tool(annotations=ToolAnnotations(title="知识库统计", readOnlyHint=True))
async def get_stats() -> dict:
    return await anyio.to_thread.run_sync(_do_get_stats)
```

| 项目 | 说明 |
|------|------|
| **调用链** | `SQLiteStore.get_statistics()` — **需新增** |
| **新增 SQL** | `SELECT COUNT(*), source_type, AVG(word_count) FROM knowledge_items GROUP BY ...` |
| **实现成本** | SQLiteStore 新增约 25 行 + Tool handler 约 5 行 |
| **风险** | 低 |

### Resource 详细设计与可行性

##### Resource 1-2: `pkv://entries/{id}` 和 `pkv://entries/{id}/metadata` ✅ 低成本

```python
@mcp.resource("pkv://entries/{knowledge_id}")
async def get_entry_content(knowledge_id: str) -> str:
    return await anyio.to_thread.run_sync(lambda: _do_get_entry_content(knowledge_id))

@mcp.resource("pkv://entries/{knowledge_id}/metadata")
async def get_entry_metadata(knowledge_id: str) -> str:
    return await anyio.to_thread.run_sync(lambda: _do_get_entry_metadata(knowledge_id))
```

| 项目 | 说明 |
|------|------|
| **调用链** | `SQLiteStore.query_by_id(int(knowledge_id))` + `MarkdownStore.load()` — 均已有 |
| **实现成本** | 每个约 10 行 |
| **注意** | Resource 返回 `str`（文本），Tool 返回 `dict`（结构化数据），两者互补 |

##### Resource 3-4: `pkv://tags` 和 `pkv://stats` ✅ 低成本

```python
@mcp.resource("pkv://tags")
async def get_tags_resource() -> str:
    return await anyio.to_thread.run_sync(_do_get_tags_json)

@mcp.resource("pkv://stats")
async def get_stats_resource() -> str:
    return await anyio.to_thread.run_sync(_do_get_stats_json)
```

| 项目 | 说明 |
|------|------|
| **调用链** | 复用 `list_tags` / `get_stats` 的 SQLiteStore 方法，输出 `json.dumps()` |
| **实现成本** | 每个约 8 行 |

### 前置工作：SQLiteStore 扩展（M8 核心工作量）

M8 最大的实现工作不是 MCP 框架搭建，而是补全 SQLiteStore 的查询能力：

| 新增方法 | SQL 复杂度 | 估算行数 |
|---------|-----------|---------|
| `list_entries(limit, offset, source_type, tag)` | 中（动态 WHERE + JOIN） | ~30 行 |
| `count_entries(source_type, tag)` | 低（COUNT + WHERE） | ~15 行 |
| `get_all_tags_with_count()` | 低（GROUP BY + ORDER BY） | ~15 行 |
| `get_statistics()` | 中（多个聚合查询） | ~25 行 |
| **合计** | | **~85 行** |

这些全是标准 SQL，基于现有 Schema 即可实现，不涉及任何表结构变更。

### 交付文件清单

- [ ] `src/mcp/__init__.py` - MCP 模块初始化
- [ ] `src/mcp/server.py` - FastMCP 主入口（stdio + streamable-http）
- [ ] `src/mcp/tools.py` - 5 个只读 Tool
- [ ] `src/mcp/resources.py` - 4 个 Resource
- [ ] `src/mcp/utils.py` - 序列化和错误处理
- [ ] `src/storage/sqlite_store.py` - 补充 4 个查询方法
- [ ] `tests/unit/test_mcp_tools.py` - Tool 单元测试
- [ ] `tests/unit/test_mcp_resources.py` - Resource 单元测试
- [ ] `tests/unit/test_sqlite_store_new.py` - 新增 SQLiteStore 方法测试
- [ ] `tests/integration/test_mcp_integration.py` - MCP 集成测试

**验收标准**:
```bash
# stdio 模式启动
python -m src.mcp.server

# HTTP 模式启动（M9 完成后）
python -m src.mcp.server --transport streamable-http --port 3000

# MCP Inspector 可视化测试
npx @modelcontextprotocol/inspector python -m src.mcp.server
```

**验收检查点**:
1. MCP Inspector 中可发现所有 5 个 Tool 和 4 个 Resource
2. `search_knowledge` 返回正确结果，支持策略切换
3. `get_entry` 返回完整条目（含 Markdown 全文）
4. stdio 传输方式正常工作
5. 单元测试全部通过，覆盖率 ≥ 85%

---

## 🏗️ Milestone 9: MCP 写入 + Prompts (v0.7.0)

**目标**: 补全 MCP 写入能力、Prompt 模板和安全加固，交付完整 v0.7.0

**前置**: M8 完成

### Tool 详细设计与可行性

##### Tool 6: `archive_url` — 归档网页 ✅ 低成本

```python
@mcp.tool(annotations=ToolAnnotations(title="归档网页"))
async def archive_url(url: str) -> dict:
    # WorkflowEngine() 无参构造，内部自动 get_config()；execute_async() 是原生 async，可直接 await
    engine = WorkflowEngine()
    result = await engine.execute_async("archive-url", {"url": url})
    return {"success": result.success, **result.data} if result.success \
        else {"success": False, "error": result.errors[0] if result.errors else "归档失败"}
```

| 项目 | 说明 |
|------|------|
| **调用链** | `await WorkflowEngine().execute_async("archive-url", {"url": url})` — 无参构造，原生 async，无需 threadpool |
| **实现成本** | 约 15 行（调用 + 结果序列化 + 错误处理） |
| **风险** | 归档耗时较长（10-30s），但 MCP 协议无超时限制 |
| **安全** | 需验证 URL 格式，拒绝 `127.0.0.1` / `localhost` / `10.*` / `192.168.*` 内网地址 |

##### Tool 7: `archive_text` — 归档文本 ✅ 低成本

```python
@mcp.tool(annotations=ToolAnnotations(title="归档文本"))
async def archive_text(text: str, title: str = "") -> dict:
    engine = WorkflowEngine()  # 无参构造，内部自动 get_config()
    result = await engine.execute_async("archive-text", {"text": text, "title": title})
    return {"success": result.success, **result.data} if result.success \
        else {"success": False, "error": result.errors[0] if result.errors else "归档失败"}
```

| 项目 | 说明 |
|------|------|
| **注意** | 现有工作流只有 `archive-url`，若无 `archive-text` 工作流需新增 YAML 配置 |
| **安全** | 文本长度限制 100,000 字符 |

##### Tool 8: `get_related` — 获取关联知识 ✅ 中等成本

```python
@mcp.tool(annotations=ToolAnnotations(title="获取关联知识", readOnlyHint=True))
async def get_related(knowledge_id: str, limit: int = 5) -> dict:
    return await anyio.to_thread.run_sync(lambda: _do_get_related(knowledge_id, limit))
```

| 项目 | 说明 |
|------|------|
| **调用链** | 读取条目 → 取其 embedding → `VectorRetriever` 做相似度搜索 |
| **实现成本** | 约 25 行（取已有 embedding + 向量搜索 + 排除自身） |
| **风险** | 依赖向量索引已建立；若条目无 embedding 需优雅降级 |

### Prompt 详细设计与可行性

> **关键说明**: MCP Prompt 是**用户可通过客户端 UI 选择的提示词模板**，不是"AI 系统提示词"。
> 用户在 Prompt 列表里选择模板 → 填入参数 → 模板生成引导文本 → 客户端发给 AI。
> AI 根据引导文本自行决定调用哪些 Tool。

##### Prompt 1: `search_and_summarize` ✅ 零成本

```python
@mcp.prompt()
def search_and_summarize(query: str, context: str = "") -> str:
    base = f"请在我的知识库中搜索关于「{query}」的内容。"
    if context:
        base += f"\n\n背景信息：{context}"
    base += "\n\n请搜索、总结，并指出最相关的 1-3 条内容的标题和关键信息。"
    return base
```

##### Prompt 2: `knowledge_qa` ✅ 零成本

```python
@mcp.prompt()
def knowledge_qa(question: str) -> str:
    return f"请基于知识库回答：{question}\n请先搜索相关条目，再基于内容回答，引用具体标题。"
```

##### Prompt 3: `idea_sharpen` ✅ 零成本

```python
@mcp.prompt()
def idea_sharpen(content: str, entry_id: str = "") -> str:
    base = f"让我们对以下内容进行 idea Sharpen（思想磨砺）：\n\n{content[:2000]}\n\n"
    if entry_id:
        base += f"（知识条目 ID：{entry_id}）\n\n"
    base += "请提炼核心价值、关键观点、知识关联和应用场景。"
    return base
```

### 安全加固

| 安全措施 | 实现方式 | 成本 |
|---------|---------|------|
| URL 格式验证 | `urllib.parse.urlparse()` + 正则匹配 | ~10 行 |
| 拒绝内网地址 | 检查 IP 是否为 `127.*` / `10.*` / `192.168.*` / `172.16-31.*` | ~15 行 |
| 文本长度限制 | `len(text) > 100000` → 返回错误 | ~5 行 |
| 参数范围限制 | `top_k = min(top_k, 50)`, `per_page = min(per_page, 100)` | ~5 行 |

合计约 35 行安全代码，放在 `src/mcp/utils.py` 中。

### 交付文件清单

- [ ] `src/mcp/tools.py` 补充 3 个写入 Tool（archive_url, archive_text, get_related）
- [ ] `src/mcp/prompts.py` - 3 个 Prompt 模板
- [ ] `src/mcp/utils.py` 补充安全验证函数
- [ ] `config/workflows/archive-text.yaml` - 文本归档工作流（若不存在则新增）
- [ ] `tests/unit/test_mcp_prompts.py` - Prompt 模板测试
- [ ] `tests/unit/test_mcp_security.py` - 安全验证测试
- [ ] `docs/MCP_INTEGRATION_GUIDE.md` - Claude Desktop / Cursor 配置文档
- [ ] 更新 README.md、CHANGELOG.md

**验收检查点**:
1. Claude Code 能通过 MCP 归档 URL 和文本
2. MCP Prompt 模板在 MCP Inspector 中正确显示并可填入参数
3. 安全验证：`archive_url("http://127.0.0.1/admin")` 被拒绝
4. 完整的配置文档（用户可照做集成到 Claude Desktop / Cursor）
5. 所有测试通过，MCP 模块覆盖率 ≥ 85%

---

## 🧩 关键设计决策

### 决策 1: GUI 与 MCP 的集成策略 — 分步走

**决策**: 三个入口共享 Service Layer，各自独立演进。

```
┌─────────────┐  ┌─────────────┐  ┌─────────────┐
│   CLI 入口   │  │  MCP Server │  │  GUI 应用    │
│  (v0.6.x)   │  │  (v0.7.0)   │  │  (v0.8.0)   │
└──────┬──────┘  └──────┬──────┘  └──────┬──────┘
       │                │                │
       └────────────────┼────────────────┘
                        ↓
              ┌──────────────────┐
              │  Service Layer    │
              │  (共享核心逻辑)    │
              └──────────────────┘
```

**分步计划**:
1. **v0.7.0**: MCP Server 作为独立进程（stdio/HTTP），不依赖 GUI
2. **v0.8.0**: GUI 通过 Python import 直接调用 Service Layer，**不经过 MCP**
3. **远期**: 按需集成（GUI 内嵌 MCP / GUI 作为 MCP Client 调用外部服务）

**理由**: 进程内调用（~0ms）远优于 MCP 协议通信（~50ms + 序列化开销）

### 决策 2: AI 对话笔记能力 — MCP 轻量方案

**背景**: MCP Server 看不到客户端内部的完整对话流，只能感知 Tool 调用。

**典型使用流程**（手动保存笔记）:
```
1. 用户选择 Prompt "knowledge_qa" 或 "idea_sharpen"
   → 自动注入文章上下文（AI 调用 get_entry Tool）
2. 用户与 AI 自由讨论文章内容
   → AI 可调用 search_knowledge / get_related 拉取关联知识
3. 讨论结束时用户说"总结我们的对话并保存到知识库"
   → AI 调用 archive_text(text="对话摘要...", title="XXX文章笔记")
```

| 能力 | 支持情况 | 说明 |
|------|---------|------|
| AI 读取已归档文章 | ✅ 完全支持 | `get_entry` Tool |
| 对话中拉取关联知识 | ✅ 完全支持 | `search_knowledge` / `get_related` Tool |
| 用户主动保存笔记 | ✅ 完全支持 | 用户指示 AI 调用 `archive_text` |
| 自动记录完整对话 | ❌ 无法实现 | MCP Server 看不到客户端内部对话流 |

**结论**: 以上能力完全由已设计的 Tool + Prompt 组合覆盖，**M9 无需额外开发**。
完整自动对话记录能力在 M12 GUI 中实现。

---

## 🧪 测试要求

| 测试类型 | 最低覆盖率 | 工具 |
|---------|-----------|------|
| 单元测试 | 85% | pytest + mock |
| 集成测试 | 关键路径 100% | pytest |
| MCP 功能测试 | 所有 Tool/Resource/Prompt | MCP Inspector |

---

## 📦 交付清单汇总

### v0.7.0-alpha 交付 (M8)
- [ ] `src/mcp/` - MCP 只读服务模块（5 个文件）
- [ ] 5 个只读 Tool + 4 个 Resource
- [ ] 单元测试 + 集成测试
- [ ] stdio 传输支持

### v0.7.0 交付 (M9)
- [ ] 3 个写入 Tool + 3 个 Prompt 模板
- [ ] streamable-http 传输支持
- [ ] 安全加固（输入验证、长度限制）
- [ ] Claude Desktop / Cursor 配置文档
- [ ] 更新 README.md、CHANGELOG.md

---

**文档版本**: v1.0
**创建日期**: 2026-02-18
**对应里程碑**: M8 (v0.7.0-alpha) + M9 (v0.7.0)
