"""Structured Markdown preview loading shared by the GUI adapters."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from pathlib import Path
import re
from typing import Any, Literal, TYPE_CHECKING

from src.relations.citations import sanitize_public_source_url
from src.runtime.errors import ErrorCode, PKVRuntimeError

if TYPE_CHECKING:
    from src.storage.markdown_store import MarkdownStore

logger = logging.getLogger("pkv.gui.utils.preview")

PreviewStatus = Literal["success", "degraded", "error"]

_CAUSE_TYPE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]{0,95}$")
_STAGE = re.compile(r"^[a-z][a-z0-9_.:-]{0,95}$")


@dataclass(frozen=True)
class PreviewIssue:
    """Stable, adapter-safe reason why a full preview was unavailable."""

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
    """Explicit success/degraded/error outcome for one preview request."""

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
    """Revalidate an outcome at adapter boundaries, including frozen corruption."""

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
    """Return a bounded exception type identifier without exception text."""

    cause_type = type(exc).__name__
    return cause_type if _CAUSE_TYPE.fullmatch(cause_type) else "Exception"


def _safe_stage(value: Any, fallback: str) -> str:
    """Allow only stable stage identifiers in the public issue contract."""

    return value if isinstance(value, str) and _STAGE.fullmatch(value) else fallback


def _issue_from_exception(exc: BaseException, *, stage: str) -> PreviewIssue:
    """Convert a loader exception without retaining its potentially secret text."""

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
    """Log only stable diagnostics; paths and exception messages stay private."""

    log = logger.error if status == "error" else logger.warning
    log(
        "预览加载未完整完成: status=%s, code=%s, error_type=%s",
        status,
        issue.code.value,
        issue.cause_type,
    )


def _fallback_outcome(entry: dict, issue: PreviewIssue) -> PreviewOutcome:
    """Return a safe summary degradation or an explicit fallback error."""

    try:
        fallback = _build_summary_fallback(entry)
    except Exception as exc:
        fallback_issue = _issue_from_exception(exc, stage="preview_summary")
        _log_outcome("error", fallback_issue)
        return PreviewOutcome(status="error", content="", issue=fallback_issue)

    _log_outcome("degraded", issue)
    return PreviewOutcome(status="degraded", content=fallback, issue=issue)


def load_entry_preview_outcome(
    entry: dict,
    md_store: "MarkdownStore",
) -> PreviewOutcome:
    """Load a full Markdown preview with an explicit three-state outcome."""

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
        issue = PreviewIssue(
            code=ErrorCode.RESOURCE_MISSING,
            stage="preview_path",
            cause_type="MissingPreviewPath",
        )
        return _fallback_outcome(entry, issue)

    try:
        loaded = md_store.load(Path(file_path_value))
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


def load_entry_preview(entry: dict, md_store: "MarkdownStore") -> str:
    """Compatibility text adapter retained for Chat until its owner migrates.

    BrowserView and SearchView must consume :func:`load_entry_preview_outcome`
    so they cannot erase degraded/error semantics.
    """

    return load_entry_preview_outcome(entry, md_store).content


def _build_summary_fallback(entry: dict) -> str:
    """Build a summary preview while sanitizing the shared public source URL."""

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

    source_url = sanitize_public_source_url(entry.get("source_url", ""))
    if source_url:
        lines.append(f"**来源**: {source_url}")

    archived_at = entry.get("archived_at", "")
    if archived_at:
        lines.append(f"**归档时间**: {archived_at}")

    if not (summary or source_url):
        lines.append("_（无内容预览）_")

    return "\n".join(lines)


__all__ = [
    "PreviewIssue",
    "PreviewOutcome",
    "PreviewStatus",
    "is_strict_preview_outcome",
    "load_entry_preview",
    "load_entry_preview_outcome",
]
