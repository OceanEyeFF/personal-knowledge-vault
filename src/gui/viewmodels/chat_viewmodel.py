"""AI 对话 ViewModel (M12)

提供 AI 对话功能的 ViewModel 层，使用 qasync 实现异步流式输出。

核心特性：
- 流式输出：使用 OpenAI SDK 的 stream=True，实时发射 token
- Token 统计：stream_options={"include_usage": True} 获取精确统计
- 会话管理：多会话切换、新建、归档
- 数据持久化：自动保存到 SQLiteStore
"""

from __future__ import annotations

import asyncio
import copy
from dataclasses import dataclass
import logging
import re
from types import MappingProxyType
import uuid
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Optional

from PySide6.QtCore import QObject, Signal
from qasync import asyncSlot

from src.ai.chat_provider import (
    ChatProvider,
    ChatStream,
    create_chat_provider,
    is_strict_chat_stream_event,
    is_supported_chat_finish_reason,
)
from src.ai.provider_factory import (
    ChatProviderSettings,
    chat_settings_from_config,
)
from src.mcp.utils import validate_url_security_result
from src.processors.safe_fetch import describe_url_target
from src.relations.citations import sanitize_public_source_url
from src.runtime.errors import ErrorCode, PKVRuntimeError
from src.storage.sqlite_store import SQLiteStore
from src.utils.config import Config

logger = logging.getLogger("pkv.gui.viewmodels.chat")

_RESOURCE_CLOSE_TIMEOUT_SECONDS = 0.25
_URL_ARCHIVE_WORKFLOW_RESULT_ERROR = (
    "归档失败（错误代码：workflow_step_failed，阶段：workflow_result）"
)
_ERROR_CODE_VALUES = frozenset(code.value for code in ErrorCode)
_URL_ARCHIVE_PUBLIC_STAGES = frozenset({
    "workflow",
    "workflow_step",
    "workflow_fetch",
    "workflow_analyze",
    "workflow_review",
    "fetch",
    "analyze",
    "review",
    "network_policy",
    "storage",
    "archive_url",
    "url_preflight",
    "workflow_result",
})
_URL_ARCHIVE_PUBLIC_STATUSES = frozenset({
    "ready",
    "degraded",
    "repair_required",
    "rejected",
    "deleted",
    "error",
})
_URL_ARCHIVE_REPAIR_ACTIONS = frozenset({
    "repair_operation_journal",
    "audit_published_markdown",
    "remove_or_reindex_orphan_markdown",
    "audit_sqlite_commit_state",
    "audit_entry_consistency",
    "purge_committed_quarantine",
    "rebuild_index",
    "rebuild_vector_index",
    "rebuild_vectors_for_entry",
    "remove_stale_vectors_for_entry",
    "repair_secondary_indexes",
    "restore_quarantined_markdown",
    "audit_delete_commit_state",
    "audit_missing_primary_file",
})
_STORAGE_OPERATION_ID = re.compile(r"^[0-9a-f]{32}$")
_CHAT_MESSAGE_ROLES = frozenset({"system", "user", "assistant"})
_URL_ARCHIVE_ENTRY_FIELDS = frozenset({
    "knowledge_id",
    "title",
    "content",
    "summary_one_sentence",
    "summary_100_words",
    "keywords",
    "tags",
    "outline",
    "source_type",
    "source_url",
    "search_strategy",
    "file_path",
    "word_count",
    "event_time",
    "published_at",
    "archived_at",
    "updated_at",
})
_URL_ARCHIVE_ENTRY_TEXT_FIELDS = _URL_ARCHIVE_ENTRY_FIELDS - frozenset({
    "knowledge_id",
    "word_count",
})
_SESSION_LIST_FIELDS = frozenset({
    "session_id",
    "title",
    "created_at",
    "updated_at",
    "total_tokens",
    "round_count",
    "is_archived",
    "knowledge_id",
    "summary",
})
_ARCHIVE_SUCCESS_PAYLOAD_FIELDS = frozenset({
    "knowledge_id",
    "title",
    "file_path",
    "status",
    "operation_id",
    "core_committed",
    "do_not_retry",
    "repair_actions",
    "workflow_terminal",
    "workflow_warnings",
    "workflow_issues",
})
_ARCHIVE_SUCCESS_ISSUE_FIELDS = frozenset({
    "code",
    "message",
    "severity",
    "recoverable",
    "stage",
    "step_id",
    "cause_type",
})


@dataclass(frozen=True)
class ChatTurnCheckpoint:
    """Immutable state preceding all UI-side preparation for one turn."""

    session_id: str
    messages: tuple[Mapping[str, Any], ...]
    total_tokens: int
    round_count: int


@dataclass
class _ChatRequest:
    """All mutable execution state for one immutable chat turn snapshot."""

    request_id: str
    session_id: str
    user_message: str
    settings: ChatProviderSettings
    pre_messages: tuple[Mapping[str, Any], ...]
    request_messages: tuple[Mapping[str, Any], ...]
    pre_total_tokens: int
    pre_round_count: int
    embedded_url_contexts: tuple[tuple[str, str], ...] = ()
    loop: asyncio.AbstractEventLoop | None = None
    task: asyncio.Task[Any] | None = None
    provider: ChatProvider | None = None
    stream: ChatStream | None = None
    cleanup_task: asyncio.Task[Any] | None = None
    assistant_content: str = ""
    stop_requested: bool = False
    # Monotonic arbitration point: once the durable save returns successfully,
    # neither Stop nor cancellation may rewrite this request to stopped/error.
    committed: bool = False
    terminal_emitted: bool = False
    stream_closed: bool = False
    provider_closed: bool = False


def _freeze_messages(messages: List[dict]) -> tuple[Mapping[str, Any], ...]:
    """Deep-copy and freeze the message snapshot passed to one request."""

    return tuple(
        MappingProxyType(copy.deepcopy(dict(message))) for message in messages
    )


def _is_strict_chat_messages(value: Any) -> bool:
    if type(value) is not list:
        return False
    for message in value:
        if type(message) is not dict:
            return False
        if set(message) != {"role", "content"}:
            return False
        if (
            type(message["role"]) is not str
            or message["role"] not in _CHAT_MESSAGE_ROLES
            or type(message["content"]) is not str
        ):
            return False
    return True


def _is_strict_session_projection(
    value: Any,
    *,
    expected_session_id: str | None = None,
) -> bool:
    if type(value) is not dict:
        return False
    session_id = value.get("session_id")
    if type(session_id) is not str or not session_id:
        return False
    if expected_session_id is not None and session_id != expected_session_id:
        return False
    if not _is_strict_chat_messages(value.get("messages")):
        return False
    for field in ("total_tokens", "round_count"):
        counter = value.get(field)
        if type(counter) is not int or counter < 0:
            return False
    return True


def is_strict_session_list_projection(value: Any) -> bool:
    """Validate the exact SQLite session-list projection before UI mutation."""

    if type(value) is not list:
        return False
    for session in value:
        if type(session) is not dict or set(session) != _SESSION_LIST_FIELDS:
            return False
        for field in ("session_id", "title", "created_at", "updated_at"):
            text = session[field]
            if type(text) is not str or not text.strip():
                return False
        for field in ("total_tokens", "round_count"):
            counter = session[field]
            if type(counter) is not int or counter < 0:
                return False
        if type(session["is_archived"]) is not int:
            return False
        if session["is_archived"] not in (0, 1):
            return False
        knowledge_id = session["knowledge_id"]
        if knowledge_id is not None and (
            type(knowledge_id) is not int or knowledge_id <= 0
        ):
            return False
        summary = session["summary"]
        if summary is not None and type(summary) is not str:
            return False
    return True


def _safe_url_archive_code(value: Any) -> str:
    """Project an untrusted workflow issue code onto the public enum."""

    if isinstance(value, ErrorCode):
        return value.value
    if type(value) is str and value in _ERROR_CODE_VALUES:
        return value
    return ErrorCode.WORKFLOW_STEP_FAILED.value


def _safe_url_archive_stage(value: Any) -> str:
    """Return only a known workflow stage, never an arbitrary backend token."""

    if type(value) is str and value in _URL_ARCHIVE_PUBLIC_STAGES:
        return value
    return "workflow"


def _safe_url_archive_status(value: Any) -> str:
    if type(value) is str and value in _URL_ARCHIVE_PUBLIC_STATUSES:
        return value
    return "unknown"


def _safe_url_archive_operation_id(value: Any) -> str:
    if type(value) is str and _STORAGE_OPERATION_ID.fullmatch(value):
        return value
    return ""


def _safe_url_archive_repair_actions(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return list(dict.fromkeys(
        action
        for action in value
        if type(action) is str and action in _URL_ARCHIVE_REPAIR_ACTIONS
    ))


def _url_archive_completion_diagnostics_are_valid(
    result: Any,
    *,
    terminal: str,
) -> bool:
    """Reject completed workflow results that conceal malformed diagnostics."""

    try:
        errors = getattr(result, "errors", None)
        warnings = getattr(result, "warnings", None)
        issues = getattr(result, "issues", None)
    except Exception:
        return False
    if type(errors) is not list or type(warnings) is not list:
        return False
    if type(issues) is not list:
        return False
    error_snapshot = list(errors)
    warning_snapshot = list(warnings)
    if error_snapshot or not all(
        type(warning) is str for warning in warning_snapshot
    ):
        return False
    if not all(type(issue) is dict for issue in issues):
        return False
    issue_snapshot = [dict(issue) for issue in issues]
    if any(issue.get("severity") != "warning" for issue in issue_snapshot):
        return False
    if terminal == "success":
        return not warning_snapshot and not issue_snapshot
    return terminal == "degraded" and bool(warning_snapshot or issue_snapshot)


def _project_url_archive_workflow_completion(
    result: Any,
) -> tuple[str, dict[str, Any]] | None:
    """Project one completed WorkflowResult onto the durable Chat URL contract."""

    try:
        terminal = getattr(result, "terminal", None)
        success = getattr(result, "success", None)
    except Exception:
        return None
    if (
        type(terminal) is not str
        or terminal not in {"success", "degraded"}
        or success is not True
        or not _url_archive_completion_diagnostics_are_valid(
            result,
            terminal=terminal,
        )
    ):
        return None
    try:
        raw_data = getattr(result, "data", None)
    except Exception:
        return None
    if type(raw_data) is not dict or not all(
        type(key) is str for key in raw_data
    ):
        return None

    data = dict(raw_data)
    knowledge_id = data.get("knowledge_id")
    status = data.get("status")
    operation_id = data.get("operation_id")
    repair_actions = data.get("repair_actions")
    if (
        type(knowledge_id) is not int
        or knowledge_id <= 0
        or type(status) is not str
        or (terminal == "success" and status != "ready")
        or (
            terminal == "degraded"
            and status not in {"ready", "degraded"}
        )
        or type(operation_id) is not str
        or not _STORAGE_OPERATION_ID.fullmatch(operation_id)
        or data.get("core_committed") is not True
        or data.get("do_not_retry") is not True
        or type(repair_actions) is not list
    ):
        return None
    repair_snapshot = list(repair_actions)
    if (
        not all(
            type(action) is str and action in _URL_ARCHIVE_REPAIR_ACTIONS
            for action in repair_snapshot
        )
        or len(repair_snapshot) != len(set(repair_snapshot))
        or (status == "ready" and bool(repair_snapshot))
        or (status == "degraded" and not repair_snapshot)
    ):
        return None
    return terminal, {
        "knowledge_id": knowledge_id,
        "status": status,
        "operation_id": operation_id,
        "core_committed": True,
        "do_not_retry": True,
        "repair_actions": repair_snapshot,
    }


def _is_strict_archive_success_issue(value: Any) -> bool:
    if (
        type(value) is not dict
        or not {"code", "message", "severity", "recoverable"}.issubset(value)
        or not set(value).issubset(_ARCHIVE_SUCCESS_ISSUE_FIELDS)
        or type(value["code"]) is not str
        or value["code"] not in _ERROR_CODE_VALUES
        or type(value["message"]) is not str
        or value["message"] != "归档步骤降级"
        or value["severity"] != "warning"
        or type(value["recoverable"]) is not bool
    ):
        return False
    return all(
        field not in value or type(value[field]) is str
        for field in ("stage", "step_id", "cause_type")
    )


def _project_archive_success_payload(value: Any) -> dict[str, Any] | None:
    """Revalidate ArchiveWorker's flattened Qt success payload."""

    if type(value) is not dict or set(value) != _ARCHIVE_SUCCESS_PAYLOAD_FIELDS:
        return None
    terminal = value["workflow_terminal"]
    status = value["status"]
    knowledge_id = value["knowledge_id"]
    operation_id = value["operation_id"]
    repairs = value["repair_actions"]
    warnings = value["workflow_warnings"]
    issues = value["workflow_issues"]
    if (
        type(terminal) is not str
        or terminal not in {"success", "degraded"}
        or type(status) is not str
        or (terminal == "success" and status != "ready")
        or (terminal == "degraded" and status not in {"ready", "degraded"})
        or type(knowledge_id) is not int
        or knowledge_id <= 0
        or type(value["title"]) is not str
        or type(value["file_path"]) is not str
        or not value["file_path"]
        or type(operation_id) is not str
        or not _STORAGE_OPERATION_ID.fullmatch(operation_id)
        or value["core_committed"] is not True
        or value["do_not_retry"] is not True
        or type(repairs) is not list
        or type(warnings) is not list
        or type(issues) is not list
    ):
        return None
    repair_snapshot = list(repairs)
    warning_snapshot = list(warnings)
    if (
        not all(
            type(action) is str and action in _URL_ARCHIVE_REPAIR_ACTIONS
            for action in repair_snapshot
        )
        or len(repair_snapshot) != len(set(repair_snapshot))
        or (status == "ready" and bool(repair_snapshot))
        or (status == "degraded" and not repair_snapshot)
        or not all(
            type(warning) is str
            and warning == "工作流存在降级警告"
            for warning in warning_snapshot
        )
        or len(warning_snapshot) != len(set(warning_snapshot))
        or not all(_is_strict_archive_success_issue(issue) for issue in issues)
        or (terminal == "success" and bool(warning_snapshot or issues))
        or (terminal == "degraded" and not (warning_snapshot or issues))
    ):
        return None
    return {
        "knowledge_id": knowledge_id,
        "title": value["title"],
        "file_path": value["file_path"],
        "status": status,
        "operation_id": operation_id,
        "core_committed": True,
        "do_not_retry": True,
        "repair_actions": repair_snapshot,
        "workflow_terminal": terminal,
        "workflow_warnings": warning_snapshot,
        "workflow_issues": [dict(issue) for issue in issues],
    }


def _project_strict_url_archive_entry(
    value: Any,
    *,
    expected_knowledge_id: int | None = None,
) -> dict[str, Any] | None:
    """Project one SQLite entry row without invoking coercion protocols."""

    if type(value) is not dict or not set(value).issubset(
        _URL_ARCHIVE_ENTRY_FIELDS
    ):
        return None
    knowledge_id = value.get("knowledge_id")
    if type(knowledge_id) is not int or knowledge_id <= 0:
        return None
    if (
        expected_knowledge_id is not None
        and knowledge_id != expected_knowledge_id
    ):
        return None
    if type(value.get("title")) is not str:
        return None
    for field in _URL_ARCHIVE_ENTRY_TEXT_FIELDS:
        if field not in value:
            continue
        item = value[field]
        if item is not None and type(item) is not str:
            return None
    if "word_count" in value:
        word_count = value["word_count"]
        if word_count is not None and (
            type(word_count) is not int or word_count < 0
        ):
            return None
    return dict(value)


class ChatViewModel(QObject):
    """AI 对话 ViewModel

    负责管理 AI 对话会话，处理流式输出，发射 Signal 更新 UI。

    Signals:
        chat_request_started(session_id, request_id): 请求已被原子接纳
        chat_token_received(session_id, request_id, token): 请求归属明确的 token
        chat_request_terminal(...): completed/stopped/error 唯一机器终态
        chat_request_rejected(...): busy/config 等未启动拒绝
        token_received/stream_finished/...: 仅保留当前会话兼容投影
        error_occurred(str): 非 Chat 请求错误
        session_created(str, str): 新会话创建（session_id, title）
        session_loaded(str): 会话加载完成（session_id）
        url_archive_operation_*: 带 origin session/operation 的 URL 归档事件
        url_archive_*: 兼容旧调用方的非归属化事件
    """

    URL_ARCHIVE_REFERENCE_CARD_HTML_KEY = "_pkv_chat_reference_card_html"

    token_received = Signal(str)  # token 内容
    token_usage_updated = Signal(int, int, int)  # input_tokens, output_tokens, total_tokens
    stream_finished = Signal()
    stream_stopped = Signal()
    error_occurred = Signal(str)  # 错误消息
    # W2: Chat 请求事件始终携带其发起会话，避免切换会话后串线。
    chat_request_started = Signal(str, str)  # session_id, request_id
    chat_token_received = Signal(str, str, str)  # session_id, request_id, token
    chat_token_usage_updated = Signal(str, str, int, int, int)
    chat_request_completed = Signal(str, str)  # session_id, request_id
    chat_request_finished = Signal(str, str)  # legacy alias
    chat_request_stopped = Signal(str, str)  # session_id, request_id
    chat_request_failed = Signal(str, str, str, str)
    chat_request_rejected = Signal(str, str, str, str)
    # session_id, request_id, completed|stopped|error, error_code, safe_message
    chat_request_terminal = Signal(str, str, str, str, str)
    session_created = Signal(str, str)  # session_id, title
    session_loaded = Signal(str)  # session_id
    # M12 Phase 3: URL 归档信号
    url_archive_started = Signal(str)
    url_archive_completed = Signal(str, dict)
    url_archive_failed = Signal(str, str)
    url_archive_warning = Signal(str, str)
    # W2: authoritative URL archive signals.  The legacy signals above remain
    # available, but UI mutation must consume these scoped variants only.
    url_archive_operation_started = Signal(str, str, str)
    url_archive_operation_completed = Signal(str, str, str, dict)
    url_archive_operation_failed = Signal(str, str, str, str)
    url_archive_operation_warning = Signal(str, str, str, str)

    # 对话保存到知识库
    session_saved_to_kb = Signal(str, int)  # session_id, knowledge_id
    session_save_to_kb_failed = Signal(str, str)  # session_id, error_msg
    session_save_to_kb_warning = Signal(str, str)  # session_id, safe warning

    def __init__(
        self,
        parent: Optional[QObject] = None,
        *,
        config: Config | None = None,
        store: SQLiteStore | None = None,
        provider_factory: Callable[[ChatProviderSettings], ChatProvider] | None = None,
    ) -> None:
        """初始化 ChatViewModel

        Args:
            parent: Qt 父对象
        """
        super().__init__(parent)

        # 配置
        self.config = config if config is not None else Config()
        self.db_path = self.config.db_path
        self.store = store if store is not None else SQLiteStore(self.db_path)
        self._provider_factory = (
            provider_factory
            if provider_factory is not None
            else create_chat_provider
        )

        # 当前会话状态
        self.current_session_id: Optional[str] = None
        self.current_messages: List[dict] = []
        self.current_total_tokens: int = 0
        self.current_round_count: int = 0

        # 全局只允许一个请求；保留 _stop_flag 兼容旧调用方的只读检查。
        self._stop_flag = False
        self._active_request: _ChatRequest | None = None
        self._latest_attempt_id: str | None = None
        # Same-session URL archive completions are merged at the active turn's
        # durable commit boundary instead of mutating its frozen snapshot.
        self._pending_knowledge_contexts: dict[
            str,
            dict[str, str | None],
        ] = {}
        self._ephemeral_url_context_systems: dict[str, str] = {}
        self._kb_worker: Any | None = None
        # Timed-out close coroutines remain strongly referenced until their
        # cancellation settles; their exceptions are always drained.
        self._detached_cleanup_tasks: set[asyncio.Task[Any]] = set()

        logger.info("ChatViewModel 初始化完成")

    def reload_provider_config(self) -> None:
        """重新加载 LLM Provider 配置，供设置保存后热更新下一次请求。"""
        self.config = Config()
        logger.info("ChatViewModel Provider 配置已重新加载")

    @property
    def is_busy(self) -> bool:
        """Whether a chat request is reserved or running."""

        return self._active_request is not None

    @property
    def active_session_id(self) -> str | None:
        request = self._active_request
        return request.session_id if request is not None else None

    @property
    def active_request_id(self) -> str | None:
        request = self._active_request
        return request.request_id if request is not None else None

    @property
    def latest_attempt_id(self) -> str | None:
        """Identity of the newest accepted or rejected send attempt."""

        return self._latest_attempt_id

    def get_active_turn(self, session_id: str) -> dict[str, str] | None:
        """Return a copy of the provisional turn for session-aware rendering."""

        request = self._active_request
        if request is None or request.session_id != session_id:
            return None
        return {
            "request_id": request.request_id,
            "user": request.user_message,
            "assistant": request.assistant_content,
        }

    def create_new_session(self, title: Optional[str] = None) -> str:
        """创建新会话

        Args:
            title: 会话标题（可选，默认为"新对话"）

        Returns:
            新会话的 session_id
        """
        try:
            session_id = str(uuid.uuid4())
            session_title = title or "新对话"

            # 创建数据库记录
            self.store.create_session(session_id, session_title)

            # 重置当前会话状态
            self.current_session_id = session_id
            self.current_messages = []
            self.current_total_tokens = 0
            self.current_round_count = 0

            logger.info("创建新会话成功")
            self.session_created.emit(session_id, session_title)
            return session_id

        except Exception as e:
            error_msg = "创建会话失败，请检查本地数据库状态"
            logger.error(
                "创建会话失败: error_type=%s",
                type(e).__name__,
            )
            self.error_occurred.emit(error_msg)
            raise

    def load_session(self, session_id: str) -> bool:
        """加载已有会话

        Args:
            session_id: 会话 ID

        Returns:
            是否加载成功
        """
        try:
            if type(session_id) is not str or not session_id:
                raise TypeError("session id contract violation")
            session = self.store.get_session(session_id)
            if session is None:
                error_msg = "会话不存在"
                logger.warning("加载会话失败: session_missing")
                self.error_occurred.emit(error_msg)
                return False
            if not _is_strict_session_projection(
                session,
                expected_session_id=session_id,
            ):
                raise TypeError("session projection contract violation")

            # 恢复会话状态
            self.current_session_id = session_id
            self.current_messages = copy.deepcopy(session["messages"])
            self.current_total_tokens = session["total_tokens"]
            self.current_round_count = session["round_count"]

            logger.info(
                "加载会话成功: rounds=%s tokens=%s",
                self.current_round_count,
                self.current_total_tokens,
            )
            self.session_loaded.emit(session_id)
            return True

        except Exception as e:
            error_msg = "加载会话失败，请检查本地数据库状态"
            logger.error(
                "加载会话失败: error_type=%s",
                type(e).__name__,
            )
            self.error_occurred.emit(error_msg)
            return False

    def list_sessions(
        self,
        is_archived: bool = False,
    ) -> Optional[List[Dict[str, Any]]]:
        """列出会话

        Args:
            is_archived: 是否只显示归档会话

        Returns:
            会话列表；查询或投影失败时返回 None，与真正的空列表区分。
        """
        try:
            if type(is_archived) is not bool:
                raise TypeError("session archive filter contract violation")
            sessions = self.store.list_sessions(is_archived=is_archived)
            if not is_strict_session_list_projection(sessions):
                raise TypeError("session list projection contract violation")
            return [dict(session) for session in sessions]
        except Exception as e:
            error_msg = "列出会话失败，请检查本地数据库状态"
            logger.error(
                "列出会话失败: error_type=%s",
                type(e).__name__,
            )
            self.error_occurred.emit(error_msg)
            return None

    def set_knowledge_context(self, context_text: str) -> None:
        """设置知识引用上下文（作为 system message 注入）

        在消息列表开头插入 system message，作为 AI 回答的参考上下文。
        每次调用会替换已有的 system message。

        Args:
            context_text: 格式化的知识引用上下文
        """
        if type(context_text) is not str or not context_text:
            return

        self.current_messages = self._messages_with_knowledge_context(
            self.current_messages,
            context_text,
        )

        logger.info(
            f"📎 已设置知识引用上下文 "
            f"(~{len(context_text)} chars)"
        )

    @staticmethod
    def _messages_with_knowledge_context(
        messages: List[dict],
        context_text: str,
    ) -> List[dict]:
        """Return an exact message copy with one authoritative system context."""

        if type(context_text) is not str or not context_text:
            raise TypeError("knowledge context contract violation")
        if not _is_strict_chat_messages(messages):
            raise TypeError("chat messages contract violation")
        projected = [
            copy.deepcopy(message)
            for message in messages
            if message["role"] != "system"
        ]
        projected.insert(0, {"role": "system", "content": context_text})
        return projected

    @staticmethod
    def _messages_with_appended_knowledge_contexts(
        messages: List[dict],
        contexts: tuple[str, ...],
    ) -> List[dict]:
        """Append ordered reference contexts without duplicating exact entries."""

        if not _is_strict_chat_messages(messages):
            raise TypeError("chat messages contract violation")
        if type(contexts) is not tuple or not all(
            type(context) is str and bool(context)
            for context in contexts
        ):
            raise TypeError("knowledge context queue contract violation")

        existing_contexts = [
            message["content"]
            for message in messages
            if message["role"] == "system"
        ]
        combined_contexts: list[str] = []
        for context in (*existing_contexts, *contexts):
            if context not in combined_contexts:
                combined_contexts.append(context)
        projected = [
            copy.deepcopy(message)
            for message in messages
            if message["role"] != "system"
        ]
        if combined_contexts:
            projected.insert(
                0,
                {
                    "role": "system",
                    "content": "\n\n".join(combined_contexts),
                },
            )
        return projected

    def _ready_url_archive_contexts(
        self,
        session_id: str,
    ) -> tuple[tuple[str, str], ...]:
        """Return the completed prefix of one session's ordered URL queue."""

        operations = self._pending_knowledge_contexts.get(session_id)
        if not operations:
            return ()
        ready: list[tuple[str, str]] = []
        for operation_id, context in operations.items():
            if context is None:
                break
            if type(operation_id) is not str or type(context) is not str or not context:
                raise TypeError("URL archive context queue contract violation")
            ready.append((operation_id, context))
        return tuple(ready)

    def _consume_url_archive_contexts(
        self,
        session_id: str,
        ready: tuple[tuple[str, str], ...],
    ) -> None:
        """Remove only the exact ordered snapshot applied at a commit boundary."""

        operations = self._pending_knowledge_contexts.get(session_id)
        if operations is None:
            return
        for operation_id, context in ready:
            if operations.get(operation_id) == context:
                operations.pop(operation_id, None)
        self._ephemeral_url_context_systems.pop(session_id, None)
        if not operations:
            self._pending_knowledge_contexts.pop(session_id, None)

    def _apply_ready_url_contexts_to_current(self, session_id: str) -> None:
        """Apply the ready URL prefix when no active turn owns the commit seam."""

        if self.current_session_id != session_id:
            return
        active = self._active_request
        if (
            active is not None
            and active.session_id == session_id
            and not active.committed
            and not active.terminal_emitted
        ):
            return
        ready = self._ready_url_archive_contexts(session_id)
        if not ready:
            return
        projected = self._messages_with_appended_knowledge_contexts(
            self.current_messages,
            tuple(context for _, context in ready),
        )
        system_context = next(
            message["content"]
            for message in projected
            if message["role"] == "system"
        )
        self.set_knowledge_context(system_context)
        if self._current_system_context(self.current_messages) == system_context:
            self._ephemeral_url_context_systems[session_id] = system_context

    @staticmethod
    def _current_system_context(messages: List[dict]) -> str | None:
        if not _is_strict_chat_messages(messages):
            return None
        system_messages = [
            message["content"]
            for message in messages
            if message["role"] == "system"
        ]
        return system_messages[0] if len(system_messages) == 1 else None

    def get_current_messages(self) -> List[dict]:
        """获取当前会话消息

        Returns:
            消息列表（OpenAI 格式）
        """
        return self.current_messages

    def get_token_stats(self) -> Dict[str, int]:
        """获取 Token 统计

        Returns:
            统计字典，包含 total_tokens, round_count
        """
        return {
            "total_tokens": self.current_total_tokens,
            "round_count": self.current_round_count,
        }

    def can_dispatch_message(self, user_message: str) -> bool:
        """Run synchronous admission checks before provisional UI changes."""

        session_id = self.current_session_id or ""
        request_id = str(uuid.uuid4())
        self._latest_attempt_id = request_id
        if self._active_request is not None:
            self._emit_chat_rejection(
                session_id,
                request_id,
                ErrorCode.CHAT_BUSY,
                "已有对话请求正在进行，请先停止或等待完成",
            )
            return False
        if not session_id:
            self._emit_chat_rejection(
                "",
                request_id,
                ErrorCode.CHAT_STATE_CONFLICT,
                "未选择会话，请先创建或加载会话",
            )
            return False
        if not user_message or not user_message.strip():
            self._emit_chat_rejection(
                session_id,
                request_id,
                ErrorCode.CHAT_STATE_CONFLICT,
                "消息不能为空",
            )
            return False
        try:
            chat_settings_from_config(self.config)
        except PKVRuntimeError as exc:
            self._emit_chat_rejection(
                session_id,
                request_id,
                exc.code,
                "LLM Provider 配置无效",
            )
            return False
        except Exception as exc:
            logger.error(
                "读取 Chat Provider 配置失败: error_type=%s",
                type(exc).__name__,
            )
            self._emit_chat_rejection(
                session_id,
                request_id,
                ErrorCode.PROVIDER_CONFIG_INVALID,
                "LLM Provider 配置无效",
            )
            return False
        return True

    def capture_turn_checkpoint(self) -> ChatTurnCheckpoint:
        """Capture state before reference parsing mutates the request context."""

        if not self.current_session_id:
            raise PKVRuntimeError(
                ErrorCode.CHAT_STATE_CONFLICT,
                "未选择会话",
                stage="chat_admission",
                recoverable=True,
            )
        return ChatTurnCheckpoint(
            session_id=self.current_session_id,
            messages=_freeze_messages(self.current_messages),
            total_tokens=self.current_total_tokens,
            round_count=self.current_round_count,
        )

    def restore_turn_checkpoint(self, checkpoint: ChatTurnCheckpoint) -> bool:
        """Rollback synchronous reference preparation before request admission."""

        if (
            not isinstance(checkpoint, ChatTurnCheckpoint)
            or self.current_session_id != checkpoint.session_id
            or self._active_request is not None
        ):
            return False
        self.current_messages = [
            copy.deepcopy(dict(message)) for message in checkpoint.messages
        ]
        self.current_total_tokens = checkpoint.total_tokens
        self.current_round_count = checkpoint.round_count
        return True

    def dispatch_message(
        self,
        user_message: str,
        *,
        checkpoint: ChatTurnCheckpoint | None = None,
    ) -> bool:
        """Synchronously reserve a request and schedule it on the Qt loop.

        The synchronous reservation lets the view decide whether to render a
        provisional user turn.  It also closes the tiny race in which two UI
        clicks could both be queued before the async slot starts.
        """

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._emit_chat_rejection(
                self.current_session_id or "",
                str(uuid.uuid4()),
                ErrorCode.CHAT_STATE_CONFLICT,
                "Chat 需要在应用事件循环中运行",
            )
            return False

        request = self._reserve_request(user_message, checkpoint=checkpoint)
        if request is None:
            return False

        request.loop = loop
        coroutine = self._run_request(request)
        try:
            request.task = loop.create_task(coroutine)
        except Exception as exc:
            coroutine.close()
            logger.error(
                "创建 Chat task 失败: error_type=%s",
                type(exc).__name__,
            )
            self._emit_terminal(
                request,
                "error",
                code=ErrorCode.CHAT_STATE_CONFLICT,
                message="无法启动对话任务，本轮内容已回滚",
            )
            return False
        request.task.add_done_callback(
            lambda task: self._on_dispatched_task_done(request, task)
        )
        return True

    @asyncSlot()
    async def send_message(self, user_message: str) -> bool:
        """Reserve and execute one chat request.

        Direct callers retain the historical async entry point.  The GUI uses
        :meth:`dispatch_message` so reservation happens before provisional UI
        is rendered.
        """

        request = self._reserve_request(user_message)
        if request is None:
            return False
        request.loop = asyncio.get_running_loop()
        request.task = asyncio.current_task()
        return await self._run_request(request)

    def _reserve_request(
        self,
        user_message: str,
        *,
        checkpoint: ChatTurnCheckpoint | None = None,
    ) -> _ChatRequest | None:
        session_id = self.current_session_id or ""
        request_id = str(uuid.uuid4())
        self._latest_attempt_id = request_id
        if self._active_request is not None:
            self._emit_chat_rejection(
                session_id,
                request_id,
                ErrorCode.CHAT_BUSY,
                "已有对话请求正在进行，请先停止或等待完成",
            )
            return None
        if not session_id:
            self._emit_chat_rejection(
                "",
                request_id,
                ErrorCode.CHAT_STATE_CONFLICT,
                "未选择会话，请先创建或加载会话",
            )
            return None
        if not user_message or not user_message.strip():
            self._emit_chat_rejection(
                session_id,
                request_id,
                ErrorCode.CHAT_STATE_CONFLICT,
                "消息不能为空",
            )
            return None

        try:
            settings = chat_settings_from_config(self.config)
        except PKVRuntimeError as exc:
            self._emit_chat_rejection(
                session_id,
                request_id,
                exc.code,
                "LLM Provider 配置无效",
            )
            return None
        except Exception as exc:
            logger.error(
                "读取 Chat Provider 配置失败: error_type=%s",
                type(exc).__name__,
            )
            self._emit_chat_rejection(
                session_id,
                request_id,
                ErrorCode.PROVIDER_CONFIG_INVALID,
                "LLM Provider 配置无效",
            )
            return None

        request_base = copy.deepcopy(self.current_messages)
        pending_contexts = self._ready_url_archive_contexts(session_id)
        ephemeral_system = self._ephemeral_url_context_systems.get(session_id)
        context_already_projected = (
            ephemeral_system is not None
            and self._current_system_context(request_base) == ephemeral_system
        )
        if pending_contexts and not context_already_projected:
            request_base = self._messages_with_appended_knowledge_contexts(
                request_base,
                tuple(context for _, context in pending_contexts),
            )
        request_base_messages = _freeze_messages(request_base)
        if checkpoint is not None:
            if checkpoint.session_id != session_id:
                self._emit_chat_rejection(
                    session_id,
                    request_id,
                    ErrorCode.CHAT_STATE_CONFLICT,
                    "会话在发送准备期间发生变化，请重试",
                )
                return None
            pre_messages = checkpoint.messages
            pre_total_tokens = checkpoint.total_tokens
            pre_round_count = checkpoint.round_count
        else:
            pre_messages = request_base_messages
            pre_total_tokens = self.current_total_tokens
            pre_round_count = self.current_round_count
        user = MappingProxyType({"role": "user", "content": user_message})
        request = _ChatRequest(
            request_id=request_id,
            session_id=session_id,
            user_message=user_message,
            settings=settings,
            pre_messages=pre_messages,
            request_messages=request_base_messages + (user,),
            pre_total_tokens=pre_total_tokens,
            pre_round_count=pre_round_count,
            embedded_url_contexts=pending_contexts,
        )
        self._active_request = request
        self._stop_flag = False
        self.chat_request_started.emit(session_id, request.request_id)
        return request

    async def _run_request(self, request: _ChatRequest) -> bool:
        input_tokens = 0
        output_tokens = 0
        finish_seen = False
        usage_seen = False
        outcome = "error"
        error_code = ErrorCode.CHAT_PROVIDER_FAILED
        error_message = "发送消息失败，请检查 LLM Provider 配置或网络连接"

        if request.task is None:
            request.task = asyncio.current_task()
        if request.loop is None:
            request.loop = asyncio.get_running_loop()

        try:
            if request.stop_requested:
                raise asyncio.CancelledError
            request.provider = self._provider_factory(request.settings)
            request.stream = await request.provider.open_stream(
                request.request_messages
            )
            async for event in request.stream:
                if request.stop_requested:
                    raise asyncio.CancelledError
                if not is_strict_chat_stream_event(event):
                    raise PKVRuntimeError(
                        ErrorCode.PROVIDER_PROTOCOL_FAILED,
                        "Chat Provider 返回了无效事件",
                        stage="provider_stream",
                        recoverable=True,
                    )
                has_usage = (
                    event.prompt_tokens is not None
                    or event.completion_tokens is not None
                )
                if has_usage and (
                    event.prompt_tokens is None
                    or event.completion_tokens is None
                ):
                    raise PKVRuntimeError(
                        ErrorCode.PROVIDER_PROTOCOL_FAILED,
                        "Chat Provider 返回了不完整的 usage 事件",
                        stage="provider_stream",
                        recoverable=True,
                    )
                if finish_seen and (
                    event.content
                    or event.finish_reason is not None
                    or not has_usage
                ):
                    raise PKVRuntimeError(
                        ErrorCode.PROVIDER_PROTOCOL_FAILED,
                        "Chat Provider 返回了无效的完成后事件",
                        stage="provider_stream",
                        recoverable=True,
                    )
                if event.finish_reason is not None:
                    if finish_seen or not is_supported_chat_finish_reason(
                        event.finish_reason
                    ):
                        raise PKVRuntimeError(
                            ErrorCode.PROVIDER_PROTOCOL_FAILED,
                            "Chat Provider 返回了无效的完成标记",
                            stage="provider_stream",
                            recoverable=True,
                        )
                    finish_seen = True
                if has_usage:
                    if not finish_seen or usage_seen:
                        raise PKVRuntimeError(
                            ErrorCode.PROVIDER_PROTOCOL_FAILED,
                            "Chat Provider 返回了无效的 usage 顺序",
                            stage="provider_stream",
                            recoverable=True,
                        )
                    usage_seen = True
                if event.content:
                    request.assistant_content += event.content
                    self.chat_token_received.emit(
                        request.session_id,
                        request.request_id,
                        event.content,
                    )
                    # Legacy signal remains scoped to the currently displayed
                    # session; new code should consume chat_token_received.
                    if self.current_session_id == request.session_id:
                        self.token_received.emit(event.content)
                if event.prompt_tokens is not None:
                    input_tokens = event.prompt_tokens
                if event.completion_tokens is not None:
                    output_tokens = event.completion_tokens

            if request.stop_requested:
                outcome = "stopped"
            elif not finish_seen:
                error_code = ErrorCode.PROVIDER_PROTOCOL_FAILED
                error_message = "LLM Provider 流未完整结束，本轮内容未保存"
            elif not request.assistant_content:
                error_code = ErrorCode.PROVIDER_PROTOCOL_FAILED
                error_message = "LLM Provider 未返回可用内容"
            else:
                commit_base = [
                    dict(message) for message in request.request_messages
                ]
                pending_contexts = self._ready_url_archive_contexts(
                    request.session_id
                )
                embedded_operation_ids = {
                    operation_id
                    for operation_id, _ in request.embedded_url_contexts
                }
                newly_ready_contexts = tuple(
                    (operation_id, context)
                    for operation_id, context in pending_contexts
                    if operation_id not in embedded_operation_ids
                )
                if newly_ready_contexts:
                    commit_base = self._messages_with_appended_knowledge_contexts(
                        commit_base,
                        tuple(context for _, context in newly_ready_contexts),
                    )
                committed_messages = [
                    *commit_base,
                    {
                        "role": "assistant",
                        "content": request.assistant_content,
                    },
                ]
                committed_total = (
                    request.pre_total_tokens + input_tokens + output_tokens
                )
                committed_rounds = request.pre_round_count + 1
                try:
                    await self._save_session_state(
                        session_id=request.session_id,
                        messages=committed_messages,
                        total_tokens=committed_total,
                        round_count=committed_rounds,
                    )
                except Exception as exc:
                    logger.error(
                        "保存 Chat 会话失败: error_type=%s",
                        type(exc).__name__,
                    )
                    rollback_confirmed = await self._reconcile_failed_save(
                        request,
                        committed_messages=committed_messages,
                        committed_total=committed_total,
                        committed_rounds=committed_rounds,
                    )
                    error_code = ErrorCode.CHAT_SAVE_FAILED
                    error_message = (
                        "保存对话失败，本轮内容已回滚"
                        if rollback_confirmed
                        else "保存对话失败，持久化状态需检查，请勿直接重试"
                    )
                else:
                    # This assignment is the irreversible commit boundary.  It
                    # intentionally happens before signals and before any
                    # cleanup await so Stop can no longer contradict durable
                    # storage.
                    request.committed = True
                    self._consume_url_archive_contexts(
                        request.session_id,
                        pending_contexts,
                    )
                    outcome = "completed"
                    if self.current_session_id == request.session_id:
                        self.current_messages = copy.deepcopy(committed_messages)
                        self.current_total_tokens = committed_total
                        self.current_round_count = committed_rounds
                    self.chat_token_usage_updated.emit(
                        request.session_id,
                        request.request_id,
                        input_tokens,
                        output_tokens,
                        committed_total,
                    )
                    if self.current_session_id == request.session_id:
                        self.token_usage_updated.emit(
                            input_tokens,
                            output_tokens,
                            committed_total,
                        )

        except asyncio.CancelledError:
            if request.committed:
                outcome = "completed"
            elif request.stop_requested:
                outcome = "stopped"
            else:
                error_code = ErrorCode.CHAT_STATE_CONFLICT
                error_message = "对话请求被意外取消，本轮内容已回滚"
        except PKVRuntimeError as exc:
            logger.error(
                "Chat Provider 请求失败: code=%s",
                exc.code.value,
            )
            error_code = exc.code
            error_message = (
                "LLM Provider 返回格式无效，本轮内容未保存"
                if exc.code is ErrorCode.PROVIDER_PROTOCOL_FAILED
                else "发送消息失败，请检查 LLM Provider 配置或网络连接"
            )
        except Exception as exc:
            logger.error(
                "Chat Provider 请求失败: error_type=%s",
                type(exc).__name__,
            )
        finally:
            await self._close_request_resources(request)

        if outcome == "completed":
            self._emit_terminal(request, "completed")
            return True
        if outcome == "stopped":
            self._emit_terminal(request, "stopped")
            return False
        self._emit_terminal(
            request,
            "error",
            code=error_code,
            message=error_message,
        )
        return False

    async def _save_session_state(
        self,
        *,
        session_id: str,
        messages: List[dict],
        total_tokens: int,
        round_count: int,
    ) -> None:
        expected_messages = copy.deepcopy(messages)
        self.store.update_session(
            session_id=session_id,
            messages=expected_messages,
            total_tokens=total_tokens,
            round_count=round_count,
            summary=None,
        )
        # SQLite UPDATE of a missing row can otherwise report no exception.
        # Read-after-write is the durable acknowledgement used by the Chat
        # state machine; missing, malformed, or stale state must never become
        # a completed terminal.
        try:
            observed = self.store.get_session(session_id)
        except Exception as exc:
            raise PKVRuntimeError(
                ErrorCode.CHAT_SAVE_FAILED,
                "无法核验对话持久化结果",
                stage="chat_save_verify",
                recoverable=False,
            ) from exc
        if not self._session_state_equals(
            observed,
            session_id=session_id,
            messages=expected_messages,
            total_tokens=total_tokens,
            round_count=round_count,
        ):
            raise PKVRuntimeError(
                ErrorCode.CHAT_SAVE_FAILED,
                "对话持久化结果与提交快照不一致",
                stage="chat_save_verify",
                recoverable=False,
            )
        logger.debug("Chat 会话状态已持久化并核验")

    @staticmethod
    def _session_state_equals(
        observed: Mapping[str, Any] | None,
        *,
        session_id: str,
        messages: List[dict],
        total_tokens: int,
        round_count: int,
    ) -> bool:
        if not _is_strict_session_projection(
            observed,
            expected_session_id=session_id,
        ):
            return False
        if not _is_strict_chat_messages(messages):
            return False
        return (
            observed["messages"] == messages
            and type(total_tokens) is int
            and total_tokens >= 0
            and observed["total_tokens"] == total_tokens
            and type(round_count) is int
            and round_count >= 0
            and observed["round_count"] == round_count
        )

    async def _save_session(self) -> None:
        """保存当前会话；失败会向调用者传播，禁止伪装成功。"""

        if not self.current_session_id:
            raise PKVRuntimeError(
                ErrorCode.CHAT_STATE_CONFLICT,
                "未选择会话",
                stage="chat_save",
                recoverable=True,
            )
        await self._save_session_state(
            session_id=self.current_session_id,
            messages=copy.deepcopy(self.current_messages),
            total_tokens=self.current_total_tokens,
            round_count=self.current_round_count,
        )

    async def _reconcile_failed_save(
        self,
        request: _ChatRequest,
        *,
        committed_messages: List[dict],
        committed_total: int,
        committed_rounds: int,
    ) -> bool:
        """Detect commit-after-raise and restore the durable pre-turn state."""

        try:
            observed = self.store.get_session(request.session_id)
        except Exception as exc:
            logger.error(
                "核验 Chat 保存失败状态异常: error_type=%s",
                type(exc).__name__,
            )
            return False
        if observed is None:
            # The request checkpoint came from an existing session.  A missing
            # row is data loss/unknown state, never proof of rollback.
            return False

        pre_messages = [dict(message) for message in request.pre_messages]
        if self._session_state_equals(
            observed,
            session_id=request.session_id,
            messages=pre_messages,
            total_tokens=request.pre_total_tokens,
            round_count=request.pre_round_count,
        ):
            return True

        if not self._session_state_equals(
            observed,
            session_id=request.session_id,
            messages=committed_messages,
            total_tokens=committed_total,
            round_count=committed_rounds,
        ):
            logger.error("Chat 保存失败后检测到并发状态冲突")
            return False

        try:
            await self._save_session_state(
                session_id=request.session_id,
                messages=pre_messages,
                total_tokens=request.pre_total_tokens,
                round_count=request.pre_round_count,
            )
            restored = self.store.get_session(request.session_id)
        except Exception as exc:
            logger.error(
                "补偿 Chat commit-after-raise 失败: error_type=%s",
                type(exc).__name__,
            )
            return False
        return self._session_state_equals(
            restored,
            session_id=request.session_id,
            messages=pre_messages,
            total_tokens=request.pre_total_tokens,
            round_count=request.pre_round_count,
        )

    def stop_stream(self) -> bool:
        """Request a pre-commit stop.

        ``True`` means the stop was accepted (including an idempotent repeat).
        ``False`` means there is no active request or its durable commit already
        succeeded; in the latter case cleanup continues and the sole terminal
        outcome remains ``completed``.
        """

        request = self._active_request
        if request is None:
            return False
        if request.committed:
            logger.info("Chat 已提交，忽略过晚的停止请求")
            return False
        request.stop_requested = True
        self._stop_flag = True

        loop = request.loop
        if loop is None or loop.is_closed():
            return True

        def cancel_request() -> None:
            # A queued cross-thread stop can run after the synchronous durable
            # save completed.  Re-check the monotonic boundary on the owner
            # loop before cancelling anything.
            if request.committed:
                return
            self._ensure_cleanup_task(request)
            task = request.task
            if task is not None and not task.done():
                task.cancel()

        loop.call_soon_threadsafe(cancel_request)
        logger.info("🛑 已请求取消 Chat 流")
        return True

    def _on_dispatched_task_done(
        self,
        request: _ChatRequest,
        task: asyncio.Task[Any],
    ) -> None:
        """Finalize a task cancelled before its coroutine got a first turn."""

        if self._active_request is not request or request.terminal_emitted:
            return
        loop = request.loop
        if loop is None or loop.is_closed():
            return

        async def finalize_unstarted() -> None:
            await self._close_request_resources(request)
            if request.committed:
                self._emit_terminal(request, "completed")
                return
            elif request.stop_requested:
                self._emit_terminal(request, "stopped")
                return
            try:
                task.exception()
            except (asyncio.CancelledError, Exception):
                pass
            self._emit_terminal(
                request,
                "error",
                code=ErrorCode.CHAT_STATE_CONFLICT,
                message="对话任务异常结束，本轮内容已回滚",
            )

        loop.create_task(finalize_unstarted())

    async def _close_request_resources(self, request: _ChatRequest) -> None:
        """Wait for the independent close-once cleanup despite task cancellation."""

        cleanup_task = self._ensure_cleanup_task(request)
        # Shield prevents cancellation of the Chat runner from cancelling the
        # resource closer.  Repeated Stop/task.cancel calls are drained until
        # cleanup has actually reached a terminal state.
        while not cleanup_task.done():
            try:
                await asyncio.shield(cleanup_task)
            except asyncio.CancelledError:
                continue
        if cleanup_task.cancelled():
            logger.warning("Chat 资源清理任务被意外取消")
            return
        try:
            cleanup_task.result()
        except Exception as exc:
            logger.warning(
                "关闭 Chat 资源失败: error_type=%s",
                type(exc).__name__,
            )

    def _ensure_cleanup_task(self, request: _ChatRequest) -> asyncio.Task[Any]:
        cleanup_task = request.cleanup_task
        if cleanup_task is None:
            loop = request.loop
            if loop is None or loop.is_closed():
                loop = asyncio.get_running_loop()
            cleanup_task = loop.create_task(self._close_resources_once(request))
            request.cleanup_task = cleanup_task
        return cleanup_task

    async def _close_resources_once(self, request: _ChatRequest) -> None:
        await self._close_operation_bounded(
            self._close_stream_once(request),
            label="Chat 流",
        )
        await self._close_operation_bounded(
            self._close_provider_once(request),
            label="Chat Provider",
        )

    async def _close_operation_bounded(
        self,
        operation: Awaitable[None],
        *,
        label: str,
    ) -> None:
        """Run one close operation once without holding the UI indefinitely."""

        task = asyncio.create_task(operation)
        try:
            done, _ = await asyncio.wait(
                {task},
                timeout=_RESOURCE_CLOSE_TIMEOUT_SECONDS,
            )
        except asyncio.CancelledError:
            task.cancel()
            self._retain_cleanup_task(task)
            raise
        if not done:
            task.cancel()
            self._retain_cleanup_task(task)
            logger.warning("关闭 %s 超时，已转入后台清理", label)
            return
        try:
            task.result()
        except asyncio.CancelledError:
            logger.warning("关闭 %s 时清理任务被取消", label)
        except Exception as exc:
            logger.warning(
                "关闭 %s 失败: error_type=%s",
                label,
                type(exc).__name__,
            )

    def _retain_cleanup_task(self, task: asyncio.Task[Any]) -> None:
        self._detached_cleanup_tasks.add(task)

        def drain(done: asyncio.Task[Any]) -> None:
            self._detached_cleanup_tasks.discard(done)
            try:
                done.exception()
            except (asyncio.CancelledError, Exception):
                pass

        task.add_done_callback(drain)

    async def _close_stream_once(self, request: _ChatRequest) -> None:
        if request.stream_closed or request.stream is None:
            return
        request.stream_closed = True
        await request.stream.aclose()

    async def _close_provider_once(self, request: _ChatRequest) -> None:
        if request.provider_closed or request.provider is None:
            return
        request.provider_closed = True
        await request.provider.aclose()

    def _emit_terminal(
        self,
        request: _ChatRequest,
        outcome: str,
        *,
        code: ErrorCode | None = None,
        message: str = "",
    ) -> None:
        if request.terminal_emitted:
            return
        # Durable truth wins every late Stop/cancellation arbitration.
        if request.committed:
            outcome = "completed"
        request.terminal_emitted = True
        if outcome != "completed" and self.current_session_id == request.session_id:
            self.current_messages = [
                dict(message) for message in request.pre_messages
            ]
            self.current_total_tokens = request.pre_total_tokens
            self.current_round_count = request.pre_round_count
        self._release_request(request)

        if outcome == "completed":
            self.chat_request_terminal.emit(
                request.session_id,
                request.request_id,
                "completed",
                "",
                "",
            )
            self.chat_request_completed.emit(
                request.session_id,
                request.request_id,
            )
            self.chat_request_finished.emit(
                request.session_id,
                request.request_id,
            )
            if self.current_session_id == request.session_id:
                self.stream_finished.emit()
        elif outcome == "stopped":
            self.chat_request_terminal.emit(
                request.session_id,
                request.request_id,
                "stopped",
                "",
                "",
            )
            self.chat_request_stopped.emit(
                request.session_id,
                request.request_id,
            )
            if self.current_session_id == request.session_id:
                self.stream_stopped.emit()
        else:
            terminal_code = code or ErrorCode.CHAT_PROVIDER_FAILED
            self.chat_request_terminal.emit(
                request.session_id,
                request.request_id,
                "error",
                terminal_code.value,
                message,
            )
            self._emit_chat_error(
                request.session_id,
                request.request_id,
                terminal_code,
                message,
            )

    def _release_request(self, request: _ChatRequest) -> None:
        if self._active_request is request:
            self._active_request = None
            self._stop_flag = False
            try:
                self._apply_ready_url_contexts_to_current(request.session_id)
            except Exception as exc:
                logger.error(
                    "应用 URL 归档上下文失败: error_type=%s",
                    type(exc).__name__,
                )

    def _emit_chat_error(
        self,
        session_id: str,
        request_id: str,
        code: ErrorCode,
        safe_message: str,
    ) -> None:
        self.chat_request_failed.emit(
            session_id,
            request_id,
            code.value,
            safe_message,
        )

    def _emit_chat_rejection(
        self,
        session_id: str,
        request_id: str,
        code: ErrorCode,
        safe_message: str,
    ) -> None:
        self._latest_attempt_id = request_id
        self.chat_request_rejected.emit(
            session_id,
            request_id,
            code.value,
            safe_message,
        )

    def delete_current_session(self) -> bool:
        """删除当前会话

        Returns:
            是否删除成功
        """
        if not self.current_session_id:
            return False
        return self.delete_session(self.current_session_id)

    def delete_session(self, session_id: str) -> bool:
        """删除指定会话

        Args:
            session_id: 要删除的会话 ID

        Returns:
            是否删除成功
        """
        active = self._active_request
        if active is not None and active.session_id == session_id:
            message = "会话正在生成回复，请先停止后再删除 [chat_state_conflict]"
            self._emit_chat_rejection(
                session_id,
                str(uuid.uuid4()),
                ErrorCode.CHAT_STATE_CONFLICT,
                message,
            )
            self.error_occurred.emit(message)
            return False
        try:
            self.store.delete_session(session_id)
            logger.info("删除会话成功")

            # 如果删除的是当前会话，重置状态
            if session_id == self.current_session_id:
                self.current_session_id = None
                self.current_messages = []
                self.current_total_tokens = 0
                self.current_round_count = 0
            self._pending_knowledge_contexts.pop(session_id, None)
            self._ephemeral_url_context_systems.pop(session_id, None)

            return True
        except Exception as e:
            error_msg = "删除会话失败，请检查本地数据库状态"
            logger.error(
                "删除会话失败: error_type=%s",
                type(e).__name__,
            )
            self.error_occurred.emit(error_msg)
            return False

    def archive_current_session(self) -> bool:
        """归档当前会话

        Returns:
            是否归档成功
        """
        if not self.current_session_id:
            return False

        try:
            self.store.archive_session(self.current_session_id, is_archived=True)
            logger.info("归档会话成功")
            return True
        except Exception as e:
            error_msg = "归档会话失败，请检查本地数据库状态"
            logger.error(
                "归档会话失败: error_type=%s",
                type(e).__name__,
            )
            self.error_occurred.emit(error_msg)
            return False

    # ------------------------------------------------------------------
    # M12 Phase 3: URL 自动检测和归档
    # ------------------------------------------------------------------

    def begin_url_archive(self, url: str) -> str | None:
        """Synchronously freeze URL archive ownership before scheduling work."""

        origin_session_id = self.current_session_id
        if not origin_session_id:
            return None
        operation_id = str(uuid.uuid4())
        self._register_url_archive_operation(origin_session_id, operation_id)
        self.archive_url_and_inject(
            url,
            origin_session_id,
            operation_id,
        )
        return operation_id

    @asyncSlot()
    async def archive_url_and_inject(
        self,
        url: str,
        origin_session_id: str | None = None,
        operation_id: str | None = None,
    ) -> None:
        """归档 URL 并将内容注入对话上下文

        使用 WorkflowEngine 归档 URL，成功后：
        1. 构建知识引用
        2. 注入到当前会话上下文
        3. 发射完成信号

        Args:
            url: 要归档的 URL
        """
        origin_session_id = origin_session_id or self.current_session_id or ""
        operation_id = operation_id or str(uuid.uuid4())
        if origin_session_id:
            self._register_url_archive_operation(origin_session_id, operation_id)
        self._emit_url_archive_started(origin_session_id, operation_id, url)
        target_label = describe_url_target(url)
        logger.info("开始归档 URL target=%s", target_label)

        try:
            # 与 ArchiveWorker 共用同一前置策略，且必须发生在任何查重读取前。
            url_failure = validate_url_security_result(url)
            if url_failure is not None:
                code = _safe_url_archive_code(url_failure.code)
                stage = _safe_url_archive_stage(
                    url_failure.stage or "url_preflight"
                )
                self._emit_url_archive_failed(
                    origin_session_id,
                    operation_id,
                    url,
                    f"归档失败（错误代码：{code}，阶段：{stage}）",
                )
                return

            # 仅在安全前置验证通过后检查 URL 是否已归档。
            existing = self.store.query_by_url(url)
            if existing is not None:
                projected_existing = _project_strict_url_archive_entry(existing)
                if projected_existing is None:
                    logger.warning(
                        "URL 归档查重回读无效 target=%s",
                        target_label,
                    )
                    self._emit_url_archive_failed(
                        origin_session_id,
                        operation_id,
                        url,
                        _URL_ARCHIVE_WORKFLOW_RESULT_ERROR,
                    )
                    return
                logger.info("URL 已归档，直接引用 target=%s", target_label)
                self._publish_url_archive_completion(
                    origin_session_id,
                    operation_id,
                    url,
                    projected_existing,
                    target_label,
                )
                return

            # 使用 WorkflowEngine 归档
            from src.workflow.engine import WorkflowEngine
            engine = WorkflowEngine()

            result = await engine.execute_async(
                "archive-url",
                {
                    "url": url,
                    "skip_sharpen": True,  # GUI 不触发交互
                    "skip_review": True,
                },
            )

            try:
                raw_terminal = getattr(result, "terminal", None)
                raw_success = getattr(result, "success", None)
            except Exception:
                raw_terminal = None
                raw_success = None
            if raw_terminal in {"success", "degraded"} and raw_success is True:
                completion = _project_url_archive_workflow_completion(result)
                if completion is None:
                    terminal = raw_terminal
                    data = {}
                    knowledge_id = None
                    result_status = None
                    contract_valid = False
                else:
                    terminal, data = completion
                    knowledge_id = data["knowledge_id"]
                    result_status = data["status"]
                    contract_valid = True
            elif raw_terminal == "error" and raw_success is False:
                terminal = raw_terminal
                try:
                    raw_data = getattr(result, "data", None)
                except Exception:
                    raw_data = None
                contract_valid = (
                    type(raw_data) is dict
                    and all(type(key) is str for key in raw_data)
                )
                data = dict(raw_data) if contract_valid else {}
                knowledge_id = None
                result_status = None
            else:
                terminal = raw_terminal
                data = {}
                knowledge_id = None
                result_status = None
                contract_valid = False

            if not contract_valid:
                # WorkflowResult.terminal + exact success consistency are the
                # authoritative contract.  A committed terminal additionally
                # needs a typed durable identity before any UI completion or
                # context injection may be published.
                logger.warning(
                    "URL 归档结果合同无效 target=%s",
                    target_label,
                )
                self._emit_url_archive_failed(
                    origin_session_id,
                    operation_id,
                    url,
                    _URL_ARCHIVE_WORKFLOW_RESULT_ERROR,
                )
                return

            if terminal != "error":
                if result_status not in {"ready", "degraded"}:
                    fatal_code = (
                        ErrorCode.STORAGE_REPAIR_REQUIRED.value
                        if result_status == "repair_required"
                        else ErrorCode.WORKFLOW_STEP_FAILED.value
                    )
                    error_msg = (
                        f"归档失败（错误代码：{fatal_code}，阶段：storage）"
                    )
                    if (
                        result_status == "repair_required"
                        or data.get("core_committed") is True
                        or data.get("do_not_retry") is True
                    ):
                        storage_operation_id = _safe_url_archive_operation_id(
                            data.get("operation_id")
                        )
                        operation_text = (
                            f", operation_id={storage_operation_id}"
                            if storage_operation_id
                            else ""
                        )
                        repairs = _safe_url_archive_repair_actions(
                            data.get("repair_actions")
                        )
                        repair_text = "、".join(repairs) or "repair_required"
                        error_msg = (
                            f"{error_msg} [核心存储已提交或需先修复: "
                            f"status={result_status}{operation_text}, "
                            f"repair={repair_text}] 请勿盲目重试！"
                        )
                    logger.warning(
                        "URL 归档返回非完成存储终态 target=%s",
                        target_label,
                    )
                    self._emit_url_archive_failed(
                        origin_session_id,
                        operation_id,
                        url,
                        error_msg,
                    )
                    return

                entry = _project_strict_url_archive_entry(
                    self.store.query_by_id(knowledge_id),
                    expected_knowledge_id=knowledge_id,
                )
                if entry is None:
                    logger.warning(
                        "URL 归档持久化回读无效 target=%s",
                        target_label,
                    )
                    self._emit_url_archive_failed(
                        origin_session_id,
                        operation_id,
                        url,
                        _URL_ARCHIVE_WORKFLOW_RESULT_ERROR,
                    )
                    return

                # DEGRADED 仍是核心成功，但必须向用户可见地警告辅助索引需要修复；
                # 已提交但需修复的终态同样禁止盲目重试。
                warning = None
                if terminal == "degraded" or result_status == "degraded":
                    repairs = "、".join(
                        _safe_url_archive_repair_actions(
                            data.get("repair_actions")
                        )
                    )
                    status = result_status
                    storage_operation_id = _safe_url_archive_operation_id(
                        data.get("operation_id")
                    )
                    operation_text = (
                        f", operation_id={storage_operation_id}"
                        if storage_operation_id
                        else ""
                    )
                    warning = (
                        "URL 归档已完成但存在警告: "
                        f"workflow_terminal={terminal}, status={status}"
                        f"{operation_text}, "
                        f"repair={repairs or '见日志'}。请勿盲目重试归档。"
                    )
                    logger.warning(
                        "URL 归档降级 target=%s",
                        target_label,
                    )
                self._publish_url_archive_completion(
                    origin_session_id,
                    operation_id,
                    url,
                    entry,
                    target_label,
                    warning,
                )
                return
            else:
                data = data if isinstance(data, dict) else {}
                issues = list(getattr(result, "issues", []) or [])
                first_issue = issues[0] if issues else {}
                if isinstance(first_issue, dict):
                    code = _safe_url_archive_code(first_issue.get("code"))
                    stage = _safe_url_archive_stage(first_issue.get("stage"))
                else:
                    code = _safe_url_archive_code(
                        getattr(first_issue, "code", None)
                    )
                    stage = _safe_url_archive_stage(
                        getattr(first_issue, "stage", None)
                    )
                error_msg = f"归档失败（错误代码：{code}，阶段：{stage}）"
                if (
                    data.get("core_committed") is True
                    or data.get("do_not_retry") is True
                ):
                    status = _safe_url_archive_status(data.get("status"))
                    storage_operation_id = _safe_url_archive_operation_id(
                        data.get("operation_id")
                    )
                    repairs = _safe_url_archive_repair_actions(
                        data.get("repair_actions")
                    )
                    operation_text = (
                        f", operation_id={storage_operation_id}"
                        if storage_operation_id
                        else ""
                    )
                    repair_text = "、".join(repairs) or "repair_required"
                    error_msg = (
                        f"{error_msg} [核心存储已提交或需先修复: "
                        f"status={status}{operation_text}, "
                        f"repair={repair_text}] 请勿盲目重试！"
                    )
                logger.warning(
                    "URL 归档失败 target=%s",
                    target_label,
                )
                self._emit_url_archive_failed(
                    origin_session_id,
                    operation_id,
                    url,
                    error_msg,
                )

        except Exception as e:
            if isinstance(e, PKVRuntimeError):
                code = _safe_url_archive_code(e.code)
                stage = _safe_url_archive_stage(e.stage or "archive_url")
            else:
                code = ErrorCode.WORKFLOW_STEP_FAILED.value
                stage = "archive_url"
            error_msg = f"归档失败（错误代码：{code}，阶段：{stage}）"
            logger.error(
                "URL 归档异常: error_type=%s, code=%s, stage=%s",
                type(e).__name__,
                code,
                stage,
            )
            self._emit_url_archive_failed(
                origin_session_id,
                operation_id,
                url,
                error_msg,
            )

    def _publish_url_archive_completion(
        self,
        session_id: str,
        operation_id: str,
        url: str,
        entry: dict,
        target_label: str,
        warning: str | None = None,
    ) -> bool:
        """Prepare and inject a durable reference before publishing completed."""

        try:
            projected_entry = _project_strict_url_archive_entry(entry)
            if projected_entry is None:
                raise ValueError("URL archive entry projection is invalid")

            normalized_entry = projected_entry
            for field in (
                "title",
                "source_type",
                "summary_one_sentence",
                "summary_100_words",
            ):
                value = normalized_entry.get(field, "")
                normalized_entry[field] = "" if value is None else value
            source_url = normalized_entry.get("source_url", "")
            if source_url is None:
                source_url = ""
            normalized_entry["source_url"] = sanitize_public_source_url(
                source_url
            )

            from src.gui.utils.knowledge_ref import (
                build_knowledge_reference,
                format_context_message,
                format_reference_card_html,
            )

            reference = build_knowledge_reference(normalized_entry)
            context = format_context_message([reference])
            card_html = format_reference_card_html(reference)
            if type(context) is not str or not context:
                raise ValueError("URL archive context is invalid")
            if type(card_html) is not str or not card_html:
                raise ValueError("URL archive card is invalid")

            # Inject before emitting completed.  Any preparation/injection
            # failure therefore remains a single failed terminal rather than
            # a completed event followed by an unobservable UI exception.
            if session_id:
                self._register_url_archive_operation(session_id, operation_id)
                self._pending_knowledge_contexts[session_id][operation_id] = context
                self._apply_ready_url_contexts_to_current(session_id)
            normalized_entry[self.URL_ARCHIVE_REFERENCE_CARD_HTML_KEY] = card_html
        except Exception as exc:
            logger.error(
                "URL 归档引用准备失败: error_type=%s target=%s",
                type(exc).__name__,
                target_label,
            )
            self._emit_url_archive_failed(
                session_id,
                operation_id,
                url,
                _URL_ARCHIVE_WORKFLOW_RESULT_ERROR,
            )
            return False

        if warning:
            self._emit_url_archive_warning(
                session_id,
                operation_id,
                url,
                warning,
            )
        logger.info("URL 归档成功 target=%s", target_label)
        self._emit_url_archive_completed(
            session_id,
            operation_id,
            url,
            normalized_entry,
        )
        return True

    def _register_url_archive_operation(
        self,
        session_id: str,
        operation_id: str,
    ) -> None:
        """Reserve insertion order before concurrent URL archive work starts."""

        if type(session_id) is not str or not session_id:
            raise TypeError("URL archive session identity contract violation")
        if type(operation_id) is not str or not operation_id:
            raise TypeError("URL archive operation identity contract violation")
        operations = self._pending_knowledge_contexts.setdefault(session_id, {})
        operations.setdefault(operation_id, None)

    def _emit_url_archive_started(
        self,
        session_id: str,
        operation_id: str,
        url: str,
    ) -> None:
        self.url_archive_operation_started.emit(session_id, operation_id, url)
        self.url_archive_started.emit(url)

    def _emit_url_archive_completed(
        self,
        session_id: str,
        operation_id: str,
        url: str,
        entry: dict,
    ) -> None:
        self.url_archive_operation_completed.emit(
            session_id,
            operation_id,
            url,
            entry,
        )
        self.url_archive_completed.emit(url, entry)

    def _emit_url_archive_failed(
        self,
        session_id: str,
        operation_id: str,
        url: str,
        message: str,
    ) -> None:
        operations = self._pending_knowledge_contexts.get(session_id)
        if operations is not None:
            operations.pop(operation_id, None)
            if not operations:
                self._pending_knowledge_contexts.pop(session_id, None)
        try:
            self._apply_ready_url_contexts_to_current(session_id)
        except Exception as exc:
            logger.error(
                "应用 URL 归档上下文失败: error_type=%s",
                type(exc).__name__,
            )
        self.url_archive_operation_failed.emit(
            session_id,
            operation_id,
            url,
            message,
        )
        self.url_archive_failed.emit(url, message)

    def _emit_url_archive_warning(
        self,
        session_id: str,
        operation_id: str,
        url: str,
        message: str,
    ) -> None:
        self.url_archive_operation_warning.emit(
            session_id,
            operation_id,
            url,
            message,
        )
        self.url_archive_warning.emit(url, message)

    # ------------------------------------------------------------------
    # 对话保存到知识库
    # ------------------------------------------------------------------

    def save_session_to_knowledge_base(self, session_id: str) -> bool:
        """将对话内容保存到知识库

        将指定会话的完整对话记录格式化为文本，使用 TextFallback 工作流归档。

        Args:
            session_id: 会话 ID

        Returns:
            是否成功触发归档（归档本身在后台线程执行）
        """
        if type(session_id) is not str or not session_id:
            self.session_save_to_kb_failed.emit(
                "",
                "保存对话失败，请检查本地存储状态",
            )
            return False

        existing_worker = self._kb_worker
        if existing_worker is not None:
            try:
                still_running = existing_worker.isRunning()
            except Exception:
                still_running = True
            if still_running is not False:
                self.session_save_to_kb_failed.emit(
                    session_id,
                    "已有保存任务正在进行，请稍后重试",
                )
                return False
            self._kb_worker = None

        try:
            session = self.store.get_session(session_id)
            if session is None:
                self.session_save_to_kb_failed.emit(session_id, "会话不存在")
                return False

            if (
                not _is_strict_session_projection(
                    session,
                    expected_session_id=session_id,
                )
                or type(session.get("title")) is not str
                or not session["title"].strip()
            ):
                raise TypeError("session knowledge export contract violation")
            messages = copy.deepcopy(session["messages"])

            if not messages:
                self.session_save_to_kb_failed.emit(session_id, "会话无对话内容")
                return False

            # 格式化对话记录为文本
            title = session["title"]
            lines = [f"# {title}\n"]
            for msg in messages:
                role = msg["role"]
                content = msg["content"]
                if role == "system":
                    continue  # 跳过 system 消息
                label = "**User**" if role == "user" else "**Assistant**"
                lines.append(f"{label}:\n{content}\n")

            text_content = "\n".join(lines)

            # 使用 ArchiveWorker 在后台线程归档
            from src.gui.viewmodels.archive_viewmodel import (
                ArchiveWorker,
                sanitize_archive_failure,
            )

            worker = ArchiveWorker(
                mode="text",
                data={"text": text_content, "title": title},
            )
            self._kb_worker = worker
            worker.finished_ok.connect(
                lambda result: self._on_session_saved_to_kb(
                    session_id, result
                )
            )
            failure_seen = {"value": False}

            def on_structured_failure(payload: dict) -> None:
                failure_seen["value"] = True
                safe_payload = sanitize_archive_failure(
                    payload if type(payload) is dict else {}
                )
                self.session_save_to_kb_failed.emit(
                    session_id,
                    safe_payload["safe_message"],
                )

            def on_legacy_failure(error_msg: str) -> None:
                if not failure_seen["value"]:
                    safe_payload = sanitize_archive_failure({
                        "safe_message": (
                            error_msg if type(error_msg) is str else ""
                        )
                    })
                    self.session_save_to_kb_failed.emit(
                        session_id,
                        safe_payload["safe_message"],
                    )

            structured_failure = getattr(
                worker,
                "finished_failure",
                None,
            )
            if structured_failure is not None:
                structured_failure.connect(on_structured_failure)
            worker.finished_err.connect(on_legacy_failure)
            worker.finished.connect(lambda: self._release_kb_worker(worker))
            worker.start()

            logger.info("开始保存对话到知识库")
            return True

        except Exception as e:
            error_msg = "保存对话失败，请检查本地存储状态"
            logger.error(
                "保存对话到知识库失败: error_type=%s",
                type(e).__name__,
            )
            self.session_save_to_kb_failed.emit(session_id, error_msg)
            return False

    def _release_kb_worker(self, worker: Any) -> None:
        """Release only the worker that still owns the save-to-KB slot."""

        if self._kb_worker is worker:
            self._kb_worker = None
            try:
                worker.deleteLater()
            except Exception:
                pass

    def _on_session_saved_to_kb(
        self,
        session_id: str,
        result: dict,
    ) -> bool:
        """对话保存到知识库成功回调

        Args:
            session_id: 会话 ID
            result: 归档结果字典
        """
        projected = _project_archive_success_payload(result)
        if projected is None:
            logger.error("保存对话完成载荷无效: code=invalid_completion_payload")
            self.session_save_to_kb_failed.emit(
                session_id,
                "保存对话到知识库失败",
            )
            return False

        knowledge_id = projected["knowledge_id"]
        terminal = projected["workflow_terminal"]
        if terminal == "degraded" or projected["status"] == "degraded":
            repairs = "、".join(
                projected["repair_actions"]
            )
            self.session_save_to_kb_warning.emit(
                session_id,
                "对话已保存到知识库，但存在工作流警告"
                + (f"（repair={repairs}）" if repairs else ""),
            )
        logger.info("对话已保存到知识库")
        self.session_saved_to_kb.emit(session_id, knowledge_id)
        return True
