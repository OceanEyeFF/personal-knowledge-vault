"""知识引用工具模块 (M12)

提供知识条目引用的核心功能：
- 智能截断：前 3000 tokens + 原文摘要
- 引用内容格式化：构建 system message 上下文
- @ 语法解析：解析 @知识库/ID 和 @搜索/keyword 引用

技术说明：
- Token 估算使用简化规则（中文 1 字 ≈ 1.5 token，英文 1 词 ≈ 1 token）
- 截断保留完整段落边界，避免在句子中间截断
"""

from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, List

from src.gui.styles import theme_colors
from src.relations.citations import sanitize_public_source_url

logger = logging.getLogger("pkv.gui.utils.knowledge_ref")

# Token 估算常量
_CHINESE_CHAR_PATTERN = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]")
_TOKEN_PER_CHINESE_CHAR = 1.5
_TOKEN_PER_ENGLISH_WORD = 1.0
_MAX_CONTEXT_TOKENS = 3000

# @ 语法正则
_AT_KNOWLEDGE_PATTERN = re.compile(r"@知识库/(\d+)")
_AT_SEARCH_PATTERN = re.compile(r"@搜索/(.+?)(?:\s|$)")
_URL_PATTERN = re.compile(
    r"https?://[^\s<>\"')\]]+",
    re.IGNORECASE,
)


@dataclass
class KnowledgeReference:
    """知识引用数据类

    Attributes:
        knowledge_id: 知识条目 ID
        title: 条目标题
        source_type: 来源类型（wechat/zhihu/...）
        source_url: 原始 URL
        summary: 一句话摘要
        content_truncated: 智能截断后的内容
        token_count: 估算的 token 数
        is_truncated: 是否被截断
    """
    knowledge_id: int
    title: str
    source_type: str = ""
    source_url: str = ""
    summary: str = ""
    content_truncated: str = ""
    token_count: int = 0
    is_truncated: bool = False


@dataclass
class AtReference:
    """@ 语法引用解析结果

    Attributes:
        ref_type: 引用类型（"knowledge" 或 "search"）
        value: 引用值（ID 或搜索关键词）
        original_text: 原始 @ 文本
        start: 在原文中的起始位置
        end: 在原文中的结束位置
    """
    ref_type: str  # "knowledge" | "search"
    value: str
    original_text: str
    start: int
    end: int


def estimate_tokens(text: str) -> int:
    """估算文本的 token 数

    使用简化规则：
    - 中文字符：1 字 ≈ 1.5 tokens
    - 英文/数字：按空格分词，1 词 ≈ 1 token
    - 标点符号：1 个 ≈ 1 token

    Args:
        text: 输入文本

    Returns:
        估算的 token 数
    """
    if not text:
        return 0

    chinese_chars = len(_CHINESE_CHAR_PATTERN.findall(text))
    # 移除中文字符后计算英文词数
    text_no_chinese = _CHINESE_CHAR_PATTERN.sub("", text)
    english_words = len(text_no_chinese.split())

    tokens = int(
        chinese_chars * _TOKEN_PER_CHINESE_CHAR
        + english_words * _TOKEN_PER_ENGLISH_WORD
    )
    return max(tokens, 1) if text.strip() else 0


def smart_truncate(
    content: str,
    max_tokens: int = _MAX_CONTEXT_TOKENS,
    summary: str = "",
) -> tuple[str, int, bool]:
    """智能截断内容

    保留前 max_tokens 个 token 的内容，在段落边界截断。
    如果内容被截断，追加摘要信息。

    Args:
        content: 原始内容
        max_tokens: 最大 token 数（默认 3000）
        summary: 原文摘要（截断时追加）

    Returns:
        (截断后内容, 估算 token 数, 是否被截断)
    """
    if not content:
        return "", 0, False

    total_tokens = estimate_tokens(content)
    if total_tokens <= max_tokens:
        return content, total_tokens, False

    # 按段落分割
    paragraphs = content.split("\n\n")
    result_parts: list[str] = []
    current_tokens = 0

    for para in paragraphs:
        para_tokens = estimate_tokens(para)
        if current_tokens + para_tokens > max_tokens:
            # 如果当前段落超限，尝试按行截取
            if not result_parts:
                # 第一个段落就超限，按行截取
                lines = para.split("\n")
                for line in lines:
                    line_tokens = estimate_tokens(line)
                    if current_tokens + line_tokens > max_tokens:
                        break
                    result_parts.append(line)
                    current_tokens += line_tokens
            break
        result_parts.append(para)
        current_tokens += para_tokens

    truncated_content = "\n\n".join(result_parts)

    # 追加截断提示和摘要
    truncated_content += "\n\n---\n[内容已截断，以上为前 ~{} tokens]\n".format(
        current_tokens
    )
    if summary:
        truncated_content += f"\n**原文摘要**: {summary}\n"

    final_tokens = estimate_tokens(truncated_content)
    return truncated_content, final_tokens, True


def build_knowledge_reference(
    entry: Dict[str, Any],
    content: str = "",
    max_tokens: int = _MAX_CONTEXT_TOKENS,
) -> KnowledgeReference:
    """从知识条目构建引用对象

    Args:
        entry: SQLiteStore 返回的条目字典
        content: Markdown 全文内容（可选，为空时使用 entry 中的摘要）
        max_tokens: 最大上下文 token 数

    Returns:
        KnowledgeReference 对象
    """
    knowledge_id = entry.get("knowledge_id", 0)
    title = entry.get("title", "")
    source_type = entry.get("source_type", "")
    source_url = sanitize_public_source_url(entry.get("source_url", ""))
    summary = entry.get("summary_one_sentence", "") or entry.get(
        "summary_100_words", ""
    )

    if content:
        truncated, token_count, is_truncated = smart_truncate(
            content, max_tokens, summary
        )
    else:
        # 无全文时使用摘要
        truncated = summary or "(无内容)"
        token_count = estimate_tokens(truncated)
        is_truncated = False

    return KnowledgeReference(
        knowledge_id=knowledge_id,
        title=title,
        source_type=source_type,
        source_url=source_url,
        summary=summary,
        content_truncated=truncated,
        token_count=token_count,
        is_truncated=is_truncated,
    )


def format_context_message(refs: List[KnowledgeReference]) -> str:
    """将知识引用列表格式化为 system message 上下文

    Args:
        refs: 知识引用列表

    Returns:
        格式化的上下文文本（用于 system message）
    """
    if not refs:
        return ""

    parts = ["以下是用户引用的知识库内容，请基于这些内容回答问题：\n"]

    for i, ref in enumerate(refs, 1):
        header = f"## 引用 {i}: {ref.title}"
        if ref.source_type:
            header += f" [{ref.source_type}]"
        parts.append(header)

        if ref.source_url:
            parts.append(f"来源: {ref.source_url}")

        parts.append("")
        parts.append(ref.content_truncated)
        parts.append("")

    return "\n".join(parts)


def format_reference_card_html(ref: KnowledgeReference) -> str:
    """将知识引用格式化为 HTML 卡片（用于 ChatView 显示）

    Args:
        ref: 知识引用对象

    Returns:
        HTML 格式的引用卡片
    """
    colors = theme_colors.get_current_colors()
    truncated_marker = " (已截断)" if ref.is_truncated else ""
    safe_title = html.escape(str(ref.title), quote=True)
    safe_source_type = html.escape(str(ref.source_type), quote=True)
    safe_summary = html.escape(
        str(ref.summary[:100] if ref.summary else "(无摘要)"),
        quote=True,
    )
    safe_knowledge_id = html.escape(str(ref.knowledge_id), quote=True)
    safe_token_count = html.escape(str(ref.token_count), quote=True)
    source_info = f" | {safe_source_type}" if safe_source_type else ""

    return f"""
    <div style="
        background-color: {colors['ref_card_bg']};
        border-left: 4px solid {colors['ref_card_border']};
        padding: 10px;
        margin: 6px;
        border-radius: 4px;
        color: {colors['msg_fg']};
    ">
        <strong>📎 引用: {safe_title}</strong>{source_info}<br>
        <span style="color: {colors['ref_card_meta']}; font-size: 12px;">
            ID: {safe_knowledge_id} | ~{safe_token_count} tokens{truncated_marker}
        </span><br>
        <em style="color: {colors['ref_card_summary']};">{safe_summary}</em>
    </div>
    """


def parse_at_references(text: str) -> List[AtReference]:
    """解析文本中的 @ 引用语法

    支持两种引用格式：
    - @知识库/123 — 直接引用知识条目 ID
    - @搜索/关键词 — 搜索引用

    Args:
        text: 用户输入文本

    Returns:
        AtReference 列表
    """
    refs: list[AtReference] = []

    # 解析 @知识库/ID
    for match in _AT_KNOWLEDGE_PATTERN.finditer(text):
        refs.append(AtReference(
            ref_type="knowledge",
            value=match.group(1),
            original_text=match.group(0),
            start=match.start(),
            end=match.end(),
        ))

    # 解析 @搜索/keyword
    for match in _AT_SEARCH_PATTERN.finditer(text):
        refs.append(AtReference(
            ref_type="search",
            value=match.group(1).strip(),
            original_text=match.group(0),
            start=match.start(),
            end=match.end(),
        ))

    # 按位置排序
    refs.sort(key=lambda r: r.start)
    return refs


def strip_at_references(text: str) -> str:
    """从文本中移除 @ 引用语法，保留其他内容

    Args:
        text: 包含 @ 引用的文本

    Returns:
        移除引用后的纯文本
    """
    result = _AT_KNOWLEDGE_PATTERN.sub("", text)
    result = _AT_SEARCH_PATTERN.sub("", result)
    return result.strip()


def detect_urls(text: str) -> List[str]:
    """检测文本中的 URL

    Args:
        text: 用户输入文本

    Returns:
        URL 列表
    """
    return _URL_PATTERN.findall(text)
