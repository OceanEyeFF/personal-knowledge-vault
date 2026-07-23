# MCP 服务技术设计文档

> Personal Knowledge Vault - Model Context Protocol 服务端设计
>
> **文档版本**: v1.1
> **创建日期**: 2026-02-16
> **最后更新**: 2026-03-31 (补充当前代码基线的推理型 MCP Tool 状态说明)
> **作者**: 幽浮喵 (猫娘工程师)
> **目标版本**: v0.7.0

> **当前代码补注（2026-03-31）**：
> - 本文主体仍保留 `v0.7.0` 设计稿结构；当前代码真相以 `README.md`、`docs/overview/当前事实基线-2026-03.md` 与 `src/mcp/tools.py` 为准。
> - 当前 MCP 代码基线已扩展为 `14` 个 Tool，其中 `query_subgraph`、`explain_relation`、`collect_evidence`、`find_bridges`、`timeline_of`、`contrast` 已落地。
> - `find_bridges`、`timeline_of`、`contrast` 仍属于 `partial`：当前分别补入 `graph_bridge_signal`、`structured_time_fields`、`relation_graph_signal` 等受限推理信号。

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
│  │  • FastMCP 实例                          │   │
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
├── server.py            # FastMCP 主入口
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
# FastMCP 是官方 MCP Python SDK 中的高级封装，正确导入方式：
# - 官方 SDK (mcp>=1.0): from mcp.server.fastmcp import FastMCP
# - 独立 FastMCP 库 (fastmcp>=2.0): from fastmcp import FastMCP
# 两者 API 兼容，优先使用官方 SDK 中的 FastMCP
from mcp.server.fastmcp import FastMCP

mcp = FastMCP(
    name="Personal Knowledge Vault",
    instructions=(
        "个人知识库 MCP 服务。支持知识搜索、归档、浏览和统计。\n"
        "可用工具：search_knowledge（搜索）、get_entry（查看详情）、"
        "archive_url（归档URL）、archive_text（归档文本）等。\n"
        "知识条目包含标题、摘要、标签、全文等信息。"
    ),
)

# Tool/Resource/Prompt 注册：使用 @mcp.tool() / @mcp.resource() / @mcp.prompt() 装饰器
# 实际注册代码分散在各子模块，通过导入副作用完成注册
from src.mcp import tools, resources, prompts  # noqa: F401


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

    if args.transport == "stdio":
        mcp.run(transport="stdio")
    else:
        mcp.run(
            transport="streamable-http",
            port=args.port,
        )


if __name__ == "__main__":
    main()
```

### 4.2 Tool 实现

```python
# src/mcp/tools.py
#
# 同步/异步策略说明（重要！）：
#   - FastMCP 的同步 def handler 会【直接在 asyncio 事件循环中调用】，不会自动放入 threadpool
#   - 这与 FastAPI 不同：FastAPI 会用 run_in_threadpool() 包装同步函数，FastMCP 不会
#   - 若在同步 handler 中执行 SQLite/文件/网络等阻塞操作，会【冻结整个事件循环】
#   - 正确做法：统一使用 async def + anyio.to_thread.run_sync() 包装现有同步 API
#
# 调用模式：
#   async def my_tool(...) -> dict:
#       result = await anyio.to_thread.run_sync(existing_sync_function, arg1, arg2)
#       return result
#
import anyio
from typing import Optional

from src.mcp.server import mcp  # 从 server 模块获取共享的 FastMCP 实例
from src.retrieval import QueryRouter, BM25Retriever, VectorRetriever, HybridRetriever
from src.retrieval.result import SearchResult
from src.storage.sqlite_store import SQLiteStore
from src.storage.markdown_store import MarkdownStore
from src.workflow.engine import WorkflowEngine
from src.ai.openai_client import OpenAIClient
from src.utils.config import get_config

config = get_config()

# ============================================================
# 服务对象单例管理（⚠️ 关键架构决策）
#
# 以下对象在模块加载时延迟初始化，整个 Server 生命周期内复用：
# - SQLiteStore：数据库连接池复用
# - QueryRouter：内部含 BM25Retriever + HybridRetriever + VectorStore（hnswlib 索引加载耗时）
# - MarkdownStore：文件系统操作，无状态但避免重复创建
#
# 为什么不能每次请求重建？
# - VectorRetriever 需要加载 hnswlib 索引文件到内存，首次加载约 1-3s
# - QueryRouter 内部创建 BM25Retriever + HybridRetriever，重复创建浪费资源
# - OpenAIClient（Embedder）内部维护 HTTP 连接池，复用可减少连接开销
# ============================================================

_sqlite_store: Optional[SQLiteStore] = None
_markdown_store: Optional[MarkdownStore] = None
_query_router: Optional[QueryRouter] = None


def _get_sqlite_store() -> SQLiteStore:
    """获取 SQLiteStore 单例"""
    global _sqlite_store
    if _sqlite_store is None:
        _sqlite_store = SQLiteStore(config.db_path)
    return _sqlite_store


def _get_markdown_store() -> MarkdownStore:
    """获取 MarkdownStore 单例"""
    global _markdown_store
    if _markdown_store is None:
        _markdown_store = MarkdownStore(config.vault_dir)
    return _markdown_store


def _get_query_router() -> QueryRouter:
    """获取 QueryRouter 单例（内含 BM25 + HybridRetriever + VectorStore）"""
    global _query_router
    if _query_router is None:
        embedder = OpenAIClient(config)
        _query_router = QueryRouter(
            db_path=config.db_path,
            vector_index_dir=config.vector_index_dir,
            embedder=embedder,
        )
    return _query_router


def _parse_tags_string(tags_str: str) -> List[str]:
    """将 SQLite 中逗号分隔的 tags 字符串转换为列表。

    数据库中 tags 以 ','.join(tags) 方式存储（如 "AI,知识管理"），
    SearchResult.metadata["tags"] 返回的是字符串，需转换为列表。
    """
    if not tags_str:
        return []
    if isinstance(tags_str, list):
        return tags_str
    return [t.strip() for t in tags_str.split(",") if t.strip()]


def _do_search_knowledge(
    query: str, strategy: str, top_k: int,
    source_type: Optional[str], tag: Optional[str],
) -> dict:
    """同步搜索实现，由 anyio.to_thread.run_sync 在 threadpool 中执行。

    接口对齐说明：
    - QueryRouter.search() 不支持外部传入 strategy，内部根据分词数自动路由
    - 若用户指定 strategy != "auto"，则绕过 QueryRouter 直接实例化对应 Retriever
    - SearchResult 字段：knowledge_id, title, score, highlight, metadata
      其中 source_type/tags/file_path 等在 metadata dict 中
    """
    router = _get_query_router()

    if strategy == "auto":
        # 直接使用 QueryRouter.search()，内部自动路由 BM25/Hybrid
        results = router.search(query, limit=top_k)
    elif strategy == "bm25":
        retriever = BM25Retriever(config.db_path)
        results = retriever.search(query, limit=top_k)
    elif strategy == "vector":
        embedder = OpenAIClient(config)
        retriever = VectorRetriever(config.db_path, config.vector_index_dir, embedder)
        results = retriever.search(query, limit=top_k)
    else:  # hybrid
        embedder = OpenAIClient(config)
        retriever = HybridRetriever(config.db_path, config.vector_index_dir, embedder)
        results = retriever.search(query, limit=top_k)

    # 后过滤：检索层不支持 source_type/tag 过滤，在结果层过滤
    # 注意：tags 在 metadata 中是逗号分隔字符串，需 _parse_tags_string() 转换
    if source_type:
        results = [r for r in results if r.metadata.get("source_type") == source_type]
    if tag:
        results = [r for r in results if tag in _parse_tags_string(r.metadata.get("tags", ""))]

    return {
        "total": len(results),
        "strategy_used": strategy,
        "results": [
            {
                "knowledge_id": r.knowledge_id,
                "title": r.title,
                "abstract": r.highlight,  # SearchResult.highlight 是摘要/snippet
                "score": round(r.score, 4),
                "tags": _parse_tags_string(r.metadata.get("tags", "")),
                "source_type": r.metadata.get("source_type", ""),
                "archived_at": r.metadata.get("archived_at", ""),
            }
            for r in results
        ],
    }


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
    # anyio.to_thread.run_sync 在独立线程中执行阻塞操作，不阻塞事件循环
    return await anyio.to_thread.run_sync(
        lambda: _do_search_knowledge(query, strategy, top_k, source_type, tag)
    )


@mcp.tool()
async def get_entry(knowledge_id: str) -> dict:
    """获取知识条目完整内容。

    Args:
        knowledge_id: 知识条目 ID

    Returns:
        包含标题、摘要、标签、全文内容等完整信息的字典
    """
    def _fetch():
        store = _get_sqlite_store()
        # knowledge_id 在数据库中是 INTEGER，MCP 层接收 str，需转换
        entry = store.query_by_id(int(knowledge_id))
        if not entry:
            return {"error": f"未找到条目: {knowledge_id}"}
        md_store = _get_markdown_store()
        loaded_entry = md_store.load(entry.get("file_path", ""))
        content = loaded_entry.content if loaded_entry else "(content unavailable)"
        # 注意：DB 中无 abstract 列，用 summary_one_sentence 代替
        # 注意：DB 中 tags/keywords 是逗号分隔字符串，需转换为列表
        return {
            "knowledge_id": entry["knowledge_id"],
            "title": entry["title"],
            "abstract": entry.get("summary_one_sentence", ""),
            "summary_one_sentence": entry.get("summary_one_sentence", ""),
            "summary_100_words": entry.get("summary_100_words", ""),
            "tags": _parse_tags_string(entry.get("tags", "")),
            "keywords": _parse_tags_string(entry.get("keywords", "")),
            "source_type": entry.get("source_type", ""),
            "source_url": entry.get("source_url", ""),
            "archived_at": entry.get("archived_at", ""),
            "word_count": entry.get("word_count", 0),
            "content": content if content else "(内容不可用)",
        }
    return await anyio.to_thread.run_sync(_fetch)


@mcp.tool()
async def list_tags() -> dict:
    """列出知识库所有标签及统计。

    Returns:
        标签列表，每项包含标签名和关联条目数
    """
    def _fetch():
        store = _get_sqlite_store()
        tags = store.get_all_tags_with_count()
        return {
            "total_tags": len(tags),
            "tags": [{"name": t["name"], "count": t["count"]} for t in tags],
        }
    return await anyio.to_thread.run_sync(_fetch)


@mcp.tool()
async def list_entries(
    page: int = 1,
    per_page: int = 20,
    sort_by: str = "archived_at",
    sort_order: str = "desc",
    source_type: Optional[str] = None,
    tag: Optional[str] = None,
) -> dict:
    """浏览知识条目列表。

    Args:
        page: 页码 (从 1 开始)
        per_page: 每页数量 (默认 20，最大 100)
        sort_by: 排序字段 - "archived_at", "title", "word_count"
        sort_order: 排序方向 - "asc" 或 "desc"
        source_type: 按来源类型过滤
        tag: 按标签过滤

    Returns:
        分页的条目列表
    """
    def _fetch():
        store = _get_sqlite_store()
        _per_page = min(per_page, 100)
        offset = (page - 1) * _per_page
        entries = store.list_entries(
            limit=_per_page, offset=offset,
            sort_by=sort_by, sort_order=sort_order,
            source_type=source_type, tag=tag,
        )
        total = store.count_entries(source_type=source_type, tag=tag)
        return {
            "total": total, "page": page, "per_page": _per_page,
            "total_pages": (total + _per_page - 1) // _per_page,
            "entries": [
                {
                    "knowledge_id": e["knowledge_id"],
                    "title": e["title"],
                    "abstract": e.get("summary_one_sentence", ""),  # DB 无 abstract 列
                    "tags": _parse_tags_string(e.get("tags", "")),  # DB 中是逗号字符串
                    "source_type": e.get("source_type", ""),
                    "word_count": e.get("word_count", 0),
                    "archived_at": e.get("archived_at", ""),
                }
                for e in entries
            ],
        }
    return await anyio.to_thread.run_sync(_fetch)


@mcp.tool()
async def archive_url(url: str) -> dict:
    """归档网页 URL 到知识库。

    Args:
        url: 要归档的网页链接

    Returns:
        归档结果，包含生成的 knowledge_id 和文件路径

    Note:
        WorkflowEngine.execute_async() 是原生 async 方法，内部步骤（网络请求/AI 调用）
        均为 async def，可直接 await，无需 threadpool 包装。
    """
    # WorkflowEngine() 无参构造，内部自动调用 get_config() 加载配置
    engine = WorkflowEngine()
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
        return {"success": False, "error": (result.errors[0] if result.errors else "归档失败")}


@mcp.tool()
async def archive_text(text: str, title: str = "") -> dict:
    """归档纯文本到知识库。

    Args:
        text: 要归档的文本内容
        title: 可选的标题（不提供则自动生成）

    Returns:
        归档结果，包含生成的 knowledge_id 和文件路径
    """
    engine = WorkflowEngine()
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
        return {"success": False, "error": (result.errors[0] if result.errors else "归档失败")}


@mcp.tool()
async def get_stats() -> dict:
    """获取知识库统计信息。

    Returns:
        包含条目总数、标签分布、来源类型分布等统计数据
    """
    def _fetch():
        store = _get_sqlite_store()
        return store.get_statistics()
    return await anyio.to_thread.run_sync(_fetch)
```

### 4.3 Resource 实现

```python
# src/mcp/resources.py
# Resource handler 与 Tool 策略一致：async def + anyio.to_thread.run_sync() 包装阻塞 I/O
# 原因：FastMCP 的同步 def handler 会直接阻塞 asyncio 事件循环（不会自动 threadpool 化）
#
# 注意：Resource handler 通过 tools.py 中的单例访问函数获取服务对象，
# 不重复创建 SQLiteStore / MarkdownStore 实例。

import json
import anyio

from src.mcp.server import mcp  # 共享 FastMCP 实例
from src.mcp.tools import _get_sqlite_store, _get_markdown_store, _parse_tags_string


@mcp.resource("pkv://entries/{knowledge_id}")
async def get_entry_content(knowledge_id: str) -> str:
    """获取知识条目的 Markdown 全文"""
    def _fetch():
        store = _get_sqlite_store()
        entry = store.query_by_id(int(knowledge_id))
        if not entry:
            return f"# 未找到条目\n\nknowledge_id: {knowledge_id}"
        md_store = _get_markdown_store()
        loaded_entry = md_store.load(entry.get("file_path", ""))
        if loaded_entry:
            return loaded_entry.content or f"# {entry.get('title', '无标题')}\n\n(内容不可用)"
        return f"# {entry.get('title', '无标题')}\n\n(内容不可用)"
    return await anyio.to_thread.run_sync(_fetch)


@mcp.resource("pkv://entries/{knowledge_id}/metadata")
async def get_entry_metadata(knowledge_id: str) -> str:
    """获取知识条目的元数据（JSON 格式）"""
    def _fetch():
        store = _get_sqlite_store()
        entry = store.query_by_id(int(knowledge_id))
        if not entry:
            return json.dumps({"error": f"未找到条目: {knowledge_id}"})
        # 转换 tags/keywords 为列表后再序列化
        entry_dict = dict(entry)
        entry_dict["tags"] = _parse_tags_string(entry_dict.get("tags", ""))
        entry_dict["keywords"] = _parse_tags_string(entry_dict.get("keywords", ""))
        return json.dumps(entry_dict, ensure_ascii=False, indent=2, default=str)
    return await anyio.to_thread.run_sync(_fetch)


@mcp.resource("pkv://tags")
async def get_tags_resource() -> str:
    """获取所有标签列表（Resource 版，返回 JSON 字符串）"""
    def _fetch():
        store = _get_sqlite_store()
        tags = store.get_all_tags_with_count()
        return json.dumps({"tags": tags}, ensure_ascii=False, indent=2)
    return await anyio.to_thread.run_sync(_fetch)


@mcp.resource("pkv://stats")
async def get_stats_resource() -> str:
    """获取知识库统计信息（Resource 版，返回 JSON 字符串）"""
    def _fetch():
        store = _get_sqlite_store()
        return json.dumps(store.get_statistics(), ensure_ascii=False, indent=2, default=str)
    return await anyio.to_thread.run_sync(_fetch)
```

### 4.4 Prompt 模板

```python
# src/mcp/prompts.py
from src.mcp.server import mcp  # 共享 FastMCP 实例


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

Windows 客户端通过 `run-windows.ps1` 固定使用 `py311-private`。Provider 配置由该工作目录下 Git 忽略的 `config/local.yaml` 提供，不放入 MCP 客户端 JSON。

### 5.2 HTTP 方式（远程访问）

```powershell
# 先按“HTTP 认证”一节无回显注入令牌，再启动服务
.\scripts\run-windows.ps1 python -m src.mcp.server --transport streamable-http --port 3000
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
| `search_knowledge` | `src/retrieval/` | `strategy="auto"` 时调用 `QueryRouter.search(query, limit)`（内部自动路由）；指定具体策略时直接实例化 `BM25Retriever` / `VectorRetriever` / `HybridRetriever` |
| `get_entry` | `src/storage/sqlite_store.py` + `markdown_store.py` | `SQLiteStore.query_by_id(int(knowledge_id))` + `MarkdownStore.load()` |
| `list_tags` | `src/storage/sqlite_store.py` | `SQLiteStore.get_all_tags_with_count()` （需新增） |
| `list_entries` | `src/storage/sqlite_store.py` | `SQLiteStore.list_entries()` + `SQLiteStore.count_entries()` （均需新增） |
| `archive_url` | `src/workflow/engine.py` | `await WorkflowEngine().execute_async("archive-url", {...})` （构造器无参，无需 threadpool） |
| `archive_text` | `src/workflow/engine.py` | `await WorkflowEngine().execute_async("archive-text", {...})` （构造器无参，无需 threadpool） |
| `get_stats` | `src/storage/sqlite_store.py` | `SQLiteStore.get_statistics()` （需新增） |

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
  - HTTP 模式：**必须**启用 Bearer Token 认证（见 7.4）

### 7.2 输入验证

- URL 归档：验证 URL 格式，拒绝内网地址（`127.*`, `10.*`, `192.168.*`, `172.16-31.*`）
- 文本归档：限制最大长度（100,000 字符）
- 搜索查询：复用现有 AI 安全防护（Prompt 注入检测）

### 7.3 资源限制

- `top_k` 最大值：50
- `per_page` 最大值：100
- 单次归档超时：120 秒

### 7.4 HTTP 传输认证方案（M9 实现）

> ⚠️ streamable-http 模式直接暴露在网络上，**无认证 = 任何人都能操作你的知识库**。

**实现方案**：基于环境变量的 Bearer Token 认证

```python
# src/mcp/utils.py — HTTP 认证中间件

import os
from functools import wraps

# 从环境变量读取 Token（不硬编码）
MCP_AUTH_TOKEN = os.environ.get("PKV_MCP_AUTH_TOKEN", "")


def validate_http_auth(request_headers: dict) -> bool:
    """验证 HTTP 请求的 Bearer Token"""
    if not MCP_AUTH_TOKEN:
        # 未配置 Token 时拒绝所有 HTTP 请求（安全默认）
        return False
    auth_header = request_headers.get("Authorization", "")
    return auth_header == f"Bearer {MCP_AUTH_TOKEN}"
```

**配置方式**：

```powershell
# HTTP 模式必需；无回显读取，令牌不会写入命令历史。
$secureToken = Read-Host "PKV_MCP_AUTH_TOKEN" -AsSecureString
$env:PKV_MCP_AUTH_TOKEN = [Net.NetworkCredential]::new("", $secureToken).Password
Remove-Variable secureToken

# 启动 HTTP 服务
.\scripts\run-windows.ps1 python -m src.mcp.server --transport streamable-http --port 3000
```

**客户端配置**：

```json
{
  "mcpServers": {
    "personal-knowledge-vault": {
      "url": "http://localhost:3000/mcp",
      "headers": {
        "Authorization": "Bearer <由客户端秘密存储注入>"
      }
    }
  }
}
```

令牌应由客户端的秘密存储或未纳入版本控制的本机配置注入；不要把实际值写入命令、截图或可提交的 JSON。

**安全默认原则**：
- 未设置 `PKV_MCP_AUTH_TOKEN` 时，HTTP 模式**拒绝所有请求**
- stdio 模式**不做认证**（进程由用户本地启动，天然安全）

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

```powershell
# 使用 MCP Inspector
npx @modelcontextprotocol/inspector powershell.exe -ExecutionPolicy Bypass -Command "& '.\scripts\run-test.ps1' -DataRoot '.data-test\mcp-inspector' -Direct -Command @('python','-m','src.mcp.server')"

# 或使用 Python client
.\scripts\run-test.ps1 -DataRoot .data-test\mcp-manual -Direct -Command @("python", "-m", "tests.manual_test_mcp")
```

---

## 9. 依赖管理

### 新增依赖

```txt
# requirements.txt 新增
mcp[cli]>=1.6.0       # MCP Python SDK（含 FastMCP 和 CLI 调试工具），2026-02 最新 v1.26.0
                      # 正确导入：from mcp.server.fastmcp import FastMCP
                      # Tool 注解：from mcp.types import ToolAnnotations（注意参数驼峰：readOnlyHint）
anyio>=4.0.0          # 异步 I/O 工具库（FastMCP 自身已依赖，通常无需单独安装）
                      # 用于 anyio.to_thread.run_sync() 将阻塞操作放入 threadpool
```

### 同步/异步策略说明（⚠️ 关键）

本项目 MCP Tool/Resource handler **统一使用 `async def` + `anyio.to_thread.run_sync()`**：

| 方式 | 行为 | 是否推荐 |
|------|------|---------|
| 同步 `def` handler（直接调用阻塞操作） | ⚠️ **直接阻塞 asyncio 事件循环**，冻结整个服务器 | ❌ 禁止 |
| `async def` + `await anyio.to_thread.run_sync(fn)` | ✅ 在 threadpool 执行，不阻塞事件循环 | ✅ 推荐 |
| `async def` + 原生 async I/O（aiosqlite 等） | ✅ 最高效，适合未来异步化改造 | ✅ 长期目标 |

**重要原因**：FastMCP 与 FastAPI 不同——FastAPI 会自动用 `run_in_threadpool()` 包装同步函数，
而 FastMCP 的同步 handler **直接在事件循环中调用**，绝对不能有阻塞操作。

### 无需新增的依赖

以下功能由现有依赖覆盖：
- asyncio (Python 标准库) — MCP Server 内部事件循环（由 SDK 管理，无需手动处理）
- json (Python 标准库) — 序列化

---

## 10. 实施路线

### M8: 只读服务 (v0.7.0-alpha)

- [ ] 搭建 MCP Server 框架 (`src/mcp/server.py`)
- [ ] 实现 P0 Tools: `search_knowledge`, `get_entry`, `list_tags`
- [ ] 实现 P1 Tools（只读）: `list_entries`, `get_stats`
- [ ] 实现 P0 Resources: `pkv://entries/{id}`, `pkv://entries/{id}/metadata`
- [ ] 实现 P1 Resources: `pkv://tags`, `pkv://stats`
- [ ] stdio 传输支持
- [ ] 单元测试

### M9: 写入 + Prompts (v0.7.0)

- [ ] 实现 P1 Tools（写入）: `archive_url`, `archive_text`
- [ ] 实现 Prompts: `search_and_summarize`, `knowledge_qa`
- [ ] 实现 P2 Tools: `get_related`
- [ ] 实现 P2 Prompts: `idea_sharpen`
- [ ] streamable-http 传输支持
- [ ] Claude Desktop / Cursor 配置文档
- [ ] 安全加固（输入验证、长度限制）
- [ ] 集成测试

---

## 11. 运维与维护

### 11.1 日志策略

MCP Server 复用项目现有的 `src/utils/logger.py` 日志基础设施：

| 传输模式 | stdout | stderr | 日志文件 |
|---------|--------|--------|---------|
| **stdio** | ❌ 被 MCP 协议占用 | ✅ 可输出日志（客户端可捕获） | ✅ `.data/logs/pkv-mcp.log` |
| **HTTP** | ✅ 可输出日志 | ✅ 可输出日志 | ✅ `.data/logs/pkv-mcp.log` |

**关键约束**：stdio 模式下 **stdout 是 MCP 协议通道**，绝对不能 `print()`，只能用 `logger` 写到 stderr 或文件。

```python
# server.py 中的日志初始化
import logging
logger = logging.getLogger("pkv.mcp")
# stdio 模式：日志输出到 stderr + 文件
# HTTP 模式：日志输出到 stdout + 文件
```

### 11.2 进程管理

**stdio 模式**（由客户端管理生命周期）：
- Claude Code / Cursor 按需启动/停止 MCP Server 进程
- 进程崩溃后客户端自动重启（取决于客户端实现）
- **无需额外的进程管理工具**

**HTTP 模式**（需手动管理）：
```bash
# 前台运行（开发/调试）
python -m src.mcp.server --transport streamable-http --port 3000

# 后台运行（生产）
nohup python -m src.mcp.server --transport streamable-http --port 3000 &

# 健康检查（可选，M9+ 实现）
# GET http://localhost:3000/health → {"status": "ok"}
```

### 11.3 常见错误排查

| 现象 | 原因 | 解决方式 |
|------|------|---------|
| Claude Code 无法发现 Tool | MCP Server 未启动或配置错误 | 检查 `claude_desktop_config.json` 中的 `cwd` 和 `command` |
| Tool 调用返回空结果 | 数据库为空或路径错误 | 检查 YAML 的 `storage.db_path` 或进程级 `DB_PATH` 覆盖 |
| 归档超时 | 网络不通或 AI API 超时 | 检查 `config/local.yaml` 的 `ai.llm.*` / `ai.embedding.*` |
| HTTP 模式 401 | Token 未配置或不匹配 | 检查 `PKV_MCP_AUTH_TOKEN` 环境变量 |
| "冻结"无响应 | 同步阻塞了事件循环 | 检查所有 handler 是否使用了 `async def` + `anyio.to_thread.run_sync()` |

### 11.4 MCP SDK 升级路径

当前锁定 `mcp[cli]>=1.6.0`，未来升级注意：
- `mcp` SDK 遵循语义化版本，次版本升级（1.x → 1.y）向后兼容
- 主版本升级（1.x → 2.x）可能有破坏性变更，需检查：
  - `FastMCP` 的导入路径是否变化
  - `@mcp.tool()` / `@mcp.resource()` 装饰器签名
  - `ToolAnnotations` 参数命名（驼峰 vs 蛇形）
- 独立 `fastmcp>=2.0.0` 库与 SDK 内置 FastMCP 的差异需关注

---

## 12. 扩展指南

### 12.1 添加新 Tool 的步骤

```python
# 1. 在 src/mcp/tools.py 中添加新 Tool
from mcp.types import ToolAnnotations  # 可选：添加注解

@mcp.tool(annotations=ToolAnnotations(title="我的新工具", readOnlyHint=True))
async def my_new_tool(param1: str, param2: int = 10) -> dict:
    """工具描述（会显示在 MCP Inspector 和客户端中）。

    Args:
        param1: 参数说明
        param2: 参数说明
    """
    def _impl():
        store = _get_sqlite_store()
        # ... 业务逻辑 ...
        return {"result": "..."}
    return await anyio.to_thread.run_sync(_impl)

# 2. 添加对应测试
# tests/unit/test_mcp_tools.py 中添加测试用例

# 3. 在隔离数据目录中使用 MCP Inspector 验证
# npx @modelcontextprotocol/inspector powershell.exe -ExecutionPolicy Bypass -Command "& '.\scripts\run-test.ps1' -DataRoot '.data-test\mcp-inspector' -Direct -Command @('python','-m','src.mcp.server')"
```

**命名约定**：
- Tool 名称使用 `snake_case`
- 只读 Tool 添加 `readOnlyHint=True` 注解
- 同步业务逻辑放在 `_do_xxx` 或 `_impl` 内部函数中

### 12.2 添加新 Resource 的步骤

```python
# 在 src/mcp/resources.py 中添加
@mcp.resource("pkv://my-resource/{param}")
async def get_my_resource(param: str) -> str:
    """资源描述"""
    def _fetch():
        # Resource 返回 str（文本），不是 dict
        return json.dumps({"data": "..."}, ensure_ascii=False)
    return await anyio.to_thread.run_sync(_fetch)
```

**Resource vs Tool 选择原则**：
- **Resource**：静态/准静态数据，客户端可缓存（如标签列表、统计信息）
- **Tool**：需要参数、有副作用或结果动态变化的操作（如搜索、归档）

### 12.3 未来扩展方向（不阻塞 M8/M9）

| 扩展点 | 优先级 | 依赖 |
|--------|--------|------|
| `pkv://config` Resource（系统配置信息） | P2 | 无 |
| SSE 传输方式 | P3 | FastMCP 已支持 |
| 多用户 / 权限控制 | P3+ | 需架构评估 |
| GUI 内嵌 MCP Client | Phase 2B | GUI 框架就绪 |
| 知识图谱关联 Tool | Phase 3 | 知识图谱功能就绪 |

---

**文档结束**

*本文档定义了 PKV MCP 服务的完整技术方案，v1.1 修复了接口不匹配问题并补充了运维/扩展设计*
