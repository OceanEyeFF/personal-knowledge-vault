"""
CLI commands for Personal Knowledge Vault (PKV).

实现核心命令：archive / archive-text / search / show / list / tags /
related / config / stats
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import stat
import sys
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Dict, List, Optional, Sequence, Tuple

import click
import yaml
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from src.application import KnowledgeApplication, get_application
from src.application.validation import validate_text_length
from src.utils.config import (
    Config,
    redact_url_credentials as _redact_url_credentials,
    set_yaml_config_value,
    url_contains_credentials as _url_contains_credentials,
)
from src.workflow.steps import _grant_cli_local_file_import
from src.relations.citations import is_local_reference, sanitize_public_source_url
from src.retrieval.result import (
    RetrievalIssue,
    SearchResponse,
    is_strict_search_response,
)
from src.runtime.errors import ErrorCode, PKVRuntimeError
from src.runtime.layout import validate_path_components
from src.storage.markdown_store import Entry
from src.utils.logger import get_logger


console = Console()
logger = get_logger(__name__)

_PUBLIC_DIAGNOSTIC_TOKEN = re.compile(r"^[A-Za-z0-9_.:-]{1,96}$")
_PUBLIC_OPERATION_ID = re.compile(r"^[0-9a-f]{32}$")
_TERMINAL_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_URI_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
_WINDOWS_ABSOLUTE_DRIVE = re.compile(r"^[A-Za-z]:[\\/]")
_MAX_TAG_LIST_LIMIT = 200
_ARCHIVE_PUBLIC_STAGES = frozenset(
    {
        "archive_text",
        "archive_url",
        "completed",
        "compensating",
        "index",
        "index_committed",
        "preparing",
        "primary_committed",
        "provider_configuration",
        "sqlite_index",
        "storage_finalize",
        "url_preflight",
        "vector_committed",
        "vector_index",
        "workflow",
        "workflow_analyze",
        "workflow_contract",
        "workflow_fetch",
        "workflow_local_file_capability",
        "workflow_processor_selection",
        "workflow_review",
        "workflow_review_editor",
        "workflow_step",
    }
)
_ARCHIVE_STEP_IDS = frozenset(
    {"fetch_content", "ai_analyze", "idea_sharpen", "review_entry", "store_entry"}
)
_RETRIEVAL_PUBLIC_STAGES = frozenset(
    {
        "bm25_metadata",
        "bm25_query",
        "chunk_hit_mapping",
        "chunk_index_metadata",
        "chunk_limit_validation",
        "chunk_metadata_mapping",
        "chunk_metadata_read",
        "chunk_query_validation",
        "chunk_result_mapping",
        "chunk_vector_index_search",
        "cli_search",
        "cli_search_protocol",
        "embedding_protocol",
        "hybrid_executor",
        "hybrid_fusion",
        "limit_validation",
        "metadata_hydration",
        "provider_configuration",
        "provider_connect",
        "query_router_tokenize",
        "query_validation",
        "related_entry_lookup",
        "retrieval",
        "strategy_validation",
        "document_vector",
        "vector_hit_mapping",
        "vector_index",
        "vector_index_load",
        "vector_index_pair_load",
        "vector_index_probe",
        "vector_index_search",
        "vector_metadata_mapping",
        "vector_metadata_read",
        "vector_result_mapping",
        "vector_related",
    }
)
_RETRIEVAL_PUBLIC_MESSAGES = {
    ErrorCode.RETRIEVAL_INVALID_QUERY.value: "查询条件无效",
    ErrorCode.RETRIEVAL_BACKEND_FAILED.value: "检索服务暂不可用",
    ErrorCode.RETRIEVAL_INDEX_UNAVAILABLE.value: "检索索引不可用",
    ErrorCode.RETRIEVAL_METADATA_INCONSISTENT.value: "检索结果元数据不一致",
    ErrorCode.PROVIDER_CONFIG_INVALID.value: "检索 Provider 配置无效",
    ErrorCode.PROVIDER_UNAVAILABLE.value: "检索 Provider 暂不可用",
    ErrorCode.PROVIDER_PROTOCOL_FAILED.value: "检索 Provider 响应无效",
    ErrorCode.PATH_STATE_UNDETERMINED.value: "检索索引状态不可判定",
}
_CLI_RESPONSE_STRATEGIES = {
    "bm25": frozenset({"bm25"}),
    "vector": frozenset({"vector"}),
    "hybrid": frozenset({"hybrid"}),
    "auto": frozenset({"router", "bm25", "hybrid"}),
}
_ARCHIVE_STATUSES = frozenset(
    {"ready", "degraded", "repair_required", "rejected", "deleted", "error"}
)
_COMPLETED_ARCHIVE_STATUSES = frozenset({"ready", "degraded"})
_WORKFLOW_TERMINALS = frozenset({"success", "degraded", "error"})
_MISSING_TERMINAL = object()
_ARCHIVE_REPAIR_ACTIONS = frozenset(
    {
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
    }
)
_ARCHIVE_PUBLIC_MESSAGES = {
    ErrorCode.RESOURCE_MISSING.value: "归档所需运行资源缺失",
    ErrorCode.RESOURCE_NOT_READABLE.value: "归档所需运行资源不可读取",
    ErrorCode.DATA_ROOT_UNSAFE.value: "数据目录未通过安全检查",
    ErrorCode.PATH_OUTSIDE_VAULT.value: "归档路径超出知识库范围",
    ErrorCode.PATH_LINK_UNSAFE.value: "归档路径包含不安全链接",
    ErrorCode.PATH_STATE_UNDETERMINED.value: "无法安全确认归档路径状态",
    ErrorCode.PATH_NOT_REGULAR_FILE.value: "归档目标不是常规文件",
    ErrorCode.DATABASE_MISSING.value: "知识库数据库缺失",
    ErrorCode.DATABASE_NOT_SQLITE.value: "知识库数据库格式无效",
    ErrorCode.DATABASE_VERSION_TABLE_MISSING.value: "知识库版本信息缺失",
    ErrorCode.DATABASE_VERSION_TABLE_INVALID.value: "知识库版本信息无效",
    ErrorCode.DATABASE_INTEGRITY_FAILED.value: "知识库完整性检查失败",
    ErrorCode.DATABASE_SCHEMA_DRIFT.value: "知识库结构与当前版本不一致",
    ErrorCode.DATABASE_UPGRADE_REQUIRED.value: "知识库需要先升级",
    ErrorCode.DATABASE_FUTURE_VERSION.value: "知识库版本高于当前应用版本",
    ErrorCode.MIGRATION_BACKUP_FAILED.value: "知识库升级备份失败",
    ErrorCode.MIGRATION_LOCKED.value: "知识库正在被其他升级操作占用",
    ErrorCode.MIGRATION_FAILED.value: "知识库升级失败",
    ErrorCode.STORAGE_PRIMARY_FAILED.value: "核心内容存储失败",
    ErrorCode.STORAGE_INDEX_FAILED.value: "知识索引写入失败",
    ErrorCode.STORAGE_VECTOR_FAILED.value: "向量索引写入失败",
    ErrorCode.STORAGE_COMPENSATION_FAILED.value: "存储回滚失败",
    ErrorCode.STORAGE_REPAIR_REQUIRED.value: "核心存储已提交，需要修复",
    ErrorCode.WORKFLOW_CONFIG_INVALID.value: "归档工作流配置无效",
    ErrorCode.WORKFLOW_STEP_FAILED.value: "归档步骤未能完成",
    ErrorCode.WORKFLOW_CONDITION_INVALID.value: "归档工作流条件无效",
    ErrorCode.WORKFLOW_PROCESSOR_UNKNOWN.value: "归档处理器未注册",
    ErrorCode.PROVIDER_CONFIG_INVALID.value: "AI Provider 配置无效",
    ErrorCode.PROVIDER_UNAVAILABLE.value: "AI Provider 暂不可用",
    ErrorCode.PROVIDER_PROTOCOL_FAILED.value: "AI Provider 响应无效",
    ErrorCode.URL_INVALID.value: "归档 URL 无效",
    ErrorCode.SSRF_TARGET_FORBIDDEN.value: "归档 URL 目标不允许访问",
    ErrorCode.SSRF_RESOLUTION_FAILED.value: "归档 URL 目标解析失败",
    ErrorCode.SSRF_REDIRECT_LIMIT.value: "归档 URL 重定向次数过多",
}

CONFIG_KEY_ALIASES = {
    "data_dir": lambda config: config.data_dir,
    "vault_dir": lambda config: config.vault_dir,
    "db_path": lambda config: config.db_path,
    "vector_index_dir": lambda config: config.vector_index_dir,
    "log_dir": lambda config: config.log_dir,
    "tmp_dir": lambda config: config.tmp_dir,
}

LEGACY_CONFIG_KEYS = {
    "DEEPSEEK_API_KEY": "ai.llm.api_key",
    "DEEPSEEK_BASE_URL": "ai.llm.base_url",
    "DEEPSEEK_MODEL": "ai.llm.model",
    "OPENAI_API_KEY": "ai.embedding.api_key",
    "OPENAI_BASE_URL": "ai.embedding.base_url",
    "OPENAI_EMBEDDING_DIM": "ai.embedding.dim",
    "OPENAI_EMBEDDING_MODEL": "ai.embedding.model",
    "PKV_LLM_BASE_URL": "ai.llm.base_url",
    "PKV_LLM_API_KEY": "ai.llm.api_key",
    "PKV_LLM_MODEL": "ai.llm.model",
    "PKV_EMBD_BASE_URL": "ai.embedding.base_url",
    "PKV_EMBD_API_KEY": "ai.embedding.api_key",
    "PKV_EMBD_MODEL": "ai.embedding.model",
    "PKV_EMBD_DIM": "ai.embedding.dim",
}

SENSITIVE_CONFIG_KEYS = {
    "ai.llm.api_key",
    "ai.embedding.api_key",
    "processors.zhihu.cookie",
}
SENSITIVE_CONFIG_KEY_PARTS = {
    "access_token",
    "api_key",
    "apikey",
    "auth",
    "auth_token",
    "authorization",
    "basic_auth",
    "bearer",
    "bearer_token",
    "client_secret",
    "cookie",
    "credential",
    "credentials",
    "jsession_id",
    "jsessionid",
    "jwt",
    "pass",
    "passcode",
    "passphrase",
    "passwd",
    "password",
    "private_key",
    "pwd",
    "refresh_token",
    "secret",
    "session_id",
    "sessionid",
    "sid",
    "sig",
    "signature",
    "subscription_key",
    "token",
}


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _load_config() -> Config:
    config = Config()
    config.ensure_dirs()
    return config


def _parse_tags(tags: Optional[str]) -> List[str]:
    if not tags:
        return []
    parts = [part.strip() for part in tags.split(",")]
    return [part for part in parts if part]


def _format_bytes(size: int) -> str:
    if size <= 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(size)
    for unit in units:
        if value < 1024.0:
            return f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} PB"


def _dir_size(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    total = 0
    for item in path.rglob("*"):
        if item.is_file():
            total += item.stat().st_size
    return total


class _BackendReadContractError(RuntimeError):
    """A storage adapter returned a malformed read projection."""


_ENTRY_REQUIRED_FIELDS = frozenset(
    {
        "knowledge_id",
        "title",
        "source_type",
        "source_url",
        "file_path",
        "archived_at",
    }
)
_LIST_REQUIRED_FIELDS = frozenset(
    {"knowledge_id", "title", "source_type", "archived_at", "tags"}
)


def _require_nonnegative_count(value: Any) -> int:
    if type(value) is not int or value < 0:
        raise _BackendReadContractError
    return value


def _validate_entry_projection(value: Any) -> Optional[Dict[str, Any]]:
    """Distinguish exact not-found from a corrupt storage projection."""

    if value is None:
        return None
    if type(value) is not dict or not _ENTRY_REQUIRED_FIELDS.issubset(value):
        raise _BackendReadContractError
    if type(value["knowledge_id"]) is not int or value["knowledge_id"] <= 0:
        raise _BackendReadContractError
    if type(value["title"]) is not str or not value["title"]:
        raise _BackendReadContractError
    if type(value["source_type"]) is not str or not value["source_type"]:
        raise _BackendReadContractError
    if value["source_url"] is not None and type(value["source_url"]) is not str:
        raise _BackendReadContractError
    if type(value["file_path"]) is not str or not value["file_path"]:
        raise _BackendReadContractError
    if value["archived_at"] is not None and type(value["archived_at"]) is not str:
        raise _BackendReadContractError
    return value


def _validate_list_projection(value: Any) -> List[Dict[str, Any]]:
    if type(value) is not list:
        raise _BackendReadContractError
    rows: List[Dict[str, Any]] = []
    for row in value:
        if type(row) is not dict or not _LIST_REQUIRED_FIELDS.issubset(row):
            raise _BackendReadContractError
        if type(row["knowledge_id"]) is not int or row["knowledge_id"] <= 0:
            raise _BackendReadContractError
        if type(row["title"]) is not str or not row["title"]:
            raise _BackendReadContractError
        if type(row["source_type"]) is not str or not row["source_type"]:
            raise _BackendReadContractError
        if type(row["archived_at"]) is not str or not row["archived_at"]:
            raise _BackendReadContractError
        if row["tags"] is not None and type(row["tags"]) is not str:
            raise _BackendReadContractError
        rows.append(row)
    return rows


def _get_entry_by_id(store: Any, knowledge_id: int) -> Optional[Dict[str, Any]]:
    return _validate_entry_projection(store.query_by_id(knowledge_id))


def _get_entry_by_url(store: Any, url: str) -> Optional[Dict[str, Any]]:
    return _validate_entry_projection(store.query_by_url(url))


def _query_entries(
    store: Any,
    tag: Optional[str],
    order_by: str,
    desc: bool,
    limit: Optional[int],
) -> List[Dict[str, Any]]:
    return _validate_list_projection(
        store.list_entries(
            limit=limit,
            sort_by=order_by,
            sort_order="desc" if desc else "asc",
            tag=tag,
        )
    )


def _count_entries(store: Any) -> int:
    return _require_nonnegative_count(store.count_entries())


def _count_entries_by_source_type(store: Any) -> List[Tuple[str, int]]:
    value = store.count_entries_by_source_type()
    if type(value) is not list:
        raise _BackendReadContractError
    rows: List[Tuple[str, int]] = []
    for row in value:
        if type(row) is not tuple or len(row) != 2:
            raise _BackendReadContractError
        source, count = row
        if type(source) is not str or not source:
            raise _BackendReadContractError
        rows.append((source, _require_nonnegative_count(count)))
    return rows


def _get_top_tags(store: Any, limit: int = 10) -> List[Tuple[str, int]]:
    value = store.get_all_tags_with_count(limit=limit)
    if type(value) is not list:
        raise _BackendReadContractError
    rows: List[Tuple[str, int]] = []
    for row in value:
        if type(row) is not dict or set(row) != {"name", "count"}:
            raise _BackendReadContractError
        name = row["name"]
        if type(name) is not str or not name:
            raise _BackendReadContractError
        rows.append((name, _require_nonnegative_count(row["count"])))
    if len(rows) > limit or len({name for name, _ in rows}) != len(rows):
        raise _BackendReadContractError
    return rows


_RELATED_ENTRY_REQUIRED_FIELDS = frozenset(
    {"summary_one_sentence", "tags"}
)


def _get_related_entry_by_id(
    store: Any,
    knowledge_id: int,
) -> Optional[Dict[str, Any]]:
    """Read the exact SQLite projection consumed by ``related``."""

    entry = _get_entry_by_id(store, knowledge_id)
    if entry is None:
        return None
    if not _RELATED_ENTRY_REQUIRED_FIELDS.issubset(entry):
        raise _BackendReadContractError
    if entry["summary_one_sentence"] is not None and type(
        entry["summary_one_sentence"]
    ) is not str:
        raise _BackendReadContractError
    if entry["tags"] is not None and type(entry["tags"]) is not str:
        raise _BackendReadContractError
    return entry


def _stored_tags(value: Any) -> List[str]:
    """Project the SQLite comma-separated tag field without accepting drift."""

    if value is None or value == "":
        return []
    if type(value) is not str:
        raise _BackendReadContractError
    return [part.strip() for part in value.split(",") if part.strip()]


def _safe_terminal_text(value: Any, *, limit: int = 240) -> Text:
    """Render untrusted table/panel fields without Rich markup or control bytes.

    JSON output deliberately preserves its data contract. Human-facing output
    instead receives a bounded :class:`~rich.text.Text` instance, so a title,
    tag, or abstract cannot inject Rich markup or terminal control characters.
    """

    rendered = value if type(value) is str else str(value)
    rendered = _TERMINAL_CONTROL_CHARACTERS.sub(" ", rendered)
    if len(rendered) > limit:
        rendered = rendered[:limit] + "..."
    return Text(rendered)


def _normalise_archive_text_title(title: str) -> str:
    """Accept a human title, never a path-shaped or control-bearing value."""

    normalized = title.strip()
    if not normalized:
        return ""
    if (
        normalized in {".", ".."}
        or "/" in normalized
        or "\\" in normalized
        or _TERMINAL_CONTROL_CHARACTERS.search(normalized) is not None
    ):
        raise ValueError("archive-text title is not a safe display value")
    return normalized


def _related_issue(
    code: ErrorCode,
    *,
    stage: str,
    recoverable: bool,
) -> Dict[str, Any]:
    return _public_retrieval_issue(
        RetrievalIssue(
            code=code,
            message=_RETRIEVAL_PUBLIC_MESSAGES.get(code.value, "检索服务暂不可用"),
            stage=stage,
            recoverable=recoverable,
        )
    )


def _related_failure_payload(
    exc: BaseException,
    *,
    stage: str,
    message: str,
) -> Dict[str, Any]:
    """Return a sanitized fail-closed related-query payload."""

    logger.error(
        "related CLI 读取失败: stage=%s, error_type=%s",
        stage,
        type(exc).__name__,
    )
    issue = RetrievalIssue.from_exception(
        exc,
        fallback_code=ErrorCode.RETRIEVAL_BACKEND_FAILED,
        public_message=message,
        stage=stage,
        recoverable=False,
    )
    return {
        "status": "error",
        "strategy": "vector_related",
        "total": 0,
        "results": [],
        "issues": [_public_retrieval_issue(issue)],
        "message": "向量关联查询不可用",
    }


def _related_payload(knowledge_id: str, limit: int) -> Dict[str, Any]:
    """Run the read-only vector-neighbour lookup used by ``related``.

    This adapter intentionally consumes only existing index artifacts.  It never
    creates an index or a Provider, so an offline user-data root can report an
    explicit ``degraded`` state instead of turning a missing auxiliary index into
    a write or a false empty result.
    """

    try:
        if type(knowledge_id) is not str:
            raise ValueError
        related_id = int(knowledge_id)
        if related_id <= 0:
            raise ValueError
    except (TypeError, ValueError):
        message = "无效的 knowledge_id，需要正整数"
        return {
            "status": "invalid",
            "strategy": "vector_related",
            "total": 0,
            "results": [],
            "issues": [
                _related_issue(
                    ErrorCode.RETRIEVAL_INVALID_QUERY,
                    stage="related_entry_lookup",
                    recoverable=True,
                )
            ],
            "message": message,
        }

    if type(limit) is not int or isinstance(limit, bool) or limit <= 0:
        message = "limit 必须是正整数"
        return {
            "status": "invalid",
            "strategy": "vector_related",
            "total": 0,
            "results": [],
            "issues": [
                _related_issue(
                    ErrorCode.RETRIEVAL_INVALID_QUERY,
                    stage="limit_validation",
                    recoverable=True,
                )
            ],
            "message": message,
        }

    safe_limit = min(limit, 20)
    try:
        config = _load_config()
        application = get_application(config)
        store = application.sqlite_store
        seed_entry = _get_related_entry_by_id(store, related_id)
    except Exception as exc:
        return _related_failure_payload(
            exc,
            stage="related_entry_lookup",
            message="条目读取失败",
        )

    if seed_entry is None:
        return {
            "status": "no_hits",
            "strategy": "vector_related",
            "total": 0,
            "results": [],
            "issues": [],
            "message": "未找到条目",
        }

    try:
        vector_store = application.readonly_vector_store
        if vector_store is None:
            return {
                "status": "degraded",
                "strategy": "vector_related",
                "total": 0,
                "results": [],
                "issues": [
                    _related_issue(
                        ErrorCode.RETRIEVAL_INDEX_UNAVAILABLE,
                        stage="vector_index",
                        recoverable=True,
                    )
                ],
                "message": "向量索引不可用，无法获取关联知识",
            }

        document_vector = vector_store.get_doc_vector(related_id)
        if document_vector is None:
            return {
                "status": "degraded",
                "strategy": "vector_related",
                "total": 0,
                "results": [],
                "issues": [
                    _related_issue(
                        ErrorCode.RETRIEVAL_INDEX_UNAVAILABLE,
                        stage="document_vector",
                        recoverable=True,
                    )
                ],
                "message": "该条目暂无向量，无法获取关联知识",
            }

        raw_results = vector_store.search_doc(document_vector, k=safe_limit + 1)
        if (
            type(raw_results) is not list
            or len(raw_results) > safe_limit + 1
            or not all(
                type(item) is tuple
                and len(item) == 2
                and type(item[0]) is int
                and item[0] > 0
                and type(item[1]) in {int, float}
                and math.isfinite(item[1])
                for item in raw_results
            )
            or len({item[0] for item in raw_results}) != len(raw_results)
        ):
            raise _BackendReadContractError

        results: List[Dict[str, Any]] = []
        for neighbour_id, distance in raw_results:
            if neighbour_id == related_id:
                continue
            if len(results) >= safe_limit:
                break
            neighbour = _get_related_entry_by_id(store, neighbour_id)
            if neighbour is None:
                raise PKVRuntimeError(
                    ErrorCode.RETRIEVAL_METADATA_INCONSISTENT,
                    "related vector metadata is inconsistent",
                    stage="vector_metadata_read",
                    recoverable=False,
                )
            results.append(
                {
                    "knowledge_id": neighbour_id,
                    "title": neighbour["title"],
                    "abstract": neighbour["summary_one_sentence"] or "",
                    "tags": _stored_tags(neighbour["tags"]),
                    "source_type": neighbour["source_type"],
                    "score": round(min(1.0, max(0.0, 1.0 - distance)), 4),
                }
            )
    except Exception as exc:
        return _related_failure_payload(
            exc,
            stage="vector_related",
            message="向量关联查询不可用",
        )

    payload: Dict[str, Any] = {
        "status": "success" if results else "no_hits",
        "strategy": "vector_related",
        "total": len(results),
        "results": results,
        "issues": [],
    }
    if not results:
        payload["message"] = "未找到关联条目"
    return payload


def _extract_result_id(result: Any) -> Optional[int]:
    if result is None:
        return None
    if hasattr(result, "entry_id"):
        return getattr(result, "entry_id")
    if hasattr(result, "knowledge_id"):
        return getattr(result, "knowledge_id")
    if isinstance(result, dict):
        return result.get("entry_id") or result.get("knowledge_id")
    return None


def _extract_result_snippet(result: Any) -> str:
    if result is None:
        return ""
    for key in ("snippet", "highlight"):
        if hasattr(result, key):
            return getattr(result, key) or ""
        if isinstance(result, dict) and key in result:
            return result.get(key) or ""
    metadata = None
    if hasattr(result, "metadata"):
        metadata = getattr(result, "metadata")
    elif isinstance(result, dict):
        metadata = result.get("metadata")
    if isinstance(metadata, dict):
        return (
            metadata.get("summary_one_sentence")
            or metadata.get("summary_100_words")
            or ""
        )
    return ""


def _extract_result_metadata(result: Any) -> Dict[str, Any]:
    if result is None:
        return {}
    if hasattr(result, "metadata"):
        return getattr(result, "metadata") or {}
    if isinstance(result, dict):
        return result.get("metadata") or {}
    return {}


def _result_to_dict(result: Any) -> Dict[str, Any]:
    return {
        "entry_id": _extract_result_id(result),
        "title": getattr(result, "title", None)
        if not isinstance(result, dict)
        else result.get("title"),
        "snippet": _extract_result_snippet(result),
        "score": getattr(result, "score", None)
        if not isinstance(result, dict)
        else result.get("score"),
        "metadata": _extract_result_metadata(result),
    }


def _render_search_table(results: Sequence[Any], title: str) -> Table:
    table = Table(title=title)
    table.add_column("ID", style="cyan", justify="right")
    table.add_column("标题", style="bold")
    table.add_column("得分", style="green", justify="right")
    table.add_column("片段", style="")

    for result in results:
        rid = _extract_result_id(result)
        title_text = getattr(result, "title", None) if not isinstance(result, dict) else result.get("title")
        score = getattr(result, "score", None) if not isinstance(result, dict) else result.get("score")
        snippet = _extract_result_snippet(result)

        table.add_row(
            str(rid) if rid is not None else "-",
            title_text or "(无标题)",
            f"{score:.3f}" if isinstance(score, (float, int)) else "-",
            snippet[:80] + ("..." if snippet and len(snippet) > 80 else ""),
        )

    return table


def _issue_to_dict(issue: Any) -> Dict[str, Any]:
    """Normalize workflow/retrieval issues without discarding their error code."""
    if hasattr(issue, "to_dict") and callable(issue.to_dict):
        return dict(issue.to_dict())
    if isinstance(issue, dict):
        payload = dict(issue)
    else:
        payload = {
            key: getattr(issue, key)
            for key in (
                "code",
                "message",
                "severity",
                "stage",
                "step_id",
                "recoverable",
                "cause_type",
            )
            if hasattr(issue, key)
        }
    code = payload.get("code")
    if hasattr(code, "value"):
        payload["code"] = code.value
    return payload


def _issue_text(issue: Any) -> str:
    payload = _public_retrieval_issue(issue)
    code = payload["code"]
    message = payload["message"]
    stage = payload["stage"]
    prefix = f"{code}: " if code else ""
    suffix = f" ({stage})" if stage else ""
    return f"{prefix}{message}{suffix}"


def _public_retrieval_issue(issue: Any) -> Dict[str, Any]:
    """Project one retrieval issue through fixed code/message/stage allowlists."""

    payload = _issue_to_dict(issue)
    raw_code = payload.get("code")
    code = str(getattr(raw_code, "value", raw_code or ""))
    if code not in _RETRIEVAL_PUBLIC_MESSAGES:
        code = ErrorCode.RETRIEVAL_BACKEND_FAILED.value
    raw_stage = payload.get("stage")
    stage = (
        raw_stage
        if type(raw_stage) is str and raw_stage in _RETRIEVAL_PUBLIC_STAGES
        else "retrieval"
    )
    return {
        "code": code,
        "message": _RETRIEVAL_PUBLIC_MESSAGES[code],
        "stage": stage,
        "recoverable": payload.get("recoverable") is True,
    }


def _safe_diagnostic_token(value: Any, fallback: str) -> str:
    normalized = str(getattr(value, "value", value or ""))
    return (
        normalized
        if _PUBLIC_DIAGNOSTIC_TOKEN.fullmatch(normalized)
        else fallback
    )


def _archive_error_code(value: Any) -> str:
    normalized = _safe_diagnostic_token(
        value,
        ErrorCode.WORKFLOW_STEP_FAILED.value,
    )
    return (
        normalized
        if normalized in _ARCHIVE_PUBLIC_MESSAGES
        else ErrorCode.WORKFLOW_STEP_FAILED.value
    )


def _normalise_archive_issue(
    issue: Any,
    *,
    default_severity: str,
) -> Dict[str, Any]:
    payload = _issue_to_dict(issue)
    code = _archive_error_code(payload.get("code"))
    severity = _safe_diagnostic_token(payload.get("severity"), default_severity)
    if severity not in {"warning", "error"}:
        severity = default_severity
    message = _ARCHIVE_PUBLIC_MESSAGES[code]
    if severity == "warning" and code == ErrorCode.WORKFLOW_STEP_FAILED.value:
        message = "归档步骤已降级"
    raw_stage = payload.get("stage")
    stage = (
        raw_stage
        if type(raw_stage) is str and raw_stage in _ARCHIVE_PUBLIC_STAGES
        else "workflow"
    )
    normalized = {
        "code": code,
        "message": message,
        "severity": severity,
        "stage": stage,
        "recoverable": payload.get("recoverable") is True,
    }
    raw_step_id = payload.get("step_id")
    if raw_step_id is not None:
        normalized["step_id"] = (
            raw_step_id
            if type(raw_step_id) is str and raw_step_id in _ARCHIVE_STEP_IDS
            else "unknown_step"
        )
    return normalized


def _resolve_workflow_terminal(result: Any) -> Tuple[str, bool]:
    try:
        raw_terminal = getattr(result, "terminal", _MISSING_TERMINAL)
        raw_success = getattr(result, "success", _MISSING_TERMINAL)
    except Exception as exc:
        logger.error(
            "archive CLI 无法读取工作流终态: error_type=%s",
            type(exc).__name__,
        )
        return "error", False
    if (
        isinstance(raw_terminal, str)
        and raw_terminal in _WORKFLOW_TERMINALS
        and type(raw_success) is bool
        and raw_success == (raw_terminal != "error")
    ):
        if raw_terminal != "error" and not _valid_completion_diagnostics(
            result,
            terminal=raw_terminal,
        ):
            logger.error(
                "archive CLI 收到不一致的完成诊断: terminal=%s",
                raw_terminal,
            )
            return "error", False
        return raw_terminal, True
    logger.error(
        "archive CLI 收到无效工作流终态: present=%s, type=%s, "
        "success_type=%s",
        raw_terminal is not _MISSING_TERMINAL,
        type(raw_terminal).__name__,
        type(raw_success).__name__,
    )
    return "error", False


def _valid_completion_diagnostics(result: Any, *, terminal: str) -> bool:
    """Reject completed results that hide errors or contradict their terminal."""

    try:
        errors = getattr(result, "errors", None)
        warnings = getattr(result, "warnings", None)
        issues = getattr(result, "issues", None)
    except Exception:
        return False
    if type(errors) is not list or type(warnings) is not list or type(issues) is not list:
        return False
    if errors or not all(type(warning) is str for warning in warnings):
        return False
    if not all(type(issue) is dict for issue in issues):
        return False
    if any(issue.get("severity") != "warning" for issue in issues):
        return False
    if terminal == "success":
        return not warnings and not issues
    return bool(warnings or issues)


def _valid_archive_entry(value: Any) -> bool:
    """Validate every Entry field rendered after a completed archive."""

    if type(value) is not Entry:
        return False
    try:
        return (
            type(value.title) is str
            and (value.source_url is None or type(value.source_url) is str)
            and type(value.tags) is list
            and all(type(tag) is str for tag in value.tags)
            and type(value.summary_100_words) is str
        )
    except Exception:
        return False


def _valid_archive_result_data(value: Any, *, terminal: str) -> bool:
    """Require an explicit committed storage outcome before publishing success."""

    if type(value) is not dict:
        return False
    knowledge_id = value.get("knowledge_id")
    status = value.get("status")
    valid_status = (
        status == "ready"
        if terminal == "success"
        else status in _COMPLETED_ARCHIVE_STATUSES
    )
    return (
        type(knowledge_id) is int
        and knowledge_id > 0
        and type(status) is str
        and valid_status
        and value.get("core_committed") is True
        and _valid_archive_entry(value.get("entry"))
    )


def _workflow_public_notices(
    result: Any,
    *,
    error: bool,
    contract_invalid: bool = False,
) -> List[Dict[str, Any]]:
    default_severity = "error" if error else "warning"
    notices = [
        _normalise_archive_issue(issue, default_severity=default_severity)
        for issue in (getattr(result, "issues", None) or [])
    ]
    if contract_invalid:
        notices.insert(
            0,
            _normalise_archive_issue(
                {
                    "code": ErrorCode.WORKFLOW_STEP_FAILED,
                    "severity": "error",
                    "stage": "workflow_contract",
                    "recoverable": False,
                },
                default_severity="error",
            ),
        )
    severities = {notice["severity"] for notice in notices}
    if (getattr(result, "warnings", None) or []) and "warning" not in severities:
        notices.append(
            _normalise_archive_issue({}, default_severity="warning")
        )
    if error and (getattr(result, "errors", None) or []) and "error" not in severities:
        notices.append(_normalise_archive_issue({}, default_severity="error"))
    if error and not notices:
        notices.append(_normalise_archive_issue({}, default_severity="error"))
    return notices


def _print_workflow_notices(
    result: Any,
    *,
    error: bool = False,
    contract_invalid: bool = False,
) -> None:
    """Expose only allow-listed workflow diagnostics at the CLI boundary."""
    for notice in _workflow_public_notices(
        result,
        error=error,
        contract_invalid=contract_invalid,
    ):
        style = "red" if notice["severity"] == "error" else "yellow"
        recoverable = "true" if notice["recoverable"] else "false"
        console.print(
            f"[{style}]- {notice['message']} "
            f"(code={notice['code']}, stage={notice['stage']}, "
            f"recoverable={recoverable})[/{style}]"
        )


def _safe_operation_id(value: Any) -> str:
    if value is None:
        return ""
    if type(value) is not str:
        return "已隐藏"
    normalized = value
    if not normalized:
        return ""
    return normalized if _PUBLIC_OPERATION_ID.fullmatch(normalized) else "已隐藏"


def _safe_archive_status(value: Any, *, error: bool) -> str:
    if type(value) is not str:
        return "error" if error else "unknown"
    normalized = value
    if normalized in _ARCHIVE_STATUSES:
        return normalized
    return "error" if error else "unknown"


def _safe_repair_actions(value: Any) -> List[str]:
    if not isinstance(value, (list, tuple)):
        return []
    actions = [
        action
        for action in value
        if type(action) is str and action in _ARCHIVE_REPAIR_ACTIONS
    ]
    if value and not actions:
        return ["查看诊断日志"]
    return list(dict.fromkeys(actions))


def _safe_archive_source(value: Any) -> str:
    raw = str(value or "")
    if not raw:
        return ""
    public_url = sanitize_public_source_url(raw)
    if public_url:
        return public_url
    try:
        candidate = PurePosixPath(raw.replace("\\", "/"))
        if (
            is_local_reference(raw)
            or ".." in candidate.parts
            or "/" in raw
            or "\\" in raw
        ):
            return "来源已隐藏"
    except (OSError, ValueError):
        return "来源已隐藏"
    if (
        _URI_SCHEME.match(raw)
        or raw.startswith("//")
        or any(marker in raw for marker in ("@", "?", "#"))
        or any(ord(char) <= 32 or ord(char) == 127 for char in raw)
    ):
        return "来源已隐藏"
    return raw


def _safe_archive_file_path(value: Any) -> str:
    raw = str(value or "")
    if not raw:
        return ""
    try:
        posix_candidate = PurePosixPath(raw.replace("\\", "/"))
        windows_candidate = PureWindowsPath(raw)
    except (OSError, ValueError):
        return "路径已隐藏"
    if (
        is_local_reference(raw)
        or posix_candidate.is_absolute()
        or windows_candidate.is_absolute()
        or bool(windows_candidate.drive or windows_candidate.root)
        or ".." in posix_candidate.parts
        or any(marker in raw for marker in ("?", "#"))
        or _URI_SCHEME.match(raw)
        or any(ord(char) <= 31 or ord(char) == 127 for char in raw)
    ):
        return "路径已隐藏"
    return posix_candidate.as_posix()


class _LocalFileInputRejected(RuntimeError):
    """Internal marker for a path that cannot receive CLI read authority."""


def _local_file_import_candidate(value: str) -> Optional[str]:
    """Return an explicit ordinary local file, without probing URL/UNC shapes."""

    candidate = value.strip()
    if (
        not candidate
        or "\n" in candidate
        or "\r" in candidate
        or "\x00" in candidate
        or candidate.startswith(("//", "\\\\"))
        or (
            _URI_SCHEME.match(candidate)
            and not _WINDOWS_ABSOLUTE_DRIVE.match(candidate)
        )
    ):
        return None

    try:
        validated = validate_path_components(
            Path(candidate),
            label="CLI 本地导入源文件",
        )
        info = os.lstat(validated)
    except FileNotFoundError:
        return None
    except (OSError, PKVRuntimeError, ValueError) as exc:
        logger.warning(
            "CLI 本地导入源文件被拒绝: error_type=%s",
            type(exc).__name__,
        )
        raise _LocalFileInputRejected from exc

    attributes = getattr(info, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or bool(attributes & reparse_flag)
        or info.st_nlink > 1
    ):
        logger.warning("CLI 本地导入源文件不是独立常规文件")
        raise _LocalFileInputRejected
    return candidate


def _print_storage_outcome(data: Dict[str, Any], *, error: bool = False) -> None:
    """Keep W1 storage status and repair guidance visible through the CLI."""
    operation_id = _safe_operation_id(data.get("operation_id"))
    if operation_id:
        console.print(f"[dim]操作 ID: {operation_id}[/dim]")
    knowledge_id = data.get("knowledge_id")
    if error and isinstance(knowledge_id, int) and not isinstance(knowledge_id, bool):
        console.print(f"[dim]知识条目 ID: {knowledge_id}[/dim]")
    raw_status = data.get("status")
    status = _safe_archive_status(raw_status, error=error)
    if type(raw_status) is str and raw_status:
        style = (
            "red"
            if error
            else "yellow"
            if status in {"degraded", "repair_required", "rejected"}
            else "green"
        )
        console.print(f"[{style}]存储终态: {status}[/{style}]")
    for action in _safe_repair_actions(data.get("repair_actions")):
        console.print(f"[yellow]- 修复动作: {action}[/yellow]")
    warn_do_not_retry = (
        data.get("core_committed") is True or data.get("do_not_retry") is True
        if error
        else data.get("do_not_retry") is True
        and status not in {"ready", "deleted"}
    )
    if warn_do_not_retry:
        style = "red" if error else "yellow"
        console.print(
            f"[{style}]警告: 核心存储可能已提交，请勿盲目重试！"
            f"请先按上述修复动作处理[/{style}]"
        )


def _search_failure(exc: BaseException, *, strategy: str) -> SearchResponse:
    logger.error(
        "CLI 检索边界捕获异常: strategy=%s, type=%s",
        strategy,
        type(exc).__name__,
    )
    issue = RetrievalIssue.from_exception(
        exc,
        fallback_code=ErrorCode.RETRIEVAL_BACKEND_FAILED,
        public_message="检索服务暂不可用",
        stage="cli_search",
        recoverable=False,
    )
    return SearchResponse.failed_response(issue, strategy=strategy)


def _ensure_search_response(value: Any, *, strategy: str) -> SearchResponse:
    contract_valid = is_strict_search_response(value)
    allowed_strategies = _CLI_RESPONSE_STRATEGIES.get(strategy, frozenset())
    strategy_matches = contract_valid and value.strategy in allowed_strategies
    if strategy_matches:
        return value
    logger.error(
        "CLI 检索收到无效响应合同: strategy=%s, contract_valid=%s, "
        "strategy_match=%s",
        strategy,
        contract_valid,
        strategy_matches,
    )
    return SearchResponse.failed_response(
        RetrievalIssue(
            code=ErrorCode.RETRIEVAL_BACKEND_FAILED,
            message="检索服务返回无效响应",
            stage="cli_search_protocol",
            recoverable=False,
            cause_type=(
                "SearchStrategyMismatch"
                if contract_valid
                else "InvalidSearchResponse"
            ),
        ),
        strategy=strategy,
    )


def _run_search(
    config: Config,
    query: str,
    strategy: str,
    limit: int,
) -> SearchResponse:
    """Delegate retrieval to the shared application service.

    The CLI keeps only its public response validation and rendering contract;
    retriever and Provider composition live in :class:`KnowledgeApplication`.
    """
    strategy = strategy.lower()
    if not isinstance(query, str) or not query.strip():
        return SearchResponse.invalid("查询文本不能为空", strategy=strategy)
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        return SearchResponse.invalid(
            "limit 必须是正整数",
            strategy=strategy,
            stage="limit_validation",
        )
    if strategy not in _CLI_RESPONSE_STRATEGIES:
        raise ValueError(f"不支持的检索策略: {strategy}")
    try:
        application = get_application(config)
        return _ensure_search_response(
            application.search(
                query,
                strategy,
                limit,
                auto_token_threshold=10,
            ),
            strategy=strategy,
        )
    except Exception as exc:
        return _search_failure(exc, strategy=strategy)


def _search_payload(
    query: str,
    response: SearchResponse,
) -> Dict[str, Any]:
    return {
        "query": query,
        "status": response.status,
        "strategy": response.strategy,
        "total": len(response.results),
        "issues": [_public_retrieval_issue(issue) for issue in response.issues],
        "results": [_result_to_dict(result) for result in response.results],
    }


def _print_search_issues(response: SearchResponse) -> None:
    if not response.issues:
        return
    style = "yellow" if response.status == "degraded" else "red"
    label = "警告" if response.status == "degraded" else "错误"
    for issue in response.issues:
        console.print(f"[{style}]{label}: {_issue_text(issue)}[/{style}]")


def _render_list_table(rows: List[Dict[str, Any]], title: str) -> Table:
    table = Table(title=title)
    table.add_column("ID", style="cyan", justify="right")
    table.add_column("标题", style="bold")
    table.add_column("类型", style="magenta")
    table.add_column("时间", style="green")
    table.add_column("标签")

    for row in rows:
        tags = row.get("tags") or ""
        table.add_row(
            str(row.get("knowledge_id", "-")),
            row.get("title") or "(无标题)",
            row.get("source_type") or "-",
            row.get("archived_at") or "-",
            tags,
        )

    return table


def _render_entry_panel(entry: Dict[str, Any], raw: bool = False) -> Panel:
    if raw:
        content = entry.get("content") or ""
        return Panel(content, title=f"条目 #{entry.get('knowledge_id', '-')}")

    tags = entry.get("tags") or ""
    keywords = entry.get("keywords") or ""
    summary = entry.get("summary_100_words") or entry.get("summary_one_sentence") or ""
    source_url = _safe_archive_source(entry.get("source_url"))
    file_path = _safe_archive_file_path(entry.get("file_path"))

    text = (
        f"[bold]标题[/bold]: {entry.get('title') or ''}\n"
        f"[bold]来源[/bold]: {source_url}\n"
        f"[bold]类型[/bold]: {entry.get('source_type') or ''}\n"
        f"[bold]时间[/bold]: {entry.get('archived_at') or ''}\n"
        f"[bold]标签[/bold]: {tags}\n"
        f"[bold]关键词[/bold]: {keywords}\n\n"
        f"[bold]摘要[/bold]:\n{summary}\n\n"
        f"[bold]文件[/bold]: {file_path}"
    )
    return Panel(text, title=f"知识条目 #{entry.get('knowledge_id', '-')}")


class _PublicConfigError(ValueError):
    """An allow-listed configuration validation message safe for CLI output."""


def _set_config_value(config: Config, key: str, value: str) -> None:
    """写入本机 YAML 配置中的点号路径键。"""
    try:
        parsed_value = yaml.safe_load(value) if value.strip() else ""
    except yaml.YAMLError:
        raise _PublicConfigError("配置值不是有效的 YAML") from None
    if not isinstance(parsed_value, (str, int, float, bool, type(None))):
        raise _PublicConfigError(
            "config set 仅支持标量值；请使用 YAML 点号路径逐项设置"
        )
    if (
        isinstance(parsed_value, str)
        and _config_key_is_base_url(key)
        and _url_contains_credentials(parsed_value)
    ):
        raise _PublicConfigError(
            "Base URL 不得通过命令行传入认证信息；"
            "请直接编辑用户数据目录中的 config/local.yaml"
        )
    updater = getattr(config, "update_local_config", None)
    if callable(updater):
        updater({key: parsed_value})
    else:
        # Compatibility seam for injected lightweight test/admin configs.
        set_yaml_config_value(config.local_config_path, key, parsed_value)


def _resolve_config_value(config: Config, key: str) -> Any:
    """读取 YAML 点号键或只读路径快捷键。"""
    alias_getter = CONFIG_KEY_ALIASES.get(key)
    if alias_getter is not None:
        return alias_getter(config)
    return config.get(key)


def _reject_legacy_config_key(key: str) -> None:
    """拒绝已经退役的 Provider 环境变量式配置键。"""
    replacement = LEGACY_CONFIG_KEYS.get(key)
    if replacement:
        raise _PublicConfigError(f"旧配置键 {key} 已移除，请使用 {replacement}")


def _normalize_config_key_part(part: str) -> str:
    """将 snake/kebab/camel/Pascal 配置键统一为小写 snake_case。"""
    normalized = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", part)
    normalized = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", normalized)
    return re.sub(r"[^A-Za-z0-9]+", "_", normalized).strip("_").lower()


def _config_key_touches_sensitive_value(key: str) -> bool:
    """判断键是否等于、包含或位于敏感配置路径之下。"""
    normalized_parts = [_normalize_config_key_part(part) for part in key.split(".")]
    has_sensitive_part = any(
        part == marker
        or part.startswith(f"{marker}_")
        or part.endswith(f"_{marker}")
        or f"_{marker}_" in part
        for part in normalized_parts
        for marker in SENSITIVE_CONFIG_KEY_PARTS
    )
    return has_sensitive_part or any(
        key == sensitive_key
        or sensitive_key.startswith(f"{key}.")
        or key.startswith(f"{sensitive_key}.")
        for sensitive_key in SENSITIVE_CONFIG_KEYS
    )


def _config_key_is_base_url(key: str) -> bool:
    """判断配置路径是否表示 Provider Base URL。"""
    return any(
        _normalize_config_key_part(part) == "base_url" for part in key.split(".")
    )


def _redact_config_value(key: str, value: Any) -> Any:
    """递归遮罩配置树中的密钥、Cookie 等敏感叶节点。"""
    if isinstance(value, dict):
        return {
            child_key: _redact_config_value(
                f"{key}.{child_key}" if key else str(child_key),
                child_value,
            )
            for child_key, child_value in value.items()
        }
    if isinstance(value, list):
        return [_redact_config_value(key, item) for item in value]
    if _config_key_touches_sensitive_value(key):
        return "已设置" if value else "未设置"
    if isinstance(value, str):
        redacted_url = _redact_url_credentials(value)
        if redacted_url is not None:
            return redacted_url
        if _config_key_is_base_url(key):
            # 无法可靠解析的 endpoint 不直接回显，以免泄露异常形式的凭据。
            return "已设置" if value else "未设置"
    return value


def _friendly_hint(message: str) -> None:
    msg = (message or "").lower()
    if "processor" in msg or "抓取" in msg or "url" in msg:
        console.print("[yellow]提示: 请检查 URL 是否正确，或稍后重试[/yellow]")
    if "openai" in msg or "embedding" in msg:
        console.print(
            "[yellow]提示: 请检查用户数据目录中 config/local.yaml 的 Embedding 配置[/yellow]"
        )
    if "deepseek" in msg or "llm" in msg:
        console.print(
            "[yellow]提示: 请检查用户数据目录中 config/local.yaml 的 LLM 配置[/yellow]"
        )


def _print_cli_failure(
    exc: BaseException,
    *,
    code: str,
    public_message: str,
    hint: Optional[str] = None,
) -> None:
    """Render one stable CLI failure without publishing backend exception text."""

    logger.error(
        "CLI 命令失败: code=%s, error_type=%s",
        code,
        type(exc).__name__,
    )
    console.print(
        f"[red]错误: {public_message}（错误代码：{code}）[/red]"
    )
    if hint:
        console.print(f"[yellow]提示: {hint}[/yellow]")


def _print_public_config_error(exc: _PublicConfigError) -> None:
    """Render an internally constructed, allow-listed config validation error."""

    logger.warning(
        "CLI 配置输入被拒绝: code=cli_config_invalid, error_type=%s",
        type(exc).__name__,
    )
    console.print(
        f"[red]错误: {exc}（错误代码：cli_config_invalid）[/red]"
    )


def _archive_text_result_payload(
    result: Any,
    *,
    terminal: str,
    data: Dict[str, Any],
) -> Dict[str, Any]:
    """Create the machine-readable, sanitized archive-text response."""

    entry = data["entry"]
    return {
        "terminal": terminal,
        "status": data["status"],
        "knowledge_id": data["knowledge_id"],
        "title": entry.title,
        "tags": list(entry.tags),
        "file_path": _safe_archive_file_path(data.get("file_path")),
        "issues": _workflow_public_notices(result, error=False),
    }


def _archive_text_error_payload(
    result: Any | None,
    *,
    contract_invalid: bool = False,
    input_invalid: bool = False,
) -> Dict[str, Any]:
    """Create one stable archive-text error envelope without raw diagnostics."""

    if input_invalid:
        issues = [
            _normalise_archive_issue(
                {
                    "code": ErrorCode.WORKFLOW_CONFIG_INVALID,
                    "severity": "error",
                    "stage": "archive_text",
                    "recoverable": True,
                },
                default_severity="error",
            )
        ]
    elif result is None:
        issues = [
            _normalise_archive_issue(
                {
                    "code": ErrorCode.WORKFLOW_STEP_FAILED,
                    "severity": "error",
                    "stage": "archive_text",
                    "recoverable": False,
                },
                default_severity="error",
            )
        ]
    else:
        issues = _workflow_public_notices(
            result,
            error=True,
            contract_invalid=contract_invalid,
        )
    return {
        "terminal": "error",
        "status": "error",
        "knowledge_id": None,
        "title": "",
        "tags": [],
        "file_path": "",
        "issues": issues,
    }


@click.group()
def cli() -> None:
    """个人知识库 CLI 工具。"""


@cli.command("archive")
@click.argument("url_or_path")
@click.option("--skip-sharpen", is_flag=True, help="跳过 idea Sharpen 交互")
@click.option("--tags", help="手动指定标签（逗号分隔）")
@click.option("--quiet", is_flag=True, help="静默模式，跳过交互并减少输出")
@click.option(
    "--type",
    "content_type",
    type=click.Choice(["auto", "webpage", "chat", "news"], case_sensitive=False),
    default="auto",
    show_default=True,
    help="内容类型",
)
def archive(url_or_path: str, skip_sharpen: bool, tags: Optional[str], quiet: bool, content_type: str) -> None:
    """归档内容到知识库。"""
    try:
        config = _load_config()
        application: KnowledgeApplication = get_application(config)

        input_data: Dict[str, Any] = {
            "url": url_or_path,
            "skip_sharpen": bool(skip_sharpen or quiet),
            "skip_review": bool(quiet),
        }

        local_file_source = _local_file_import_candidate(url_or_path)
        if local_file_source is not None:
            input_data["url"] = local_file_source
            _grant_cli_local_file_import(input_data, local_file_source)

        manual_tags = _parse_tags(tags)
        if manual_tags:
            input_data["manual_tags"] = manual_tags
        if content_type and content_type != "auto":
            input_data["content_type"] = content_type

        if not quiet:
            console.print(
                f"正在归档: [cyan]{_safe_archive_source(url_or_path)}[/cyan]"
            )

        with console.status("[cyan]归档中...[/cyan]"):
            result = asyncio.run(application.archive_cli_input(input_data))

        terminal, terminal_valid = _resolve_workflow_terminal(result)
        raw_result_data = getattr(result, "data", None)
        if terminal != "error" and not _valid_archive_result_data(
            raw_result_data,
            terminal=terminal,
        ):
            safe_data = raw_result_data if type(raw_result_data) is dict else {}
            logger.error(
                "archive CLI 收到无效成功载荷: data_type=%s, "
                "knowledge_id_type=%s, status_type=%s",
                type(raw_result_data).__name__,
                type(safe_data.get("knowledge_id")).__name__,
                type(safe_data.get("status")).__name__,
            )
            terminal = "error"
            terminal_valid = False
        if terminal == "error":
            console.print("[red]错误: 归档失败[/red]")
            raw_failure_data = raw_result_data
            failure_data = raw_failure_data if type(raw_failure_data) is dict else {}
            console.print("[red]工作流终态: error[/red]")
            _print_workflow_notices(
                result,
                error=True,
                contract_invalid=not terminal_valid,
            )
            _print_storage_outcome(failure_data, error=True)
            sys.exit(1)

        data = raw_result_data
        entry = data.get("entry")
        knowledge_id = data["knowledge_id"]
        file_path = _safe_archive_file_path(data.get("file_path"))

        degraded = terminal == "degraded"
        if quiet:
            if knowledge_id is not None:
                console.print(str(knowledge_id))
            else:
                console.print("ok")
            if degraded:
                console.print("[yellow]警告: 归档以 degraded 终态完成[/yellow]")
            _print_workflow_notices(result)
            if degraded or data.get("status") == "degraded":
                _print_storage_outcome(data)
            return

        console.print("[green]成功: 归档完成![/green]")
        if degraded:
            console.print("[yellow]警告: 归档以 degraded 终态完成[/yellow]")
        _print_workflow_notices(result)
        _print_storage_outcome(data)

        title = getattr(entry, "title", "") if entry else ""
        source_url = _safe_archive_source(
            getattr(entry, "source_url", "") if entry else url_or_path
        )
        tags_list = getattr(entry, "tags", []) if entry else []
        tags_text = ", ".join(tags_list) if isinstance(tags_list, list) else str(tags_list)

        summary = getattr(entry, "summary_100_words", "") if entry else ""
        if summary:
            summary = summary.strip()

        detail_lines = [
            f"[bold]标题[/bold]: {title}",
            f"[bold]来源[/bold]: {source_url}",
            f"[bold]标签[/bold]: {tags_text}",
            f"[bold]文件[/bold]: {file_path or ''}",
            f"[bold]ID[/bold]: {knowledge_id if knowledge_id is not None else ''}",
        ]
        if summary:
            detail_lines.insert(3, f"[bold]摘要[/bold]: {summary[:160]}" + ("..." if len(summary) > 160 else ""))

        console.print(Panel("\n".join(detail_lines), title="归档结果"))

    except _LocalFileInputRejected as exc:
        _print_cli_failure(
            exc,
            code="cli_archive_local_file_unsafe",
            public_message="本地导入源文件未通过安全检查",
        )
        sys.exit(1)
    except Exception as exc:
        logger.error(
            "archive CLI 未知异常: type=%s",
            type(exc).__name__,
        )
        console.print("[red]错误: 归档发生内部异常，详情已记录日志[/red]")
        console.print("[yellow]提示: 使用 --debug 查看详细日志[/yellow]")
        sys.exit(1)


@cli.command("archive-text")
@click.argument("text")
@click.option("--title", default="", help="可选标题；未提供时从文本提取")
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["table", "json"], case_sensitive=False),
    default="table",
    show_default=True,
    help="输出格式",
)
def archive_text(text: str, title: str, output_format: str) -> None:
    """归档纯文本；输入始终作为字面文本处理。"""

    valid_text, _ = validate_text_length(text)
    try:
        normalized_title = _normalise_archive_text_title(title)
    except ValueError:
        normalized_title = ""
        valid_text = False
        invalid_input_message = "标题无效"
    else:
        invalid_input_message = "文本内容无效"
    if not valid_text:
        payload = _archive_text_error_payload(None, input_invalid=True)
        if output_format == "json":
            click.echo(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        else:
            console.print(f"[red]错误: {invalid_input_message}[/red]")
            for issue in payload["issues"]:
                console.print(
                    f"[red]- {issue['message']} "
                    f"(code={issue['code']}, stage={issue['stage']})[/red]"
                )
        sys.exit(1)

    result: Any | None = None
    try:
        config = _load_config()
        application: KnowledgeApplication = get_application(config)
        result = asyncio.run(
            application.archive_text(
                text,
                title=normalized_title,
                skip_review=True,
                skip_sharpen=True,
            )
        )
        terminal, terminal_valid = _resolve_workflow_terminal(result)
        raw_result_data = getattr(result, "data", None)
        if terminal != "error" and not _valid_archive_result_data(
            raw_result_data,
            terminal=terminal,
        ):
            terminal = "error"
            terminal_valid = False

        if terminal == "error":
            payload = _archive_text_error_payload(
                result,
                contract_invalid=not terminal_valid,
            )
            if output_format == "json":
                click.echo(json.dumps(payload, ensure_ascii=False, sort_keys=True))
            else:
                console.print("[red]错误: 文本归档失败[/red]")
                _print_workflow_notices(
                    result,
                    error=True,
                    contract_invalid=not terminal_valid,
                )
                failure_data = raw_result_data if type(raw_result_data) is dict else {}
                _print_storage_outcome(failure_data, error=True)
            sys.exit(1)

        data = raw_result_data
        payload = _archive_text_result_payload(result, terminal=terminal, data=data)
        if output_format == "json":
            click.echo(json.dumps(payload, ensure_ascii=False, sort_keys=True))
            return

        if terminal == "degraded":
            console.print("[yellow]警告: 文本归档以 degraded 终态完成[/yellow]")
        _print_workflow_notices(result)
        _print_storage_outcome(data)
        details = Text()
        for label, value in (
            ("标题", payload["title"]),
            ("标签", ", ".join(payload["tags"])),
            ("文件", payload["file_path"]),
            ("ID", payload["knowledge_id"]),
        ):
            details.append(f"{label}: ", style="bold")
            details.append_text(_safe_terminal_text(value))
            details.append("\n")
        console.print(Panel(details, title="文本归档结果"))
    except SystemExit:
        raise
    except Exception as exc:
        logger.error(
            "archive-text CLI 未知异常: type=%s",
            type(exc).__name__,
        )
        payload = _archive_text_error_payload(result)
        if output_format == "json":
            click.echo(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        else:
            console.print("[red]错误: 文本归档发生内部异常，详情已记录日志[/red]")
        sys.exit(1)


@cli.command("search")
@click.argument("query")
@click.option(
    "--strategy",
    type=click.Choice(["auto", "bm25", "vector", "hybrid"], case_sensitive=False),
    default="auto",
    show_default=True,
    help="检索策略",
)
@click.option("--limit", type=int, default=10, show_default=True, help="返回结果数量")
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["table", "json", "markdown"], case_sensitive=False),
    default="table",
    show_default=True,
    help="输出格式",
)
def search(query: str, strategy: str, limit: int, output_format: str) -> None:
    """搜索知识库。"""
    try:
        try:
            config = _load_config()
            response = _run_search(config, query, strategy, limit)
        except Exception as exc:
            response = _search_failure(exc, strategy=strategy.lower())
        results = response.results
        strategy_used = response.strategy

        if output_format == "json":
            payload = _search_payload(query, response)
            click.echo(json.dumps(payload, ensure_ascii=False, indent=2))
            if response.status in {"invalid", "error"}:
                sys.exit(1)
            return

        if output_format == "markdown":
            lines = [
                "# 搜索结果\n",
                f"- 查询: {query}",
                f"- 状态: {response.status}",
                f"- 策略: {strategy_used}",
                f"- 结果数: {len(results)}\n",
            ]
            if response.issues:
                lines.append("## 问题")
                for issue in response.issues:
                    lines.append(f"- {_issue_text(issue)}")
                lines.append("")
            for item in results:
                rid = _extract_result_id(item)
                title = getattr(item, "title", None) if not isinstance(item, dict) else item.get("title")
                score = getattr(item, "score", None) if not isinstance(item, dict) else item.get("score")
                snippet = _extract_result_snippet(item)
                lines.append(f"## {title or '(无标题)'}")
                lines.append(f"- ID: {rid if rid is not None else '-'}")
                lines.append(f"- 得分: {score:.3f}" if isinstance(score, (float, int)) else "- 得分: -")
                if snippet:
                    lines.append(f"- 片段: {snippet}")
                lines.append("")
            console.print("\n".join(lines), markup=False)
            if response.status in {"invalid", "error"}:
                sys.exit(1)
            return

        console.print(f"搜索: [cyan]{query}[/cyan]")
        console.print(f"状态: {response.status}")
        console.print(f"找到 {len(results)} 条结果 ({strategy_used} 策略)\n")
        _print_search_issues(response)
        table = _render_search_table(results, title="搜索结果")
        console.print(table)
        if results:
            console.print("提示: 使用 'pkv show <id>' 查看详情")
        if response.status in {"invalid", "error"}:
            sys.exit(1)

    except Exception as exc:
        logger.error(
            "search CLI 输出阶段异常: type=%s",
            type(exc).__name__,
        )
        console.print("[red]错误: 搜索发生内部异常，详情已记录日志[/red]")
        sys.exit(1)


@cli.command("show")
@click.argument("id_or_url", required=False)
@click.option("--url", "source_url", help="按 URL 查询")
@click.option("--raw", is_flag=True, help="输出原始 Markdown")
def show(id_or_url: Optional[str], source_url: Optional[str], raw: bool) -> None:
    """显示条目详情。"""
    try:
        config = _load_config()
        application: KnowledgeApplication = get_application(config)
        store = application.sqlite_store

        if not source_url and not id_or_url:
            console.print("[red]错误: 请提供 knowledge_id 或 --url[/red]")
            sys.exit(1)

        entry: Optional[Dict[str, Any]] = None

        if source_url:
            entry = _get_entry_by_url(store, source_url)
        else:
            if id_or_url and id_or_url.isdigit():
                knowledge_id = int(id_or_url)
                entry = _get_entry_by_id(store, knowledge_id)
            else:
                entry = _get_entry_by_url(store, id_or_url or "")

        if entry is None:
            console.print("[yellow]警告: 未找到对应条目[/yellow]")
            sys.exit(1)

        if raw:
            file_path = entry.get("file_path")
            if not file_path:
                console.print("[red]错误: 条目缺少 file_path，无法读取原始 Markdown[/red]")
                sys.exit(1)
            content = application.markdown_store.load(file_path).content
            console.print(content, markup=False)
            return

        panel = _render_entry_panel(entry)
        console.print(panel)

    except Exception as exc:
        _print_cli_failure(
            exc,
            code="cli_show_failed",
            public_message="条目查询失败",
            hint="请检查本地数据库状态后重试",
        )
        sys.exit(1)


@cli.command("list")
@click.option("--tag", help="按标签过滤")
@click.option(
    "--sort",
    "sort_by",
    type=click.Choice(["time", "title", "id"], case_sensitive=False),
    default="time",
    show_default=True,
    help="排序字段",
)
@click.option("--desc", is_flag=True, help="降序排列")
@click.option("--limit", type=int, default=20, show_default=True, help="返回数量")
def list_entries(tag: Optional[str], sort_by: str, desc: bool, limit: int) -> None:
    """列出知识条目。"""
    try:
        config = _load_config()
        application: KnowledgeApplication = get_application(config)
        store = application.sqlite_store

        sort_map = {
            "time": "archived_at",
            "title": "title",
            "id": "knowledge_id",
        }
        order_by = sort_map.get(sort_by.lower(), "archived_at")

        rows = _query_entries(store, tag, order_by, desc, limit)

        if not rows:
            console.print("[yellow]警告: 未找到条目[/yellow]")
            return

        title = "知识条目列表"
        if tag:
            title += f" (标签: {tag})"
        console.print(_render_list_table(rows, title=title))

    except Exception as exc:
        _print_cli_failure(
            exc,
            code="cli_list_failed",
            public_message="列表查询失败",
            hint="请检查本地数据库状态后重试",
        )
        sys.exit(1)


@cli.command("tags")
@click.option(
    "--limit",
    type=int,
    default=50,
    show_default=True,
    help=f"返回数量，最多 {_MAX_TAG_LIST_LIMIT}",
)
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["table", "json"], case_sensitive=False),
    default="table",
    show_default=True,
    help="输出格式",
)
def tags(limit: int, output_format: str) -> None:
    """列出已归档标签及其使用次数。"""

    if (
        type(limit) is not int
        or isinstance(limit, bool)
        or limit <= 0
        or limit > _MAX_TAG_LIST_LIMIT
    ):
        payload = {
            "status": "invalid",
            "total": 0,
            "tags": [],
            "message": f"limit 必须是 1 到 {_MAX_TAG_LIST_LIMIT} 的整数",
        }
        if output_format == "json":
            click.echo(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        else:
            console.print(
                f"[red]错误: limit 必须是 1 到 {_MAX_TAG_LIST_LIMIT} 的整数[/red]"
            )
        sys.exit(1)

    try:
        config = _load_config()
        application: KnowledgeApplication = get_application(config)
        store = application.sqlite_store
        rows = _get_top_tags(store, limit=limit)
        payload = {
            "status": "success" if rows else "no_hits",
            "total": len(rows),
            "tags": [
                {"name": name, "count": count}
                for name, count in rows
            ],
        }
        if output_format == "json":
            click.echo(json.dumps(payload, ensure_ascii=False, sort_keys=True))
            return
        if not rows:
            console.print("[yellow]暂无标签[/yellow]")
            return
        table = Table(title="标签")
        table.add_column("标签", style="bold")
        table.add_column("条目数", justify="right", style="cyan")
        for name, count in rows:
            table.add_row(_safe_terminal_text(name), str(count))
        console.print(table)
    except Exception as exc:
        logger.error("tags CLI 读取失败: error_type=%s", type(exc).__name__)
        if output_format == "json":
            payload = {
                "status": "error",
                "total": 0,
                "tags": [],
                "message": "标签读取失败",
            }
            click.echo(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        else:
            _print_cli_failure(
                exc,
                code="cli_tags_failed",
                public_message="标签读取失败",
                hint="请检查本地数据库状态后重试",
            )
        sys.exit(1)


@cli.command("related")
@click.argument("knowledge_id")
@click.option("--limit", type=int, default=5, show_default=True, help="返回数量，最多 20")
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["table", "json"], case_sensitive=False),
    default="table",
    show_default=True,
    help="输出格式",
)
def related(knowledge_id: str, limit: int, output_format: str) -> None:
    """按已有向量索引列出与条目相近的知识。"""

    payload = _related_payload(knowledge_id, limit)
    status = payload["status"]
    if output_format == "json":
        click.echo(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        if status in {"invalid", "error"}:
            sys.exit(1)
        return

    console.print(f"关联查询状态: {status}")
    for issue in payload["issues"]:
        console.print(f"[yellow]- {_issue_text(issue)}[/yellow]")
    message = payload.get("message")
    if type(message) is str and message:
        style = "red" if status in {"invalid", "error"} else "yellow"
        console.print(f"[{style}]{message}[/{style}]")

    results = payload["results"]
    if results:
        table = Table(title="关联知识（向量相似度）")
        table.add_column("ID", justify="right", style="cyan")
        table.add_column("标题", style="bold")
        table.add_column("得分", justify="right", style="green")
        table.add_column("摘要")
        for item in results:
            table.add_row(
                str(item["knowledge_id"]),
                _safe_terminal_text(item["title"], limit=160),
                f"{item['score']:.4f}",
                _safe_terminal_text(item["abstract"], limit=240),
            )
        console.print(table)
    elif status == "no_hits":
        console.print("[yellow]未找到关联条目[/yellow]")

    if status in {"invalid", "error"}:
        sys.exit(1)


@cli.group("config")
def config_cmd() -> None:
    """配置管理。"""


@config_cmd.command("show")
def config_show() -> None:
    """显示主要配置。"""
    try:
        config = _load_config()
        embedding_dim = (
            f"auto -> {config.embedding_dim}"
            if getattr(config, "embedding_dim_is_auto", False) and config.embedding_dim is not None
            else "auto (pending)"
            if getattr(config, "embedding_dim_is_auto", False)
            else config.embedding_dim
        )

        table = Table(title="当前配置")
        table.add_column("键")
        table.add_column("值")

        rows = [
            ("data_dir", str(config.data_dir)),
            ("vault_dir", str(config.vault_dir)),
            ("db_path", str(config.db_path)),
            ("storage.vault_dir", str(config.vault_dir)),
            ("storage.db_path", str(config.db_path)),
            ("storage.vector_index_dir", str(config.vector_index_dir)),
            ("storage.log_dir", str(config.log_dir)),
            ("storage.tmp_dir", str(config.tmp_dir)),
            ("ai.llm.base_url", config.llm_base_url),
            ("ai.llm.model", config.llm_model),
            ("ai.embedding.base_url", config.embd_base_url),
            ("ai.embedding.model", config.embd_model),
            ("ai.embedding.dim", embedding_dim),
            ("logging.level", config.log_level),
            ("ai.llm.api_key", config.llm_api_key),
            ("ai.embedding.api_key", config.embd_api_key),
        ]

        for key, value in rows:
            redacted_value = _redact_config_value(key, value)
            table.add_row(key, str(redacted_value) if redacted_value is not None else "-")

        console.print(table)

    except Exception as exc:
        _print_cli_failure(
            exc,
            code="cli_config_read_failed",
            public_message="配置读取失败",
            hint="请检查用户数据目录中的配置文件",
        )
        sys.exit(1)


@config_cmd.command("get")
@click.argument("key")
def config_get(key: str) -> None:
    """查询单个配置。"""
    try:
        config = _load_config()
        _reject_legacy_config_key(key)
        value = _resolve_config_value(config, key)

        if value is None:
            console.print(f"[yellow]警告: 未找到配置: {key}[/yellow]")
            sys.exit(1)

        console.print(str(_redact_config_value(key, value)))

    except _PublicConfigError as exc:
        _print_public_config_error(exc)
        sys.exit(1)
    except Exception as exc:
        _print_cli_failure(
            exc,
            code="cli_config_get_failed",
            public_message="配置查询失败",
            hint="请检查用户数据目录中的配置文件",
        )
        sys.exit(1)


@config_cmd.command("set")
@click.argument("key")
@click.argument("value")
def config_set(key: str, value: str) -> None:
    """修改本机私有 YAML 配置。"""
    try:
        _reject_legacy_config_key(key)
        if _config_key_touches_sensitive_value(key):
            raise _PublicConfigError(
                "该配置路径包含敏感值，不得作为命令行参数传入；"
                "请直接编辑用户数据目录中的 config/local.yaml"
            )
        if "." not in key:
            raise _PublicConfigError(
                "配置键必须使用 YAML 点号路径，例如 ai.llm.model"
            )
        config = _load_config()
        _set_config_value(config, key, value)
        console.print(
            f"[green]成功: 已更新 {key} 到用户数据目录中的 local.yaml[/green]"
        )

    except _PublicConfigError as exc:
        _print_public_config_error(exc)
        sys.exit(1)
    except Exception as exc:
        _print_cli_failure(
            exc,
            code="cli_config_set_failed",
            public_message="配置更新失败",
            hint="请检查用户数据目录中的配置文件及写入权限",
        )
        sys.exit(1)


@cli.command("stats")
def stats() -> None:
    """显示统计信息。"""
    try:
        config = _load_config()
        application: KnowledgeApplication = get_application(config)
        store = application.sqlite_store

        if not config.db_path.exists():
            console.print("[yellow]警告: 数据库不存在，请先归档内容[/yellow]")
            return

        table_exists = store.table_exists("knowledge_items")
        if type(table_exists) is not bool:
            raise _BackendReadContractError
        if not table_exists:
            console.print("[yellow]警告: 数据库未初始化[/yellow]")
            return

        total = _count_entries(store)
        source_rows = _count_entries_by_source_type(store)
        top_tags = _get_top_tags(store, limit=10)

        vault_size = _dir_size(config.vault_dir)
        db_size = _dir_size(config.db_path)
        vector_size = _dir_size(config.vector_index_dir)

        lines = [
            "[bold]知识库统计[/bold]",
            "",
            f"总条目数: {total}",
        ]
        for source, count in source_rows:
            lines.append(f"  - {source or 'unknown'}: {count}")

        lines.append("")
        lines.append("存储大小:")
        lines.append(f"  - Markdown: {_format_bytes(vault_size)}")
        lines.append(f"  - SQLite: {_format_bytes(db_size)}")
        lines.append(f"  - 向量索引: {_format_bytes(vector_size)}")

        lines.append("")
        lines.append("标签统计 (Top 10):")
        if top_tags:
            for idx, (name, count) in enumerate(top_tags, start=1):
                lines.append(f"  {idx}. {name} ({count})")
        else:
            lines.append("  - 暂无标签")

        console.print(Panel("\n".join(lines)))

    except Exception as exc:
        _print_cli_failure(
            exc,
            code="cli_stats_failed",
            public_message="统计读取失败",
            hint="请检查本地数据库状态后重试",
        )
        sys.exit(1)


if __name__ == "__main__":
    cli()
