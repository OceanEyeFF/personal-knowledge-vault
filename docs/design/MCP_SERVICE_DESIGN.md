# MCP 服务技术设计文档

> Personal Knowledge Vault - Model Context Protocol 服务端设计
>
> **文档版本**: v1.0
> **创建日期**: 2026-02-16
> **作者**: 幽浮喵 (猫娘工程师)
> **目标版本**: v0.7.0

---

## 1. 设计目标

### 核心目标

将 Personal Knowledge Vault 作为 **MCP Server** 暴露给 AI Agent（Claude Code、CodeX、Cursor 等），使 AI 能够：

1. **查询知识库** — 搜索、检索、浏览已归档的知识条目
2. **归档内容** — 通过 AI Agent 触发内容归档工作流
3. **获取推荐** — 基于主题/概念获取关联知识推荐
4. **知识库统计** — 获取知识库的整体状态和统计信息

### 设计原则

- **只读优先**：首期以查询能力为主，写入操作需确认
- **现有复用**：直接调用 `src/` 下的现有模块，不重复实现
- **标准协议**：严格遵循 MCP 规范（2025-11-05 版本）
- **轻量部署**：支持 stdio 和 streamable-http 两种传输方式

---

## 2. MCP 能力清单

### 2.1 Tools（工具）

AI Agent 可调用的操作能力：

| Tool 名称 | 描述 | 输入 | 输出 | 优先级 |
|-----------|------|------|------|--------|
| `search_knowledge` | 搜索知识库 | query, strategy?, top_k?, filters? | 搜索结果列表 | **P0** |
| `get_entry` | 获取单条知识详情 | knowledge_id 或 file_path | 完整知识条目 | **P0** |
| `list_tags` | 列出所有标签及统计 | - | 标签列表+计数 | **P0** |
| `list_entries` | 浏览知识条目列表 | page?, per_page?, sort?, filter? | 条目列表 | **P1** |
| `archive_url` | 归档网页 URL | url | 归档结果 | **P1** |
| `archive_text` | 归档纯文本 | text, title? | 归档结果 | **P1** |
| `get_stats` | 知识库统计信息 | - | 统计数据 | **P1** |
| `get_related` | 获取关联知识 | knowledge_id, limit? | 关联条目列表 | **P2** |

### 2.2 Resources（资源）

AI Agent 可读取的静态/动态数据：

| Resource URI | 描述 | 类型 | 优先级 |
|-------------|------|------|--------|
| `pkv://entries/{knowledge_id}` | 知识条目内容 | 动态 | **P0** |
| `pkv://entries/{knowledge_id}/metadata` | 条目元数据 | 动态 | **P0** |
| `pkv://tags` | 标签列表 | 动态 | **P1** |
| `pkv://stats` | 知识库统计 | 动态 | **P1** |
| `pkv://config` | 系统配置信息 | 静态 | **P2** |

### 2.3 Prompts（提示词模板）

预定义的知识库交互提示词：

| Prompt 名称 | 描述 | 参数 | 优先级 |
|-------------|------|------|--------|
| `search_and_summarize` | 搜索后自动总结 | query, context? | **P1** |
| `knowledge_qa` | 基于知识库的问答 | question | **P1** |
| `idea_sharpen` | idea Sharpen 对话 | content, entry_id? | **P2** |

---

## 3. 架构设计

### 3.1 系统架构图

```
┌────────────────────────────────────────────────┐
│            AI Agent (Claude Code / Cursor)       │
│  ┌──────────────────────────────────────────┐   │
│  │  MCP Client                              │   │
│  │  • 发现 Tools / Resources / Prompts      │   │
│  │  • 调用 Tool → 获取结果                  │   │
│  │  • 读取 Resource → 获取内容              │   │
│  └────────────────┬─────────────────────────┘   │
└───────────────────┼─────────────────────────────┘
                    │ MCP Protocol
                    │ (stdio / streamable-http)
                    ↓
┌────────────────────────────────────────────────┐
│            PKV MCP Server (新增)                │
│  ┌──────────────────────────────────────────┐   │
│  │  src/mcp/server.py                       │   │
│  │  • MCPServer 实例                        │   │
│  │  • Tool handlers                         │   │
│  │  • Resource handlers                     │   │
│  │  • Prompt handlers                       │   │
│  └────────────────┬─────────────────────────┘   │
│                   │                              │
│  ┌────────────────▼─────────────────────────┐   │
│  │  src/mcp/tools.py (Tool 实现)            │   │
│  │  src/mcp/resources.py (Resource 实现)     │   │
│  │  src/mcp/prompts.py (Prompt 模板)        │   │
│  └────────────────┬─────────────────────────┘   │
└───────────────────┼─────────────────────────────┘
                    │ 直接调用
                    ↓
┌────────────────────────────────────────────────┐
│            现有 PKV 模块（不修改）               │
│                                                 │
│  src/retrieval/  → 搜索引擎                     │
│  src/storage/    → 存储层                       │
│  src/workflow/   → 工作流引擎                   │
│  src/ai/         → AI 服务                      │
│  src/processors/ → 内容处理器                   │
└────────────────────────────────────────────────┘
```

### 3.2 模块结构

```
src/mcp/
├── __init__.py          # 模块导出
├── server.py            # MCPServer 主入口
├── tools.py             # Tool handler 实现
├── resources.py         # Resource handler 实现
├── prompts.py           # Prompt 模板定义
└── utils.py             # MCP 辅助工具（序列化、错误处理）
```

### 3.3 传输方式

| 传输方式 | 场景 | 优先级 |
|---------|------|--------|
| **stdio** | Claude Code 本地集成 | **P0** |
| **streamable-http** | 远程 / Web 集成 | **P1** |

---

## 4. 详细设计

### 4.1 Server 主入口

```python
# src/mcp/server.py
from mcp.server.mcpserver import MCPServer

from src.mcp.tools import register_tools
from src.mcp.resources import register_resources
from src.mcp.prompts import register_prompts


def create_mcp_server() -> MCPServer:
    """创建并配置 PKV MCP Server"""
    mcp = MCPServer(
        name="Personal Knowledge Vault",
        instructions=(
            "个人知识库 MCP 服务。支持知识搜索、归档、浏览和统计。\n"
            "可用工具：search_knowledge（搜索）、get_entry（查看详情）、"
            "archive_url（归档URL）、archive_text（归档文本）等。\n"
            "知识条目包含标题、摘要、标签、全文等信息。"
        ),
        version="0.7.0",
    )

    # 注册各类能力
    register_tools(mcp)
    register_resources(mcp)
    register_prompts(mcp)

    return mcp


def main():
    """CLI 入口：启动 MCP 服务"""
    import argparse
    parser = argparse.ArgumentParser(description="PKV MCP Server")
    parser.add_argument(
        "--transport", choices=["stdio", "streamable-http"],
        default="stdio", help="传输方式"
    )
    parser.add_argument("--port", type=int, default=3000, help="HTTP 端口")
    args = parser.parse_args()

    mcp = create_mcp_server()

    if args.transport == "stdio":
        mcp.run(transport="stdio")
    else:
        mcp.run(
            transport="streamable-http",
            port=args.port,
            stateless_http=True,
            json_response=True,
        )


if __name__ == "__main__":
    main()
```

### 4.2 Tool 实现

```python
# src/mcp/tools.py
from typing import Optional
from mcp.server.mcpserver import MCPServer

from src.retrieval import create_retriever, QueryRouter
from src.storage.sqlite_store import SQLiteStore
from src.storage.markdown_store import MarkdownStore
from src.workflow.engine import WorkflowEngine
from src.utils.config import get_config


def register_tools(mcp: MCPServer):
    """注册所有 MCP Tools"""

    config = get_config()

    @mcp.tool()
    async def search_knowledge(
        query: str,
        strategy: str = "auto",
        top_k: int = 5,
        source_type: Optional[str] = None,
        tag: Optional[str] = None,
    ) -> dict:
        """搜索知识库。

        Args:
            query: 搜索查询文本
            strategy: 检索策略 - "auto"(自动路由), "bm25"(关键词), "vector"(语义), "hybrid"(混合)
            top_k: 返回结果数量 (默认 5)
            source_type: 按来源类型过滤 (wechat/zhihu/generic/chat/ai_chat/text/news)
            tag: 按标签过滤

        Returns:
            包含搜索结果列表的字典，每项包含 title, abstract, score, tags, source_type
        """
        # 使用现有的检索引擎
        if strategy == "auto":
            router = QueryRouter()
            strategy = router.route(query)

        retriever = create_retriever(strategy, config)
        results = await retriever.search(query, top_k=top_k)

        # 序列化结果
        return {
            "total": len(results),
            "strategy_used": strategy,
            "results": [
                {
                    "knowledge_id": r.knowledge_id,
                    "title": r.title,
                    "abstract": r.abstract,
                    "score": round(r.score, 4),
                    "tags": r.tags,
                    "source_type": r.source_type,
                    "created_at": r.created_at,
                }
                for r in results
            ],
        }

    @mcp.tool()
    async def get_entry(knowledge_id: str) -> dict:
        """获取知识条目完整内容。

        Args:
            knowledge_id: 知识条目 ID

        Returns:
            包含标题、摘要、标签、全文内容等完整信息的字典
        """
        store = SQLiteStore(config)
        entry = store.get_entry(knowledge_id)

        if not entry:
            return {"error": f"未找到条目: {knowledge_id}"}

        # 读取 Markdown 全文
        md_store = MarkdownStore(config)
        content = md_store.load(entry.get("file_path", ""))

        return {
            "knowledge_id": entry["knowledge_id"],
            "title": entry["title"],
            "abstract": entry.get("abstract", ""),
            "summary_one_sentence": entry.get("summary_one_sentence", ""),
            "summary_100_words": entry.get("summary_100_words", ""),
            "tags": entry.get("tags", []),
            "keywords": entry.get("keywords", []),
            "source_type": entry.get("source_type", ""),
            "source_url": entry.get("source_url", ""),
            "created_at": entry.get("created_at", ""),
            "word_count": entry.get("word_count", 0),
            "content": content if content else "(内容不可用)",
        }

    @mcp.tool()
    async def list_tags() -> dict:
        """列出知识库所有标签及统计。

        Returns:
            标签列表，每项包含标签名和关联条目数
        """
        store = SQLiteStore(config)
        tags = store.get_all_tags_with_count()
        return {
            "total_tags": len(tags),
            "tags": [
                {"name": t["name"], "count": t["count"]}
                for t in tags
            ],
        }

    @mcp.tool()
    async def list_entries(
        page: int = 1,
        per_page: int = 20,
        sort_by: str = "created_at",
        sort_order: str = "desc",
        source_type: Optional[str] = None,
        tag: Optional[str] = None,
    ) -> dict:
        """浏览知识条目列表。

        Args:
            page: 页码 (从 1 开始)
            per_page: 每页数量 (默认 20，最大 100)
            sort_by: 排序字段 - "created_at", "title", "word_count"
            sort_order: 排序方向 - "asc" 或 "desc"
            source_type: 按来源类型过滤
            tag: 按标签过滤

        Returns:
            分页的条目列表
        """
        store = SQLiteStore(config)
        per_page = min(per_page, 100)  # 限制最大数量
        offset = (page - 1) * per_page

        entries = store.list_entries(
            limit=per_page,
            offset=offset,
            sort_by=sort_by,
            sort_order=sort_order,
            source_type=source_type,
            tag=tag,
        )

        total = store.count_entries(source_type=source_type, tag=tag)

        return {
            "total": total,
            "page": page,
            "per_page": per_page,
            "total_pages": (total + per_page - 1) // per_page,
            "entries": [
                {
                    "knowledge_id": e["knowledge_id"],
                    "title": e["title"],
                    "abstract": e.get("abstract", ""),
                    "tags": e.get("tags", []),
                    "source_type": e.get("source_type", ""),
                    "word_count": e.get("word_count", 0),
                    "created_at": e.get("created_at", ""),
                }
                for e in entries
            ],
        }

    @mcp.tool()
    async def archive_url(url: str) -> dict:
        """归档网页 URL 到知识库。

        Args:
            url: 要归档的网页链接

        Returns:
            归档结果，包含生成的 knowledge_id 和文件路径
        """
        engine = WorkflowEngine(config)
        result = await engine.execute_async(
            workflow_name="archive-url",
            input_data={"url": url},
        )

        if result.success:
            return {
                "success": True,
                "knowledge_id": result.data.get("knowledge_id", ""),
                "title": result.data.get("title", ""),
                "file_path": str(result.data.get("file_path", "")),
                "tags": result.data.get("tags", []),
                "abstract": result.data.get("abstract", ""),
            }
        else:
            return {
                "success": False,
                "error": result.error or "归档失败",
            }

    @mcp.tool()
    async def archive_text(text: str, title: str = "") -> dict:
        """归档纯文本到知识库。

        Args:
            text: 要归档的文本内容
            title: 可选的标题（不提供则自动生成）

        Returns:
            归档结果，包含生成的 knowledge_id 和文件路径
        """
        engine = WorkflowEngine(config)
        result = await engine.execute_async(
            workflow_name="archive-text",
            input_data={"text": text, "title": title},
        )

        if result.success:
            return {
                "success": True,
                "knowledge_id": result.data.get("knowledge_id", ""),
                "title": result.data.get("title", ""),
                "file_path": str(result.data.get("file_path", "")),
                "tags": result.data.get("tags", []),
            }
        else:
            return {
                "success": False,
                "error": result.error or "归档失败",
            }

    @mcp.tool()
    async def get_stats() -> dict:
        """获取知识库统计信息。

        Returns:
            包含条目总数、标签分布、来源类型分布等统计数据
        """
        store = SQLiteStore(config)
        stats = store.get_statistics()
        return stats
```

### 4.3 Resource 实现

```python
# src/mcp/resources.py
from mcp.server.mcpserver import MCPServer

from src.storage.sqlite_store import SQLiteStore
from src.storage.markdown_store import MarkdownStore
from src.utils.config import get_config


def register_resources(mcp: MCPServer):
    """注册所有 MCP Resources"""

    config = get_config()

    @mcp.resource("pkv://entries/{knowledge_id}")
    async def get_entry_content(knowledge_id: str) -> str:
        """获取知识条目的 Markdown 全文"""
        store = SQLiteStore(config)
        entry = store.get_entry(knowledge_id)

        if not entry:
            return f"# 未找到条目\n\nknowledge_id: {knowledge_id}"

        md_store = MarkdownStore(config)
        content = md_store.load(entry.get("file_path", ""))

        return content or f"# {entry.get('title', '无标题')}\n\n(内容不可用)"

    @mcp.resource("pkv://entries/{knowledge_id}/metadata")
    async def get_entry_metadata(knowledge_id: str) -> str:
        """获取知识条目的元数据（JSON 格式）"""
        import json

        store = SQLiteStore(config)
        entry = store.get_entry(knowledge_id)

        if not entry:
            return json.dumps({"error": f"未找到条目: {knowledge_id}"})

        return json.dumps(entry, ensure_ascii=False, indent=2, default=str)

    @mcp.resource("pkv://tags")
    async def get_tags() -> str:
        """获取所有标签列表"""
        import json

        store = SQLiteStore(config)
        tags = store.get_all_tags_with_count()

        return json.dumps(
            {"tags": tags},
            ensure_ascii=False,
            indent=2,
        )

    @mcp.resource("pkv://stats")
    async def get_stats() -> str:
        """获取知识库统计信息"""
        import json

        store = SQLiteStore(config)
        stats = store.get_statistics()

        return json.dumps(stats, ensure_ascii=False, indent=2, default=str)
```

### 4.4 Prompt 模板

```python
# src/mcp/prompts.py
from mcp.server.mcpserver import MCPServer


def register_prompts(mcp: MCPServer):
    """注册所有 MCP Prompt 模板"""

    @mcp.prompt()
    def search_and_summarize(query: str, context: str = "") -> str:
        """搜索知识库并总结结果。

        先使用 search_knowledge 工具搜索，然后总结找到的内容。
        """
        base = f"请在我的知识库中搜索关于「{query}」的内容。"
        if context:
            base += f"\n\n背景信息：{context}"
        base += (
            "\n\n请执行以下步骤：\n"
            "1. 使用 search_knowledge 工具搜索相关内容\n"
            "2. 对搜索结果进行总结归纳\n"
            "3. 如果找到多个相关条目，说明它们之间的关系\n"
            "4. 指出最相关的 1-3 条内容的标题和关键信息"
        )
        return base

    @mcp.prompt()
    def knowledge_qa(question: str) -> str:
        """基于知识库的智能问答。

        利用知识库中的内容回答用户问题。
        """
        return (
            f"请基于我的个人知识库回答以下问题：\n\n"
            f"**问题**：{question}\n\n"
            f"请执行以下步骤：\n"
            f"1. 使用 search_knowledge 搜索可能相关的知识条目\n"
            f"2. 如果找到相关内容，使用 get_entry 获取详细信息\n"
            f"3. 基于知识库中的内容给出回答\n"
            f"4. 如果知识库中没有相关信息，明确告知\n"
            f"5. 引用具体的知识条目标题和来源"
        )

    @mcp.prompt()
    def idea_sharpen(content: str, entry_id: str = "") -> str:
        """对知识条目进行 idea Sharpen 对话。

        帮助用户深入思考某个知识条目的核心价值。
        """
        base = (
            f"让我们对以下内容进行 idea Sharpen（思想磨砺）：\n\n"
            f"**内容**：\n{content[:2000]}\n\n"
        )
        if entry_id:
            base += f"（知识条目 ID：{entry_id}）\n\n"
        base += (
            "请帮我深入思考以下问题：\n"
            "1. 这篇内容的**核心价值**是什么？\n"
            "2. 有哪些**关键观点**值得记住？\n"
            "3. 与我知识库中的其他内容有什么**关联**？\n"
            "4. 这些知识可以如何**应用**到实际场景中？\n\n"
            "如果有关联条目，请使用 search_knowledge 搜索并建立关联。"
        )
        return base
```

---

## 5. Claude Code 集成配置

### 5.1 stdio 方式（推荐）

在 `.claude/claude_desktop_config.json` 或项目 MCP 配置中：

```json
{
  "mcpServers": {
    "personal-knowledge-vault": {
      "command": "python",
      "args": ["-m", "src.mcp.server"],
      "cwd": "/path/to/personal-knowledge-vault",
      "env": {
        "DEEPSEEK_API_KEY": "sk-xxx",
        "OPENAI_API_KEY": "sk-xxx"
      }
    }
  }
}
```

### 5.2 HTTP 方式（远程访问）

```bash
# 启动 HTTP 服务
python -m src.mcp.server --transport streamable-http --port 3000
```

客户端配置：

```json
{
  "mcpServers": {
    "personal-knowledge-vault": {
      "url": "http://localhost:3000/mcp"
    }
  }
}
```

---

## 6. 与现有模块的集成

### 6.1 直接调用映射

| MCP 能力 | 调用的现有模块 | 调用方式 |
|---------|---------------|---------|
| `search_knowledge` | `src/retrieval/` | `create_retriever()` + `QueryRouter` |
| `get_entry` | `src/storage/sqlite_store.py` + `markdown_store.py` | `SQLiteStore.get_entry()` + `MarkdownStore.load()` |
| `list_tags` | `src/storage/sqlite_store.py` | `SQLiteStore.get_all_tags_with_count()` |
| `archive_url` | `src/workflow/engine.py` | `WorkflowEngine.execute_async("archive-url")` |
| `archive_text` | `src/workflow/engine.py` | `WorkflowEngine.execute_async("archive-text")` |
| `get_stats` | `src/storage/sqlite_store.py` | `SQLiteStore.get_statistics()` |

### 6.2 需要新增的 SQLiteStore 方法

现有 `SQLiteStore` 可能需要补充以下方法以支持 MCP：

```python
# 可能需要新增的方法
class SQLiteStore:
    def list_entries(self, limit, offset, sort_by, sort_order, source_type, tag) -> list
    def count_entries(self, source_type, tag) -> int
    def get_all_tags_with_count(self) -> list
    def get_statistics(self) -> dict
```

这些方法的 SQL 查询较为简单，基于现有 Schema 即可实现。

---

## 7. 安全考虑

### 7.1 读写权限

- **只读操作** (search, get, list): 无需额外确认
- **写入操作** (archive_url, archive_text):
  - stdio 模式：AI Agent 自行决策（用户已授权）
  - HTTP 模式：考虑添加 Bearer Token 认证

### 7.2 输入验证

- URL 归档：验证 URL 格式，拒绝内网地址
- 文本归档：限制最大长度（100,000 字符）
- 搜索查询：复用现有 AI 安全防护（Prompt 注入检测）

### 7.3 资源限制

- `top_k` 最大值：50
- `per_page` 最大值：100
- 单次归档超时：120 秒

---

## 8. 测试计划

### 8.1 单元测试

```
tests/unit/
├── test_mcp_tools.py         # Tool handler 单元测试
├── test_mcp_resources.py     # Resource handler 单元测试
└── test_mcp_prompts.py       # Prompt 模板测试
```

### 8.2 集成测试

```
tests/integration/
└── test_mcp_integration.py   # MCP 服务端到端测试
```

### 8.3 手动测试

使用 MCP Inspector 或 `mcp-client` CLI 工具进行交互测试：

```bash
# 使用 MCP Inspector
npx @modelcontextprotocol/inspector python -m src.mcp.server

# 或使用 Python client
python -m tests.manual_test_mcp
```

---

## 9. 依赖管理

### 新增依赖

```txt
# requirements.txt 新增
mcp>=1.12.0           # MCP Python SDK
```

### 无需新增的依赖

以下功能由现有依赖覆盖：
- asyncio (Python 标准库) — 异步支持
- json (Python 标准库) — 序列化

---

## 10. 实施路线

### Phase 1: 核心能力 (v0.7.0-alpha)

- [ ] 搭建 MCP Server 框架 (`src/mcp/server.py`)
- [ ] 实现 P0 Tools: `search_knowledge`, `get_entry`, `list_tags`
- [ ] 实现 P0 Resources: `pkv://entries/{id}`, `pkv://entries/{id}/metadata`
- [ ] stdio 传输支持
- [ ] 单元测试

### Phase 2: 写入能力 (v0.7.0-beta)

- [ ] 实现 P1 Tools: `archive_url`, `archive_text`, `list_entries`, `get_stats`
- [ ] 实现 P1 Resources: `pkv://tags`, `pkv://stats`
- [ ] 实现 Prompts: `search_and_summarize`, `knowledge_qa`
- [ ] streamable-http 传输支持
- [ ] 集成测试

### Phase 3: 高级能力 (v0.7.0)

- [ ] 实现 P2 Tools: `get_related`
- [ ] 实现 P2 Prompts: `idea_sharpen`
- [ ] Claude Desktop 配置文档
- [ ] 安全加固（Token 认证、输入验证）
- [ ] 性能测试和优化

---

**文档结束**

*本文档定义了 PKV MCP 服务的完整技术方案，实际开发中可根据进度调整优先级*
