"""Headless preview contract exposed by the PKV Kernel."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from pathlib import Path
import re
from typing import Any, Literal

from src.relations.citations import sanitize_public_source_url
from src.runtime.errors import ErrorCode, PKVRuntimeError

logger = logging.getLogger("pkv.kernel.preview")

PreviewStatus = Literal["success", "degraded", "error"]
_CAUSE_TYPE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]{0,95}$")
_STAGE = re.compile(r"^[a-z][a-z0-9_.:-]{0,95}$")


@dataclass(frozen=True)
class PreviewIssue:
    """Stable reason why a full preview was unavailable."""

    code: ErrorCode
    stage: str
    recoverable: bool = False
    cause_type: str = "PreviewUnavailable"

    def __post_init__(self) -> None:
        if type(self.code) is not ErrorCode:
            raise TypeError("PreviewIssue.code 必须是 ErrorCode")
        if not isinstance(self.stage, str) or not _STAGE.fullmatch(self.stage):
            raise ValueError("PreviewIssue.stage 格式无效")
        if type(self.recoverable) is not bool:
            raise TypeError("PreviewIssue.recoverable 必须是 bool")
        if not _CAUSE_TYPE.fullmatch(self.cause_type):
            raise ValueError("PreviewIssue.cause_type 格式无效")


@dataclass(frozen=True)
class PreviewOutcome:
    """Explicit success/degraded/error result for one preview request."""

    status: PreviewStatus
    content: str
    issue: PreviewIssue | None = None

    def __post_init__(self) -> None:
        if self.status not in {"success", "degraded", "error"}:
            raise ValueError("未知预览状态")
        if type(self.content) is not str:
            raise TypeError("PreviewOutcome.content 必须是 str")
        if self.status == "success":
            if not self.content.strip() or self.issue is not None:
                raise ValueError("success 必须包含正文且不得携带 issue")
        elif self.status == "degraded":
            if not self.content.strip() or not isinstance(self.issue, PreviewIssue):
                raise ValueError("degraded 必须包含安全摘要和 issue")
        elif self.content or not isinstance(self.issue, PreviewIssue):
            raise ValueError("error 不得携带内容且必须包含 issue")


def is_strict_preview_outcome(value: Any) -> bool:
    """Revalidate the immutable Kernel outcome at a wrapper boundary."""

    if type(value) is not PreviewOutcome:
        return False
    try:
        status = value.status
        content = value.content
        issue = value.issue
        if type(status) is not str or type(content) is not str:
            return False
        if issue is not None:
            if type(issue) is not PreviewIssue:
                return False
            if (
                type(issue.code) is not ErrorCode
                or not isinstance(issue.stage, str)
                or not _STAGE.fullmatch(issue.stage)
                or type(issue.recoverable) is not bool
                or not isinstance(issue.cause_type, str)
                or not _CAUSE_TYPE.fullmatch(issue.cause_type)
            ):
                return False
        if status == "success":
            return bool(content.strip()) and issue is None
        if status == "degraded":
            return bool(content.strip()) and issue is not None
        if status == "error":
            return not content and issue is not None
        return False
    except Exception:
        return False


def _safe_cause_type(exc: BaseException) -> str:
    cause_type = type(exc).__name__
    return cause_type if _CAUSE_TYPE.fullmatch(cause_type) else "Exception"


def _safe_stage(value: Any, fallback: str) -> str:
    return value if isinstance(value, str) and _STAGE.fullmatch(value) else fallback


def _issue_from_exception(exc: BaseException, *, stage: str) -> PreviewIssue:
    if isinstance(exc, PKVRuntimeError):
        return PreviewIssue(
            code=exc.code,
            stage=_safe_stage(exc.stage, stage),
            recoverable=exc.recoverable,
            cause_type=_safe_cause_type(exc),
        )
    code = (
        ErrorCode.RESOURCE_MISSING
        if isinstance(exc, FileNotFoundError)
        else ErrorCode.RESOURCE_NOT_READABLE
    )
    return PreviewIssue(
        code=code,
        stage=stage,
        recoverable=isinstance(exc, OSError),
        cause_type=_safe_cause_type(exc),
    )


def _log_outcome(status: PreviewStatus, issue: PreviewIssue) -> None:
    log = logger.error if status == "error" else logger.warning
    log(
        "预览加载未完整完成: status=%s, code=%s, error_type=%s",
        status,
        issue.code.value,
        issue.cause_type,
    )


def _summary_fallback(entry: dict[str, Any]) -> str:
    """Preserve the established safe preview projection behind the Kernel."""

    lines: list[str] = []
    title = entry.get("title", "（无标题）")
    lines.append(f"# {title}")
    lines.append("")

    summary = entry.get("summary_one_sentence", "")
    if summary:
        if type(summary) is not str:
            raise TypeError("预览摘要必须是字符串")
        lines.append(f"> {summary}")
        lines.append("")

    tags_raw = entry.get("tags", "")
    if tags_raw:
        if isinstance(tags_raw, list):
            if not all(type(tag) is str for tag in tags_raw):
                raise TypeError("预览标签必须是字符串列表")
            tags_list = tags_raw
        elif type(tags_raw) is str:
            tags_list = [tag.strip() for tag in tags_raw.split(",") if tag.strip()]
        else:
            raise TypeError("预览标签必须是字符串或列表")
        if tags_list:
            lines.append(f"**标签**: {' '.join(tags_list)}")

    source_url = sanitize_public_source_url(entry.get("source_url", ""))
    if source_url:
        lines.append(f"**来源**: {source_url}")

    archived_at = entry.get("archived_at", "")
    if archived_at:
        if type(archived_at) is not str:
            raise TypeError("预览归档时间必须是字符串")
        lines.append(f"**归档时间**: {archived_at}")

    if not (summary or source_url):
        lines.append("_（无内容预览）_")
    return "\n".join(lines)


def _fallback_outcome(entry: dict[str, Any], issue: PreviewIssue) -> PreviewOutcome:
    try:
        fallback = _summary_fallback(entry)
    except Exception as exc:
        fallback_issue = _issue_from_exception(exc, stage="preview_summary")
        _log_outcome("error", fallback_issue)
        return PreviewOutcome(status="error", content="", issue=fallback_issue)
    _log_outcome("degraded", issue)
    return PreviewOutcome(status="degraded", content=fallback, issue=issue)


def load_preview_with_store(
    entry: dict[str, Any],
    markdown_store: Any,
) -> PreviewOutcome:
    """Load a Markdown preview using a Kernel-owned store."""

    if type(entry) is not dict:
        issue = PreviewIssue(
            code=ErrorCode.RESOURCE_NOT_READABLE,
            stage="preview_input",
            cause_type="InvalidPreviewEntry",
        )
        _log_outcome("error", issue)
        return PreviewOutcome(status="error", content="", issue=issue)

    file_path_value = entry.get("file_path")
    if not isinstance(file_path_value, str) or not file_path_value.strip():
        return _fallback_outcome(
            entry,
            PreviewIssue(
                code=ErrorCode.RESOURCE_MISSING,
                stage="preview_path",
                cause_type="MissingPreviewPath",
            ),
        )
    try:
        loaded = markdown_store.load(Path(file_path_value))
        content = getattr(loaded, "content", None)
        if type(content) is str and content.strip():
            return PreviewOutcome(status="success", content=content)
        issue = PreviewIssue(
            code=ErrorCode.RESOURCE_MISSING,
            stage="preview_content",
            cause_type="EmptyPreviewContent",
        )
    except Exception as exc:
        issue = _issue_from_exception(exc, stage="preview_markdown")
    return _fallback_outcome(entry, issue)
