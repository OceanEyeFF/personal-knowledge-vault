"""
MCP 辅助工具

提供序列化、字段转换、安全验证等通用函数。
"""

import ipaddress
import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

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


# ============================================================
# 安全验证函数（M9 新增）
# ============================================================

# HTTP Bearer Token 认证（从环境变量读取，不硬编码）
_MCP_AUTH_TOKEN: Optional[str] = os.environ.get("PKV_MCP_AUTH_TOKEN", "")


def validate_url(url: str) -> Tuple[bool, str]:
    """验证 URL 格式是否合法。

    检查项：
    - 非空
    - scheme 必须是 http 或 https
    - 必须包含有效的 netloc（域名/IP）

    Args:
        url: 待验证的 URL

    Returns:
        (is_valid, error_message) — 合法时 error_message 为空字符串
    """
    if not url or not url.strip():
        return False, "URL 不能为空"

    try:
        parsed = urlparse(url.strip())
    except Exception:
        return False, f"URL 解析失败: {url}"

    if parsed.scheme not in ("http", "https"):
        return False, f"URL scheme 必须是 http 或 https，当前: {parsed.scheme or '(空)'}"

    if not parsed.netloc:
        return False, f"URL 缺少有效的域名或 IP: {url}"

    return True, ""


def is_private_ip(hostname: str) -> bool:
    """检查主机名是否指向内网地址。

    拒绝以下地址：
    - 127.0.0.0/8 (loopback)
    - 10.0.0.0/8 (Class A 内网)
    - 172.16.0.0/12 (Class B 内网)
    - 192.168.0.0/16 (Class C 内网)
    - ::1, fe80::/10 (IPv6 loopback/link-local)
    - localhost

    Args:
        hostname: 主机名或 IP 地址

    Returns:
        True 表示是内网地址
    """
    if not hostname:
        return True

    # 直接匹配 localhost
    if hostname.lower() in ("localhost", "localhost."):
        return True

    try:
        addr = ipaddress.ip_address(hostname)
        return addr.is_private or addr.is_loopback or addr.is_link_local
    except ValueError:
        # 不是有效 IP，可能是域名，检查常见的内网域名后缀
        lower_host = hostname.lower()
        if lower_host.endswith((".local", ".internal", ".lan")):
            return True
        return False


def validate_url_security(url: str) -> Tuple[bool, str]:
    """综合验证 URL 格式和安全性（合并格式 + 内网检查）。

    Args:
        url: 待验证的 URL

    Returns:
        (is_valid, error_message)
    """
    valid, error = validate_url(url)
    if not valid:
        logger.warning(f"[安全] URL 格式验证失败: {error} (url={url!r})")
        return False, error

    parsed = urlparse(url.strip())
    hostname = parsed.hostname or ""

    if is_private_ip(hostname):
        logger.warning(f"[安全] SSRF 拦截: 禁止访问内网地址 {hostname} (url={url!r})")
        return False, f"禁止访问内网地址: {hostname}"

    return True, ""


def validate_text_length(text: str, max_length: int = 100000) -> Tuple[bool, str]:
    """验证文本长度是否在允许范围内。

    Args:
        text: 待验证的文本
        max_length: 最大允许长度（默认 100,000 字符）

    Returns:
        (is_valid, error_message)
    """
    if not text or not text.strip():
        return False, "文本内容不能为空"

    if len(text) > max_length:
        logger.warning(f"[安全] 文本长度超限: {len(text)} > {max_length}")
        return False, f"文本长度 {len(text)} 超过限制 {max_length} 字符"

    return True, ""


def validate_http_auth(request_headers: Dict[str, str]) -> bool:
    """验证 HTTP 请求的 Bearer Token。

    安全默认原则：
    - 未配置 PKV_MCP_AUTH_TOKEN 时，拒绝所有 HTTP 请求
    - stdio 模式不需要调用此函数（天然安全）

    Args:
        request_headers: HTTP 请求头字典

    Returns:
        True 表示认证通过
    """
    if not _MCP_AUTH_TOKEN:
        # 未配置 Token 时拒绝所有 HTTP 请求（安全默认）
        logger.warning("[安全] HTTP 认证拒绝: PKV_MCP_AUTH_TOKEN 未配置")
        return False

    auth_header = request_headers.get("Authorization", "")
    if auth_header != f"Bearer {_MCP_AUTH_TOKEN}":
        logger.warning("[安全] HTTP 认证失败: Bearer Token 不匹配")
        return False

    return True
