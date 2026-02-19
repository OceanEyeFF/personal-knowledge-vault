"""Markdown 预览内容加载工具。

提供从 entry 字典加载 Markdown 预览的共享逻辑，
供 BrowserView 和 SearchView 复用。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.storage.markdown_store import MarkdownStore

logger = logging.getLogger("pkv.gui.utils.preview")


def load_entry_preview(entry: dict, md_store: "MarkdownStore") -> str:
    """从知识条目字典加载 Markdown 预览内容。

    优先从 file_path 读取完整 Markdown 内容；
    若文件不存在或读取失败，则回退到摘要信息。

    Args:
        entry: 知识条目字典（来自 SQLiteStore.list_entries() 或搜索结果转换）。
        md_store: MarkdownStore 实例。

    Returns:
        预览文本内容（Markdown 格式字符串）。
    """
    file_path_str = entry.get("file_path", "")
    if file_path_str:
        try:
            file_path = Path(file_path_str)
            loaded = md_store.load(file_path)
            if loaded and loaded.content:
                return loaded.content
        except (FileNotFoundError, OSError) as exc:
            logger.warning(f"读取 Markdown 文件失败: {file_path_str} — {exc}")
        except Exception as exc:
            logger.error(f"预览加载异常: {exc}")

    return _build_summary_fallback(entry)


def _build_summary_fallback(entry: dict) -> str:
    """构建摘要降级预览内容。

    当 Markdown 文件不可用时，用元数据和摘要构建预览文本。

    Args:
        entry: 知识条目字典。

    Returns:
        摘要文本（Markdown 格式）。
    """
    lines: list[str] = []

    title = entry.get("title", "（无标题）")
    lines.append(f"# {title}")
    lines.append("")

    summary = entry.get("summary_one_sentence", "")
    if summary:
        lines.append(f"> {summary}")
        lines.append("")

    tags_raw = entry.get("tags", "")
    if tags_raw:
        if isinstance(tags_raw, list):
            tags_list = tags_raw
        else:
            tags_list = [t.strip() for t in tags_raw.split(",") if t.strip()]
        if tags_list:
            lines.append(f"**标签**: {' '.join(tags_list)}")

    source_url = entry.get("source_url", "")
    if source_url:
        lines.append(f"**来源**: {source_url}")

    archived_at = entry.get("archived_at", "")
    if archived_at:
        lines.append(f"**归档时间**: {archived_at}")

    if not (summary or source_url):
        lines.append("_（无内容预览）_")

    return "\n".join(lines)
