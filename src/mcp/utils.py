"""
MCP 辅助工具

提供序列化、字段转换、错误处理等通用函数。
"""

import logging
from typing import Any, Dict, List

logger = logging.getLogger("pkv.mcp")


def parse_tags_string(tags_str: Any) -> List[str]:
    """将 SQLite 中逗号分隔的 tags 字符串转换为列表。

    数据库中 tags 以 ','.join(tags) 方式存储（如 "AI,知识管理"），
    SearchResult.metadata["tags"] 返回的也是字符串，需转换为列表。

    Args:
        tags_str: 逗号分隔的标签字符串，或已经是列表

    Returns:
        标签列表

    Examples:
        >>> parse_tags_string("AI,知识管理,NLP")
        ['AI', '知识管理', 'NLP']
        >>> parse_tags_string("")
        []
        >>> parse_tags_string(["AI", "NLP"])
        ['AI', 'NLP']
    """
    if not tags_str:
        return []
    if isinstance(tags_str, list):
        return tags_str
    return [t.strip() for t in str(tags_str).split(",") if t.strip()]


def serialize_entry_summary(entry: Dict[str, Any]) -> Dict[str, Any]:
    """将 SQLiteStore 返回的条目 dict 序列化为 MCP 摘要格式。

    处理以下字段映射：
    - summary_one_sentence → abstract（DB 无 abstract 列）
    - tags/keywords 逗号字符串 → 列表

    Args:
        entry: SQLiteStore.query_by_id() 或 list_entries() 返回的条目字典

    Returns:
        适合 MCP 返回的序列化字典
    """
    return {
        "knowledge_id": entry.get("knowledge_id"),
        "title": entry.get("title", ""),
        "abstract": entry.get("summary_one_sentence", ""),
        "tags": parse_tags_string(entry.get("tags", "")),
        "source_type": entry.get("source_type", ""),
        "word_count": entry.get("word_count", 0),
        "archived_at": entry.get("archived_at", ""),
    }


def serialize_search_result(result: Any) -> Dict[str, Any]:
    """将 SearchResult 序列化为 MCP 返回格式。

    处理以下字段映射：
    - result.highlight → abstract（SearchResult 无 abstract 属性）
    - result.metadata.get("tags") → tags 列表（metadata 中是逗号字符串）

    Args:
        result: SearchResult(frozen dataclass) 实例

    Returns:
        适合 MCP 返回的序列化字典
    """
    return {
        "knowledge_id": result.knowledge_id,
        "title": result.title,
        "abstract": result.highlight,  # SearchResult.highlight 是摘要/snippet
        "score": round(result.score, 4),
        "tags": parse_tags_string(result.metadata.get("tags", "")),
        "source_type": result.metadata.get("source_type", ""),
        "archived_at": result.metadata.get("archived_at", ""),
    }


def clamp_param(value: int, min_val: int, max_val: int) -> int:
    """将参数值限制在合法范围内。

    Args:
        value: 输入值
        min_val: 最小值
        max_val: 最大值

    Returns:
        限制后的值
    """
    return max(min_val, min(value, max_val))
