from __future__ import annotations

import json
import textwrap
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from rich.panel import Panel
from rich.table import Table


def format_as_json(data: Any) -> str:
    """Format data as JSON (supports dataclasses)."""
    normalized = _to_serializable(data)
    return json.dumps(normalized, indent=2, ensure_ascii=False)


def format_as_markdown(entry: Any) -> str:
    """Format an Entry as Markdown (title/author/time/tags/abstract)."""
    title = _get_field(entry, "title", "Untitled")
    metadata = _get_entry_metadata(entry)
    author = metadata.get("author") or metadata.get("publisher") or "未知"
    time_value = (
        metadata.get("published_time")
        or metadata.get("publish_time")
        or _get_field(entry, "archived_at", "")
        or "未知"
    )
    tags_value = _get_field(entry, "tags", None) or metadata.get("tags")
    tags_text = _format_tags(tags_value) or "无"
    abstract = (
        _get_field(entry, "abstract", "")
        or metadata.get("description")
        or _get_field(entry, "summary_100_words", "")
        or _get_field(entry, "summary_one_sentence", "")
    )
    abstract = abstract.strip() if isinstance(abstract, str) else str(abstract)
    if not abstract:
        abstract = "（无摘要）"

    lines = [
        f"# {title}",
        "",
        f"**作者**: {author}",
        f"**时间**: {time_value}",
        f"**标签**: {tags_text}",
        "",
        "## 摘要",
        abstract,
        "",
    ]
    return "\n".join(lines).strip() + "\n"


def format_search_results(results: Iterable[Any]) -> Table:
    """Format search results as a Rich table."""
    table = Table(title="搜索结果")
    table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("标题")
    table.add_column("得分", style="green", justify="right")
    table.add_column("标签")

    for result in results:
        result_id = _get_result_id(result)
        title = _get_field(result, "title", "")
        score = _get_field(result, "score", None)
        metadata = _get_field(result, "metadata", {}) or {}
        tags = metadata.get("tags") if isinstance(metadata, Mapping) else None
        if tags is None:
            tags = _get_field(result, "tags", None)

        table.add_row(
            str(result_id) if result_id is not None else "",
            _truncate(str(title), 30),
            _format_score(score),
            _format_tags(tags),
        )

    return table


def format_entry_detail(entry: Any) -> Panel:
    """Format entry metadata as a Rich panel."""
    metadata = _get_entry_metadata(entry)
    title = _get_field(entry, "title", "Untitled")
    entry_id = _get_result_id(entry)
    panel_title = f"条目详情 #{entry_id}" if entry_id is not None else "条目详情"

    lines = [
        f"标题: {title}",
        f"作者: {_format_value(metadata.get('author'))}",
        f"发布时间: {_format_value(metadata.get('published_time') or metadata.get('publish_time'))}",
        f"来源类型: {_format_value(_get_field(entry, 'source_type', ''))}",
        f"来源 URL: {_format_value(_get_field(entry, 'source_url', ''))}",
        f"归档时间: {_format_value(_get_field(entry, 'archived_at', ''))}",
        f"标签: {_format_tags(_get_field(entry, 'tags', None) or metadata.get('tags'))}",
        f"关键词: {_format_tags(_get_field(entry, 'keywords', None))}",
        "摘要:",
        _format_paragraph(
            _get_field(entry, "abstract", "")
            or metadata.get("description")
            or _get_field(entry, "summary_100_words", "")
            or _get_field(entry, "summary_one_sentence", "")
        ),
        f"一句话摘要: {_format_value(_get_field(entry, 'summary_one_sentence', ''))}",
        "100字摘要:",
        _format_paragraph(_get_field(entry, "summary_100_words", "")),
        f"检索策略: {_format_value(_get_field(entry, 'search_strategy', ''))}",
        f"字数: {_format_value(_get_field(entry, 'word_count', 0))}",
        f"相关文档: {_format_value(_get_field(entry, 'related_docs', None))}",
        f"阅读状态: {_format_value(_get_field(entry, 'reading_status', ''))}",
        f"评分: {_format_value(_get_field(entry, 'rating', 0))}",
        "笔记:",
        _format_paragraph(_get_field(entry, "notes", "")),
    ]

    return Panel("\n".join(lines), title=panel_title)


def _get_result_id(obj: Any) -> Any:
    for key in ("knowledge_id", "entry_id", "id"):
        value = _get_field(obj, key, None)
        if value is not None:
            return value
    return None


def _get_entry_metadata(entry: Any) -> dict:
    metadata = getattr(entry, "metadata", None)
    if isinstance(entry, Mapping):
        metadata = entry.get("metadata", metadata)
    return metadata if isinstance(metadata, Mapping) else {}


def _get_field(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _format_score(score: Any) -> str:
    if score is None:
        return ""
    try:
        return f"{float(score):.2f}"
    except (TypeError, ValueError):
        return str(score)


def _format_tags(value: Any) -> str:
    tags = _normalize_list(value)
    return ", ".join(tags) if tags else ""


def _normalize_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [item for item in (part.strip() for part in value.split(",")) if item]
    if isinstance(value, (list, tuple, set)):
        items = []
        for item in value:
            if item is None:
                continue
            text = str(item).strip()
            if text:
                items.append(text)
        return items
    text = str(value).strip()
    return [text] if text else []


def _format_value(value: Any) -> str:
    if value is None:
        return "无"
    if isinstance(value, str):
        text = value.strip()
        return text if text else "无"
    if isinstance(value, (list, tuple, set)):
        text = ", ".join(_normalize_list(value))
        return text if text else "无"
    return str(value)


def _format_paragraph(text: Any, width: int = 80, indent: int = 2) -> str:
    content = text if isinstance(text, str) else str(text)
    content = content.strip()
    if not content:
        return " " * indent + "无"
    padding = " " * indent
    return textwrap.fill(content, width=width, initial_indent=padding, subsequent_indent=padding)


def _truncate(text: str, max_length: int) -> str:
    if len(text) <= max_length:
        return text
    if max_length <= 3:
        return text[:max_length]
    return text[: max_length - 3] + "..."


def _to_serializable(value: Any) -> Any:
    if is_dataclass(value):
        data = {key: _to_serializable(val) for key, val in asdict(value).items()}
        if hasattr(value, "__dict__"):
            for key, val in value.__dict__.items():
                if key not in data:
                    data[key] = _to_serializable(val)
        return data
    if isinstance(value, Mapping):
        return {str(key): _to_serializable(val) for key, val in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_to_serializable(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if hasattr(value, "__dict__"):
        return {key: _to_serializable(val) for key, val in value.__dict__.items()}
    return str(value)
