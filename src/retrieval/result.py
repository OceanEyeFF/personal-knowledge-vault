"""Stable result contracts shared by every retrieval strategy."""

from __future__ import annotations

from dataclasses import dataclass
import math
import re
from typing import Any, Iterable, Literal

from src.runtime.errors import ErrorCode, PKVRuntimeError


_VALID_STATUSES = frozenset({"success", "no_hits", "invalid", "error", "degraded"})
_STRATEGY_TOKEN = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")
_CAUSE_TYPE_TOKEN = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]{0,95}$")
_METADATA_STRING_FIELDS = frozenset(
    {
        "archived_at",
        "bm25_match_mode",
        "bm25_match_query",
        "chunk_text",
        "event_time",
        "file_path",
        "published_at",
        "published_time",
        "publish_time",
        "source_type",
        "source_url",
        "summary",
        "summary_100_words",
        "summary_one_sentence",
        "title",
        "updated_at",
    }
)
_METADATA_POSITIVE_INT_FIELDS = frozenset(
    {"bm25_rank", "chunk_id", "knowledge_id", "vector_rank"}
)
_METADATA_NONNEGATIVE_INT_FIELDS = frozenset({"chunk_index"})
_METADATA_FINITE_NUMBER_FIELDS = frozenset({"bm25_score", "vector_distance"})
_MAX_METADATA_DEPTH = 16
_MAX_METADATA_CONTAINER_ITEMS = 4096


def _is_strategy_token(value: Any) -> bool:
    return (
        type(value) is str
        and len(value) <= 64
        and _STRATEGY_TOKEN.fullmatch(value) is not None
    )


def _is_json_safe_metadata_value(
    value: Any,
    *,
    depth: int = 0,
    ancestors: set[int] | None = None,
) -> bool:
    """Validate an exact, finite JSON tree without invoking user hooks."""

    if value is None or type(value) in {str, bool, int}:
        return True
    if type(value) is float:
        return math.isfinite(value)
    if type(value) not in {list, dict} or depth >= _MAX_METADATA_DEPTH:
        return False
    if len(value) > _MAX_METADATA_CONTAINER_ITEMS:
        return False

    value_id = id(value)
    active = set() if ancestors is None else ancestors
    if value_id in active:
        return False
    active.add(value_id)
    try:
        if type(value) is list:
            return all(
                _is_json_safe_metadata_value(
                    item,
                    depth=depth + 1,
                    ancestors=active,
                )
                for item in value
            )
        return all(
            type(key) is str
            and _is_json_safe_metadata_value(
                item,
                depth=depth + 1,
                ancestors=active,
            )
            for key, item in value.items()
        )
    except Exception:
        return False
    finally:
        active.remove(value_id)


def _is_strict_metadata(
    value: Any,
    *,
    knowledge_id: int | None = None,
) -> bool:
    """Validate the generic JSON boundary plus fields consumed by adapters."""

    if type(value) is not dict or not _is_json_safe_metadata_value(value):
        return False
    try:
        for field in _METADATA_STRING_FIELDS:
            if field in value and value[field] is not None and type(value[field]) is not str:
                return False
        if "source_type" in value and (
            type(value["source_type"]) is not str
            or not value["source_type"].strip()
        ):
            return False

        for field in {"tags", "keywords"}:
            if field not in value or value[field] is None or type(value[field]) is str:
                continue
            if type(value[field]) is not list or not all(
                type(item) is str for item in value[field]
            ):
                return False

        for field in _METADATA_POSITIVE_INT_FIELDS:
            if field in value and (
                type(value[field]) is not int or value[field] <= 0
            ):
                return False
        if (
            "knowledge_id" in value
            and knowledge_id is not None
            and value["knowledge_id"] != knowledge_id
        ):
            return False
        for field in _METADATA_NONNEGATIVE_INT_FIELDS:
            if field in value and (
                type(value[field]) is not int or value[field] < 0
            ):
                return False
        if "word_count" in value and value["word_count"] is not None and (
            type(value["word_count"]) is not int or value["word_count"] < 0
        ):
            return False
        for field in _METADATA_FINITE_NUMBER_FIELDS:
            if field in value and (
                type(value[field]) not in {int, float}
                or not math.isfinite(value[field])
            ):
                return False
        return True
    except Exception:
        return False


@dataclass(frozen=True)
class SearchResult:
    """One ranked knowledge item returned by a retriever."""

    knowledge_id: int
    title: str
    score: float
    highlight: str
    metadata: dict[str, Any]

    def __post_init__(self) -> None:
        if type(self.knowledge_id) is not int or self.knowledge_id <= 0:
            raise ValueError("knowledge_id 必须是正整数")
        if type(self.title) is not str:
            raise TypeError("title 必须是 str")
        if (
            type(self.score) not in {int, float}
            or not 0.0 <= self.score <= 1.0
            or not math.isfinite(self.score)
        ):
            raise ValueError("分数必须是 [0.0, 1.0] 范围内的有限数值")
        if type(self.highlight) is not str:
            raise TypeError("highlight 必须是 str")
        if not _is_strict_metadata(self.metadata, knowledge_id=self.knowledge_id):
            raise TypeError("metadata 必须是安全且字段有效的 JSON dict")


SearchStatus = Literal["success", "no_hits", "invalid", "error", "degraded"]


@dataclass(frozen=True)
class RetrievalIssue:
    """Machine-readable reason for an invalid, failed, or degraded search."""

    code: ErrorCode
    message: str
    stage: str
    recoverable: bool = False
    cause_type: str | None = None

    def __post_init__(self) -> None:
        if type(self.code) is not ErrorCode:
            raise TypeError("RetrievalIssue.code 必须是 ErrorCode")
        if type(self.message) is not str or not self.message.strip():
            raise ValueError("RetrievalIssue.message 不能为空")
        if type(self.stage) is not str or not self.stage.strip():
            raise ValueError("RetrievalIssue.stage 不能为空")
        if type(self.recoverable) is not bool:
            raise TypeError("RetrievalIssue.recoverable 必须是 bool")
        if self.cause_type is not None and (
            type(self.cause_type) is not str
            or _CAUSE_TYPE_TOKEN.fullmatch(self.cause_type) is None
        ):
            raise ValueError("RetrievalIssue.cause_type 格式无效")

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "code": self.code.value,
            "message": self.message,
            "stage": self.stage,
            "recoverable": self.recoverable,
        }
        if self.cause_type is not None:
            payload["cause_type"] = self.cause_type
        return payload

    @classmethod
    def from_exception(
        cls,
        exc: BaseException,
        *,
        fallback_code: ErrorCode,
        public_message: str,
        stage: str,
        recoverable: bool,
    ) -> "RetrievalIssue":
        """Preserve stable metadata without publishing ``str(exc)``.

        Exception text may contain credentials, queries, or absolute paths.  It
        belongs in the private log only; callers must supply a fixed public
        message for the response contract.
        """

        cause_type = type(exc).__name__
        if _CAUSE_TYPE_TOKEN.fullmatch(cause_type) is None:
            cause_type = "Exception"

        if isinstance(exc, PKVRuntimeError):
            return cls(
                code=(
                    exc.code
                    if type(exc.code) is ErrorCode
                    else fallback_code
                ),
                message=public_message,
                stage=(
                    exc.stage
                    if type(exc.stage) is str and exc.stage.strip()
                    else stage
                ),
                recoverable=(
                    exc.recoverable
                    if type(exc.recoverable) is bool
                    else recoverable
                ),
                cause_type=cause_type,
            )
        return cls(
            code=fallback_code,
            message=public_message,
            stage=stage,
            recoverable=recoverable,
            cause_type=cause_type,
        )


def _is_strict_search_result(value: Any) -> bool:
    """Revalidate a frozen result, including post-construction corruption."""

    if type(value) is not SearchResult:
        return False
    try:
        return (
            type(value.knowledge_id) is int
            and value.knowledge_id > 0
            and type(value.title) is str
            and type(value.score) in {int, float}
            and 0.0 <= value.score <= 1.0
            and math.isfinite(value.score)
            and type(value.highlight) is str
            and _is_strict_metadata(
                value.metadata,
                knowledge_id=value.knowledge_id,
            )
        )
    except Exception:
        return False


def _is_strict_retrieval_issue(value: Any) -> bool:
    """Revalidate a frozen issue, including post-construction corruption."""

    if type(value) is not RetrievalIssue:
        return False
    try:
        cause_type = value.cause_type
        return (
            type(value.code) is ErrorCode
            and type(value.message) is str
            and bool(value.message.strip())
            and type(value.stage) is str
            and bool(value.stage.strip())
            and type(value.recoverable) is bool
            and (
                cause_type is None
                or (
                    type(cause_type) is str
                    and _CAUSE_TYPE_TOKEN.fullmatch(cause_type) is not None
                )
            )
        )
    except Exception:
        return False


def _materialize_tuple(value: Any, *, field_name: str) -> tuple[Any, ...]:
    """Materialize an iterable without publishing an arbitrary exception."""

    try:
        return tuple(value)
    except Exception:
        raise TypeError(f"{field_name} 必须是可迭代集合") from None


@dataclass(frozen=True)
class SearchResponse:
    """Explicit retrieval outcome.

    ``SearchResponse`` deliberately is not a list.  Callers must branch on
    ``status`` and then consume ``results``; using it as a truth value raises
    immediately so an outage cannot silently become "no hits".
    """

    status: SearchStatus
    results: tuple[SearchResult, ...] = ()
    strategy: str = "unknown"
    issues: tuple[RetrievalIssue, ...] = ()

    _STATUSES = _VALID_STATUSES

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "results",
            _materialize_tuple(self.results, field_name="results"),
        )
        object.__setattr__(
            self,
            "issues",
            _materialize_tuple(self.issues, field_name="issues"),
        )

        if type(self.status) is not str or self.status not in self._STATUSES:
            raise ValueError("未知检索状态")
        if not _is_strategy_token(self.strategy):
            raise ValueError("strategy 必须是安全的 lower_snake 标识符")
        if not all(_is_strict_search_result(result) for result in self.results):
            raise TypeError("results 只能包含有效的精确 SearchResult")
        if not all(_is_strict_retrieval_issue(issue) for issue in self.issues):
            raise TypeError("issues 只能包含有效的精确 RetrievalIssue")

        if self.status == "success":
            if not self.results:
                raise ValueError("success 必须包含至少一条结果")
            if self.issues:
                raise ValueError("success 不得携带 issue")
        elif self.status == "no_hits":
            if self.results or self.issues:
                raise ValueError("no_hits 不得携带结果或 issue")
        elif self.status == "invalid":
            if self.results:
                raise ValueError("invalid 不得携带结果")
            if not self.issues:
                raise ValueError("invalid 必须携带 issue")
            if any(
                issue.code is not ErrorCode.RETRIEVAL_INVALID_QUERY
                for issue in self.issues
            ):
                raise ValueError("invalid 只能携带 RETRIEVAL_INVALID_QUERY")
        elif self.status == "error":
            if self.results:
                raise ValueError("error 不得携带部分结果；请使用 degraded")
            if not self.issues:
                raise ValueError("error 必须携带 issue")
        elif not self.issues:
            raise ValueError("degraded 必须携带 issue")

    def __bool__(self) -> bool:
        raise TypeError("SearchResponse 没有隐式真值；请显式检查 status 或 results")

    @property
    def has_results(self) -> bool:
        return bool(self.results)

    @property
    def ok(self) -> bool:
        """Whether the requested strategy completed without invalid/degraded state."""

        return self.status in {"success", "no_hits"}

    @property
    def failed(self) -> bool:
        return self.status == "error"

    @property
    def error_code(self) -> ErrorCode | None:
        return self.issues[0].code if self.issues else None

    @property
    def error_message(self) -> str | None:
        return self.issues[0].message if self.issues else None

    @property
    def error_type(self) -> str | None:
        return self.issues[0].cause_type if self.issues else None

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "strategy": self.strategy,
            "results": [
                {
                    "knowledge_id": result.knowledge_id,
                    "title": result.title,
                    "score": result.score,
                    "highlight": result.highlight,
                    "metadata": result.metadata,
                }
                for result in self.results
            ],
            "issues": [issue.to_dict() for issue in self.issues],
        }

    @classmethod
    def completed(
        cls,
        results: Iterable[SearchResult],
        *,
        strategy: str,
    ) -> "SearchResponse":
        materialized = _materialize_tuple(results, field_name="results")
        return cls(
            status="success" if materialized else "no_hits",
            results=materialized,
            strategy=strategy,
        )

    @classmethod
    def invalid(
        cls,
        message: str,
        *,
        strategy: str,
        stage: str = "query_validation",
    ) -> "SearchResponse":
        return cls(
            status="invalid",
            strategy=strategy,
            issues=(
                RetrievalIssue(
                    code=ErrorCode.RETRIEVAL_INVALID_QUERY,
                    message=message,
                    stage=stage,
                    recoverable=True,
                ),
            ),
        )

    @classmethod
    def failed_response(
        cls,
        issue: RetrievalIssue,
        *,
        strategy: str,
    ) -> "SearchResponse":
        return cls(status="error", strategy=strategy, issues=(issue,))

    @classmethod
    def degraded_response(
        cls,
        results: Iterable[SearchResult],
        issues: Iterable[RetrievalIssue],
        *,
        strategy: str,
    ) -> "SearchResponse":
        return cls(
            status="degraded",
            results=_materialize_tuple(results, field_name="results"),
            strategy=strategy,
            issues=_materialize_tuple(issues, field_name="issues"),
        )


def is_strict_search_response(response: Any) -> bool:
    """Return whether *response* satisfies the complete retrieval contract.

    Frozen dataclasses can still be corrupted through low-level mutation or
    deserialization hooks.  Adapter and composition boundaries must therefore
    revalidate every field and terminal-state invariant before consumption.
    This function is deliberately exception-safe and has no adapter imports.
    """

    if type(response) is not SearchResponse:
        return False
    try:
        status = response.status
        results = response.results
        strategy = response.strategy
        issues = response.issues

        if type(status) is not str or status not in _VALID_STATUSES:
            return False
        if type(results) is not tuple or type(issues) is not tuple:
            return False
        if not _is_strategy_token(strategy):
            return False
        if not all(_is_strict_search_result(result) for result in results):
            return False
        if not all(_is_strict_retrieval_issue(issue) for issue in issues):
            return False

        if status == "success":
            return bool(results) and not issues
        if status == "no_hits":
            return not results and not issues
        if status == "invalid":
            return (
                not results
                and bool(issues)
                and all(
                    issue.code is ErrorCode.RETRIEVAL_INVALID_QUERY
                    for issue in issues
                )
            )
        if status == "error":
            return not results and bool(issues)
        return bool(issues)
    except Exception:
        return False


__all__ = [
    "RetrievalIssue",
    "SearchResponse",
    "SearchResult",
    "SearchStatus",
    "is_strict_search_response",
]
