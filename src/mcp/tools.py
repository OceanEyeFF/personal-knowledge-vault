"""
MCP Tool handler 实现

提供 14 个 Tool:
- 只读: search_knowledge, get_entry, list_tags, list_entries, get_stats, get_related, query_subgraph, explain_relation, collect_evidence, find_bridges, timeline_of, contrast
- 写入: archive_url, archive_text

同步/异步策略说明：
    - FastMCP 的同步 def handler 会直接在 asyncio 事件循环中调用（不同于 FastAPI）
    - 任何阻塞 I/O（SQLite/文件读取）都会冻结整个服务器
    - 只读 Tool 统一使用 async def + anyio.to_thread.run_sync() 包装同步操作
    - 写入 Tool (archive_url/archive_text) 使用 WorkflowEngine.execute_async()（原生 async，无需 threadpool）
"""

import logging
from pathlib import Path
from typing import Optional

import anyio
from mcp.types import ToolAnnotations

from src.mcp.server import (
    mcp,
    get_evidence_collection_service,
    get_exploration_service,
    get_sqlite_store,
    get_markdown_store,
    get_query_router,
    get_relation_query_service,
)
from src.mcp.utils import (
    parse_tags_string, serialize_search_result, clamp_param,
    validate_url_security, validate_text_length,
)
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

        logger.info(f"search_knowledge: query={query!r}, strategy={strategy}, top_k={top_k_safe}")

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

    result = await anyio.to_thread.run_sync(_impl)
    logger.info(f"search_knowledge: 返回 {result.get('total', 0)} 条结果")
    return result


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


# ============================================================
# Tool 6: archive_url — 归档网页 (M9 新增)
# ============================================================

@mcp.tool()
async def archive_url(url: str) -> dict:
    """归档网页 URL 到知识库。

    自动抓取网页内容，AI 生成摘要和标签，存储到 Markdown + SQLite + 向量索引。
    归档过程可能需要 10-30 秒（包含网络请求和 AI 分析）。

    Args:
        url: 要归档的网页链接（必须是 http/https，禁止内网地址）

    Returns:
        归档结果，包含 knowledge_id、标题、文件路径等
    """
    # 前置安全验证
    valid, error = validate_url_security(url)
    if not valid:
        return {"success": False, "error": error}

    try:
        logger.info(f"archive_url: 开始归档 url={url!r}")
        # WorkflowEngine() 无参构造，内部自动 get_config()
        # execute_async() 是原生 async，可直接 await，无需 threadpool
        from src.workflow.engine import WorkflowEngine
        engine = WorkflowEngine()
        result = await engine.execute_async(
            "archive-url",
            {"url": url, "skip_review": True},
        )

        if result.success:
            logger.info(f"archive_url: 归档成功 kid={result.data.get('knowledge_id', '')}, title={result.data.get('title', '')!r}")
            return {
                "success": True,
                "knowledge_id": result.data.get("knowledge_id", ""),
                "title": result.data.get("title", ""),
                "file_path": str(result.data.get("file_path", "")),
                "tags": result.data.get("tags", []),
                "abstract": result.data.get("summary_one_sentence", ""),
            }
        else:
            logger.warning(f"archive_url: 归档失败 url={url!r}, errors={result.errors}")
            return {
                "success": False,
                "error": result.errors[0] if result.errors else "归档失败",
            }
    except Exception as e:
        logger.error(f"archive_url 执行异常: {e}")
        return {"success": False, "error": f"归档异常: {e}"}


# ============================================================
# Tool 7: archive_text — 归档文本 (M9 新增)
# ============================================================

@mcp.tool()
async def archive_text(text: str, title: str = "") -> dict:
    """归档纯文本到知识库。

    将文本内容（如 AI 对话摘要、笔记、纯文本等）归档到知识库。
    先由 TextFallbackProcessor 解析文本结构，再经 AI 分析生成摘要和标签，
    最后存储到 Markdown + SQLite + 向量索引。

    Args:
        text: 要归档的文本内容（最大 100,000 字符）
        title: 可选标题（不提供则自动从文本提取）

    Returns:
        归档结果，包含 knowledge_id、标题、文件路径等
    """
    # 前置安全验证：文本长度
    valid, error = validate_text_length(text)
    if not valid:
        return {"success": False, "error": error}

    try:
        logger.info(f"archive_text: 开始归档 text_len={len(text)}, title={title!r}")
        # 步骤 1: 用 TextFallbackProcessor 解析文本，获得 Entry 对象
        from src.processors.text_fallback_processor import TextFallbackProcessor
        processor = TextFallbackProcessor()

        # TextFallbackProcessor.process() 接收文本（非 URL）
        # 如果提供了 title，在生成 Entry 后覆盖
        entry = await processor.process(text)
        if title and title.strip():
            entry.title = title.strip()

        # 步骤 2: 将 Entry 注入工作流上下文，执行 ai_analyze → store_entry
        from src.workflow.engine import WorkflowEngine
        engine = WorkflowEngine()
        result = await engine.execute_async(
            "archive-text",
            {
                "text": text,
                "title": entry.title,
                "entry": entry,
                "content": entry.content,
                "skip_review": True,
            },
        )

        if result.success:
            logger.info(f"archive_text: 归档成功 kid={result.data.get('knowledge_id', '')}, title={result.data.get('title', entry.title)!r}")
            return {
                "success": True,
                "knowledge_id": result.data.get("knowledge_id", ""),
                "title": result.data.get("title", entry.title),
                "file_path": str(result.data.get("file_path", "")),
                "tags": result.data.get("tags", entry.tags),
            }
        else:
            logger.warning(f"archive_text: 归档失败 errors={result.errors}")
            return {
                "success": False,
                "error": result.errors[0] if result.errors else "归档失败",
            }
    except Exception as e:
        logger.error(f"archive_text 执行异常: {e}")
        return {"success": False, "error": f"归档异常: {e}"}


# ============================================================
# Tool 8: get_related — 获取关联知识 (M9 新增)
# ============================================================

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def get_related(knowledge_id: str, limit: int = 5) -> dict:
    """获取与指定条目相关的知识条目。

    基于向量相似度查找关联知识。利用条目归档时生成的 embedding 向量，
    在向量索引中搜索最近邻，返回内容相似的条目列表。

    Args:
        knowledge_id: 知识条目 ID（数字字符串）
        limit: 返回结果数量，默认 5，最大 20

    Returns:
        关联条目列表，每项包含 knowledge_id, title, abstract, score
    """
    def _impl():
        try:
            kid = int(knowledge_id)
        except (ValueError, TypeError):
            return {"error": f"无效的 knowledge_id: {knowledge_id}，需要数字"}

        _limit = clamp_param(limit, 1, 20)

        # 获取条目信息（确认存在）
        store = get_sqlite_store()
        entry = store.query_by_id(kid)
        if not entry:
            return {"error": f"未找到条目: {knowledge_id}"}

        # 尝试从 VectorStore 取回该条目的 embedding
        try:
            from src.storage.vector_store import VectorStore
            config = get_config()
            vector_store = VectorStore(
                index_dir=config.vector_index_dir,
                dim=None,
            )

            doc_vector = vector_store.get_doc_vector(kid)
            if doc_vector is None:
                return {
                    "results": [],
                    "message": "该条目暂无向量索引，无法获取关联知识",
                }

            # 搜索相似文档（+1 因为需要排除自身）
            raw_results = vector_store.search_doc(doc_vector, k=_limit + 1)

            # 排除自身并获取条目信息
            results = []
            for related_kid, distance in raw_results:
                if related_kid == kid:
                    continue
                if len(results) >= _limit:
                    break
                related_entry = store.query_by_id(related_kid)
                if related_entry:
                    # cosine distance → similarity score (1 - distance)
                    score = round(max(0.0, 1.0 - distance), 4)
                    results.append({
                        "knowledge_id": related_kid,
                        "title": related_entry.get("title", ""),
                        "abstract": related_entry.get("summary_one_sentence", ""),
                        "tags": parse_tags_string(related_entry.get("tags", "")),
                        "source_type": related_entry.get("source_type", ""),
                        "score": score,
                    })

            return {
                "total": len(results),
                "results": results,
            }

        except Exception as e:
            logger.warning(f"get_related 向量搜索失败: {e}")
            return {
                "results": [],
                "message": f"向量搜索不可用: {e}",
            }

    return await anyio.to_thread.run_sync(_impl)


# ============================================================
# Tool 9: query_subgraph — 获取关系子图 (Phase B)
# ============================================================

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def query_subgraph(
    knowledge_id: str,
    depth: int = 2,
    relation_types: Optional[list[str]] = None,
    max_nodes: int = 50,
) -> dict:
    """获取指定条目周围的关系子图。

    Args:
        knowledge_id: 种子知识条目 ID（数字字符串）
        depth: 查询跳数，默认 2，最大 4
        relation_types: 可选关系类型过滤列表
        max_nodes: 最多返回节点数，默认 50，最大 200

    Returns:
        子图结果，包含 nodes、edges、grouped_edges、truncated 等字段
    """

    def _impl():
        try:
            kid = int(knowledge_id)
        except (ValueError, TypeError):
            return {"error": f"无效的 knowledge_id: {knowledge_id}，需要数字"}

        depth_safe = clamp_param(depth, 1, 4)
        max_nodes_safe = clamp_param(max_nodes, 1, 200)
        relation_query_service = get_relation_query_service()

        try:
            result = relation_query_service.query_subgraph(
                seed_knowledge_id=kid,
                depth=depth_safe,
                relation_types=relation_types or None,
                per_node_limit=max_nodes_safe,
                max_nodes=max_nodes_safe,
                max_edges=max(max_nodes_safe * 4, 20),
            )
        except ValueError as e:
            return {"error": str(e)}

        return result.to_dict()

    return await anyio.to_thread.run_sync(_impl)


# ============================================================
# Tool 10: explain_relation — 解释条目关系 (Phase B)
# ============================================================

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def explain_relation(
    source_knowledge_id: str,
    target_knowledge_id: str,
    relation_types: Optional[list[str]] = None,
    max_depth: int = 2,
) -> dict:
    """解释两个知识条目之间为何相关。

    Args:
        source_knowledge_id: 起始知识条目 ID（数字字符串）
        target_knowledge_id: 目标知识条目 ID（数字字符串）
        relation_types: 可选关系类型过滤列表
        max_depth: 最多允许的解释跳数，默认 2，最大 4

    Returns:
        关系解释结果，包含 summary、path、evidence_items 等字段
    """

    def _impl():
        try:
            source_kid = int(source_knowledge_id)
            target_kid = int(target_knowledge_id)
        except (ValueError, TypeError):
            return {
                "error": (
                    "无效的 knowledge_id，"
                    f"需要数字: source={source_knowledge_id}, target={target_knowledge_id}"
                )
            }

        max_depth_safe = clamp_param(max_depth, 1, 4)
        relation_query_service = get_relation_query_service()

        try:
            result = relation_query_service.explain_relation(
                source_knowledge_id=source_kid,
                target_knowledge_id=target_kid,
                relation_types=relation_types or None,
                max_depth=max_depth_safe,
                per_node_limit=100,
            )
        except ValueError as e:
            return {"error": str(e)}

        return result.to_dict()

    return await anyio.to_thread.run_sync(_impl)


# ============================================================
# Tool 11: collect_evidence — 聚合问题证据包 (Phase B)
# ============================================================

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def collect_evidence(
    question: str,
    top_k: int = 5,
    relation_max_depth: int = 2,
    include_chunks: bool = False,
) -> dict:
    """围绕问题聚合最小证据包。

    Args:
        question: 待回答的问题或主题
        top_k: 最多聚合的证据条目数，默认 5，最大 10
        relation_max_depth: 与种子条目解释关系时允许的最大跳数，默认 2，最大 4
        include_chunks: 是否显式返回 chunk 级证据字段，默认 False

    Returns:
        证据聚合结果，包含 seed、summary 和 evidence[] 等字段
    """

    def _impl():
        if not question or not question.strip():
            return {"error": "question 不能为空"}

        top_k_safe = clamp_param(top_k, 1, 10)
        relation_max_depth_safe = clamp_param(relation_max_depth, 1, 4)
        evidence_collection_service = get_evidence_collection_service()

        try:
            result = evidence_collection_service.collect_evidence(
                question=question,
                top_k=top_k_safe,
                relation_max_depth=relation_max_depth_safe,
                include_chunks=bool(include_chunks),
            )
        except ValueError as e:
            return {"error": str(e)}

        return result.to_dict()

    return await anyio.to_thread.run_sync(_impl)


# ============================================================
# Tool 12: find_bridges — 发现桥接节点 (Phase B partial)
# ============================================================

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def find_bridges(
    seed_knowledge_id: str,
    top_k: int = 5,
    max_depth: int = 2,
) -> dict:
    """发现 seed 周围关系子图中的桥接候选。

    注意：
        当前是 partial implementation，只基于显式关系子图和简单邻接度启发式。
        它适合作为桥接探索入口，不代表完整主题桥接发现。
    """

    def _impl():
        try:
            seed_kid = int(seed_knowledge_id)
        except (ValueError, TypeError):
            return {"error": f"无效的 seed_knowledge_id: {seed_knowledge_id}，需要数字"}

        exploration_service = get_exploration_service()
        try:
            result = exploration_service.find_bridges(
                seed_knowledge_id=seed_kid,
                top_k=clamp_param(top_k, 1, 10),
                max_depth=clamp_param(max_depth, 1, 4),
            )
        except ValueError as e:
            return {"error": str(e)}

        return result.to_dict()

    return await anyio.to_thread.run_sync(_impl)


# ============================================================
# Tool 13: timeline_of — 重建弱时间线 (Phase B partial)
# ============================================================

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def timeline_of(
    topic: str,
    top_k: int = 8,
    sort_order: str = "asc",
) -> dict:
    """按结构化时间字段重建主题的弱时间线。

    注意：
        当前是 partial implementation，只会按 event_time > published_at > archived_at
        选择结构化时间字段排序。
        它不代表正文中的完整真实事件时间，也还未接入 video_timestamps 或事件时间抽取。
    """

    def _impl():
        if not topic or not topic.strip():
            return {"error": "topic 不能为空"}

        exploration_service = get_exploration_service()
        try:
            result = exploration_service.timeline_of(
                topic=topic,
                top_k=clamp_param(top_k, 1, 20),
                sort_order=sort_order,
            )
        except ValueError as e:
            return {"error": str(e)}

        return result.to_dict()

    return await anyio.to_thread.run_sync(_impl)


# ============================================================
# Tool 14: contrast — 主题对比 (Phase B partial)
# ============================================================

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def contrast(
    topic_a: str,
    topic_b: str,
    top_k: int = 5,
) -> dict:
    """对比两个主题的检索候选表面特征。

    注意：
        当前是 partial implementation，只对比候选集、标签和摘要。
        它不代表完整语义对比，也未引入 contrast 关系类型。
    """

    def _impl():
        if not topic_a or not topic_a.strip():
            return {"error": "topic_a 不能为空"}
        if not topic_b or not topic_b.strip():
            return {"error": "topic_b 不能为空"}

        exploration_service = get_exploration_service()
        try:
            result = exploration_service.contrast(
                topic_a=topic_a,
                topic_b=topic_b,
                top_k=clamp_param(top_k, 1, 10),
            )
        except ValueError as e:
            return {"error": str(e)}

        return result.to_dict()

    return await anyio.to_thread.run_sync(_impl)
