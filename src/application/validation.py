"""Input-policy checks owned by the application layer.

These checks intentionally stop before DNS/network activity.  The processor's
safe fetcher remains the authority for pinned resolution and redirect-time
validation; adapters merely project the stable error into their own UI or
protocol envelope.
"""

from __future__ import annotations

from src.processors.safe_fetch import is_forbidden_hostname, parse_http_target
from src.runtime.errors import ErrorCode, PKVRuntimeError


def validate_url_security_result(url: str) -> PKVRuntimeError | None:
    """Return a stable URL-policy error or ``None`` for an allowed HTTP URL."""

    try:
        target = parse_http_target(url)
    except PKVRuntimeError as exc:
        return PKVRuntimeError(
            exc.code,
            str(exc),
            stage="url_preflight",
            recoverable=exc.recoverable,
        )
    except Exception:
        return PKVRuntimeError(
            ErrorCode.URL_INVALID,
            "URL 解析失败",
            stage="url_preflight",
            recoverable=False,
        )
    if is_forbidden_hostname(target.hostname):
        return PKVRuntimeError(
            ErrorCode.SSRF_TARGET_FORBIDDEN,
            "禁止访问内网地址或其他非公网目标",
            stage="url_preflight",
            recoverable=False,
        )
    return None


def validate_text_length(text: str, max_length: int = 100000) -> tuple[bool, str]:
    """Validate literal-text archive input without interpreting it as a path."""

    if not text or not text.strip():
        return False, "文本内容不能为空"
    if len(text) > max_length:
        return False, f"文本长度 {len(text)} 超过限制 {max_length} 字符"
    return True, ""
