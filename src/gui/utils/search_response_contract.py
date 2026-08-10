"""Lightweight validation for retrieval responses consumed by the GUI.

The GUI must not import :mod:`src.retrieval` while its views are imported:
that package also exports vector retrievers and would turn a small response
check into eager vector initialization.  This module therefore validates the
runtime contract without importing the retrieval package.

The checks intentionally mirror ``SearchResponse.__post_init__``.  Merely
having ``status``, ``results`` and ``issues`` attributes is not sufficient: an
inconsistent terminal state must fail closed at the adapter boundary.
"""

from __future__ import annotations

import math
import re
import sys
from typing import Any


_RESULT_MODULE = "src.retrieval.result"
_ERROR_MODULE = "src.runtime.errors"
_VALID_STATUSES = frozenset(
    {"success", "no_hits", "invalid", "error", "degraded"}
)
_INVALID_QUERY_CODE = "retrieval_invalid_query"
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


def _is_exact_contract_type(value: Any, expected_name: str) -> bool:
    """Check a retrieval contract type without importing its package."""

    loaded_module = sys.modules.get(_RESULT_MODULE)
    expected_type = getattr(loaded_module, expected_name, None)
    return expected_type is not None and type(value) is expected_type


def _is_json_safe_metadata_value(
    value: Any,
    *,
    depth: int = 0,
    ancestors: set[int] | None = None,
) -> bool:
    """Mirror the neutral result's exact, finite JSON-tree boundary."""

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


def _is_strict_metadata(value: Any, *, knowledge_id: int) -> bool:
    """Mirror metadata fields consumed by retrieval and GUI adapters."""

    if type(value) is not dict or not _is_json_safe_metadata_value(value):
        return False
    try:
        for field in _METADATA_STRING_FIELDS:
            if (
                field in value
                and value[field] is not None
                and type(value[field]) is not str
            ):
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
        if "knowledge_id" in value and value["knowledge_id"] != knowledge_id:
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


def _is_search_result(value: Any) -> bool:
    """Mirror every strict ``SearchResult`` field invariant."""

    if not _is_exact_contract_type(value, "SearchResult"):
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


def _is_retrieval_issue(value: Any) -> bool:
    """Mirror every strict ``RetrievalIssue`` field invariant."""

    if not _is_exact_contract_type(value, "RetrievalIssue"):
        return False
    try:
        loaded_module = sys.modules.get(_ERROR_MODULE)
        error_code_type = getattr(loaded_module, "ErrorCode", None)
        cause_type = value.cause_type
        return (
            error_code_type is not None
            and type(value.code) is error_code_type
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


def is_strict_search_response(response: Any) -> bool:
    """Return whether *response* satisfies the complete five-state contract.

    This is deliberately an exception-safe boundary check.  Any missing,
    malformed, or internally inconsistent field returns ``False`` so callers
    render an adapter failure instead of success or "no hits".
    """

    if not _is_exact_contract_type(response, "SearchResponse"):
        return False

    try:
        status = response.status
        results = response.results
        issues = response.issues
        strategy = response.strategy

        if type(status) is not str or status not in _VALID_STATUSES:
            return False
        if type(results) is not tuple or type(issues) is not tuple:
            return False
        if not _is_strategy_token(strategy):
            return False
        if not all(_is_search_result(result) for result in results):
            return False
        if not all(_is_retrieval_issue(issue) for issue in issues):
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
                    issue.code.value == _INVALID_QUERY_CODE
                    for issue in issues
                )
            )
        if status == "error":
            return not results and bool(issues)

        # ``degraded`` may contain zero or more usable results, but it must
        # always explain the degradation with at least one issue.
        return bool(issues)
    except Exception:
        return False


__all__ = ["is_strict_search_response"]
