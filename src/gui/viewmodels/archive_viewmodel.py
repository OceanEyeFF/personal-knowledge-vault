"""归档操作 ViewModel。

提供 ArchiveWorker（QThread 后台线程）和 ArchiveViewModel（状态管理），
支持 URL 归档和纯文本归档两种模式。

归档流程与 src/mcp/tools.py 的 archive_url / archive_text 保持一致：
- URL 模式: validate_url_security → WorkflowEngine.execute_async("archive-url")
- 文本模式: validate_text_length → TextFallbackProcessor.process() → WorkflowEngine.execute_async("archive-text")
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Dict, Literal, Optional

from PySide6.QtCore import QObject, QThread, Signal

from src.mcp.utils import validate_text_length, validate_url_security_result
from src.runtime.errors import ErrorCode, PKVRuntimeError

logger = logging.getLogger("pkv.gui.viewmodels.archive")

_PUBLIC_OPERATION_ID = re.compile(r"^[0-9a-f]{32}$")
_ERROR_CODE_VALUES = frozenset(code.value for code in ErrorCode)
_WORKFLOW_TERMINALS = frozenset({"success", "degraded", "error"})
_COMPLETED_STORAGE_STATUSES = frozenset({"ready", "degraded"})
_FATAL_STORAGE_STATUSES = frozenset({"repair_required", "rejected"})
_PUBLIC_STORAGE_STATUSES = (
    _COMPLETED_STORAGE_STATUSES | _FATAL_STORAGE_STATUSES | {"error"}
)
_PUBLIC_ARCHIVE_STAGES = frozenset({
    "ai_analyze",
    "archive_text",
    "archive_url",
    "completed",
    "compensating",
    "dispatch",
    "fetch_content",
    "idea_sharpen",
    "index_commit",
    "index_committed",
    "input_validation",
    "network_policy",
    "operation_journal",
    "preparing",
    "primary_committed",
    "provider_configuration",
    "provider_request",
    "provider_stream",
    "review_entry",
    "store_entry",
    "url_preflight",
    "vector_committed",
    "worker",
    "workflow",
    "workflow_analyze",
    "workflow_condition",
    "workflow_configuration",
    "workflow_fetch",
    "workflow_local_file_capability",
    "workflow_processor_selection",
    "workflow_result",
    "workflow_review",
    "workflow_review_editor",
    "workflow_step",
    "workflow_terminal",
})
_PUBLIC_ARCHIVE_STEP_IDS = frozenset({
    "ai_analyze",
    "fetch_content",
    "idea_sharpen",
    "review_entry",
    "store_entry",
})
_PUBLIC_CAUSE_TYPES = frozenset({
    "ConnectionError",
    "Exception",
    "OSError",
    "PKVRuntimeError",
    "RuntimeError",
    "TimeoutError",
    "TypeError",
    "ValueError",
})
_ARCHIVE_REPAIR_ACTIONS = frozenset({
    "audit_delete_commit_state",
    "audit_entry_consistency",
    "audit_missing_primary_file",
    "audit_published_markdown",
    "audit_sqlite_commit_state",
    "purge_committed_quarantine",
    "rebuild_index",
    "rebuild_vector_index",
    "rebuild_vectors_for_entry",
    "remove_or_reindex_orphan_markdown",
    "remove_stale_vectors_for_entry",
    "repair_operation_journal",
    "repair_secondary_indexes",
    "restore_quarantined_markdown",
})
_SAFE_STATIC_FAILURE_MESSAGES = frozenset({
    "URL 不能为空",
    "URL 格式无效，请检查后重试",
    "该 URL 目标不允许访问",
    "URL 目标解析失败，请稍后重试",
    "文本内容不能为空",
    "文本内容无效，请检查后重试",
    "归档失败（错误代码：workflow_config_invalid，阶段：dispatch）",
})


def _safe_stage(value: Any, fallback: str = "workflow") -> str:
    """Project an untrusted stage onto the release diagnostic vocabulary."""

    safe_fallback = fallback if fallback in _PUBLIC_ARCHIVE_STAGES else "workflow"
    if type(value) is str and value in _PUBLIC_ARCHIVE_STAGES:
        return value
    return safe_fallback


def _safe_step_id(value: Any) -> str:
    if type(value) is str and value in _PUBLIC_ARCHIVE_STEP_IDS:
        return value
    return "unknown_step"


def _safe_cause_type(value: Any) -> str:
    if type(value) is str and value in _PUBLIC_CAUSE_TYPES:
        return value
    return "Exception"


def _exception_cause_type(exc: BaseException) -> str:
    if isinstance(exc, PKVRuntimeError):
        return "PKVRuntimeError"
    for exception_type, public_name in (
        (TimeoutError, "TimeoutError"),
        (ConnectionError, "ConnectionError"),
        (OSError, "OSError"),
        (ValueError, "ValueError"),
        (TypeError, "TypeError"),
        (RuntimeError, "RuntimeError"),
    ):
        if isinstance(exc, exception_type):
            return public_name
    return "Exception"


def _safe_operation_id(value: Any) -> str:
    if type(value) is not str:
        return ""
    return value if _PUBLIC_OPERATION_ID.fullmatch(value) else ""


def _safe_storage_status(value: Any) -> str:
    if type(value) is not str:
        return "error"
    return value if value in _PUBLIC_STORAGE_STATUSES else "error"


def _safe_repair_actions(value: Any) -> list[str]:
    if type(value) not in {list, tuple}:
        return []
    return list(dict.fromkeys(
        item
        for item in value
        if type(item) is str and item in _ARCHIVE_REPAIR_ACTIONS
    ))


def _code_value(code: Any) -> str:
    """Normalize enum/string codes for Qt signal payloads."""

    if type(code) is ErrorCode:
        normalized = code.value
    elif type(code) is str:
        normalized = code
    else:
        normalized = ErrorCode.WORKFLOW_STEP_FAILED.value
    return (
        normalized
        if normalized in _ERROR_CODE_VALUES
        else ErrorCode.WORKFLOW_STEP_FAILED.value
    )


def _normalise_issue(issue: Any) -> Dict[str, Any]:
    """Keep workflow diagnostics structured without exposing them directly."""

    if type(issue) is dict:
        payload = {
            key: issue[key]
            for key in (
                "code",
                "message",
                "severity",
                "recoverable",
                "stage",
                "step_id",
                "cause_type",
            )
            if key in issue
        }
    else:
        payload = {}
    payload["code"] = _code_value(payload.get("code"))
    raw_severity = payload.get("severity")
    severity = (
        raw_severity
        if type(raw_severity) is str and raw_severity in {"warning", "error"}
        else "error"
    )
    payload["severity"] = severity
    payload["message"] = (
        "归档步骤降级" if severity == "warning" else "归档步骤未能完成"
    )
    payload["recoverable"] = payload.get("recoverable") is True
    if payload.get("stage") is not None:
        payload["stage"] = _safe_stage(payload["stage"])
    if payload.get("step_id") is not None:
        payload["step_id"] = _safe_step_id(payload["step_id"])
    if payload.get("cause_type") is not None:
        payload["cause_type"] = _safe_cause_type(payload["cause_type"])
    return payload


def _workflow_terminal(result: Any) -> Optional[str]:
    """Validate the explicit W2 terminal and its exact boolean projection."""

    try:
        terminal = getattr(result, "terminal", None)
        success = getattr(result, "success", None)
    except Exception:
        return None
    if type(terminal) is not str or terminal not in _WORKFLOW_TERMINALS:
        return None
    if type(success) is not bool or success != (terminal != "error"):
        return None
    if terminal != "error" and not _completion_diagnostics_are_valid(
        result,
        terminal=terminal,
    ):
        return None
    return terminal


def _completion_diagnostics_are_valid(result: Any, *, terminal: str) -> bool:
    """Prevent success/degraded terminals from hiding errors or warnings."""

    return _completion_diagnostics_projection(result, terminal=terminal) is not None


def _completion_diagnostics_projection(
    result: Any,
    *,
    terminal: str,
) -> Optional[tuple[list[str], list[Dict[str, Any]]]]:
    """Capture one exact diagnostics snapshot for a completed result."""

    try:
        errors = getattr(result, "errors", None)
        warnings = getattr(result, "warnings", None)
        issues = getattr(result, "issues", None)
    except Exception:
        return None
    if type(errors) is not list or type(warnings) is not list or type(issues) is not list:
        return None
    error_snapshot = list(errors)
    warning_snapshot = list(warnings)
    issue_snapshot = list(issues)
    if error_snapshot or not all(
        type(warning) is str for warning in warning_snapshot
    ):
        return None
    if not all(type(issue) is dict for issue in issue_snapshot):
        return None
    if any(issue.get("severity") != "warning" for issue in issue_snapshot):
        return None
    if terminal == "success":
        if warning_snapshot or issue_snapshot:
            return None
    elif not warning_snapshot and not issue_snapshot:
        return None
    return warning_snapshot, [dict(issue) for issue in issue_snapshot]


def _workflow_success_data(
    result: Any,
    *,
    terminal: str,
) -> Optional[Dict[str, Any]]:
    """Validate and project the exact public contract for a completed archive."""

    try:
        data = getattr(result, "data", None)
    except Exception:
        return None
    if type(data) is not dict or not all(type(key) is str for key in data):
        return None

    knowledge_id = data.get("knowledge_id")
    if type(knowledge_id) is not int or knowledge_id <= 0:
        return None

    status = data.get("status")
    if type(status) is not str:
        return None
    if terminal == "success" and status != "ready":
        return None
    if terminal == "degraded" and status not in _COMPLETED_STORAGE_STATUSES:
        return None
    if terminal not in {"success", "degraded"}:
        return None

    title = data.get("title")
    file_path = data.get("file_path")
    operation_id = data.get("operation_id")
    core_committed = data.get("core_committed")
    do_not_retry = data.get("do_not_retry")
    repair_actions = data.get("repair_actions")
    if type(repair_actions) is not list:
        return None
    repair_snapshot = list(repair_actions)
    if (
        type(title) is not str
        or type(file_path) is not str
        or not file_path
        or type(operation_id) is not str
        or not _PUBLIC_OPERATION_ID.fullmatch(operation_id)
        or core_committed is not True
        or do_not_retry is not True
        or not all(
            type(action) is str and action in _ARCHIVE_REPAIR_ACTIONS
            for action in repair_snapshot
        )
        or len(repair_snapshot) != len(set(repair_snapshot))
        or (status == "ready" and bool(repair_snapshot))
        or (status == "degraded" and not repair_snapshot)
    ):
        return None

    # Never forward Workflow state wholesale.  It also contains the original
    # URL/text, Entry objects and other internal values that are not part of the
    # GUI completion boundary.
    return {
        "knowledge_id": knowledge_id,
        "title": title,
        "file_path": file_path,
        "status": status,
        "operation_id": operation_id,
        "core_committed": True,
        "do_not_retry": True,
        "repair_actions": repair_snapshot,
    }


def _workflow_fatal_storage_status(result: Any) -> Optional[str]:
    """Return an explicit fatal W1 storage terminal without accepting mappings."""

    try:
        data = getattr(result, "data", None)
    except Exception:
        return None
    if type(data) is not dict:
        return None
    status = data.get("status")
    if type(status) is str and status in _FATAL_STORAGE_STATUSES:
        return status
    return None


def _success_payload(
    result: Any,
    *,
    terminal: str,
    data: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Attach workflow terminal metadata to the stored-entry result."""

    if terminal not in {"success", "degraded"}:
        raise ValueError("success payload requires an explicit completed terminal")
    diagnostics = _completion_diagnostics_projection(result, terminal=terminal)
    if diagnostics is None:
        return None
    raw_warnings, raw_issues = diagnostics
    payload = dict(data)
    payload["workflow_terminal"] = terminal
    payload["workflow_warnings"] = (
        ["工作流存在降级警告"] if raw_warnings else []
    )
    payload["workflow_issues"] = [
        _normalise_issue(issue) for issue in raw_issues
    ]
    return payload


def _invalid_workflow_contract_payload(*, stage: str) -> Dict[str, Any]:
    """Build a stable adapter failure for a malformed WorkflowResult."""

    return _failure_payload_from_exception(
        PKVRuntimeError(
            ErrorCode.WORKFLOW_STEP_FAILED,
            "工作流返回不符合适配器合同",
            stage=stage,
            recoverable=False,
        ),
        stage=stage,
    )


def _failure_payload_from_result(result: Any) -> Dict[str, Any]:
    """Build a structured fatal result while keeping the user message sanitized."""

    raw_data = getattr(result, "data", None)
    data = dict(raw_data) if type(raw_data) is dict else {}
    raw_issues = getattr(result, "issues", None)
    issues = [
        _normalise_issue(issue)
        for issue in (raw_issues if type(raw_issues) is list else [])
    ]
    core_committed = data.get("core_committed") is True
    do_not_retry = (
        data.get("do_not_retry") is True
        or core_committed
        or data.get("status") == "repair_required"
    )

    primary_issue = next(
        (issue for issue in issues if issue.get("severity") == "error"),
        issues[0] if issues else None,
    )
    if primary_issue is not None:
        code = primary_issue["code"]
        stage = _safe_stage(
            primary_issue.get("stage") or data.get("stage"),
            "workflow",
        )
        recoverable = primary_issue.get("recoverable") is True and not do_not_retry
    else:
        code = (
            ErrorCode.STORAGE_REPAIR_REQUIRED.value
            if do_not_retry
            else ErrorCode.WORKFLOW_STEP_FAILED.value
        )
        stage = _safe_stage(data.get("stage"), "workflow")
        recoverable = False

    if do_not_retry:
        # W1 repair_required 是更具体且具操作意义的 fatal，不得被前置 warning
        # 或工作流合成错误覆盖成普通 workflow_step_failed。
        code = ErrorCode.STORAGE_REPAIR_REQUIRED.value

    repair_actions = _safe_repair_actions(data.get("repair_actions"))
    operation_id = _safe_operation_id(data.get("operation_id"))
    status = _safe_storage_status(data.get("status"))
    if do_not_retry:
        repair_text = ", ".join(repair_actions) or "查看诊断日志"
        operation_text = f", operation_id={operation_id}" if operation_id else ""
        safe_message = (
            "归档未能安全完成：核心存储已提交或需要修复"
            f"（status={status}{operation_text}, repair={repair_text}）。请勿盲目重试！"
        )
    else:
        safe_message = f"归档失败（错误代码：{code}，阶段：{stage}）"

    return {
        "terminal": "error",
        "code": code,
        "stage": stage,
        "recoverable": recoverable,
        "safe_message": safe_message,
        "issues": issues,
        "status": status,
        "operation_id": operation_id,
        "core_committed": core_committed,
        "do_not_retry": do_not_retry,
        "repair_actions": repair_actions,
    }


def _failure_payload_from_exception(exc: BaseException, *, stage: str) -> Dict[str, Any]:
    """Preserve machine diagnostics while exposing only a stable safe message."""

    if isinstance(exc, PKVRuntimeError):
        code = exc.code.value
        resolved_stage = _safe_stage(exc.stage, stage)
        recoverable = exc.recoverable is True
    else:
        code = ErrorCode.WORKFLOW_STEP_FAILED.value
        resolved_stage = _safe_stage(stage)
        recoverable = False
    issue = {
        "code": code,
        "message": "归档步骤未能完成",
        "severity": "error",
        "recoverable": recoverable,
        "stage": resolved_stage,
        "cause_type": _exception_cause_type(exc),
    }
    return {
        "terminal": "error",
        "code": code,
        "stage": resolved_stage,
        "recoverable": recoverable,
        "safe_message": f"归档失败（错误代码：{code}，阶段：{resolved_stage}）",
        "issues": [issue],
        "status": "error",
        "operation_id": "",
        "core_committed": False,
        "do_not_retry": False,
        "repair_actions": [],
    }


def sanitize_archive_failure(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Enforce the public failure schema at every Qt signal boundary."""

    if type(payload) is not dict:
        payload = {}
    code = _code_value(payload.get("code"))
    stage = _safe_stage(payload.get("stage"), "workflow")
    recoverable = payload.get("recoverable") is True
    core_committed = payload.get("core_committed") is True
    status = _safe_storage_status(payload.get("status"))
    do_not_retry = (
        payload.get("do_not_retry") is True
        or core_committed
        or status == "repair_required"
    )
    if do_not_retry:
        recoverable = False
    operation_id = _safe_operation_id(payload.get("operation_id"))
    repair_actions = _safe_repair_actions(payload.get("repair_actions"))
    raw_issues = payload.get("issues")
    issues = [
        _normalise_issue(issue)
        for issue in (raw_issues if type(raw_issues) is list else [])
    ]

    if do_not_retry:
        repair_text = ", ".join(repair_actions) or "查看诊断日志"
        operation_text = f", operation_id={operation_id}" if operation_id else ""
        safe_message = (
            "归档未能安全完成：核心存储已提交或需要修复"
            f"（status={status}{operation_text}, repair={repair_text}）。请勿盲目重试！"
        )
    else:
        raw_message = payload.get("safe_message")
        requested_message = raw_message if type(raw_message) is str else ""
        safe_message = (
            requested_message
            if requested_message in _SAFE_STATIC_FAILURE_MESSAGES
            else f"归档失败（错误代码：{code}，阶段：{stage}）"
        )

    return {
        "terminal": "error",
        "code": code,
        "stage": stage,
        "recoverable": recoverable,
        "safe_message": safe_message,
        "issues": issues,
        "status": status,
        "operation_id": operation_id,
        "core_committed": core_committed,
        "do_not_retry": do_not_retry,
        "repair_actions": repair_actions,
    }


# ============================================================
# ArchiveWorker — 后台归档线程
# ============================================================


class ArchiveWorker(QThread):
    """后台归档工作线程。

    在独立线程中运行 asyncio 事件循环，执行归档工作流，
    避免阻塞 GUI 主线程。

    Signals:
        progress_text: 进度文本消息。
        finished_ok: 归档成功/降级，携带工作流终态与结果数据。
        finished_failure: 归档失败，携带结构化错误数据。
        finished_err: 兼容既有调用方的脱敏错误消息。
    """

    progress_text = Signal(str)
    finished_ok = Signal(dict)
    finished_failure = Signal(dict)
    finished_err = Signal(str)

    def __init__(
        self,
        mode: Literal["url", "text"],
        data: Dict[str, Any],
        parent: Optional[QObject] = None,
    ) -> None:
        """初始化归档工作线程。

        Args:
            mode: 归档模式，"url" 或 "text"。
            data: 归档数据字典。
                - URL 模式: {"url": "https://..."}
                - 文本模式: {"text": "...", "title": "..."}
            parent: Qt 父对象。
        """
        super().__init__(parent)
        self._mode = mode
        self._data = data

    def run(self) -> None:
        """线程入口：创建独立事件循环并执行异步归档。"""
        try:
            asyncio.run(self._execute())
        except Exception as exc:
            logger.error("ArchiveWorker 异常: type=%s", type(exc).__name__)
            self._emit_failure(_failure_payload_from_exception(exc, stage="worker"))

    def _emit_failure(self, payload: Dict[str, Any]) -> None:
        """Publish structured failure plus a legacy sanitized string signal."""

        safe_payload = sanitize_archive_failure(payload)
        self.finished_failure.emit(safe_payload)
        self.finished_err.emit(safe_payload["safe_message"])

    async def _execute(self) -> None:
        """异步执行归档工作流。

        根据 mode 分发到对应的归档逻辑：
        - "url": URL 安全验证 → archive-url 工作流
        - "text": 文本长度验证 → TextFallbackProcessor → archive-text 工作流
        """
        if self._mode == "url":
            await self._execute_url()
        elif self._mode == "text":
            await self._execute_text()
        else:
            self._emit_failure({
                "terminal": "error",
                "code": ErrorCode.WORKFLOW_CONFIG_INVALID.value,
                "stage": "dispatch",
                "recoverable": False,
                "safe_message": "归档失败（错误代码：workflow_config_invalid，阶段：dispatch）",
                "issues": [],
                "status": "error",
                "operation_id": "",
                "core_committed": False,
                "do_not_retry": False,
                "repair_actions": [],
            })

    async def _execute_url(self) -> None:
        """执行 URL 归档（与 src/mcp/tools.py archive_url 一致）。"""
        url = self._data.get("url", "").strip()
        self.progress_text.emit("正在验证 URL...")

        # 前置安全验证
        url_failure = validate_url_security_result(url)
        if url_failure is not None:
            payload = _failure_payload_from_exception(
                url_failure,
                stage="url_preflight",
            )
            if payload["code"] == ErrorCode.URL_INVALID.value:
                payload["safe_message"] = "URL 格式无效，请检查后重试"
            elif payload["code"] == ErrorCode.SSRF_TARGET_FORBIDDEN.value:
                payload["safe_message"] = "该 URL 目标不允许访问"
            elif payload["code"] == ErrorCode.SSRF_RESOLUTION_FAILED.value:
                payload["safe_message"] = "URL 目标解析失败，请稍后重试"
            self._emit_failure(payload)
            return

        try:
            self.progress_text.emit("正在抓取网页内容...")
            from src.workflow.engine import WorkflowEngine

            engine = WorkflowEngine()
            self.progress_text.emit("正在执行归档工作流（AI 分析中）...")
            result = await engine.execute_async(
                "archive-url",
                {
                    "url": url,
                    "skip_sharpen": True,
                    "skip_review": True,
                },
            )

            terminal = _workflow_terminal(result)
            if terminal is None:
                logger.error("URL 归档协议异常: code=invalid_workflow_terminal")
                self._emit_failure(
                    _invalid_workflow_contract_payload(stage="workflow_terminal")
                )
                return
            if terminal in {"success", "degraded"}:
                if _workflow_fatal_storage_status(result) is not None:
                    payload = _failure_payload_from_result(result)
                    logger.error(
                        "URL 归档返回 fatal 存储状态: code=%s, stage=%s",
                        payload["code"],
                        payload["stage"],
                    )
                    self._emit_failure(payload)
                    return
                data = _workflow_success_data(result, terminal=terminal)
                if data is None:
                    logger.error("URL 归档结果数据异常: code=invalid_completed_data")
                    self._emit_failure(
                        _invalid_workflow_contract_payload(stage="workflow_result")
                    )
                    return
                payload = _success_payload(result, terminal=terminal, data=data)
                if payload is None:
                    logger.error("URL 归档完成诊断异常: code=invalid_diagnostics")
                    self._emit_failure(
                        _invalid_workflow_contract_payload(stage="workflow_result")
                    )
                    return
                logger.info(
                    "URL 归档完成: terminal=%s, kid=%s",
                    terminal,
                    payload.get("knowledge_id", ""),
                )
                self.progress_text.emit(
                    "归档完成（有警告）" if terminal == "degraded" else "归档完成!"
                )
                self.finished_ok.emit(payload)
            else:
                payload = _failure_payload_from_result(result)
                logger.warning(
                    "URL 归档失败: code=%s, stage=%s",
                    payload["code"],
                    payload["stage"],
                )
                self._emit_failure(payload)
        except Exception as exc:
            logger.error("URL 归档异常: type=%s", type(exc).__name__)
            self._emit_failure(_failure_payload_from_exception(exc, stage="archive_url"))

    async def _execute_text(self) -> None:
        """执行纯文本归档（与 src/mcp/tools.py archive_text 一致）。"""
        text = self._data.get("text", "")
        title = self._data.get("title", "").strip()
        self.progress_text.emit("正在验证文本...")

        # 前置安全验证：文本长度
        valid, _error = validate_text_length(text)
        if not valid:
            self._emit_failure({
                "terminal": "error",
                "code": ErrorCode.WORKFLOW_STEP_FAILED.value,
                "stage": "input_validation",
                "recoverable": True,
                "safe_message": "文本内容无效，请检查后重试",
                "issues": [{
                    "code": ErrorCode.WORKFLOW_STEP_FAILED.value,
                    "message": "文本内容无效",
                    "severity": "error",
                    "recoverable": True,
                    "stage": "input_validation",
                }],
                "status": "error",
                "operation_id": "",
                "core_committed": False,
                "do_not_retry": False,
                "repair_actions": [],
            })
            return

        try:
            # 步骤 1: 用 TextFallbackProcessor 解析文本，获得 Entry 对象
            self.progress_text.emit("正在解析文本内容...")
            from src.processors.text_fallback_processor import TextFallbackProcessor

            processor = TextFallbackProcessor()
            # GUI "纯文本"输入永远按字面内容处理；即使单行文本恰好等于
            # 本地路径，也不得触发文件探测或读取。
            entry = await processor.process_text(text)

            # 如果提供了 title，覆盖自动提取的标题
            if title and title.strip():
                entry.title = title.strip()

            # 步骤 2: 将 Entry 注入工作流上下文，执行 ai_analyze → store_entry
            self.progress_text.emit("正在执行归档工作流（AI 分析中）...")
            from src.workflow.engine import WorkflowEngine

            engine = WorkflowEngine()
            result = await engine.execute_async(
                "archive-text",
                {
                    "text": text,
                    "title": entry.title,
                    "entry": entry,
                    "content": entry.content,
                    "skip_sharpen": True,
                    "skip_review": True,
                },
            )

            terminal = _workflow_terminal(result)
            if terminal is None:
                logger.error("文本归档协议异常: code=invalid_workflow_terminal")
                self._emit_failure(
                    _invalid_workflow_contract_payload(stage="workflow_terminal")
                )
                return
            if terminal in {"success", "degraded"}:
                if _workflow_fatal_storage_status(result) is not None:
                    payload = _failure_payload_from_result(result)
                    logger.error(
                        "文本归档返回 fatal 存储状态: code=%s, stage=%s",
                        payload["code"],
                        payload["stage"],
                    )
                    self._emit_failure(payload)
                    return
                data = _workflow_success_data(result, terminal=terminal)
                if data is None:
                    logger.error("文本归档结果数据异常: code=invalid_completed_data")
                    self._emit_failure(
                        _invalid_workflow_contract_payload(stage="workflow_result")
                    )
                    return
                payload = _success_payload(result, terminal=terminal, data=data)
                if payload is None:
                    logger.error("文本归档完成诊断异常: code=invalid_diagnostics")
                    self._emit_failure(
                        _invalid_workflow_contract_payload(stage="workflow_result")
                    )
                    return
                logger.info(
                    "文本归档完成: terminal=%s, kid=%s",
                    terminal,
                    payload.get("knowledge_id", ""),
                )
                self.progress_text.emit(
                    "归档完成（有警告）" if terminal == "degraded" else "归档完成!"
                )
                self.finished_ok.emit(payload)
            else:
                payload = _failure_payload_from_result(result)
                logger.warning(
                    "文本归档失败: code=%s, stage=%s",
                    payload["code"],
                    payload["stage"],
                )
                self._emit_failure(payload)
        except Exception as exc:
            logger.error("文本归档异常: type=%s", type(exc).__name__)
            self._emit_failure(_failure_payload_from_exception(exc, stage="archive_text"))


# ============================================================
# ArchiveViewModel — 归档状态管理
# ============================================================


class ArchiveViewModel(QObject):
    """归档操作的 ViewModel，管理 ArchiveWorker 生命周期。

    提供统一的归档接口（URL / 纯文本），通过信号通知视图层
    状态变化、进度更新、结果数据和错误信息。

    状态机:
        idle → running → success | degraded | error → idle（下次归档时重置）

    Signals:
        state_changed: 状态变更通知（idle/running/success/degraded/error）。
        progress_text: 进度文本消息。
        result_ready: 归档成功/降级，携带结果和工作流终态。
        failure_ready: 归档失败，携带结构化错误信息。
        error_occurred: 兼容既有调用方的脱敏错误字符串。
    """

    state_changed = Signal(str)
    progress_text = Signal(str)
    result_ready = Signal(dict)
    failure_ready = Signal(dict)
    error_occurred = Signal(str)

    def __init__(self, parent: Optional[QObject] = None) -> None:
        """初始化 ArchiveViewModel。

        Args:
            parent: Qt 父对象。
        """
        super().__init__(parent)
        self._worker: Optional[ArchiveWorker] = None

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    def archive_url(self, url: str) -> None:
        """归档 URL。

        验证 URL 非空后启动后台工作线程。

        Args:
            url: 要归档的网页链接。
        """
        url = url.strip() if url else ""
        if not url:
            self._publish_failure({
                "terminal": "error",
                "code": ErrorCode.URL_INVALID.value,
                "stage": "input_validation",
                "recoverable": True,
                "safe_message": "URL 不能为空",
                "issues": [],
                "status": "error",
                "operation_id": "",
                "core_committed": False,
                "do_not_retry": False,
                "repair_actions": [],
            })
            return

        self._start_worker("url", {"url": url})

    def archive_text(self, text: str, title: str = "") -> None:
        """归档纯文本。

        验证文本非空后启动后台工作线程。

        Args:
            text: 要归档的文本内容。
            title: 可选标题（不提供则自动提取）。
        """
        if not text or not text.strip():
            self._publish_failure({
                "terminal": "error",
                "code": ErrorCode.WORKFLOW_STEP_FAILED.value,
                "stage": "input_validation",
                "recoverable": True,
                "safe_message": "文本内容不能为空",
                "issues": [],
                "status": "error",
                "operation_id": "",
                "core_committed": False,
                "do_not_retry": False,
                "repair_actions": [],
            })
            return

        self._start_worker("text", {"text": text, "title": title})

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    def _start_worker(self, mode: Literal["url", "text"], data: Dict[str, Any]) -> None:
        """创建并启动归档工作线程。

        如果已有工作线程在运行，则忽略新请求。

        注意：isRunning() 检查和线程启动之间存在理论上的竞态窗口，
        但由于本方法只在 Qt 主线程中调用（通过 UI 交互触发），
        不会有并发调用，因此是安全的。

        Args:
            mode: 归档模式（"url" 或 "text"）。
            data: 归档数据字典。
        """
        if self._worker is not None and self._worker.isRunning():
            logger.warning("归档工作线程正在运行，忽略新请求")
            return

        self.state_changed.emit("running")

        self._worker = ArchiveWorker(mode, data, parent=self)
        self._worker.progress_text.connect(self.progress_text)
        self._worker.finished_ok.connect(self._on_worker_ok)
        self._worker.finished_failure.connect(self._on_worker_failure)
        self._worker.finished.connect(self._on_worker_finished)
        self._worker.start()

        logger.info(f"归档工作线程已启动: mode={mode}")

    def _on_worker_ok(self, data: Dict[str, Any]) -> None:
        """处理归档成功。

        Args:
            data: 工作流返回的结果数据。
        """
        self.result_ready.emit(data)
        terminal = data.get("workflow_terminal")
        storage_degraded = data.get("status") == "degraded"
        self.state_changed.emit(
            "degraded" if terminal == "degraded" or storage_degraded else "success"
        )

    def _on_worker_failure(self, payload: Dict[str, Any]) -> None:
        """处理结构化归档失败。"""

        self._publish_failure(payload)

    def _on_worker_err(self, msg: str) -> None:
        """兼容旧测试/调用方，将字符串失败收敛为结构化错误。"""

        logger.warning("收到 legacy archive error signal")
        self._publish_failure({
            "terminal": "error",
            "code": ErrorCode.WORKFLOW_STEP_FAILED.value,
            "stage": "workflow",
            "recoverable": False,
            "safe_message": "归档失败（错误代码：workflow_step_failed，阶段：workflow）",
            "issues": [{
                "code": ErrorCode.WORKFLOW_STEP_FAILED.value,
                "message": "归档步骤未能完成",
                "severity": "error",
                "recoverable": False,
                "stage": "workflow",
            }],
            "status": "error",
            "operation_id": "",
            "core_committed": False,
            "do_not_retry": False,
            "repair_actions": [],
        })

    def _publish_failure(self, payload: Dict[str, Any]) -> None:
        """Emit structured + legacy failure exactly once and enter error state."""

        safe_payload = sanitize_archive_failure(payload)
        self.failure_ready.emit(safe_payload)
        self.error_occurred.emit(safe_payload["safe_message"])
        self.state_changed.emit("error")

    def _on_worker_finished(self) -> None:
        """工作线程结束后清理引用和资源。"""
        if self._worker is not None:
            self._worker.wait()        # 等待线程完全退出
            self._worker.deleteLater()  # 调度 Qt 对象销毁
        self._worker = None
    "fetch_content",
    "idea_sharpen",
