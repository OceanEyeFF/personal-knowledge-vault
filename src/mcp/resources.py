"""
MCP Resource handler 实现

提供 4 个 Resource（M8），供 AI Agent 读取知识条目内容和元数据。

Resource vs Tool 选择原则：
    - Resource：静态/准静态数据，客户端可缓存（如标签列表、统计信息）
    - Tool：需要参数、有副作用或结果动态变化的操作（如搜索、归档）
    - Resource 返回 str（文本），Tool 返回 dict（结构化数据），两者互补
"""

import json
import logging
from pathlib import Path

import anyio

from src.mcp.server import mcp, get_sqlite_store, get_markdown_store
from src.mcp.utils import parse_tags_string

logger = logging.getLogger("pkv.mcp")


# ============================================================
# Resource 1: pkv://entries/{knowledge_id} — 条目全文
# ============================================================

@mcp.resource("pkv://entries/{knowledge_id}")
async def get_entry_content(knowledge_id: str) -> str:
    """获取知识条目的 Markdown 全文。

    Args:
        knowledge_id: 知识条目 ID

    Returns:
        Markdown 格式的全文内容
    """
    def _impl():
        try:
            kid = int(knowledge_id)
        except (ValueError, TypeError):
            return f"# 错误\n\n无效的 knowledge_id: {knowledge_id}"

        store = get_sqlite_store()
        entry = store.query_by_id(kid)
        if not entry:
            return f"# 未找到条目\n\nknowledge_id: {knowledge_id}"

        file_path_str = entry.get("file_path", "")
        if not file_path_str:
            return f"# {entry.get('title', '无标题')}\n\n(文件路径缺失)"

        try:
            md_store = get_markdown_store()
            loaded_entry = md_store.load(Path(file_path_str))
            if loaded_entry and loaded_entry.content:
                return loaded_entry.content
            return f"# {entry.get('title', '无标题')}\n\n(内容不可用)"
        except FileNotFoundError:
            return f"# {entry.get('title', '无标题')}\n\n(Markdown 文件不存在)"
        except Exception as e:
            logger.error(f"读取 Resource pkv://entries/{knowledge_id} 失败: {e}")
            return f"# {entry.get('title', '无标题')}\n\n(读取失败: {e})"

    return await anyio.to_thread.run_sync(_impl)


# ============================================================
# Resource 2: pkv://entries/{knowledge_id}/metadata — 条目元数据
# ============================================================

@mcp.resource("pkv://entries/{knowledge_id}/metadata")
async def get_entry_metadata(knowledge_id: str) -> str:
    """获取知识条目的元数据（JSON 格式）。

    Args:
        knowledge_id: 知识条目 ID

    Returns:
        JSON 格式的元数据字符串
    """
    def _impl():
        try:
            kid = int(knowledge_id)
        except (ValueError, TypeError):
            return json.dumps(
                {"error": f"无效的 knowledge_id: {knowledge_id}"},
                ensure_ascii=False,
            )

        store = get_sqlite_store()
        entry = store.query_by_id(kid)
        if not entry:
            return json.dumps(
                {"error": f"未找到条目: {knowledge_id}"},
                ensure_ascii=False,
            )

        # 转换 tags/keywords 为列表后再序列化
        entry_dict = dict(entry)
        entry_dict["tags"] = parse_tags_string(entry_dict.get("tags", ""))
        entry_dict["keywords"] = parse_tags_string(entry_dict.get("keywords", ""))

        return json.dumps(entry_dict, ensure_ascii=False, indent=2, default=str)

    return await anyio.to_thread.run_sync(_impl)


# ============================================================
# Resource 3: pkv://tags — 标签列表
# ============================================================

@mcp.resource("pkv://tags")
async def get_tags_resource() -> str:
    """获取所有标签列表（Resource 版，返回 JSON 字符串）。

    Returns:
        JSON 格式的标签列表，每项包含 name 和 count
    """
    def _impl():
        store = get_sqlite_store()
        tags = store.get_all_tags_with_count()
        return json.dumps(
            {"total_tags": len(tags), "tags": tags},
            ensure_ascii=False,
            indent=2,
        )

    return await anyio.to_thread.run_sync(_impl)


# ============================================================
# Resource 4: pkv://stats — 统计信息
# ============================================================

@mcp.resource("pkv://stats")
async def get_stats_resource() -> str:
    """获取知识库统计信息（Resource 版，返回 JSON 字符串）。

    Returns:
        JSON 格式的统计数据
    """
    def _impl():
        store = get_sqlite_store()
        stats = store.get_statistics()
        return json.dumps(stats, ensure_ascii=False, indent=2, default=str)

    return await anyio.to_thread.run_sync(_impl)
