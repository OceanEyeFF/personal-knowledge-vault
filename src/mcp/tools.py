"""
MCP Tool handler 实现

提供 5 个只读 Tool（M8），后续 M9 将补充写入 Tool。

同步/异步策略说明：
    - FastMCP 的同步 def handler 会直接在 asyncio 事件循环中调用（不同于 FastAPI）
    - 任何阻塞 I/O（SQLite/文件读取）都会冻结整个服务器
    - 所有 handler 统一使用 async def + anyio.to_thread.run_sync() 包装同步操作
"""

import logging
from pathlib import Path
from typing import Optional

import anyio
from mcp.types import ToolAnnotations

from src.mcp.server import mcp, get_sqlite_store, get_markdown_store, get_query_router
from src.mcp.utils import parse_tags_string, serialize_search_result, clamp_param
from src.utils.config import get_config

logger = logging.getLogger("pkv.mcp")


# ============================================================
# Tool 1: search_knowledge — 搜索知识库
# ============================================================

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
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
        top_k: 返回结果数量，默认 5，最大 50
        source_type: 按来源类型过滤 (wechat/zhihu/generic/chat/ai_chat/text)
        tag: 按标签过滤

    Returns:
        包含搜索结果列表的字典，每项包含 title, abstract, score, tags, source_type
    """
    def _impl():
        top_k_safe = clamp_param(top_k, 1, 50)
        config = get_config()

        # 根据 strategy 选择检索器
        if strategy == "auto":
            # 直接使用 QueryRouter.search()，内部根据分词数自动路由
            router = get_query_router()
            results = router.search(query, limit=top_k_safe)
        elif strategy == "bm25":
            from src.retrieval.bm25_retriever import BM25Retriever
            retriever = BM25Retriever(config.db_path)
            results = retriever.search(query, limit=top_k_safe)
        elif strategy == "vector":
            from src.retrieval.vector_retriever import VectorRetriever
            from src.ai.openai_client import OpenAIClient
            embedder = OpenAIClient(config)
            retriever = VectorRetriever(config.db_path, config.vector_index_dir, embedder)
            results = retriever.search(query, limit=top_k_safe)
        elif strategy == "hybrid":
            from src.retrieval.hybrid_retriever import HybridRetriever
            from src.ai.openai_client import OpenAIClient
            embedder = OpenAIClient(config)
            retriever = HybridRetriever(config.db_path, config.vector_index_dir, embedder)
            results = retriever.search(query, limit=top_k_safe)
        else:
            return {"error": f"不支持的检索策略: {strategy}，可选: auto, bm25, vector, hybrid"}

        # 后过滤：检索层不支持 source_type/tag 过滤，在结果层过滤
        if source_type:
            results = [r for r in results if r.metadata.get("source_type") == source_type]
        if tag:
            results = [
                r for r in results
                if tag in parse_tags_string(r.metadata.get("tags", ""))
            ]

        return {
            "total": len(results),
            "strategy_used": strategy,
            "results": [serialize_search_result(r) for r in results],
        }

    return await anyio.to_thread.run_sync(_impl)


# ============================================================
# Tool 2: get_entry — 获取条目详情
# ============================================================

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def get_entry(knowledge_id: str) -> dict:
    """获取知识条目完整内容。

    Args:
        knowledge_id: 知识条目 ID（数字字符串）

    Returns:
        包含标题、摘要、标签、全文内容等完整信息的字典
    """
    def _impl():
        try:
            kid = int(knowledge_id)
        except (ValueError, TypeError):
            return {"error": f"无效的 knowledge_id: {knowledge_id}，需要数字"}

        store = get_sqlite_store()
        entry = store.query_by_id(kid)
        if not entry:
            return {"error": f"未找到条目: {knowledge_id}"}

        # 读取 Markdown 全文
        content = ""
        file_path_str = entry.get("file_path", "")
        if file_path_str:
            try:
                md_store = get_markdown_store()
                # MarkdownStore.load() 接收 Path 对象
                loaded_entry = md_store.load(Path(file_path_str))
                content = loaded_entry.content if loaded_entry else ""
            except FileNotFoundError:
                logger.warning(f"Markdown 文件不存在: {file_path_str}")
                content = "(文件不存在)"
            except Exception as e:
                logger.error(f"读取 Markdown 失败: {e}")
                content = "(读取失败)"

        return {
            "knowledge_id": entry["knowledge_id"],
            "title": entry.get("title", ""),
            "abstract": entry.get("summary_one_sentence", ""),  # DB 无 abstract 列
            "summary_one_sentence": entry.get("summary_one_sentence", ""),
            "summary_100_words": entry.get("summary_100_words", ""),
            "tags": parse_tags_string(entry.get("tags", "")),
            "keywords": parse_tags_string(entry.get("keywords", "")),
            "source_type": entry.get("source_type", ""),
            "source_url": entry.get("source_url", ""),
            "archived_at": entry.get("archived_at", ""),
            "word_count": entry.get("word_count", 0),
            "content": content or "(内容不可用)",
        }

    return await anyio.to_thread.run_sync(_impl)


# ============================================================
# Tool 3: list_tags — 列出标签
# ============================================================

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def list_tags() -> dict:
    """列出知识库所有标签及统计。

    Returns:
        标签列表，每项包含标签名和关联条目数
    """
    def _impl():
        store = get_sqlite_store()
        tags = store.get_all_tags_with_count()
        return {
            "total_tags": len(tags),
            "tags": [{"name": t["name"], "count": t["count"]} for t in tags],
        }

    return await anyio.to_thread.run_sync(_impl)


# ============================================================
# Tool 4: list_entries — 浏览条目列表
# ============================================================

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
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
        page: 页码，从 1 开始
        per_page: 每页数量，默认 20，最大 100
        sort_by: 排序字段 - "archived_at", "title", "word_count", "knowledge_id", "source_type"
        sort_order: 排序方向 - "asc" 或 "desc"
        source_type: 按来源类型过滤
        tag: 按标签过滤

    Returns:
        分页的条目列表，包含总数和分页信息
    """
    def _impl():
        store = get_sqlite_store()
        _per_page = clamp_param(per_page, 1, 100)
        _page = max(1, page)
        offset = (_page - 1) * _per_page

        try:
            entries = store.list_entries(
                limit=_per_page,
                offset=offset,
                sort_by=sort_by,
                sort_order=sort_order,
                source_type=source_type if source_type else None,
                tag=tag if tag else None,
            )
        except ValueError as e:
            return {"error": str(e)}

        total = store.count_entries(
            source_type=source_type if source_type else None,
            tag=tag if tag else None,
        )

        return {
            "total": total,
            "page": _page,
            "per_page": _per_page,
            "total_pages": (total + _per_page - 1) // _per_page if total > 0 else 0,
            "entries": [
                {
                    "knowledge_id": e["knowledge_id"],
                    "title": e.get("title", ""),
                    "abstract": e.get("summary_one_sentence", ""),
                    "tags": parse_tags_string(e.get("tags", "")),
                    "source_type": e.get("source_type", ""),
                    "word_count": e.get("word_count", 0),
                    "archived_at": e.get("archived_at", ""),
                }
                for e in entries
            ],
        }

    return await anyio.to_thread.run_sync(_impl)


# ============================================================
# Tool 5: get_stats — 知识库统计
# ============================================================

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def get_stats() -> dict:
    """获取知识库统计信息。

    Returns:
        包含条目总数、来源类型分布、标签统计等综合数据
    """
    def _impl():
        store = get_sqlite_store()
        return store.get_statistics()

    return await anyio.to_thread.run_sync(_impl)
