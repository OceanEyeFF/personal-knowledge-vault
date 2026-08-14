"""
MCP Tool handler 实现

提供 14 个 Tool:
- 只读: search_knowledge, get_entry, list_tags, list_entries, get_stats,
  get_related, query_subgraph, explain_relation, collect_evidence, find_bridges,
  timeline_of, contrast
- 写入: archive_url, archive_text

同步/异步策略说明：
    - FastMCP 的同步 def handler 会直接在 asyncio 事件循环中调用（不同于 FastAPI）
    - 任何阻塞 I/O（SQLite/文件读取）都会冻结整个服务器
    - 只读 Tool 统一使用 async def + anyio.to_thread.run_sync() 包装同步操作
    - 写入 Tool (archive_url/archive_text) 委托 KnowledgeApplication 的原生 async 工作流入口
"""

import logging
import math
from typing import Optional

import anyio
from mcp.types import ToolAnnotations

from src.mcp.server import (
    mcp,
    get_application,
    get_evidence_collection_service,
    get_exploration_service,
    get_sqlite_store,
    get_markdown_store,
    get_relation_query_service,
)
from src.mcp.utils import (
    parse_tags_string, serialize_search_result,
    validate_url_security_result, validate_text_length,
)
from src.relations.citations import (
    build_chunk_locator,
    build_entry_locator,
    build_entry_metadata_locator,
    build_metadata_locator,
    build_relation_locator,
    resolve_vault_file_path,
    sanitize_public_evidence,
    sanitize_public_source_url,
)
from src.relations.models import (
    BridgeDiscoveryResult,
    CHUNK_RETRIEVAL_STATUSES,
    CollectedEvidenceResult,
    ContrastResult,
    RelationDirection,
    RelationExplanationResult,
    RelationSourceType,
    RelationSubgraphResult,
    RelationType,
    TimelineResult,
)
from src.runtime.errors import ErrorCode, PKVRuntimeError
from src.retrieval.result import (
    RetrievalIssue,
    SearchResponse,
    is_strict_search_response,
)
from src.storage.markdown_store import Entry

logger = logging.getLogger("pkv.mcp")


_SEARCH_STRATEGIES = frozenset({"auto", "bm25", "vector", "hybrid"})
_PUBLIC_SEARCH_RESPONSE_STRATEGIES = _SEARCH_STRATEGIES | frozenset(
    {"router", "vector_chunks", "unknown"}
)
_AUTO_SEARCH_RESPONSE_STRATEGIES = frozenset({"router", "bm25", "hybrid"})
_LIST_ENTRY_SORT_FIELDS = frozenset(
    {"archived_at", "title", "word_count", "knowledge_id", "source_type"}
)
_SORT_ORDERS = frozenset({"asc", "desc"})
_RELATION_TYPE_VALUES = frozenset(item.value for item in RelationType)
_RELATION_SOURCE_TYPE_VALUES = frozenset(item.value for item in RelationSourceType)
_RELATION_DIRECTION_VALUES = frozenset(item.value for item in RelationDirection)
_COMPLETED_STORAGE_STATUSES = frozenset({"ready", "degraded"})
_FATAL_STORAGE_STATUSES = frozenset({"repair_required", "rejected"})
_PUBLIC_STORAGE_STATUSES = frozenset(
    {"ready", "degraded", "repair_required", "rejected", "deleted", "error"}
)
_PUBLIC_REPAIR_ACTIONS = frozenset(
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

_PUBLIC_RUNTIME_MESSAGES: dict[ErrorCode, str] = {
    ErrorCode.RESOURCE_MISSING: "请求的资源不存在",
    ErrorCode.RESOURCE_NOT_READABLE: "请求的资源不可读取",
    ErrorCode.PATH_OUTSIDE_VAULT: "请求的资源路径不受信任",
    ErrorCode.PATH_LINK_UNSAFE: "请求的资源路径不受信任",
    ErrorCode.PATH_STATE_UNDETERMINED: "无法安全确认资源路径",
    ErrorCode.PATH_NOT_REGULAR_FILE: "请求的资源不是常规文件",
    ErrorCode.WORKFLOW_CONFIG_INVALID: "工作流配置无效",
    ErrorCode.WORKFLOW_STEP_FAILED: "工作流步骤执行失败",
    ErrorCode.WORKFLOW_CONDITION_INVALID: "工作流条件无效",
    ErrorCode.WORKFLOW_PROCESSOR_UNKNOWN: "工作流处理器无效",
    ErrorCode.RETRIEVAL_INVALID_QUERY: "检索参数无效",
    ErrorCode.RETRIEVAL_BACKEND_FAILED: "检索后端不可用",
    ErrorCode.RETRIEVAL_INDEX_UNAVAILABLE: "检索索引不可用",
    ErrorCode.RETRIEVAL_METADATA_INCONSISTENT: "检索元数据不一致",
    ErrorCode.PROVIDER_CONFIG_INVALID: "Provider 配置无效",
    ErrorCode.PROVIDER_UNAVAILABLE: "Provider 暂时不可用",
    ErrorCode.PROVIDER_PROTOCOL_FAILED: "Provider 响应协议无效",
    ErrorCode.URL_INVALID: "URL 格式无效",
    ErrorCode.SSRF_TARGET_FORBIDDEN: "禁止访问内网地址或其他非公网目标",
    ErrorCode.SSRF_RESOLUTION_FAILED: "目标主机无法安全解析",
    ErrorCode.SSRF_REDIRECT_LIMIT: "网页重定向次数超过安全限制",
    ErrorCode.PROCESSOR_RESOURCE_LIMIT: "处理器资源预算已达上限",
    ErrorCode.STORAGE_PRIMARY_FAILED: "主存储写入失败",
    ErrorCode.STORAGE_INDEX_FAILED: "索引写入失败",
    ErrorCode.STORAGE_VECTOR_FAILED: "向量索引写入失败",
    ErrorCode.STORAGE_COMPENSATION_FAILED: "存储回滚失败",
    ErrorCode.STORAGE_REPAIR_REQUIRED: "存储需要修复",
}
_ERROR_CODE_VALUES = frozenset(item.value for item in ErrorCode)
_ARCHIVE_STEP_IDS = frozenset(
    {"fetch_content", "ai_analyze", "idea_sharpen", "review_entry", "store_entry"}
)
_PUBLIC_CAUSE_TYPES = frozenset(
    {
        "PKVRuntimeError",
        "RuntimeError",
        "ValueError",
        "TypeError",
        "OSError",
        "FileNotFoundError",
        "PermissionError",
        "TimeoutError",
        "ConnectionError",
        "OperationalError",
        "IntegrityError",
        "JSONDecodeError",
    }
)
_PUBLIC_WORKFLOW_STAGES = frozenset(
    {
        "workflow",
        "workflow_step",
        "workflow_terminal",
        "workflow_result",
        "workflow_fetch",
        "workflow_analyze",
        "workflow_review",
        "workflow_review_editor",
        "workflow_local_file_capability",
        "workflow_processor_selection",
        "archive_url",
        "archive_text",
        "url_validation",
        "text_validation",
        "url_preflight",
        "network_policy",
        "safe_fetch_dns",
        "fetch_content",
        "ai_analyze",
        "idea_sharpen",
        "review_entry",
        "store_entry",
        "preparing",
        "primary_committed",
        "index_committed",
        "vector_committed",
        "completed",
        "compensating",
    }
)
_PUBLIC_RETRIEVAL_STAGES = frozenset(
    {
        "retrieval",
        "mcp_search_adapter",
        "query_validation",
        "filter_validation",
        "top_k_validation",
        "strategy_validation",
        "limit_validation",
        "knowledge_id_validation",
        "entry_lookup",
        "entry_content_read",
        "entry_serialization",
        "list_tags",
        "list_entries_validation",
        "list_entries",
        "list_entries_serialization",
        "get_stats",
        "related_entry_lookup",
        "vector_index",
        "document_vector",
        "document_vector_read",
        "vector_related",
        "subgraph_id_validation",
        "subgraph_parameter_validation",
        "subgraph_relation_types_validation",
        "query_subgraph",
        "relation_id_validation",
        "relation_parameter_validation",
        "relation_types_validation",
        "explain_relation",
        "evidence_question_validation",
        "evidence_parameter_validation",
        "evidence_chunk_retrieval",
        "evidence_retrieval",
        "collect_evidence",
        "bridge_seed_validation",
        "bridge_parameter_validation",
        "find_bridges",
        "timeline_topic_validation",
        "timeline_parameter_validation",
        "timeline_sort_validation",
        "timeline_of",
        "contrast_topic_validation",
        "contrast_parameter_validation",
        "contrast",
        "find_bridges_retrieval",
        "timeline_retrieval",
        "contrast_retrieval",
        "contrast_topic_a_retrieval",
        "contrast_topic_b_retrieval",
        "evidence_document_retrieval",
        "bm25_search",
        "bm25_metadata",
        "hybrid_vector",
        "hybrid_executor",
        "hybrid_fusion",
        "query_router_tokenize",
        "vector_index_search",
        "chunk_query_validation",
        "chunk_vector_index_search",
        "chunk_limit_validation",
        "vector_index_probe",
        "vector_index_load",
        "provider_configuration",
        "embedding_protocol",
        "vector_hit_mapping",
        "vector_metadata_read",
        "vector_metadata_mapping",
        "vector_result_mapping",
        "chunk_hit_mapping",
        "chunk_metadata_read",
        "chunk_metadata_mapping",
        "chunk_result_mapping",
    }
)
_PUBLIC_ISSUE_STAGES = _PUBLIC_WORKFLOW_STAGES | _PUBLIC_RETRIEVAL_STAGES


def _stable_runtime_message(exc: PKVRuntimeError, fallback: str) -> str:
    """Map runtime codes to allow-listed text; never echo exception details."""

    return _PUBLIC_RUNTIME_MESSAGES.get(exc.code, fallback)


def _failure_kind(exc: BaseException) -> str:
    """Return a log-safe failure identifier without exception text or traceback."""

    if isinstance(exc, PKVRuntimeError):
        return exc.code.value
    return type(exc).__name__


def _public_issue(
    issue: object,
    *,
    fallback_code: ErrorCode,
    fallback_message: str,
    fallback_stage: str,
    fallback_recoverable: bool = False,
    fallback_severity: str = "error",
) -> dict[str, object]:
    """Serialize one structured issue without exposing arbitrary objects.

    Retrieval and Workflow already promise stable public messages.  This
    adapter still allow-lists fields and runs them through the common evidence
    sanitizer so a future backend cannot accidentally expose a local path.
    """

    if type(issue) is RetrievalIssue:
        raw: dict[str, object] = {
            "code": issue.code,
            "stage": issue.stage,
            "recoverable": issue.recoverable,
            "cause_type": issue.cause_type,
        }
    elif type(issue) is PKVRuntimeError:
        raw = {
            "code": issue.code,
            "stage": issue.stage,
            "recoverable": issue.recoverable,
        }
    elif type(issue) is dict:
        raw = issue
    else:
        raw = {}

    raw_code = raw.get("code", fallback_code)
    if type(raw_code) is ErrorCode:
        code_value = raw_code.value
    elif type(raw_code) is str and raw_code in _ERROR_CODE_VALUES:
        code_value = raw_code
    else:
        code_value = fallback_code.value
    code = ErrorCode(code_value)
    message = _PUBLIC_RUNTIME_MESSAGES.get(code, fallback_message)

    raw_stage = raw.get("stage")
    stage = (
        raw_stage
        if type(raw_stage) is str and raw_stage in _PUBLIC_ISSUE_STAGES
        else (
            fallback_stage
            if fallback_stage in _PUBLIC_ISSUE_STAGES
            else "unknown"
        )
    )
    recoverable = raw.get("recoverable", fallback_recoverable)

    payload: dict[str, object] = {
        "code": code.value,
        "message": message,
        "stage": stage,
        "recoverable": recoverable if type(recoverable) is bool else fallback_recoverable,
    }
    severity = raw.get("severity")
    if type(severity) is str and severity in {"warning", "error"}:
        payload["severity"] = severity
    step_id = raw.get("step_id")
    if type(step_id) is str and step_id in _ARCHIVE_STEP_IDS:
        payload["step_id"] = step_id
    cause_type = raw.get("cause_type")
    if type(cause_type) is str and cause_type in _PUBLIC_CAUSE_TYPES:
        payload["cause_type"] = cause_type
    if "severity" not in payload and fallback_severity:
        payload["severity"] = fallback_severity
    return sanitize_public_evidence(payload)


def _runtime_failure_payload(
    exc: BaseException,
    *,
    fallback_code: ErrorCode,
    message: str,
    stage: str,
    error_label: str | None = None,
) -> dict[str, object]:
    """Return a stable, code-bearing error terminal for an escaped failure."""

    if isinstance(exc, PKVRuntimeError):
        public_message = _stable_runtime_message(exc, message)
        issue: object = exc
    else:
        public_message = message
        issue = {}
    public_issue = _public_issue(
        issue,
        fallback_code=fallback_code,
        fallback_message=public_message,
        fallback_stage=stage,
        fallback_recoverable=isinstance(exc, PKVRuntimeError) and exc.recoverable,
    )
    return sanitize_public_evidence({
        "success": False,
        "terminal": "error",
        "error": error_label or public_issue["message"],
        "warnings": [],
        "issues": [public_issue],
    })


def _search_error_response(
    exc: BaseException,
    *,
    strategy: str,
) -> SearchResponse:
    """Convert adapter/factory failures into the five-state retrieval contract."""

    if isinstance(exc, PKVRuntimeError):
        issue = RetrievalIssue(
            code=exc.code,
            message=_stable_runtime_message(exc, "检索依赖不可用"),
            stage=exc.stage or "mcp_search_adapter",
            recoverable=exc.recoverable,
            cause_type=type(exc).__name__,
        )
    else:
        issue = RetrievalIssue(
            code=ErrorCode.RETRIEVAL_BACKEND_FAILED,
            message="检索后端不可用",
            stage="mcp_search_adapter",
            recoverable=True,
            cause_type=type(exc).__name__,
        )
    return SearchResponse.failed_response(issue, strategy=strategy)


def _readonly_error_payload(
    *,
    status: str,
    code: ErrorCode,
    message: str,
    stage: str,
    recoverable: bool,
    exc: PKVRuntimeError | None = None,
    extra: dict[str, object] | None = None,
) -> dict[str, object]:
    """Build a stable error/degradation envelope for non-search read tools."""

    issue = _public_issue(
        exc or {},
        fallback_code=code,
        fallback_message=(
            _stable_runtime_message(exc, message) if exc is not None else message
        ),
        fallback_stage=stage,
        fallback_recoverable=exc.recoverable if exc is not None else recoverable,
        fallback_severity="",
    )
    payload: dict[str, object] = {
        "status": status,
        "error": issue["message"],
        "issues": [issue],
    }
    if extra:
        payload.update(extra)
    return sanitize_public_evidence(payload)


def _readonly_service_failure(
    exc: BaseException,
    *,
    operation: str,
    stage: str,
) -> dict[str, object]:
    """Normalize post-validation backend failures to stable public codes."""

    if isinstance(exc, PKVRuntimeError):
        return _readonly_error_payload(
            status="error",
            code=ErrorCode.RETRIEVAL_BACKEND_FAILED,
            message=f"{operation}暂时不可用",
            stage=stage,
            recoverable=exc.recoverable,
            exc=exc,
        )
    logger.error("%s失败: kind=%s", operation, _failure_kind(exc))
    return _readonly_error_payload(
        status="error",
        code=ErrorCode.RETRIEVAL_BACKEND_FAILED,
        message=f"{operation}暂时不可用",
        stage=stage,
        recoverable=True,
    )


def _is_positive_int(value: object) -> bool:
    """Return True only for real positive integers (``bool`` is not an ID/count)."""

    return type(value) is int and value > 0


def _is_nonnegative_int(value: object) -> bool:
    return type(value) is int and value >= 0


def _is_optional_db_text(value: object) -> bool:
    return value is None or type(value) is str


_ENTRY_ROW_REQUIRED_FIELDS = frozenset(
    {
        "knowledge_id",
        "title",
        "summary_one_sentence",
        "summary_100_words",
        "tags",
        "keywords",
        "source_type",
        "source_url",
        "archived_at",
        "word_count",
        "file_path",
    }
)


def _is_entry_row(value: object, *, expected_id: int | None = None) -> bool:
    """Validate the SQLite row shape before projecting it to a public Tool."""

    if type(value) is not dict or not _ENTRY_ROW_REQUIRED_FIELDS.issubset(value):
        return False
    knowledge_id = value["knowledge_id"]
    return (
        _is_positive_int(knowledge_id)
        and (expected_id is None or knowledge_id == expected_id)
        and type(value["title"]) is str
        and type(value["source_type"]) is str
        and bool(value["source_type"].strip())
        and type(value["file_path"]) is str
        and _is_nonnegative_int(value["word_count"])
        and all(
            _is_optional_db_text(value[field])
            for field in (
                "summary_one_sentence",
                "summary_100_words",
                "tags",
                "keywords",
                "source_url",
                "archived_at",
            )
        )
    )


def _is_tag_count_row(value: object) -> bool:
    return (
        type(value) is dict
        and set(value) == {"name", "count"}
        and type(value["name"]) is str
        and bool(value["name"].strip())
        and _is_nonnegative_int(value["count"])
    )


def _is_source_count_row(value: object) -> bool:
    return (
        type(value) is tuple
        and len(value) == 2
        and type(value[0]) is str
        and bool(value[0].strip())
        and _is_nonnegative_int(value[1])
    )


def _is_statistics_payload(value: object) -> bool:
    if type(value) is not dict or set(value) != {
        "total_entries",
        "by_source_type",
        "top_tags",
    }:
        return False
    total_entries = value["total_entries"]
    source_counts = value["by_source_type"]
    top_tags = value["top_tags"]
    return (
        _is_nonnegative_int(total_entries)
        and type(source_counts) is list
        and all(_is_source_count_row(item) for item in source_counts)
        and len({item[0] for item in source_counts}) == len(source_counts)
        and sum(item[1] for item in source_counts) == total_entries
        and type(top_tags) is list
        and len(top_tags) <= 20
        and all(_is_tag_count_row(item) for item in top_tags)
        and len({item["name"] for item in top_tags}) == len(top_tags)
        and all(item["count"] <= total_entries for item in top_tags)
        and [item["count"] for item in top_tags]
        == sorted((item["count"] for item in top_tags), reverse=True)
    )


def _is_strict_json_tree(value: object) -> bool:
    """Accept only finite JSON primitives behind relation Tool projections."""

    if value is None or type(value) in {str, bool, int}:
        return True
    if type(value) is float:
        return math.isfinite(value)
    if type(value) is list:
        return all(_is_strict_json_tree(item) for item in value)
    if type(value) is dict:
        return all(
            type(key) is str and _is_strict_json_tree(item)
            for key, item in value.items()
        )
    return False


def _is_unit_score(value: object) -> bool:
    return (
        type(value) in {int, float}
        and math.isfinite(value)
        and 0.0 <= value <= 1.0
    )


def _is_string_list(value: object) -> bool:
    return type(value) is list and all(type(item) is str for item in value)


def _strict_domain_payload(
    result: object,
    *,
    expected_type: type,
    required_fields: frozenset[str],
) -> dict[str, object]:
    """Call ``to_dict`` only for the exact promised domain result class."""

    if type(result) is not expected_type:
        raise TypeError("domain result type is invalid")
    payload = sanitize_public_evidence(expected_type.to_dict(result))
    if (
        type(payload) is not dict
        or set(payload) != required_fields
        or not _is_strict_json_tree(payload)
    ):
        raise TypeError("domain result payload is invalid")
    return payload


_COMMON_RELATION_RESULT_FIELDS = frozenset(
    {
        "schema_version",
        "implementation_level",
        "limitation_notes",
        "evidence_count",
        "confidence",
        "coverage",
    }
)


_RELATION_EDGE_FIELDS = frozenset(
    {
        "relation_id",
        "source_knowledge_id",
        "target_knowledge_id",
        "relation_type",
        "relation_source_type",
        "direction",
        "weight",
        "evidence_payload",
        "created_at",
        "updated_at",
    }
)
_RELATION_EXPLANATION_EVIDENCE_FIELDS = frozenset(
    {
        "step_index",
        "relation_type",
        "relation_source_type",
        "direction",
        "weight",
        "source_knowledge_id",
        "target_knowledge_id",
        "evidence_payload",
    }
)
_COLLECTED_EVIDENCE_ITEM_FIELDS = frozenset(
    {
        "knowledge_id",
        "title",
        "abstract",
        "source_type",
        "archived_at",
        "tags",
        "source_url",
        "citation_source",
        "citation_locator",
        "content_preview",
        "chunk_id",
        "chunk_index",
        "chunk_text",
        "retrieval_rank",
        "retrieval_score",
        "ranking_score",
        "coverage_score",
        "freshness_score",
        "relation_score",
        "is_seed",
        "relation_found",
        "relation_explanation_type",
        "relation_hops",
        "relation_summary",
        "relation_path",
        "relation_evidence_items",
    }
)
_BRIDGE_ITEM_FIELDS = frozenset(
    {
        "knowledge_id",
        "title",
        "depth",
        "bridge_score",
        "structural_bridge_score",
        "graph_bridge_score",
        "semantic_bridge_score",
        "connected_knowledge_ids",
        "relation_types",
        "evidence_path",
        "supporting_subgraph",
        "summary",
    }
)
_TIMELINE_ITEM_FIELDS = frozenset(
    {
        "knowledge_id",
        "title",
        "time_value",
        "event_time",
        "published_at",
        "archived_at",
        "time_source",
        "time_source_field",
        "time_precision",
        "source_type",
        "source_url",
        "source",
        "citation_locator",
        "abstract",
        "tags",
        "retrieval_score",
    }
)
_CONTRAST_CANDIDATE_FIELDS = frozenset(
    {
        "knowledge_id",
        "title",
        "abstract",
        "archived_at",
        "source_type",
        "source_url",
        "source",
        "citation_locator",
        "tags",
        "retrieval_score",
        "relation_signal_score",
        "relation_types",
    }
)
_RELATION_EVIDENCE_FIELDS = frozenset(
    {
        "raw_target",
        "normalized_target",
        "anchor_text",
        "field",
        "note",
        "stub",
        "declared_in_knowledge_id",
        "source_file_path",
        "target_file_path",
        "source_url",
        "source",
    }
)
_BRIDGE_EDGE_EXTRA_FIELDS = frozenset(
    {
        "evidence_roles",
        "citation_locator",
        "from_knowledge_id",
        "to_knowledge_id",
        "traversal_direction",
        "hop_index",
    }
)
_BRIDGE_EVIDENCE_ROLES = frozenset(
    {"seed_path", "candidate_adjacency", "bounded_subgraph_edge"}
)
_TIMELINE_PRIORITY = ["event_time", "published_at", "archived_at"]
_TIMELINE_TIME_SOURCES = frozenset(_TIMELINE_PRIORITY) | frozenset({"unavailable"})
_TIMELINE_INFERRED_FIELDS = frozenset(_TIMELINE_PRIORITY) | frozenset(
    {"mixed", "unavailable"}
)
_TIMELINE_PHYSICAL_FIELDS = {
    "event_time": frozenset({"event_time"}),
    "published_at": frozenset({"published_at", "published_time", "publish_time"}),
    "archived_at": frozenset({"archived_at"}),
}
_BRIDGE_EVIDENCE_SOURCES = [
    "relation_subgraph",
    "graph_bridge_signal",
    "entry_tags",
    "entry_title_summary",
]
_TIMELINE_EVIDENCE_SOURCES = [
    "query_results",
    "entry_metadata",
    "structured_time_fields",
]
_CONTRAST_EVIDENCE_SOURCES = [
    "query_results",
    "relation_graph",
    "entry_tags",
    "entry_summary",
]


def _score_equals(value: object, expected: float) -> bool:
    return _is_unit_score(value) and abs(float(value) - float(expected)) <= 1e-9


def _has_unsafe_public_url(value: object) -> bool:
    """Reject credential-bearing/malformed HTTP(S) strings in nested evidence."""

    if type(value) is str:
        lowered = value.strip().lower()
        return lowered.startswith(("http://", "https://")) and (
            sanitize_public_source_url(value) != value
        )
    if type(value) is list:
        return any(_has_unsafe_public_url(item) for item in value)
    if type(value) is dict:
        return any(_has_unsafe_public_url(item) for item in value.values())
    return False


def _is_relation_evidence_payload(value: object) -> bool:
    if (
        type(value) is not dict
        or not _is_strict_json_tree(value)
        or _has_unsafe_public_url(value)
    ):
        return False
    for key, item in value.items():
        if key == "declared_in_knowledge_id" and not _is_positive_int(item):
            return False
        if key == "stub" and type(item) is not bool:
            return False
        if (
            key in _RELATION_EVIDENCE_FIELDS
            and key not in {"declared_in_knowledge_id", "stub"}
            and type(item) is not str
        ):
            return False
    return True


def _canonical_relation_locator(edge: dict[str, object]) -> str:
    return build_relation_locator(
        relation_id=edge["relation_id"],
        source_knowledge_id=edge["source_knowledge_id"],
        target_knowledge_id=edge["target_knowledge_id"],
        relation_type=edge["relation_type"],
        relation_source_type=edge["relation_source_type"],
    )


def _is_canonical_public_source(value: object, knowledge_id: int) -> bool:
    if type(value) is not str:
        return False
    if value == build_entry_locator(knowledge_id):
        return True
    return bool(value) and sanitize_public_source_url(value) == value


def _relation_edge_key(edge: dict[str, object]) -> tuple[object, ...]:
    relation_id = edge["relation_id"]
    if relation_id is not None:
        return ("relation_id", relation_id)
    return (
        "edge",
        edge["source_knowledge_id"],
        edge["target_knowledge_id"],
        edge["relation_type"],
        edge["relation_source_type"],
        edge["direction"],
    )


def _walk_relation_path(
    path: list[dict[str, object]],
    *,
    source_knowledge_id: int,
) -> list[int] | None:
    current = source_knowledge_id
    nodes = [current]
    for edge in path:
        source_id = edge["source_knowledge_id"]
        target_id = edge["target_knowledge_id"]
        if source_id == current:
            current = target_id
        elif target_id == current:
            current = source_id
        else:
            return None
        if current in nodes:
            return None
        nodes.append(current)
    return nodes


def _expected_explanation_evidence(
    edge: dict[str, object],
    step_index: int,
) -> dict[str, object]:
    return {
        "step_index": step_index,
        "relation_type": edge["relation_type"],
        "relation_source_type": edge["relation_source_type"],
        "direction": edge["direction"],
        "weight": edge["weight"],
        "source_knowledge_id": edge["source_knowledge_id"],
        "target_knowledge_id": edge["target_knowledge_id"],
        "evidence_payload": edge["evidence_payload"],
    }


def _expected_path_summary(
    source_knowledge_id: int,
    path: list[dict[str, object]],
) -> str:
    current = source_knowledge_id
    parts = [str(source_knowledge_id)]
    for edge in path:
        if edge["source_knowledge_id"] == current:
            next_id = edge["target_knowledge_id"]
            connector = f"-[{edge['relation_type']}]->"
        elif edge["target_knowledge_id"] == current:
            next_id = edge["source_knowledge_id"]
            connector = f"<-[{edge['relation_type']}]-"
        else:
            return ""
        parts.extend((connector, str(next_id)))
        current = next_id
    return " ".join(parts)


def _is_relation_edge_payload(value: object) -> bool:
    if type(value) is not dict or set(value) != _RELATION_EDGE_FIELDS:
        return False
    relation_id = value["relation_id"]
    return (
        (relation_id is None or _is_positive_int(relation_id))
        and _is_positive_int(value["source_knowledge_id"])
        and _is_positive_int(value["target_knowledge_id"])
        and value["source_knowledge_id"] != value["target_knowledge_id"]
        and type(value["relation_type"]) is str
        and value["relation_type"] in _RELATION_TYPE_VALUES
        and type(value["relation_source_type"]) is str
        and value["relation_source_type"] in _RELATION_SOURCE_TYPE_VALUES
        and type(value["direction"]) is str
        and value["direction"] in _RELATION_DIRECTION_VALUES
        and type(value["weight"]) in {int, float}
        and math.isfinite(value["weight"])
        and value["weight"] > 0
        and _is_relation_evidence_payload(value["evidence_payload"])
        and all(
            value[field] is None or type(value[field]) is str
            for field in ("created_at", "updated_at")
        )
    )


def _is_relation_explanation_evidence(value: object) -> bool:
    return (
        type(value) is dict
        and set(value) == _RELATION_EXPLANATION_EVIDENCE_FIELDS
        and _is_nonnegative_int(value["step_index"])
        and type(value["relation_type"]) is str
        and value["relation_type"] in _RELATION_TYPE_VALUES
        and type(value["relation_source_type"]) is str
        and value["relation_source_type"] in _RELATION_SOURCE_TYPE_VALUES
        and type(value["direction"]) is str
        and value["direction"] in _RELATION_DIRECTION_VALUES
        and type(value["weight"]) in {int, float}
        and math.isfinite(value["weight"])
        and value["weight"] > 0
        and _is_positive_int(value["source_knowledge_id"])
        and _is_positive_int(value["target_knowledge_id"])
        and _is_relation_evidence_payload(value["evidence_payload"])
    )


def _is_collected_evidence_item(value: object) -> bool:
    if type(value) is not dict or set(value) != _COLLECTED_EVIDENCE_ITEM_FIELDS:
        return False
    chunk_id = value["chunk_id"]
    chunk_index = value["chunk_index"]
    return (
        _is_positive_int(value["knowledge_id"])
        and all(
            type(value[field]) is str
            for field in (
                "title",
                "abstract",
                "source_type",
                "archived_at",
                "source_url",
                "citation_source",
                "citation_locator",
                "content_preview",
                "chunk_text",
                "relation_explanation_type",
                "relation_summary",
            )
        )
        and _is_string_list(value["tags"])
        and (
            not value["source_url"]
            or sanitize_public_source_url(value["source_url"]) == value["source_url"]
        )
        and _is_canonical_public_source(
            value["citation_source"], value["knowledge_id"]
        )
        and value["citation_source"]
        == (
            value["source_url"]
            if value["source_url"]
            else build_entry_locator(value["knowledge_id"])
        )
        and (chunk_id is None or _is_positive_int(chunk_id))
        and (chunk_index is None or _is_nonnegative_int(chunk_index))
        and _is_positive_int(value["retrieval_rank"])
        and all(
            _is_unit_score(value[field])
            for field in (
                "retrieval_score",
                "ranking_score",
                "coverage_score",
                "freshness_score",
                "relation_score",
            )
        )
        and type(value["is_seed"]) is bool
        and type(value["relation_found"]) is bool
        and _is_nonnegative_int(value["relation_hops"])
        and type(value["relation_path"]) is list
        and all(_is_relation_edge_payload(item) for item in value["relation_path"])
        and type(value["relation_evidence_items"]) is list
        and all(
            _is_relation_explanation_evidence(item)
            for item in value["relation_evidence_items"]
        )
    )


def _is_bridge_item(value: object) -> bool:
    return (
        type(value) is dict
        and set(value) == _BRIDGE_ITEM_FIELDS
        and _is_positive_int(value["knowledge_id"])
        and type(value["title"]) is str
        and _is_nonnegative_int(value["depth"])
        and _is_unit_score(value["bridge_score"])
        and all(
            _is_unit_score(value[field])
            for field in (
                "structural_bridge_score",
                "graph_bridge_score",
                "semantic_bridge_score",
            )
        )
        and type(value["connected_knowledge_ids"]) is list
        and all(
            _is_positive_int(item) for item in value["connected_knowledge_ids"]
        )
        and len(set(value["connected_knowledge_ids"]))
        == len(value["connected_knowledge_ids"])
        and _is_string_list(value["relation_types"])
        and all(item in _RELATION_TYPE_VALUES for item in value["relation_types"])
        and type(value["evidence_path"]) is list
        and all(type(item) is dict for item in value["evidence_path"])
        and type(value["supporting_subgraph"]) is dict
        and type(value["summary"]) is str
    )


def _is_bridge_edge_evidence(value: object) -> bool:
    if type(value) is not dict:
        return False
    keys = set(value)
    if not _RELATION_EDGE_FIELDS.issubset(keys):
        return False
    if not {"evidence_roles", "citation_locator"}.issubset(keys):
        return False
    if not keys.issubset(_RELATION_EDGE_FIELDS | _BRIDGE_EDGE_EXTRA_FIELDS):
        return False
    edge = {key: value[key] for key in _RELATION_EDGE_FIELDS}
    roles = value["evidence_roles"]
    if (
        not _is_relation_edge_payload(edge)
        or not _is_string_list(roles)
        or not roles
        or len(set(roles)) != len(roles)
        or any(role not in _BRIDGE_EVIDENCE_ROLES for role in roles)
        or value["citation_locator"] != _canonical_relation_locator(edge)
    ):
        return False
    traversal_keys = {
        "from_knowledge_id",
        "to_knowledge_id",
        "traversal_direction",
    }
    has_traversal = bool(keys & traversal_keys)
    if has_traversal and not traversal_keys.issubset(keys):
        return False
    if has_traversal:
        from_id = value["from_knowledge_id"]
        to_id = value["to_knowledge_id"]
        direction = value["traversal_direction"]
        if (
            not _is_positive_int(from_id)
            or not _is_positive_int(to_id)
            or {from_id, to_id}
            != {edge["source_knowledge_id"], edge["target_knowledge_id"]}
            or direction not in {"forward", "reverse"}
            or (
                direction == "forward"
                and (
                    from_id != edge["source_knowledge_id"]
                    or to_id != edge["target_knowledge_id"]
                )
            )
            or (
                direction == "reverse"
                and (
                    from_id != edge["target_knowledge_id"]
                    or to_id != edge["source_knowledge_id"]
                )
            )
        ):
            return False
    if "hop_index" in value and (
        not has_traversal or not _is_positive_int(value["hop_index"])
    ):
        return False
    return True


def _bridge_seed_path_reaches(
    evidence_path: list[dict[str, object]],
    *,
    seed_knowledge_id: int,
    candidate_knowledge_id: int,
    max_depth: int,
) -> bool:
    seed_edges = [
        item
        for item in evidence_path
        if "seed_path" in item["evidence_roles"]
    ]
    if not seed_edges or len(seed_edges) > max_depth:
        return False
    seed_edges.sort(key=lambda item: item.get("hop_index", 0))
    if [item.get("hop_index") for item in seed_edges] != list(
        range(1, len(seed_edges) + 1)
    ):
        return False
    current = seed_knowledge_id
    seen = {current}
    for item in seed_edges:
        if item.get("from_knowledge_id") != current:
            return False
        current = item.get("to_knowledge_id")
        if current in seen:
            return False
        seen.add(current)
    return current == candidate_knowledge_id


def _is_bridge_semantic_inputs(
    value: object,
    *,
    seed_knowledge_id: int,
    candidate_knowledge_id: int,
    connected_knowledge_ids: list[int],
) -> bool:
    required = {
        "fields_used",
        "candidate",
        "comparisons",
        "anchor_score",
        "support_score",
        "coverage_score",
        "semantic_score",
    }
    if type(value) is not dict or set(value) != required:
        return False
    candidate = value["candidate"]
    comparisons = value["comparisons"]
    if (
        value["fields_used"]
        != ["title", "summary_one_sentence", "summary_100_words", "tags"]
        or type(candidate) is not dict
        or set(candidate)
        != {"knowledge_id", "citation_locator", "metadata_locator", "token_count"}
        or candidate["knowledge_id"] != candidate_knowledge_id
        or candidate["citation_locator"]
        != build_entry_locator(candidate_knowledge_id)
        or candidate["metadata_locator"]
        != build_entry_metadata_locator(candidate_knowledge_id)
        or not _is_nonnegative_int(candidate["token_count"])
        or type(comparisons) is not list
        or not all(
            type(item) is dict
            and set(item)
            == {
                "comparison_role",
                "knowledge_id",
                "citation_locator",
                "metadata_locator",
                "candidate_token_count",
                "comparison_token_count",
                "overlap_token_count",
                "overlap_score",
            }
            and item["comparison_role"] in {"seed", "candidate_neighbor"}
            and _is_positive_int(item["knowledge_id"])
            and (
                item["knowledge_id"] == seed_knowledge_id
                if item["comparison_role"] == "seed"
                else item["knowledge_id"] in connected_knowledge_ids
            )
            and item["citation_locator"]
            == build_entry_locator(item["knowledge_id"])
            and item["metadata_locator"]
            == build_entry_metadata_locator(item["knowledge_id"])
            and _is_nonnegative_int(item["candidate_token_count"])
            and item["candidate_token_count"] == candidate["token_count"]
            and _is_nonnegative_int(item["comparison_token_count"])
            and _is_nonnegative_int(item["overlap_token_count"])
            and item["overlap_token_count"]
            <= min(
                item["candidate_token_count"],
                item["comparison_token_count"],
            )
            and _is_unit_score(item["overlap_score"])
            for item in comparisons
        )
        or not all(
            _is_unit_score(value[field])
            for field in (
                "anchor_score",
                "support_score",
                "coverage_score",
                "semantic_score",
            )
        )
    ):
        return False
    if (
        candidate["token_count"] == 0
        and comparisons
        or len(
            {
                (item["comparison_role"], item["knowledge_id"])
                for item in comparisons
            }
        )
        != len(comparisons)
        or sum(1 for item in comparisons if item["comparison_role"] == "seed") > 1
        or any(
            not _score_equals(
                item["overlap_score"],
                round(
                    item["overlap_token_count"]
                    / max(
                        min(
                            item["candidate_token_count"],
                            item["comparison_token_count"],
                        ),
                        1,
                    ),
                    6,
                ),
            )
            for item in comparisons
        )
    ):
        return False
    scores = sorted(
        (float(item["overlap_score"]) for item in comparisons),
        reverse=True,
    )
    if not scores:
        expected_anchor = expected_support = expected_coverage = expected_semantic = 0.0
    else:
        expected_anchor = scores[0]
        support_count = min(len(scores), 2)
        expected_support = sum(scores[:support_count]) / support_count
        expected_coverage = sum(score >= 0.08 for score in scores) / len(scores)
        expected_semantic = min(
            max(
                expected_anchor * 0.55
                + expected_support * 0.3
                + expected_coverage * 0.15,
                0.0,
            ),
            1.0,
        )
    return all(
        _score_equals(value[field], round(expected, 6))
        for field, expected in (
            ("anchor_score", expected_anchor),
            ("support_score", expected_support),
            ("coverage_score", expected_coverage),
            ("semantic_score", expected_semantic),
        )
    )


def _bridge_supporting_subgraph_is_coherent(
    value: object,
    *,
    seed_knowledge_id: int,
    candidate: dict[str, object],
    max_depth: int,
) -> bool:
    required = {
        "scope",
        "edges",
        "candidate_connected_knowledge_ids",
        "neighbor_pairs",
        "disconnected_neighbor_pairs",
        "structural_score_inputs",
        "graph_score_inputs",
        "semantic_score_inputs",
    }
    if type(value) is not dict or set(value) != required:
        return False
    scope = value["scope"]
    edges = value["edges"]
    structural = value["structural_score_inputs"]
    graph = value["graph_score_inputs"]
    semantic = value["semantic_score_inputs"]
    connected_ids = value["candidate_connected_knowledge_ids"]
    neighbor_pairs = value["neighbor_pairs"]
    disconnected_pairs = value["disconnected_neighbor_pairs"]
    pair_shape_valid = (
        type(neighbor_pairs) is list
        and all(
            type(item) is dict
            and set(item)
            == {
                "left_knowledge_id",
                "right_knowledge_id",
                "connected_within_scope",
            }
            and _is_positive_int(item["left_knowledge_id"])
            and _is_positive_int(item["right_knowledge_id"])
            and item["left_knowledge_id"] < item["right_knowledge_id"]
            and type(item["connected_within_scope"]) is bool
            for item in neighbor_pairs
        )
        and type(disconnected_pairs) is list
        and disconnected_pairs
        == [item for item in neighbor_pairs if not item["connected_within_scope"]]
    )
    if (
        type(scope) is not dict
        or set(scope)
        != {
            "seed_knowledge_id",
            "candidate_knowledge_id",
            "max_depth",
            "node_depths",
            "edge_completeness",
        }
        or scope["seed_knowledge_id"] != seed_knowledge_id
        or scope["candidate_knowledge_id"] != candidate["knowledge_id"]
        or scope["max_depth"] != max_depth
        or scope["edge_completeness"]
        != "complete_unless_result_subgraph_truncated"
        or type(scope["node_depths"]) is not dict
        or not all(
            type(key) is str
            and key.isascii()
            and key.isdigit()
            and str(int(key)) == key
            and _is_nonnegative_int(depth)
            and depth <= max_depth
            for key, depth in scope["node_depths"].items()
        )
        or scope["node_depths"].get(str(seed_knowledge_id)) != 0
        or scope["node_depths"].get(str(candidate["knowledge_id"]))
        != candidate["depth"]
        or type(edges) is not list
        or not all(
            _is_bridge_edge_evidence(edge)
            and edge["evidence_roles"] == ["bounded_subgraph_edge"]
            for edge in edges
        )
        or connected_ids != candidate["connected_knowledge_ids"]
        or not pair_shape_valid
        or [
            (item["left_knowledge_id"], item["right_knowledge_id"])
            for item in neighbor_pairs
        ]
        != [
            (left_id, right_id)
            for index, left_id in enumerate(connected_ids)
            for right_id in connected_ids[index + 1 :]
        ]
        or type(structural) is not dict
        or set(structural) != {"candidate_depth", "neighbor_count", "max_depth"}
        or structural["candidate_depth"] != candidate["depth"]
        or structural["neighbor_count"] != len(connected_ids)
        or structural["max_depth"] != max_depth
        or type(graph) is not dict
        or set(graph)
        != {
            "disconnected_pair_count",
            "neighbor_pair_count",
            "disconnected_pair_ratio",
            "neighbor_depths",
            "depth_span",
            "seed_frontier",
        }
        or not _is_nonnegative_int(graph["disconnected_pair_count"])
        or not _is_nonnegative_int(graph["neighbor_pair_count"])
        or graph["disconnected_pair_count"] > graph["neighbor_pair_count"]
        or graph["neighbor_pair_count"] != len(neighbor_pairs)
        or graph["disconnected_pair_count"] != len(disconnected_pairs)
        or not _is_unit_score(graph["disconnected_pair_ratio"])
        or not _score_equals(
            graph["disconnected_pair_ratio"],
            round(len(disconnected_pairs) / max(len(neighbor_pairs), 1), 6),
        )
        or type(graph["neighbor_depths"]) is not dict
        or set(graph["neighbor_depths"]) != {str(item) for item in connected_ids}
        or not all(
            _is_nonnegative_int(depth) and depth <= max_depth
            for depth in graph["neighbor_depths"].values()
        )
        or not _is_nonnegative_int(graph["depth_span"])
        or graph["depth_span"]
        != (
            max(graph["neighbor_depths"].values())
            - min(graph["neighbor_depths"].values())
            if graph["neighbor_depths"]
            else 0
        )
        or type(graph["seed_frontier"]) is not bool
        or not _is_bridge_semantic_inputs(
            semantic,
            seed_knowledge_id=seed_knowledge_id,
            candidate_knowledge_id=candidate["knowledge_id"],
            connected_knowledge_ids=connected_ids,
        )
    ):
        return False
    node_depths = {
        int(knowledge_id): depth
        for knowledge_id, depth in scope["node_depths"].items()
    }
    node_ids = set(node_depths)
    if (
        candidate["knowledge_id"] not in node_ids
        or not set(connected_ids).issubset(node_ids)
        or any(
            node_id != seed_knowledge_id and depth == 0
            for node_id, depth in node_depths.items()
        )
        or len({_relation_edge_key(edge) for edge in edges}) != len(edges)
        or any(
            edge["source_knowledge_id"] not in node_ids
            or edge["target_knowledge_id"] not in node_ids
            or abs(
                node_depths[edge["source_knowledge_id"]]
                - node_depths[edge["target_knowledge_id"]]
            )
            > 1
            for edge in edges
        )
    ):
        return False
    adjacency: dict[int, set[int]] = {node_id: set() for node_id in node_ids}
    for edge in edges:
        source_id = edge["source_knowledge_id"]
        target_id = edge["target_knowledge_id"]
        adjacency[source_id].add(target_id)
        adjacency[target_id].add(source_id)
    if (
        sorted(adjacency[candidate["knowledge_id"]]) != connected_ids
        or any(
            item["connected_within_scope"]
            != (item["right_knowledge_id"] in adjacency[item["left_knowledge_id"]])
            for item in neighbor_pairs
        )
        or graph["neighbor_depths"]
        != {str(node_id): node_depths[node_id] for node_id in connected_ids}
        or graph["seed_frontier"]
        is not (
            seed_knowledge_id in connected_ids
            and any(
                node_depths[node_id] > 1
                for node_id in connected_ids
                if node_id != seed_knowledge_id
            )
        )
    ):
        return False
    return True


def _is_timeline_item(value: object) -> bool:
    return (
        type(value) is dict
        and set(value) == _TIMELINE_ITEM_FIELDS
        and _is_positive_int(value["knowledge_id"])
        and all(
            type(value[field]) is str
            for field in _TIMELINE_ITEM_FIELDS - {"knowledge_id", "tags", "retrieval_score"}
        )
        and _is_string_list(value["tags"])
        and _is_unit_score(value["retrieval_score"])
        and (
            not value["source_url"]
            or sanitize_public_source_url(value["source_url"]) == value["source_url"]
        )
        and _is_canonical_public_source(value["source"], value["knowledge_id"])
        and value["source"]
        == (
            value["source_url"]
            if value["source_url"]
            else build_entry_locator(value["knowledge_id"])
        )
    )


def _timeline_sort_key(
    item: dict[str, object],
    sort_order: str,
) -> tuple[object, ...]:
    from src.relations.exploration_service import ExplorationService

    missing_rank, parse_kind, parsed_ts, raw_value = (
        ExplorationService._parse_time_sort_key(item["time_value"])
    )
    if sort_order == "desc":
        if missing_rank:
            return (1, 1, 0.0, "", item["knowledge_id"])
        if parse_kind == 0:
            return (0, 0, -parsed_ts, "", item["knowledge_id"])
        return (0, 1, raw_value, item["knowledge_id"])
    return (
        missing_rank,
        parse_kind,
        parsed_ts,
        raw_value,
        item["knowledge_id"],
    )


def _infer_timeline_field(items: list[dict[str, object]]) -> str:
    from src.relations.exploration_service import ExplorationService

    source_counts = {field: 0 for field in _TIMELINE_PRIORITY}
    parseable_counts = {field: 0 for field in _TIMELINE_PRIORITY}
    for item in items:
        source = item["time_source"]
        if not item["time_value"] or source not in source_counts:
            continue
        source_counts[source] += 1
        missing_rank, parse_kind, _, _ = ExplorationService._parse_time_sort_key(
            item["time_value"]
        )
        if missing_rank == 0 and parse_kind == 0:
            parseable_counts[source] += 1
    baseline = (
        parseable_counts
        if any(count > 0 for count in parseable_counts.values())
        else source_counts
    )
    nonzero = {key: count for key, count in baseline.items() if count > 0}
    if not nonzero:
        return "unavailable"
    maximum = max(nonzero.values())
    leaders = [key for key, count in nonzero.items() if count == maximum]
    return leaders[0] if len(leaders) == 1 else "mixed"


def _timeline_item_is_coherent(item: dict[str, object]) -> bool:
    source = item["time_source"]
    if source not in _TIMELINE_TIME_SOURCES:
        return False
    if source == "unavailable":
        return (
            item["time_value"] == ""
            and item["event_time"] == ""
            and item["published_at"] == ""
            and item["archived_at"] == ""
            and item["time_source_field"] == ""
            and item["time_precision"] == "unavailable"
            and item["citation_locator"]
            == build_entry_locator(item["knowledge_id"])
        )
    physical_field = item["time_source_field"]
    return (
        bool(item["time_value"])
        and item["time_value"] == item[source]
        and physical_field in _TIMELINE_PHYSICAL_FIELDS[source]
        and item["time_precision"] == "structured_field"
        and item["citation_locator"]
        == build_metadata_locator(item["knowledge_id"], physical_field)
    )


def _is_contrast_candidate(value: object) -> bool:
    return (
        type(value) is dict
        and set(value) == _CONTRAST_CANDIDATE_FIELDS
        and _is_positive_int(value["knowledge_id"])
        and all(
            type(value[field]) is str
            for field in _CONTRAST_CANDIDATE_FIELDS
            - {
                "knowledge_id",
                "tags",
                "retrieval_score",
                "relation_signal_score",
                "relation_types",
            }
        )
        and _is_string_list(value["tags"])
        and _is_unit_score(value["retrieval_score"])
        and _is_unit_score(value["relation_signal_score"])
        and _is_string_list(value["relation_types"])
        and all(item in _RELATION_TYPE_VALUES for item in value["relation_types"])
        and (
            not value["source_url"]
            or sanitize_public_source_url(value["source_url"]) == value["source_url"]
        )
        and _is_canonical_public_source(value["source"], value["knowledge_id"])
        and value["source"]
        == (
            value["source_url"]
            if value["source_url"]
            else build_entry_locator(value["knowledge_id"])
        )
    )


def _candidate_provenance(
    item: dict[str, object],
    topic_side: str,
) -> dict[str, object]:
    return {
        "topic_side": topic_side,
        "knowledge_id": item["knowledge_id"],
        "source": item["source"],
        "source_url": item["source_url"],
        "citation_locator": item["citation_locator"],
    }


def _contrast_relation_pair_is_coherent(
    pair: object,
    *,
    candidates_a: dict[int, dict[str, object]],
    candidates_b: dict[int, dict[str, object]],
) -> bool:
    fields = {
        "topic_a_knowledge_id",
        "topic_b_knowledge_id",
        "topic_a_source",
        "topic_b_source",
        "topic_a_citation_locator",
        "topic_b_citation_locator",
        "confidence",
        "relation_types",
        "evidence_path",
    }
    if type(pair) is not dict or set(pair) != fields:
        return False
    id_a = pair["topic_a_knowledge_id"]
    id_b = pair["topic_b_knowledge_id"]
    if id_a not in candidates_a or id_b not in candidates_b or id_a == id_b:
        return False
    candidate_a = candidates_a[id_a]
    candidate_b = candidates_b[id_b]
    path = pair["evidence_path"]
    seed_path = (
        [edge for edge in path if "seed_path" in edge["evidence_roles"]]
        if type(path) is list
        and all(type(edge) is dict and "evidence_roles" in edge for edge in path)
        else []
    )
    expected_relation_types = sorted(
        {edge["relation_type"] for edge in seed_path}
    )
    expected_confidence = (
        0.9 if len(seed_path) == 1 else 0.75 if len(seed_path) == 2 else 0.0
    )
    return (
        pair["topic_a_source"] == candidate_a["source"]
        and pair["topic_b_source"] == candidate_b["source"]
        and pair["topic_a_citation_locator"] == candidate_a["citation_locator"]
        and pair["topic_b_citation_locator"] == candidate_b["citation_locator"]
        and _score_equals(pair["confidence"], expected_confidence)
        and _is_string_list(pair["relation_types"])
        and pair["relation_types"] == sorted(set(pair["relation_types"]))
        and bool(pair["relation_types"])
        and all(item in _RELATION_TYPE_VALUES for item in pair["relation_types"])
        and pair["relation_types"] == expected_relation_types
        and type(path) is list
        and all(_is_bridge_edge_evidence(edge) for edge in path)
        and _bridge_seed_path_reaches(
            path,
            seed_knowledge_id=id_a,
            candidate_knowledge_id=id_b,
            max_depth=2,
        )
    )


def _contrast_dimensions_are_coherent(
    value: object,
    *,
    candidates_a_list: list[dict[str, object]],
    candidates_b_list: list[dict[str, object]],
    shared_tags: list[str],
    only_a_tags: list[str],
    only_b_tags: list[str],
    overlap_ids: list[int],
) -> bool:
    fields = {
        "shared_tags_count",
        "topic_a_only_tags_count",
        "topic_b_only_tags_count",
        "overlap_knowledge_count",
        "candidate_count",
        "relation_graph_signal",
        "provenance",
    }
    if type(value) is not dict or set(value) != fields:
        return False
    candidates_a = {item["knowledge_id"]: item for item in candidates_a_list}
    candidates_b = {item["knowledge_id"]: item for item in candidates_b_list}
    counts = value["candidate_count"]
    signal = value["relation_graph_signal"]
    provenance = value["provenance"]
    if (
        value["shared_tags_count"] != len(shared_tags)
        or value["topic_a_only_tags_count"] != len(only_a_tags)
        or value["topic_b_only_tags_count"] != len(only_b_tags)
        or value["overlap_knowledge_count"] != len(overlap_ids)
        or counts
        != {"topic_a": len(candidates_a_list), "topic_b": len(candidates_b_list)}
        or type(signal) is not dict
        or set(signal)
        != {
            "connected_candidate_pairs_count",
            "topic_a_connected_candidate_count",
            "topic_b_connected_candidate_count",
            "shared_relation_types",
            "max_relation_hops",
        }
        or type(provenance) is not dict
        or set(provenance)
        != {
            "shared_tags",
            "only_a_tags",
            "only_b_tags",
            "overlap_knowledge_ids",
            "relation_graph_signal",
        }
    ):
        return False
    pairs = provenance["relation_graph_signal"]
    if (
        type(pairs) is not list
        or not all(
            _contrast_relation_pair_is_coherent(
                pair,
                candidates_a=candidates_a,
                candidates_b=candidates_b,
            )
            for pair in pairs
        )
        or signal["connected_candidate_pairs_count"] != len(pairs)
        or signal["topic_a_connected_candidate_count"]
        != len({pair["topic_a_knowledge_id"] for pair in pairs})
        or signal["topic_b_connected_candidate_count"]
        != len({pair["topic_b_knowledge_id"] for pair in pairs})
        or signal["shared_relation_types"]
        != sorted(
            {
                relation_type
                for pair in pairs
                for relation_type in pair["relation_types"]
            }
        )
        or signal["max_relation_hops"]
        != max(
            (
                sum(
                    1
                    for edge in pair["evidence_path"]
                    if "seed_path" in edge["evidence_roles"]
                )
                for pair in pairs
            ),
            default=0,
        )
    ):
        return False

    expected_shared = {
        tag: {
            "topic_a": [
                _candidate_provenance(item, "topic_a")
                for item in candidates_a_list
                if tag in item["tags"]
            ],
            "topic_b": [
                _candidate_provenance(item, "topic_b")
                for item in candidates_b_list
                if tag in item["tags"]
            ],
        }
        for tag in shared_tags
    }
    expected_only_a = {
        tag: [
            _candidate_provenance(item, "topic_a")
            for item in candidates_a_list
            if tag in item["tags"]
        ]
        for tag in only_a_tags
    }
    expected_only_b = {
        tag: [
            _candidate_provenance(item, "topic_b")
            for item in candidates_b_list
            if tag in item["tags"]
        ]
        for tag in only_b_tags
    }
    expected_overlap = {
        str(knowledge_id): {
            "topic_a": _candidate_provenance(candidates_a[knowledge_id], "topic_a"),
            "topic_b": _candidate_provenance(candidates_b[knowledge_id], "topic_b"),
        }
        for knowledge_id in overlap_ids
    }
    if (
        provenance["shared_tags"] != expected_shared
        or provenance["only_a_tags"] != expected_only_a
        or provenance["only_b_tags"] != expected_only_b
        or provenance["overlap_knowledge_ids"] != expected_overlap
    ):
        return False

    for side, id_key in (
        (candidates_a_list, "topic_a_knowledge_id"),
        (candidates_b_list, "topic_b_knowledge_id"),
    ):
        for item in side:
            related_pairs = [pair for pair in pairs if pair[id_key] == item["knowledge_id"]]
            expected_score = max(
                (pair["confidence"] for pair in related_pairs),
                default=0.0,
            )
            expected_types = sorted(
                {
                    relation_type
                    for pair in related_pairs
                    for relation_type in pair["relation_types"]
                }
            )
            if (
                not _score_equals(item["relation_signal_score"], expected_score)
                or item["relation_types"] != expected_types
            ):
                return False
    return True


def _has_common_relation_result_shape(payload: dict[str, object]) -> bool:
    return (
        payload["schema_version"] == "phase_b.v1"
        and type(payload["implementation_level"]) is str
        and bool(payload["implementation_level"])
        and _is_string_list(payload["limitation_notes"])
        and _is_nonnegative_int(payload["evidence_count"])
        and _is_unit_score(payload["confidence"])
        and _is_unit_score(payload["coverage"])
    )


def _strict_subgraph_payload(
    result: object,
    *,
    seed_knowledge_id: int,
    max_depth: int,
    max_nodes: int,
    max_edges: int,
    relation_types: Optional[list[str]],
) -> dict[str, object]:
    required = _COMMON_RELATION_RESULT_FIELDS | frozenset(
        {
            "seed_knowledge_id",
            "max_depth",
            "total_nodes",
            "total_edges",
            "truncated",
            "nodes",
            "edges",
            "grouped_edges",
        }
    )
    payload = _strict_domain_payload(
        result,
        expected_type=RelationSubgraphResult,
        required_fields=required,
    )
    nodes = payload["nodes"]
    edges = payload["edges"]
    grouped_edges = payload["grouped_edges"]
    node_by_id = {
        node["knowledge_id"]: node
        for node in nodes
        if type(node) is dict and _is_positive_int(node.get("knowledge_id"))
    } if type(nodes) is list else {}
    node_ids = set(node_by_id)
    expected_groups = {
        relation_type.value: [
            edge for edge in edges if edge["relation_type"] == relation_type.value
        ]
        for relation_type in RelationType
        if type(edges) is list
        and any(edge["relation_type"] == relation_type.value for edge in edges)
    }
    valid = (
        _has_common_relation_result_shape(payload)
        and payload["implementation_level"] == "baseline"
        and _is_positive_int(payload["seed_knowledge_id"])
        and payload["seed_knowledge_id"] == seed_knowledge_id
        and _is_positive_int(payload["max_depth"])
        and payload["max_depth"] == max_depth
        and _is_positive_int(payload["total_nodes"])
        and _is_nonnegative_int(payload["total_edges"])
        and type(payload["truncated"]) is bool
        and type(nodes) is list
        and all(
            type(node) is dict
            and set(node) == {"knowledge_id", "depth"}
            and _is_positive_int(node.get("knowledge_id"))
            and _is_nonnegative_int(node.get("depth"))
            and node["depth"] <= max_depth
            for node in nodes
        )
        and len({node["knowledge_id"] for node in nodes}) == len(nodes)
        and len(nodes) <= max_nodes
        and nodes == sorted(
            nodes,
            key=lambda node: (node["depth"], node["knowledge_id"]),
        )
        and nodes[0] == {"knowledge_id": seed_knowledge_id, "depth": 0}
        and sum(1 for node in nodes if node["depth"] == 0) == 1
        and type(edges) is list
        and all(_is_relation_edge_payload(edge) for edge in edges)
        and len(edges) <= max_edges
        and len({_relation_edge_key(edge) for edge in edges}) == len(edges)
        and all(
            edge["source_knowledge_id"] in node_ids
            and edge["target_knowledge_id"] in node_ids
            and (
                relation_types is None
                or edge["relation_type"] in relation_types
            )
            for edge in edges
        )
        and all(
            any(
                node["knowledge_id"]
                in {edge["source_knowledge_id"], edge["target_knowledge_id"]}
                and any(
                    adjacent_id in node_ids
                    and node_by_id[adjacent_id]["depth"] == node["depth"] - 1
                    for adjacent_id in {
                        edge["source_knowledge_id"],
                        edge["target_knowledge_id"],
                    } - {node["knowledge_id"]}
                )
                for edge in edges
            )
            for node in nodes
            if node["knowledge_id"] != seed_knowledge_id
        )
        and type(grouped_edges) is dict
        and all(
            type(key) is str
            and key in _RELATION_TYPE_VALUES
            and type(group) is list
            and all(
                _is_relation_edge_payload(edge)
                and edge["relation_type"] == key
                for edge in group
            )
            for key, group in grouped_edges.items()
        )
        and grouped_edges == expected_groups
        and [edge for group in grouped_edges.values() for edge in group] == edges
        and payload["total_nodes"] == len(nodes)
        and payload["total_edges"] == len(edges)
        and payload["evidence_count"] == len(edges)
        and _score_equals(
            payload["confidence"],
            0.0 if not edges else (0.6 if payload["truncated"] else 0.8),
        )
        and _score_equals(
            payload["coverage"],
            round(min(len(nodes) / (max_depth + 1), 1.0), 4),
        )
        and (
            not payload["truncated"]
            or len(nodes) == max_nodes
            or len(edges) == max_edges
        )
    )
    if not valid:
        raise TypeError("subgraph result contract is invalid")
    return payload


def _strict_relation_explanation_payload(
    result: object,
    *,
    source_knowledge_id: int,
    target_knowledge_id: int,
    max_depth: int,
    relation_types: Optional[list[str]],
) -> dict[str, object]:
    required = _COMMON_RELATION_RESULT_FIELDS | frozenset(
        {
            "source_knowledge_id",
            "target_knowledge_id",
            "found",
            "explanation_type",
            "hops",
            "path",
            "supporting_relations",
            "intermediate_knowledge_ids",
            "summary",
            "evidence_items",
        }
    )
    payload = _strict_domain_payload(
        result,
        expected_type=RelationExplanationResult,
        required_fields=required,
    )
    path = payload["path"]
    supporting = payload["supporting_relations"]
    evidence_items = payload["evidence_items"]
    intermediate_ids = payload["intermediate_knowledge_ids"]
    walked_nodes = (
        _walk_relation_path(path, source_knowledge_id=source_knowledge_id)
        if type(path) is list
        and all(_is_relation_edge_payload(item) for item in path)
        else None
    )
    all_edges = (
        [*path, *supporting]
        if type(path) is list and type(supporting) is list
        else []
    )
    relation_filter_valid = all(
        relation_types is None or edge["relation_type"] in relation_types
        for edge in all_edges
        if type(edge) is dict and "relation_type" in edge
    )
    expected_confidence = (
        0.0
        if not payload["found"]
        else 0.9
        if payload["hops"] <= 1
        else 0.75
        if payload["hops"] == 2
        else 0.6
    )
    expected_coverage = 0.0 if not payload["found"] else 1.0
    direct_valid = (
        payload["found"] is True
        and payload["explanation_type"] == "direct"
        and payload["hops"] == 1
        and len(path) == 1
        and 1 <= len(supporting) <= 100
        and path[0] == supporting[0]
        and all(
            {
                edge["source_knowledge_id"],
                edge["target_knowledge_id"],
            }
            == {source_knowledge_id, target_knowledge_id}
            for edge in supporting
        )
        and not intermediate_ids
        and evidence_items
        == [
            _expected_explanation_evidence(edge, index)
            for index, edge in enumerate(supporting)
        ]
        and payload["summary"]
        == _expected_path_summary(source_knowledge_id, path)
    )
    path_valid = (
        payload["found"] is True
        and payload["explanation_type"] == "path"
        and 2 <= payload["hops"] <= max_depth
        and payload["hops"] == len(path)
        and supporting == path
        and walked_nodes is not None
        and walked_nodes[-1] == target_knowledge_id
        and intermediate_ids == walked_nodes[1:-1]
        and evidence_items
        == [
            _expected_explanation_evidence(edge, index)
            for index, edge in enumerate(path)
        ]
        and payload["summary"]
        == _expected_path_summary(source_knowledge_id, path)
    )
    not_found_valid = (
        payload["found"] is False
        and payload["explanation_type"] == "not_found"
        and payload["hops"] == 0
        and not path
        and not supporting
        and not intermediate_ids
        and not evidence_items
        and payload["summary"]
        == (
            f"未找到 {source_knowledge_id} 与 {target_knowledge_id} "
            f"在 {max_depth} 跳内的关系解释"
        )
    )
    valid = (
        _has_common_relation_result_shape(payload)
        and payload["implementation_level"] == "baseline"
        and _is_positive_int(payload["source_knowledge_id"])
        and payload["source_knowledge_id"] == source_knowledge_id
        and _is_positive_int(payload["target_knowledge_id"])
        and payload["target_knowledge_id"] == target_knowledge_id
        and source_knowledge_id != target_knowledge_id
        and type(payload["found"]) is bool
        and type(payload["explanation_type"]) is str
        and _is_nonnegative_int(payload["hops"])
        and type(path) is list
        and all(_is_relation_edge_payload(item) for item in path)
        and type(supporting) is list
        and all(_is_relation_edge_payload(item) for item in supporting)
        and type(intermediate_ids) is list
        and all(_is_positive_int(item) for item in intermediate_ids)
        and type(payload["summary"]) is str
        and type(evidence_items) is list
        and all(
            _is_relation_explanation_evidence(item)
            for item in evidence_items
        )
        and payload["evidence_count"]
        == max(len(evidence_items), len(path), len(supporting))
        and relation_filter_valid
        and _score_equals(payload["confidence"], expected_confidence)
        and _score_equals(payload["coverage"], expected_coverage)
        and (direct_valid or path_valid or not_found_valid)
    )
    if not valid:
        raise TypeError("relation explanation contract is invalid")
    return payload


def _strict_collected_evidence_payload(
    result: object,
    *,
    question: str,
    top_k: int,
    relation_max_depth: int,
    include_chunks: bool,
) -> dict[str, object]:
    required = _COMMON_RELATION_RESULT_FIELDS | frozenset(
        {
            "chunk_retrieval_status",
            "question",
            "found",
            "seed_knowledge_id",
            "seed_title",
            "total_evidence",
            "related_evidence_count",
            "summary",
            "evidence",
        }
    )
    payload = _strict_domain_payload(
        result,
        expected_type=CollectedEvidenceResult,
        required_fields=required,
    )
    evidence = payload["evidence"]
    seed_id = payload["seed_knowledge_id"]
    ranks = [
        item["retrieval_rank"]
        for item in evidence
        if type(item) is dict and _is_positive_int(item.get("retrieval_rank"))
    ] if type(evidence) is list else []
    item_keys = [
        (
            item["knowledge_id"],
            item["chunk_id"],
            item["chunk_index"],
        )
        for item in evidence
        if type(item) is dict
        and {"knowledge_id", "chunk_id", "chunk_index"}.issubset(item)
    ] if type(evidence) is list else []

    def _item_relation_is_coherent(item: dict[str, object]) -> bool:
        relation_path = item["relation_path"]
        relation_evidence = item["relation_evidence_items"]
        if item["is_seed"]:
            return (
                item["knowledge_id"] == seed_id
                and item["retrieval_rank"] == 1
                and item["relation_found"] is False
                and item["relation_explanation_type"] == ""
                and item["relation_hops"] == 0
                and item["relation_summary"] == ""
                and not relation_path
                and not relation_evidence
                and (
                    not include_chunks
                    or _score_equals(item["relation_score"], 1.0)
                )
            )
        if not item["relation_found"]:
            return (
                item["relation_explanation_type"] == ""
                and item["relation_hops"] == 0
                and item["relation_summary"] == ""
                and not relation_path
                and not relation_evidence
                and _score_equals(item["relation_score"], 0.0)
            )
        walked = _walk_relation_path(
            relation_path,
            source_knowledge_id=seed_id,
        )
        expected_explanation_type = (
            "direct" if item["relation_hops"] == 1 else "path"
        )
        return (
            item["relation_explanation_type"] == expected_explanation_type
            and 1 <= item["relation_hops"] <= relation_max_depth
            and item["relation_hops"] == len(relation_path)
            and walked is not None
            and walked[-1] == item["knowledge_id"]
            and relation_evidence
            == [
                _expected_explanation_evidence(edge, index)
                for index, edge in enumerate(relation_path)
            ]
            and item["relation_summary"]
            == _expected_path_summary(seed_id, relation_path)
            and (
                not include_chunks
                or _score_equals(
                    item["relation_score"],
                    round(1.0 / (1 + item["relation_hops"]), 4),
                )
            )
        )

    def _item_chunk_is_coherent(item: dict[str, object]) -> bool:
        chunk_id = item["chunk_id"]
        chunk_index = item["chunk_index"]
        expected_locator = build_chunk_locator(
            item["knowledge_id"],
            chunk_id=chunk_id,
            chunk_index=chunk_index,
        )
        if item["citation_locator"] != expected_locator:
            return False
        if not include_chunks:
            return (
                chunk_id is None
                and chunk_index is None
                and item["chunk_text"] == ""
            )
        if chunk_id is None and chunk_index is None:
            return item["chunk_text"] == ""
        return bool(item["chunk_text"])

    expected_confidence = 0.0
    expected_coverage = 0.0
    if type(evidence) is list and evidence:
        expected_confidence = round(
            min(
                max(
                    sum(
                        item["ranking_score"]
                        if item["ranking_score"] > 0
                        else item["retrieval_score"]
                        for item in evidence
                    )
                    / len(evidence),
                    0.0,
                ),
                1.0,
            ),
            4,
        )
        expected_coverage = round(
            min(
                max(
                    max(
                        sum(item["coverage_score"] for item in evidence)
                        / len(evidence),
                        sum(1 for item in evidence if item["relation_found"])
                        / len(evidence),
                    ),
                    0.0,
                ),
                1.0,
            ),
            4,
        )
    valid = (
        _has_common_relation_result_shape(payload)
        and payload["implementation_level"] == "baseline"
        and type(payload["question"]) is str
        and payload["question"] == question
        and type(payload["found"]) is bool
        and (seed_id is None or _is_positive_int(seed_id))
        and type(payload["seed_title"]) is str
        and _is_nonnegative_int(payload["total_evidence"])
        and _is_nonnegative_int(payload["related_evidence_count"])
        and payload["chunk_retrieval_status"] in CHUNK_RETRIEVAL_STATUSES
        and type(payload["chunk_retrieval_status"]) is str
        and type(payload["summary"]) is str
        and type(evidence) is list
        and all(_is_collected_evidence_item(item) for item in evidence)
        and len(evidence) <= top_k
        and len(ranks) == len(evidence)
        and len(set(ranks)) == len(ranks)
        and ranks == list(range(1, len(evidence) + 1))
        and len(item_keys) == len(evidence)
        and len(set(item_keys)) == len(item_keys)
        and payload["total_evidence"] == len(evidence)
        and payload["evidence_count"] == len(evidence)
        and payload["related_evidence_count"] <= len(evidence)
        and payload["related_evidence_count"]
        == sum(1 for item in evidence if item["relation_found"])
        and payload["found"] is bool(evidence)
        and (
            payload["chunk_retrieval_status"] == "not_requested"
            if not include_chunks
            else payload["chunk_retrieval_status"] != "not_requested"
        )
        and all(_item_relation_is_coherent(item) for item in evidence)
        and all(_item_chunk_is_coherent(item) for item in evidence)
        and (
            payload["chunk_retrieval_status"] != "success"
            or any(
                item["chunk_id"] is not None or item["chunk_index"] is not None
                for item in evidence
            )
        )
        and _score_equals(payload["confidence"], expected_confidence)
        and _score_equals(payload["coverage"], expected_coverage)
        and (
            (
                payload["found"]
                and _is_positive_int(seed_id)
                and sum(1 for item in evidence if item["is_seed"]) == 1
                and any(
                    item["is_seed"] and item["knowledge_id"] == seed_id
                    for item in evidence
                )
            )
            or (
                not payload["found"]
                and seed_id is None
                and payload["seed_title"] == ""
                and not evidence
            )
        )
    )
    if not valid:
        raise TypeError("collected evidence contract is invalid")
    return payload


def _strict_bridge_payload(
    result: object,
    *,
    seed_knowledge_id: int,
    max_depth: int,
    top_k: int,
) -> dict[str, object]:
    required = _COMMON_RELATION_RESULT_FIELDS | frozenset(
        {
            "seed_knowledge_id",
            "found",
            "max_depth",
            "total_bridges",
            "summary",
            "evidence_sources",
            "subgraph_truncated",
            "subgraph_max_nodes",
            "subgraph_max_edges",
            "subgraph_node_count",
            "subgraph_edge_count",
            "items",
        }
    )
    payload = _strict_domain_payload(
        result,
        expected_type=BridgeDiscoveryResult,
        required_fields=required,
    )
    items = payload["items"]

    def _candidate_is_coherent(item: dict[str, object]) -> bool:
        if (
            not _is_bridge_item(item)
            or item["knowledge_id"] == seed_knowledge_id
            or not (1 <= item["depth"] <= max_depth)
            or item["connected_knowledge_ids"]
            != sorted(item["connected_knowledge_ids"])
            or len(item["connected_knowledge_ids"]) < 2
            or item["knowledge_id"] in item["connected_knowledge_ids"]
            or not item["relation_types"]
            or item["relation_types"] != sorted(set(item["relation_types"]))
            or not item["evidence_path"]
            or not all(
                _is_bridge_edge_evidence(edge)
                for edge in item["evidence_path"]
            )
            or not _bridge_seed_path_reaches(
                item["evidence_path"],
                seed_knowledge_id=seed_knowledge_id,
                candidate_knowledge_id=item["knowledge_id"],
                max_depth=max_depth,
            )
            or not _bridge_supporting_subgraph_is_coherent(
                item["supporting_subgraph"],
                seed_knowledge_id=seed_knowledge_id,
                candidate=item,
                max_depth=max_depth,
            )
        ):
            return False
        adjacency_neighbors = set()
        for edge in item["evidence_path"]:
            if "candidate_adjacency" not in edge["evidence_roles"]:
                continue
            endpoints = {
                edge["source_knowledge_id"],
                edge["target_knowledge_id"],
            }
            if item["knowledge_id"] not in endpoints:
                return False
            adjacency_neighbors.update(endpoints - {item["knowledge_id"]})
        if adjacency_neighbors != set(item["connected_knowledge_ids"]):
            return False
        support = item["supporting_subgraph"]
        expected_relation_types = sorted(
            {
                edge["relation_type"]
                for edge in item["evidence_path"]
                if "candidate_adjacency" in edge["evidence_roles"]
            }
        )
        if (
            item["relation_types"] != expected_relation_types
            or not {
                _relation_edge_key(edge) for edge in item["evidence_path"]
            }.issubset(
                {_relation_edge_key(edge) for edge in support["edges"]}
            )
        ):
            return False
        structural = support["structural_score_inputs"]
        graph = support["graph_score_inputs"]
        semantic = support["semantic_score_inputs"]
        expected_structural = round(
            min(
                max(
                    0.7 * min(structural["neighbor_count"] / 4.0, 1.0)
                    + 0.3
                    * max(
                        0.0,
                        1
                        - (
                            (structural["candidate_depth"] - 1)
                            / max(structural["max_depth"], 1)
                        ),
                    ),
                    0.0,
                ),
                1.0,
            ),
            4,
        )
        expected_graph = round(
            min(
                max(
                    graph["disconnected_pair_ratio"] * 0.55
                    + min(graph["depth_span"] / max(max_depth, 1), 1.0) * 0.25
                    + (1.0 if graph["seed_frontier"] else 0.0) * 0.2,
                    0.0,
                ),
                1.0,
            ),
            4,
        )
        expected_semantic = round(float(semantic["semantic_score"]), 4)
        expected_bridge = round(
            expected_structural * 0.4
            + expected_graph * 0.4
            + expected_semantic * 0.2,
            4,
        )
        return (
            _score_equals(item["structural_bridge_score"], expected_structural)
            and _score_equals(item["graph_bridge_score"], expected_graph)
            and _score_equals(item["semantic_bridge_score"], expected_semantic)
            and _score_equals(item["bridge_score"], expected_bridge)
            and (expected_semantic > 0.0 or expected_graph >= 0.45)
            and len(support["scope"]["node_depths"])
            == payload["subgraph_node_count"]
            and len(support["edges"]) == payload["subgraph_edge_count"]
        )

    expected_confidence = 0.0
    expected_coverage = 0.0
    if type(items) is list and items:
        expected_confidence = round(
            min(
                max(
                    sum(min(item["bridge_score"], 3.0) / 3.0 for item in items)
                    / len(items)
                    * 0.8,
                    0.0,
                ),
                1.0,
            ),
            4,
        )
        expected_coverage = round(
            min(max(len(items) / max(max_depth, 1), 0.0), 1.0),
            4,
        )
    valid = (
        _has_common_relation_result_shape(payload)
        and payload["implementation_level"] == "partial"
        and _is_positive_int(payload["seed_knowledge_id"])
        and payload["seed_knowledge_id"] == seed_knowledge_id
        and _is_positive_int(payload["max_depth"])
        and payload["max_depth"] == max_depth
        and type(payload["found"]) is bool
        and _is_nonnegative_int(payload["total_bridges"])
        and type(payload["summary"]) is str
        and payload["evidence_sources"] == _BRIDGE_EVIDENCE_SOURCES
        and bool(payload["limitation_notes"])
        and type(payload["subgraph_truncated"]) is bool
        and _is_positive_int(payload["subgraph_max_nodes"])
        and _is_positive_int(payload["subgraph_max_edges"])
        and _is_nonnegative_int(payload["subgraph_node_count"])
        and _is_nonnegative_int(payload["subgraph_edge_count"])
        and payload["subgraph_node_count"] <= payload["subgraph_max_nodes"]
        and payload["subgraph_edge_count"] <= payload["subgraph_max_edges"]
        and type(items) is list
        and len(items) <= top_k
        and all(_candidate_is_coherent(item) for item in items)
        and len({item["knowledge_id"] for item in items}) == len(items)
        and items
        == sorted(
            items,
            key=lambda item: (
                -item["bridge_score"],
                item["depth"],
                item["knowledge_id"],
            ),
        )
        and payload["total_bridges"] == len(items)
        and payload["evidence_count"] == len(items)
        and payload["found"] is bool(items)
        and _score_equals(payload["confidence"], expected_confidence)
        and _score_equals(payload["coverage"], expected_coverage)
    )
    if not valid:
        raise TypeError("bridge result contract is invalid")
    return payload


def _strict_timeline_payload(
    result: object,
    *,
    topic: str,
    top_k: int,
    sort_order: str,
) -> dict[str, object]:
    required = _COMMON_RELATION_RESULT_FIELDS | frozenset(
        {
            "topic",
            "found",
            "inferred_time_field",
            "time_source_priority",
            "total_points",
            "summary",
            "evidence_sources",
            "items",
        }
    )
    payload = _strict_domain_payload(
        result,
        expected_type=TimelineResult,
        required_fields=required,
    )
    items = payload["items"]
    expected_confidence = 0.0
    expected_coverage = 0.0
    if type(items) is list and items:
        expected_confidence = round(
            min(
                max(
                    sum(item["retrieval_score"] for item in items)
                    / len(items)
                    * 0.85,
                    0.0,
                ),
                1.0,
            ),
            4,
        )
        expected_coverage = round(
            min(max(len(items) / 5.0, 0.0), 1.0),
            4,
        )
    valid = (
        _has_common_relation_result_shape(payload)
        and payload["implementation_level"] == "partial"
        and type(payload["topic"]) is str
        and payload["topic"] == topic
        and type(payload["found"]) is bool
        and type(payload["inferred_time_field"]) is str
        and payload["inferred_time_field"] in _TIMELINE_INFERRED_FIELDS
        and payload["time_source_priority"] == _TIMELINE_PRIORITY
        and _is_nonnegative_int(payload["total_points"])
        and type(payload["summary"]) is str
        and payload["evidence_sources"] == _TIMELINE_EVIDENCE_SOURCES
        and bool(payload["limitation_notes"])
        and type(items) is list
        and all(_is_timeline_item(item) for item in items)
        and len(items) <= top_k
        and len({item["knowledge_id"] for item in items}) == len(items)
        and all(_timeline_item_is_coherent(item) for item in items)
        and items == sorted(items, key=lambda item: _timeline_sort_key(item, sort_order))
        and payload["inferred_time_field"] == _infer_timeline_field(items)
        and payload["total_points"] == len(items)
        and payload["evidence_count"] == len(items)
        and payload["found"] is bool(items)
        and _score_equals(payload["confidence"], expected_confidence)
        and _score_equals(payload["coverage"], expected_coverage)
    )
    if not valid:
        raise TypeError("timeline result contract is invalid")
    return payload


def _strict_contrast_payload(
    result: object,
    *,
    topic_a: str,
    topic_b: str,
    top_k: int,
) -> dict[str, object]:
    required = _COMMON_RELATION_RESULT_FIELDS | frozenset(
        {
            "topic_a",
            "topic_b",
            "found",
            "summary",
            "evidence_sources",
            "comparison_dimensions",
            "shared_tags",
            "only_a_tags",
            "only_b_tags",
            "overlap_knowledge_ids",
            "topic_a_candidates",
            "topic_b_candidates",
        }
    )
    payload = _strict_domain_payload(
        result,
        expected_type=ContrastResult,
        required_fields=required,
    )
    candidates_a = payload["topic_a_candidates"]
    candidates_b = payload["topic_b_candidates"]
    tags_a = (
        {tag for item in candidates_a for tag in item["tags"]}
        if type(candidates_a) is list
        else set()
    )
    tags_b = (
        {tag for item in candidates_b for tag in item["tags"]}
        if type(candidates_b) is list
        else set()
    )
    ids_a = (
        {item["knowledge_id"] for item in candidates_a}
        if type(candidates_a) is list
        else set()
    )
    ids_b = (
        {item["knowledge_id"] for item in candidates_b}
        if type(candidates_b) is list
        else set()
    )
    expected_shared = sorted(tags_a & tags_b)
    expected_only_a = sorted(tags_a - tags_b)
    expected_only_b = sorted(tags_b - tags_a)
    expected_overlap = sorted(ids_a & ids_b)
    candidates = (
        [*candidates_a, *candidates_b]
        if type(candidates_a) is list and type(candidates_b) is list
        else []
    )
    expected_confidence = 0.0
    if candidates:
        expected_confidence = round(
            min(
                max(
                    sum(item["retrieval_score"] for item in candidates)
                    / len(candidates)
                    * 0.8,
                    0.0,
                ),
                1.0,
            ),
            4,
        )
    signal_count = len(expected_shared) + len(expected_only_a) + len(expected_only_b)
    expected_coverage = (
        round(min(max(signal_count / 4.0, 0.0), 1.0), 4)
        if signal_count
        else 0.0
    )
    valid = (
        _has_common_relation_result_shape(payload)
        and payload["implementation_level"] == "partial"
        and type(payload["topic_a"]) is str
        and payload["topic_a"] == topic_a
        and type(payload["topic_b"]) is str
        and payload["topic_b"] == topic_b
        and type(payload["found"]) is bool
        and type(payload["summary"]) is str
        and payload["evidence_sources"] == _CONTRAST_EVIDENCE_SOURCES
        and bool(payload["limitation_notes"])
        and type(payload["comparison_dimensions"]) is dict
        and _is_string_list(payload["shared_tags"])
        and _is_string_list(payload["only_a_tags"])
        and _is_string_list(payload["only_b_tags"])
        and type(payload["overlap_knowledge_ids"]) is list
        and all(_is_positive_int(item) for item in payload["overlap_knowledge_ids"])
        and type(candidates_a) is list
        and all(_is_contrast_candidate(item) for item in candidates_a)
        and len(candidates_a) <= top_k
        and len(ids_a) == len(candidates_a)
        and all(
            item["citation_locator"] == build_entry_locator(item["knowledge_id"])
            and item["relation_types"] == sorted(set(item["relation_types"]))
            for item in candidates_a
        )
        and type(candidates_b) is list
        and all(_is_contrast_candidate(item) for item in candidates_b)
        and len(candidates_b) <= top_k
        and len(ids_b) == len(candidates_b)
        and all(
            item["citation_locator"] == build_entry_locator(item["knowledge_id"])
            and item["relation_types"] == sorted(set(item["relation_types"]))
            for item in candidates_b
        )
        and payload["shared_tags"] == expected_shared
        and payload["only_a_tags"] == expected_only_a
        and payload["only_b_tags"] == expected_only_b
        and payload["overlap_knowledge_ids"] == expected_overlap
        and _contrast_dimensions_are_coherent(
            payload["comparison_dimensions"],
            candidates_a_list=candidates_a,
            candidates_b_list=candidates_b,
            shared_tags=expected_shared,
            only_a_tags=expected_only_a,
            only_b_tags=expected_only_b,
            overlap_ids=expected_overlap,
        )
        and payload["evidence_count"] == len(candidates_a) + len(candidates_b)
        and payload["found"] is bool(candidates_a or candidates_b)
        and _score_equals(payload["confidence"], expected_confidence)
        and _score_equals(payload["coverage"], expected_coverage)
    )
    if not valid:
        raise TypeError("contrast result contract is invalid")
    return payload


def _has_safe_search_metadata(response: SearchResponse) -> bool:
    """Validate fields consumed by MCP filtering/serialization before use."""

    try:
        return all(
            (
                item.metadata.get("tags") is None
                or type(item.metadata["tags"]) is str
                or _is_string_list(item.metadata["tags"])
            )
            and all(
                item.metadata.get(field) is None
                or type(item.metadata[field]) is str
                for field in ("source_type", "archived_at")
            )
            for item in response.results
        )
    except Exception:
        return False


def _normalize_relation_types(value: object) -> Optional[list[str]]:
    """Validate the public relation filter before constructing any backend."""

    if value is None:
        return None
    if type(value) is not list:
        raise ValueError("relation_types must be a list")

    normalized: list[str] = []
    seen: set[str] = set()
    for item in value:
        if type(item) is not str:
            raise ValueError("relation_types items must be strings")
        candidate = item.strip()
        if not candidate or candidate not in _RELATION_TYPE_VALUES:
            raise ValueError("relation_types contains an unsupported value")
        if candidate not in seen:
            normalized.append(candidate)
            seen.add(candidate)
    return normalized or None


def _readonly_result_payload(
    data: dict[str, object],
    *,
    has_results: bool = True,
    degraded_issue: dict[str, object] | None = None,
) -> dict[str, object]:
    """Add the common read-only success/no-hit/degraded envelope.

    Existing domain fields are retained for backwards compatibility, while
    ``status`` and ``issues`` remain adapter-owned and therefore cannot be
    overwritten by a service payload.
    """

    issues = [degraded_issue] if degraded_issue is not None else []
    status = "degraded" if issues else ("success" if has_results else "no_hits")
    return sanitize_public_evidence({
        **data,
        "status": status,
        "issues": issues,
    })


def _retrieval_degraded_issue_from_notes(
    data: dict[str, object],
    *,
    operations: tuple[str, ...],
) -> dict[str, object] | None:
    """Project only the service's stable retrieval-degradation markers.

    Ordinary partial-v1 limitation notes describe product scope and must not be
    confused with a runtime outage.  The exploration service emits a bounded
    ``{operation}_retrieval_degraded[code,...]`` marker for the latter.
    """

    notes = data.get("limitation_notes", [])
    if not isinstance(notes, list):
        return None
    for note in notes:
        if not isinstance(note, str):
            continue
        for operation in operations:
            marker = f"{operation}_retrieval_degraded["
            if not note.startswith(marker):
                continue
            codes_text, separator, _ = note[len(marker):].partition("]")
            raw_code = codes_text.split(",", 1)[0].strip() if separator else ""
            try:
                code = ErrorCode(raw_code)
            except ValueError:
                code = ErrorCode.RETRIEVAL_BACKEND_FAILED
            return _public_issue(
                {},
                fallback_code=code,
                fallback_message="部分检索能力不可用，结果可能不完整",
                fallback_stage=f"{operation}_retrieval",
                fallback_recoverable=True,
                fallback_severity="",
            )
    return None


def _serialize_search_response(
    response: SearchResponse,
    *,
    source_type: str | None,
    tag: str | None,
) -> dict[str, object]:
    """Serialize exactly one MCP search envelope; filters cannot erase errors."""

    filtered = response.results
    if response.status not in {"invalid", "error"}:
        if source_type:
            filtered = tuple(
                item
                for item in filtered
                if item.metadata.get("source_type") == source_type
            )
        if tag:
            filtered = tuple(
                item
                for item in filtered
                if tag in parse_tags_string(item.metadata.get("tags", ""))
            )

    status = response.status
    if status == "success" and not filtered:
        status = "no_hits"

    payload = {
        "status": status,
        "strategy": response.strategy,
        "total": len(filtered),
        "results": [serialize_search_result(item) for item in filtered],
        "issues": [
            _public_issue(
                issue,
                fallback_code=ErrorCode.RETRIEVAL_BACKEND_FAILED,
                fallback_message="检索后端不可用",
                fallback_stage="retrieval",
                fallback_recoverable=True,
                fallback_severity="",
            )
            for issue in response.issues
        ],
    }
    return sanitize_public_evidence(payload)


def _workflow_public_issue(
    issue: object,
    *,
    terminal: str,
) -> dict[str, object]:
    """Serialize workflow issues without trusting backend-authored prose."""

    if type(issue) is PKVRuntimeError:
        raw: dict[str, object] = {
            "code": issue.code,
            "stage": issue.stage,
            "recoverable": issue.recoverable,
        }
    elif type(issue) is dict:
        raw = issue
    else:
        raw = {}

    raw_code = raw.get("code", ErrorCode.WORKFLOW_STEP_FAILED.value)
    if type(raw_code) is ErrorCode:
        code = raw_code
    elif type(raw_code) is str and raw_code in _ERROR_CODE_VALUES:
        code = ErrorCode(raw_code)
    else:
        code = ErrorCode.WORKFLOW_STEP_FAILED

    fallback_message = (
        "归档工作流降级" if terminal == "degraded" else "归档工作流执行失败"
    )
    payload: dict[str, object] = {
        "code": code.value,
        "message": _PUBLIC_RUNTIME_MESSAGES.get(code, fallback_message),
        "stage": (
            raw["stage"]
            if (
                type(raw.get("stage")) is str
                and raw["stage"] in _PUBLIC_WORKFLOW_STAGES
            )
            else "workflow"
        ),
        "recoverable": (
            raw.get("recoverable")
            if type(raw.get("recoverable")) is bool
            else False
        ),
        "severity": "warning" if terminal == "degraded" else "error",
    }
    step_id = raw.get("step_id")
    if type(step_id) is str and step_id in _ARCHIVE_STEP_IDS:
        payload["step_id"] = step_id
    for key in ("count", "limit"):
        value = raw.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            payload[key] = value
    return sanitize_public_evidence(payload)


def _workflow_result_payload(
    result: object,
    *,
    title_fallback: str = "",
    include_abstract: bool = False,
) -> dict[str, object]:
    """Expose only an explicit, internally consistent Workflow terminal."""

    def _invalid_result(message: str, *, stage: str = "workflow_result") -> dict[str, object]:
        return _runtime_failure_payload(
            RuntimeError(message),
            fallback_code=ErrorCode.WORKFLOW_STEP_FAILED,
            message="归档工作流返回了无效的持久化结果",
            stage=stage,
            error_label="归档失败",
        )

    try:
        terminal = getattr(result, "terminal", None)
        success = getattr(result, "success", None)
        raw_data = getattr(result, "data", None)
    except Exception:
        return _runtime_failure_payload(
            RuntimeError("workflow result access failed"),
            fallback_code=ErrorCode.WORKFLOW_STEP_FAILED,
            message="归档工作流返回了无法读取的结果",
            stage="workflow_result",
            error_label="归档失败",
        )
    if (
        type(terminal) is not str
        or terminal not in {"success", "degraded", "error"}
        or type(success) is not bool
        or success != (terminal != "error")
    ):
        return _runtime_failure_payload(
            RuntimeError("inconsistent workflow terminal"),
            fallback_code=ErrorCode.WORKFLOW_STEP_FAILED,
            message="归档工作流返回了不一致的终态",
            stage="workflow_terminal",
            error_label="归档失败",
        )
    if type(raw_data) is not dict:
        return _invalid_result("invalid workflow result data")

    storage_projection_requested = terminal in {"success", "degraded"} or any(
        key in raw_data
        for key in ("status", "knowledge_id", "core_committed", "do_not_retry")
    )
    data: dict[str, object] = {}
    storage_status = ""
    if storage_projection_requested:
        storage_status = raw_data.get("status")
        knowledge_id = raw_data.get("knowledge_id")
        if (
            type(storage_status) is not str
            or storage_status
            not in (_COMPLETED_STORAGE_STATUSES | _FATAL_STORAGE_STATUSES)
            or type(knowledge_id) is not int
            or knowledge_id <= 0
        ):
            return _invalid_result("invalid workflow result data")

        if storage_status in _COMPLETED_STORAGE_STATUSES:
            if terminal == "error" or raw_data.get("core_committed") is not True:
                return _invalid_result("incoherent workflow storage terminal")
            if (
                "do_not_retry" in raw_data
                and type(raw_data["do_not_retry"]) is not bool
            ):
                return _invalid_result("invalid workflow retry marker")
        elif (
            type(raw_data.get("core_committed")) is not bool
            or type(raw_data.get("do_not_retry")) is not bool
        ):
            return _invalid_result("invalid fatal workflow result data")

        if "title" in raw_data and type(raw_data["title"]) is not str:
            return _invalid_result("invalid workflow title")
        if "tags" in raw_data and not _is_string_list(raw_data["tags"]):
            return _invalid_result("invalid workflow tags")
        if (
            include_abstract
            and "summary_one_sentence" in raw_data
            and type(raw_data["summary_one_sentence"]) is not str
        ):
            return _invalid_result("invalid workflow summary")

        for key in (
            "status",
            "knowledge_id",
            "core_committed",
            "do_not_retry",
            "operation_id",
            "repair_actions",
            "storage_errors",
            "title",
            "tags",
            "summary_one_sentence",
        ):
            if key in raw_data:
                data[key] = raw_data[key]

    completed_issues: list[dict] | None = None
    if terminal in {"success", "degraded"}:
        try:
            raw_errors = getattr(result, "errors", None)
            raw_warnings = getattr(result, "warnings", None)
            raw_completed_issues = getattr(result, "issues", None)
        except Exception:
            raw_errors = raw_warnings = raw_completed_issues = None
        diagnostics_valid = (
            type(raw_errors) is list
            and type(raw_warnings) is list
            and type(raw_completed_issues) is list
            and not raw_errors
            and all(type(warning) is str for warning in raw_warnings)
            and all(type(issue) is dict for issue in raw_completed_issues)
            and all(
                type(issue.get("severity")) is str
                and issue["severity"] == "warning"
                for issue in raw_completed_issues
            )
            and (
                (not raw_warnings and not raw_completed_issues)
                if terminal == "success"
                else bool(raw_warnings or raw_completed_issues)
            )
        )
        if not diagnostics_valid:
            return _runtime_failure_payload(
                RuntimeError("invalid workflow result diagnostics"),
                fallback_code=ErrorCode.WORKFLOW_STEP_FAILED,
                message="归档工作流返回了无效的诊断结果",
                stage="workflow_result",
                error_label="归档失败",
            )
        completed_issues = raw_completed_issues

        if storage_status in _COMPLETED_STORAGE_STATUSES and (
            (terminal == "success" and storage_status != "ready")
            or (terminal == "degraded" and storage_status not in {"ready", "degraded"})
        ):
            return _invalid_result("incoherent workflow storage terminal")

    if storage_status in _FATAL_STORAGE_STATUSES:
        do_not_retry = (
            storage_status == "repair_required"
            or data["core_committed"] is True
            or data["do_not_retry"] is True
        )
        code = (
            ErrorCode.STORAGE_REPAIR_REQUIRED
            if storage_status == "repair_required"
            else ErrorCode.WORKFLOW_STEP_FAILED
        )
        issue = _public_issue(
            {},
            fallback_code=code,
            fallback_message=_PUBLIC_RUNTIME_MESSAGES[code],
            fallback_stage="workflow_result",
            fallback_recoverable=False,
        )
        fatal_data = {**data, "do_not_retry": do_not_retry}
        payload: dict[str, object] = {
            "success": False,
            "terminal": "error",
            "error": "归档失败",
            "warnings": [],
            "issues": [issue],
            **_public_storage_terminal(fatal_data),
            "do_not_retry": do_not_retry,
            "knowledge_id": data["knowledge_id"],
            "entry_locator": _public_entry_locator(data["knowledge_id"]),
        }
        if "title" in data:
            payload["title"] = data["title"]
        if "tags" in data:
            payload["tags"] = data["tags"]
        return sanitize_public_evidence(payload)

    if completed_issues is not None:
        raw_issues = completed_issues
    else:
        try:
            raw_issues = getattr(result, "issues", [])
        except Exception:
            return _runtime_failure_payload(
                RuntimeError("workflow diagnostics access failed"),
                fallback_code=ErrorCode.WORKFLOW_STEP_FAILED,
                message="归档工作流返回了无法读取的诊断结果",
                stage="workflow_result",
                error_label="归档失败",
            )
    issues = [
        _workflow_public_issue(item, terminal=terminal)
        for item in raw_issues
    ] if isinstance(raw_issues, (list, tuple)) else []
    if terminal == "error" and not issues:
        issues = [
            _workflow_public_issue({}, terminal=terminal)
        ]
    if terminal == "degraded" and not issues:
        issues = [_workflow_public_issue({}, terminal=terminal)]

    # Never expose raw warning prose.  A degraded terminal projects the stable
    # issue messages; other terminals omit non-structured warnings entirely.
    warnings = (
        list(dict.fromkeys(str(issue["message"]) for issue in issues))
        if terminal == "degraded"
        else []
    )

    payload: dict[str, object] = {
        "success": terminal != "error",
        "terminal": terminal,
        "warnings": warnings,
        "issues": issues,
        **_public_storage_terminal(data),
    }
    if terminal == "error":
        payload["error"] = "归档失败"

    if terminal != "error":
        payload["knowledge_id"] = data["knowledge_id"]
        payload["entry_locator"] = _public_entry_locator(data["knowledge_id"])
        payload["title"] = data.get(
            "title",
            title_fallback if type(title_fallback) is str else "",
        )
        payload["tags"] = data.get("tags", [])
        if include_abstract:
            payload["abstract"] = data.get("summary_one_sentence", "")
    return sanitize_public_evidence(payload)


def _public_entry_locator(knowledge_id: object) -> str:
    """Return a stable public entry locator without revealing its vault path."""
    try:
        parsed_id = int(knowledge_id)
    except (TypeError, ValueError):
        return ""
    return build_entry_locator(parsed_id) if parsed_id > 0 else ""


def _public_storage_terminal(data: object) -> dict:
    """Expose stable W1 terminal codes without leaking local paths/messages."""

    if not isinstance(data, dict):
        return {}
    payload: dict = {}
    raw_status = data.get("status")
    status = (
        raw_status
        if type(raw_status) is str and raw_status in _PUBLIC_STORAGE_STATUSES
        else ""
    )
    if status:
        payload["storage_status"] = status
    raw_operation_id = data.get("operation_id")
    operation_id = (
        raw_operation_id
        if (
            type(raw_operation_id) is str
            and len(raw_operation_id) == 32
            and all(char in "0123456789abcdef" for char in raw_operation_id)
        )
        else ""
    )
    if operation_id:
        payload["operation_id"] = operation_id
    repair_actions = data.get("repair_actions")
    if isinstance(repair_actions, list):
        payload["repair_actions"] = list(dict.fromkeys(
            action
            for action in repair_actions
            if type(action) is str and action in _PUBLIC_REPAIR_ACTIONS
        ))
    stable_errors = data.get("storage_errors")
    if isinstance(stable_errors, list):
        payload["storage_error_codes"] = [
            item["code"]
            for item in stable_errors
            if (
                isinstance(item, dict)
                and isinstance(item.get("code"), str)
                and item["code"] in _ERROR_CODE_VALUES
            )
        ]
    core_committed = data.get("core_committed")
    if type(core_committed) is bool:
        payload["core_committed"] = core_committed
    do_not_retry = data.get("do_not_retry")
    if type(do_not_retry) is bool:
        payload["do_not_retry"] = do_not_retry
        if do_not_retry and status not in {"ready", "deleted"}:
            payload["storage_warning"] = (
                "核心存储可能已提交或需先修复，请勿盲目重试归档；"
                "请按 repair_actions 处理"
            )
    return payload


# ============================================================
# Tool 1: search_knowledge — 搜索知识库
# ============================================================

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def search_knowledge(
    query: str,
    strategy: str = "auto",
    top_k: int = 5,
    source_type: Optional[str] = None,
    tag: Optional[str] = None,
) -> dict:
    """搜索知识库。

    Args:
        query: 搜索查询文本
        strategy: 检索策略 - "auto"(自动路由), "bm25"(关键词), "vector"(语义), "hybrid"(混合)
        top_k: 返回结果数量，默认 5，最大 50
        source_type: 按来源类型过滤 (wechat/zhihu/generic/chat/ai_chat/text)
        tag: 按标签过滤

    Returns:
        固定的五态检索 envelope: status, strategy, total, results, issues。
        ``invalid``/``error`` 不会伪装成空命中；``degraded`` 的限制通过
        issues 明确公开。
    """
    def _impl():
        if type(strategy) is not str or strategy not in _SEARCH_STRATEGIES:
            response = SearchResponse.invalid(
                "不支持的检索策略，可选: auto, bm25, vector, hybrid",
                strategy="unknown",
                stage="strategy_validation",
            )
            return _serialize_search_response(
                response,
                source_type=None,
                tag=None,
            )
        strategy_safe = strategy

        if type(query) is not str or not query.strip():
            response = SearchResponse.invalid(
                "query 不能为空且必须是字符串",
                strategy=strategy_safe,
                stage="query_validation",
            )
            return _serialize_search_response(
                response,
                source_type=None,
                tag=None,
            )
        query_clean = query.strip()

        if (
            source_type is not None
            and (type(source_type) is not str or not source_type.strip())
        ) or (
            tag is not None
            and (type(tag) is not str or not tag.strip())
        ):
            response = SearchResponse.invalid(
                "source_type 和 tag 必须是非空字符串或 null",
                strategy=strategy_safe,
                stage="filter_validation",
            )
            return _serialize_search_response(
                response,
                source_type=None,
                tag=None,
            )
        source_type_clean = source_type.strip() if source_type is not None else None
        tag_clean = tag.strip() if tag is not None else None

        if type(top_k) is not int or top_k <= 0:
            response = SearchResponse.invalid(
                "top_k 必须是正整数",
                strategy=strategy_safe,
                stage="top_k_validation",
            )
            return _serialize_search_response(
                response,
                source_type=source_type_clean,
                tag=tag_clean,
            )
        top_k_safe = min(top_k, 50)
        logger.info(
            "search_knowledge: query_length=%s strategy=%s top_k=%s",
            len(query),
            strategy_safe,
            top_k_safe,
        )

        try:
            # The application service owns strategy-specific retriever and
            # Provider composition.  The MCP adapter is responsible only for
            # input validation and its public response projection.
            response = get_application().search(
                query_clean,
                strategy_safe,
                top_k_safe,
                auto_token_threshold=5,
            )
        except Exception as exc:
            logger.error(
                "search_knowledge 检索依赖失败: strategy=%s kind=%s",
                strategy_safe,
                _failure_kind(exc),
            )
            response = _search_error_response(exc, strategy=strategy_safe)

        if (
            not is_strict_search_response(response)
            or not _has_safe_search_metadata(response)
            or response.strategy not in _PUBLIC_SEARCH_RESPONSE_STRATEGIES
            or (
                strategy_safe == "auto"
                and response.strategy not in _AUTO_SEARCH_RESPONSE_STRATEGIES
            )
            or (
                strategy_safe != "auto"
                and response.strategy != strategy_safe
            )
        ):
            logger.error(
                "search_knowledge 收到非法检索返回类型: strategy=%s type=%s",
                strategy_safe,
                type(response).__name__,
            )
            response = _search_error_response(
                TypeError("retriever must return SearchResponse"),
                strategy=strategy_safe,
            )
        return _serialize_search_response(
            response,
            source_type=source_type_clean,
            tag=tag_clean,
        )

    result = await anyio.to_thread.run_sync(_impl)
    logger.info(
        "search_knowledge: status=%s strategy=%s total=%s",
        result["status"],
        result["strategy"],
        result["total"],
    )
    return result


# ============================================================
# Tool 2: get_entry — 获取条目详情
# ============================================================

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def get_entry(knowledge_id: str) -> dict:
    """获取知识条目完整内容。

    Args:
        knowledge_id: 知识条目 ID（数字字符串）

    Returns:
        包含标题、摘要、标签、全文内容等完整信息的字典
    """
    def _impl():
        try:
            if isinstance(knowledge_id, bool):
                raise ValueError
            kid = int(knowledge_id)
            if kid <= 0:
                raise ValueError
        except (ValueError, TypeError):
            return _readonly_error_payload(
                status="invalid",
                code=ErrorCode.RETRIEVAL_INVALID_QUERY,
                message="无效的 knowledge_id，需要数字",
                stage="knowledge_id_validation",
                recoverable=True,
            )

        try:
            store = get_sqlite_store()
            entry = store.query_by_id(kid)
            if entry is not None and not _is_entry_row(entry, expected_id=kid):
                raise TypeError("entry backend contract invalid")
        except PKVRuntimeError as exc:
            logger.error(
                "get_entry 数据库读取失败: knowledge_id=%s kind=%s",
                kid,
                _failure_kind(exc),
            )
            return _readonly_error_payload(
                status="error",
                code=ErrorCode.RETRIEVAL_BACKEND_FAILED,
                message="条目读取失败",
                stage="entry_lookup",
                recoverable=exc.recoverable,
                exc=exc,
            )
        except Exception as exc:
            logger.error(
                "get_entry 数据库读取失败: knowledge_id=%s kind=%s",
                kid,
                _failure_kind(exc),
            )
            return _readonly_error_payload(
                status="error",
                code=ErrorCode.RETRIEVAL_BACKEND_FAILED,
                message="条目读取失败",
                stage="entry_lookup",
                recoverable=True,
            )
        if entry is None:
            return {
                "status": "no_hits",
                "error": "未找到条目",
                "issues": [],
            }

        # 读取 Markdown 全文
        content = ""
        status = "success"
        issues: list[dict[str, object]] = []
        file_path_str = entry.get("file_path", "")
        if file_path_str:
            try:
                md_store = get_markdown_store()
                safe_path = resolve_vault_file_path(
                    file_path_str,
                    md_store.vault_dir,
                )
                loaded_entry = md_store.load(safe_path)
                if loaded_entry is None:
                    status = "degraded"
                    issues.append(
                        _public_issue(
                            {},
                            fallback_code=ErrorCode.RESOURCE_NOT_READABLE,
                            fallback_message="条目正文不可读取",
                            fallback_stage="entry_content_read",
                            fallback_recoverable=True,
                            fallback_severity="",
                        )
                    )
                elif type(loaded_entry) is not Entry or type(loaded_entry.content) is not str:
                    return _readonly_service_failure(
                        TypeError("entry content backend contract invalid"),
                        operation="条目正文读取",
                        stage="entry_content_read",
                    )
                elif not loaded_entry.content:
                    status = "degraded"
                    issues.append(
                        _public_issue(
                            {},
                            fallback_code=ErrorCode.RESOURCE_NOT_READABLE,
                            fallback_message="条目正文不可读取",
                            fallback_stage="entry_content_read",
                            fallback_recoverable=True,
                            fallback_severity="",
                        )
                    )
                else:
                    content = loaded_entry.content
            except PKVRuntimeError as exc:
                logger.warning("get_entry 正文读取失败: knowledge_id=%s", kid)
                content = "(内容不可用)"
                status = "degraded"
                issues.append(
                    _public_issue(
                        exc,
                        fallback_code=ErrorCode.RESOURCE_NOT_READABLE,
                        fallback_message="条目正文不可读取",
                        fallback_stage="entry_content_read",
                        fallback_recoverable=exc.recoverable,
                        fallback_severity="",
                    )
                )
            except Exception:
                logger.warning("get_entry 正文读取失败: knowledge_id=%s", kid)
                content = "(内容不可用)"
                status = "degraded"
                issues.append(
                    _public_issue(
                        {},
                        fallback_code=ErrorCode.RESOURCE_NOT_READABLE,
                        fallback_message="条目正文不可读取",
                        fallback_stage="entry_content_read",
                        fallback_recoverable=True,
                        fallback_severity="",
                    )
                )
        else:
            status = "degraded"
            issues.append(
                _public_issue(
                    {},
                    fallback_code=ErrorCode.RESOURCE_MISSING,
                    fallback_message="条目正文位置缺失",
                    fallback_stage="entry_content_read",
                    fallback_recoverable=True,
                    fallback_severity="",
                )
            )

        try:
            return sanitize_public_evidence({
                "status": status,
                "issues": issues,
                "knowledge_id": entry["knowledge_id"],
                "title": entry.get("title", ""),
                "abstract": entry.get("summary_one_sentence", ""),  # DB 无 abstract 列
                "summary_one_sentence": entry.get("summary_one_sentence", ""),
                "summary_100_words": entry.get("summary_100_words", ""),
                "tags": parse_tags_string(entry.get("tags", "")),
                "keywords": parse_tags_string(entry.get("keywords", "")),
                "source_type": entry.get("source_type", ""),
                "source_url": sanitize_public_source_url(entry.get("source_url", "")),
                "archived_at": entry.get("archived_at", ""),
                "word_count": entry.get("word_count", 0),
                "content": content or "(内容不可用)",
            })
        except Exception as exc:
            return _readonly_service_failure(
                exc,
                operation="条目响应序列化",
                stage="entry_serialization",
            )

    return await anyio.to_thread.run_sync(_impl)


# ============================================================
# Tool 3: list_tags — 列出标签
# ============================================================

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def list_tags() -> dict:
    """列出知识库所有标签及统计。

    Returns:
        标签列表，每项包含标签名和关联条目数
    """
    def _impl():
        try:
            store = get_sqlite_store()
            tags = store.get_all_tags_with_count()
            if type(tags) is not list or not all(
                _is_tag_count_row(item) for item in tags
            ):
                raise TypeError("tag backend contract invalid")
            payload = {
                "total_tags": len(tags),
                "tags": [{"name": t["name"], "count": t["count"]} for t in tags],
            }
        except Exception as exc:
            return _readonly_service_failure(
                exc,
                operation="标签列表读取",
                stage="list_tags",
            )
        return _readonly_result_payload(payload, has_results=bool(tags))

    return await anyio.to_thread.run_sync(_impl)


# ============================================================
# Tool 4: list_entries — 浏览条目列表
# ============================================================

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def list_entries(
    page: int = 1,
    per_page: int = 20,
    sort_by: str = "archived_at",
    sort_order: str = "desc",
    source_type: Optional[str] = None,
    tag: Optional[str] = None,
) -> dict:
    """浏览知识条目列表。

    Args:
        page: 页码，从 1 开始
        per_page: 每页数量，默认 20，最大 100
        sort_by: 排序字段 - "archived_at", "title", "word_count", "knowledge_id", "source_type"
        sort_order: 排序方向 - "asc" 或 "desc"
        source_type: 按来源类型过滤
        tag: 按标签过滤

    Returns:
        分页的条目列表，包含总数和分页信息
    """
    def _impl():
        if (
            isinstance(page, bool)
            or not isinstance(page, int)
            or page <= 0
            or isinstance(per_page, bool)
            or not isinstance(per_page, int)
            or per_page <= 0
        ):
            return _readonly_error_payload(
                status="invalid",
                code=ErrorCode.RETRIEVAL_INVALID_QUERY,
                message="page 和 per_page 必须是正整数",
                stage="list_entries_validation",
                recoverable=True,
            )
        if (
            not isinstance(sort_by, str)
            or sort_by not in _LIST_ENTRY_SORT_FIELDS
            or not isinstance(sort_order, str)
            or sort_order not in _SORT_ORDERS
        ):
            return _readonly_error_payload(
                status="invalid",
                code=ErrorCode.RETRIEVAL_INVALID_QUERY,
                message="sort_by 或 sort_order 无效",
                stage="list_entries_validation",
                recoverable=True,
            )
        _per_page = min(per_page, 100)
        _page = page
        offset = (_page - 1) * _per_page

        try:
            store = get_sqlite_store()
            entries = store.list_entries(
                limit=_per_page,
                offset=offset,
                sort_by=sort_by,
                sort_order=sort_order,
                source_type=source_type if source_type else None,
                tag=tag if tag else None,
            )
            total = store.count_entries(
                source_type=source_type if source_type else None,
                tag=tag if tag else None,
            )
            if (
                type(entries) is not list
                or not _is_nonnegative_int(total)
                or len(entries) > _per_page
                or total < len(entries)
                or not all(_is_entry_row(item) for item in entries)
            ):
                raise TypeError("entry list backend contract invalid")
        except Exception as exc:
            return _readonly_service_failure(
                exc,
                operation="条目列表读取",
                stage="list_entries",
            )

        try:
            return _readonly_result_payload({
                "total": total,
                "page": _page,
                "per_page": _per_page,
                "total_pages": (total + _per_page - 1) // _per_page if total > 0 else 0,
                "entries": [
                    {
                        "knowledge_id": e["knowledge_id"],
                        "title": e.get("title", ""),
                        "abstract": e.get("summary_one_sentence", ""),
                        "tags": parse_tags_string(e.get("tags", "")),
                        "source_type": e.get("source_type", ""),
                        "word_count": e.get("word_count", 0),
                        "archived_at": e.get("archived_at", ""),
                    }
                    for e in entries
                ],
            }, has_results=bool(entries))
        except Exception as exc:
            return _readonly_service_failure(
                exc,
                operation="条目列表响应序列化",
                stage="list_entries_serialization",
            )

    return await anyio.to_thread.run_sync(_impl)


# ============================================================
# Tool 5: get_stats — 知识库统计
# ============================================================

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def get_stats() -> dict:
    """获取知识库统计信息。

    Returns:
        包含条目总数、来源类型分布、标签统计等综合数据
    """
    def _impl():
        try:
            store = get_sqlite_store()
            stats = store.get_statistics()
            if not _is_statistics_payload(stats):
                raise TypeError("statistics backend contract invalid")
            payload = {
                "total_entries": stats["total_entries"],
                "by_source_type": stats["by_source_type"],
                "top_tags": [
                    {"name": item["name"], "count": item["count"]}
                    for item in stats["top_tags"]
                ],
            }
        except Exception as exc:
            return _readonly_service_failure(
                exc,
                operation="知识库统计读取",
                stage="get_stats",
            )
        return _readonly_result_payload(payload)

    return await anyio.to_thread.run_sync(_impl)


# ============================================================
# Tool 6: archive_url — 归档网页 (M9 新增)
# ============================================================

@mcp.tool()
async def archive_url(url: str) -> dict:
    """归档网页 URL 到知识库。

    自动抓取网页内容，AI 生成摘要和标签，存储到 Markdown + SQLite + 向量索引。
    归档过程可能需要 10-30 秒（包含网络请求和 AI 分析）。

    Args:
        url: 要归档的网页链接（必须是 http/https，禁止内网地址）

    Returns:
        归档结果，包含 knowledge_id、标题和可读取的 entry locator
    """
    # 前置安全验证
    url_failure = validate_url_security_result(url)
    if url_failure is not None:
        issue = _public_issue(
            url_failure,
            fallback_code=ErrorCode.URL_INVALID,
            fallback_message="URL 不可归档",
            fallback_stage="url_validation",
            fallback_recoverable=url_failure.recoverable,
        )
        return sanitize_public_evidence({
            "success": False,
            "terminal": "error",
            "error": issue["message"],
            "warnings": [],
            "issues": [issue],
        })

    try:
        logger.info("archive_url: 开始归档")
        result = await get_application().archive_url(
            {
                "url": url,
                "skip_review": True,
                "skip_sharpen": True,
            },
        )

        payload = _workflow_result_payload(result, include_abstract=True)
        if payload["terminal"] != "error":
            logger.info(
                "archive_url: 归档终态=%s kid=%s",
                payload["terminal"],
                payload.get("knowledge_id", ""),
            )
        else:
            logger.warning(
                "archive_url: 归档失败 issue_codes=%s",
                [item.get("code") for item in payload["issues"]],
            )
        return payload
    except Exception as exc:
        logger.error("archive_url 执行异常: kind=%s", _failure_kind(exc))
        return _runtime_failure_payload(
            exc,
            fallback_code=ErrorCode.WORKFLOW_STEP_FAILED,
            message="归档工作流执行异常",
            stage="archive_url",
            error_label="归档异常",
        )


# ============================================================
# Tool 7: archive_text — 归档文本 (M9 新增)
# ============================================================

@mcp.tool()
async def archive_text(text: str, title: str = "") -> dict:
    """归档纯文本到知识库。

    将文本内容（如 AI 对话摘要、笔记、纯文本等）归档到知识库。
    先由 TextFallbackProcessor 解析文本结构，再经 AI 分析生成摘要和标签，
    最后存储到 Markdown + SQLite + 向量索引。

    Args:
        text: 要归档的文本内容（最大 100,000 字符）
        title: 可选标题（不提供则自动从文本提取）

    Returns:
        归档结果，包含 knowledge_id、标题和可读取的 entry locator
    """
    # 前置安全验证：文本长度
    valid, error = validate_text_length(text)
    if not valid:
        issue = _public_issue(
            {},
            fallback_code=ErrorCode.WORKFLOW_CONFIG_INVALID,
            fallback_message=error or "文本内容不可归档",
            fallback_stage="text_validation",
            fallback_recoverable=True,
        )
        return sanitize_public_evidence({
            "success": False,
            "terminal": "error",
            "error": issue["message"],
            "warnings": [],
            "issues": [issue],
        })

    try:
        logger.info("archive_text: 开始归档 text_len=%s", len(text))
        # Literal text remains a raw-text operation.  Parsing and workflow
        # composition live behind the shared application service, not in this
        # protocol adapter.
        title_fallback = title.strip() if isinstance(title, str) else ""
        result = await get_application().archive_text(
            text,
            title=title_fallback,
            skip_review=True,
            skip_sharpen=True,
        )

        payload = _workflow_result_payload(result, title_fallback=title_fallback)
        if payload["terminal"] != "error":
            logger.info(
                "archive_text: 归档终态=%s kid=%s",
                payload["terminal"],
                payload.get("knowledge_id", ""),
            )
        else:
            logger.warning(
                "archive_text: 归档失败 issue_codes=%s",
                [item.get("code") for item in payload["issues"]],
            )
        return payload
    except Exception as exc:
        logger.error("archive_text 执行异常: kind=%s", _failure_kind(exc))
        return _runtime_failure_payload(
            exc,
            fallback_code=ErrorCode.WORKFLOW_STEP_FAILED,
            message="归档工作流执行异常",
            stage="archive_text",
            error_label="归档异常",
        )


# ============================================================
# Tool 8: get_related — 获取关联知识 (M9 新增)
# ============================================================

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def get_related(knowledge_id: str, limit: int = 5) -> dict:
    """获取与指定条目相关的知识条目。

    基于向量相似度查找关联知识。利用条目归档时生成的 embedding 向量，
    在向量索引中搜索最近邻，返回内容相似的条目列表。

    Args:
        knowledge_id: 知识条目 ID（数字字符串）
        limit: 返回结果数量，默认 5，最大 20

    Returns:
        关联条目列表，每项包含 knowledge_id, title, abstract, score
    """
    def _impl():
        try:
            if isinstance(knowledge_id, bool):
                raise ValueError
            kid = int(knowledge_id)
            if kid <= 0:
                raise ValueError
        except (ValueError, TypeError):
            message = "无效的 knowledge_id，需要数字"
            return {
                "status": "invalid",
                "strategy": "vector_related",
                "total": 0,
                "results": [],
                "issues": [
                    _public_issue(
                        {},
                        fallback_code=ErrorCode.RETRIEVAL_INVALID_QUERY,
                        fallback_message=message,
                        fallback_stage="knowledge_id_validation",
                        fallback_recoverable=True,
                        fallback_severity="",
                    )
                ],
                "error": message,
            }

        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            message = "limit 必须是正整数"
            return {
                "status": "invalid",
                "strategy": "vector_related",
                "total": 0,
                "results": [],
                "issues": [
                    _public_issue(
                        {},
                        fallback_code=ErrorCode.RETRIEVAL_INVALID_QUERY,
                        fallback_message=message,
                        fallback_stage="limit_validation",
                        fallback_recoverable=True,
                        fallback_severity="",
                    )
                ],
                "error": message,
            }
        _limit = min(limit, 20)

        # 获取条目信息（确认存在）
        try:
            store = get_sqlite_store()
            entry = store.query_by_id(kid)
            if entry is not None and not _is_entry_row(entry, expected_id=kid):
                raise TypeError("related seed backend contract invalid")
        except PKVRuntimeError as exc:
            return {
                "status": "error",
                "strategy": "vector_related",
                "total": 0,
                "results": [],
                "issues": [
                    _public_issue(
                        exc,
                        fallback_code=ErrorCode.RETRIEVAL_BACKEND_FAILED,
                        fallback_message="条目读取失败",
                        fallback_stage="related_entry_lookup",
                        fallback_recoverable=exc.recoverable,
                        fallback_severity="",
                    )
                ],
                "error": "条目读取失败",
            }
        except Exception as exc:
            logger.error("get_related 条目读取失败: kind=%s", _failure_kind(exc))
            return {
                "status": "error",
                "strategy": "vector_related",
                "total": 0,
                "results": [],
                "issues": [
                    _public_issue(
                        {},
                        fallback_code=ErrorCode.RETRIEVAL_BACKEND_FAILED,
                        fallback_message="条目读取失败",
                        fallback_stage="related_entry_lookup",
                        fallback_recoverable=True,
                        fallback_severity="",
                    )
                ],
                "error": "条目读取失败",
            }
        if entry is None:
            message = "未找到条目"
            return {
                "status": "no_hits",
                "strategy": "vector_related",
                "total": 0,
                "results": [],
                "issues": [],
                "error": message,
            }

        # The application opens only existing vector artifacts.  MCP must not
        # create an index or compose a VectorStore itself during a read.
        try:
            vector_store = get_application().readonly_vector_store
            if vector_store is None:
                message = "该条目暂无向量索引，无法获取关联知识"
                return {
                    "status": "degraded",
                    "strategy": "vector_related",
                    "total": 0,
                    "results": [],
                    "message": message,
                    "issues": [
                        _public_issue(
                            {},
                            fallback_code=ErrorCode.RETRIEVAL_INDEX_UNAVAILABLE,
                            fallback_message=message,
                            fallback_stage="vector_index",
                            fallback_recoverable=True,
                            fallback_severity="",
                        )
                    ],
                }
            doc_vector = vector_store.get_doc_vector(kid)
            if doc_vector is None:
                message = "该条目暂无向量，无法获取关联知识"
                return {
                    "status": "degraded",
                    "strategy": "vector_related",
                    "total": 0,
                    "results": [],
                    "message": message,
                    "issues": [
                        _public_issue(
                            {},
                            fallback_code=ErrorCode.RETRIEVAL_INDEX_UNAVAILABLE,
                            fallback_message=message,
                            fallback_stage="document_vector",
                            fallback_recoverable=True,
                            fallback_severity="",
                        )
                    ],
                }

            # 搜索相似文档（+1 因为需要排除自身）
            raw_results = vector_store.search_doc(doc_vector, k=_limit + 1)
            if (
                type(raw_results) is not list
                or len(raw_results) > _limit + 1
                or not all(
                    type(item) is tuple
                    and len(item) == 2
                    and _is_positive_int(item[0])
                    and type(item[1]) in {int, float}
                    and math.isfinite(item[1])
                    for item in raw_results
                )
                or len({item[0] for item in raw_results}) != len(raw_results)
            ):
                raise TypeError("vector related result contract invalid")

            # 排除自身并获取条目信息
            results = []
            for related_kid, distance in raw_results:
                if related_kid == kid:
                    continue
                if len(results) >= _limit:
                    break
                related_entry = store.query_by_id(related_kid)
                if related_entry is None or not _is_entry_row(
                    related_entry,
                    expected_id=related_kid,
                ):
                    raise PKVRuntimeError(
                        ErrorCode.RETRIEVAL_METADATA_INCONSISTENT,
                        "related vector metadata is inconsistent",
                        stage="vector_metadata_read",
                        recoverable=False,
                    )
                # cosine distance → bounded similarity score (1 - distance)
                score = round(min(1.0, max(0.0, 1.0 - distance)), 4)
                results.append({
                    "knowledge_id": related_kid,
                    "title": related_entry["title"],
                    "abstract": related_entry["summary_one_sentence"] or "",
                    "tags": parse_tags_string(related_entry["tags"] or ""),
                    "source_type": related_entry["source_type"],
                    "score": score,
                })

            return sanitize_public_evidence({
                "status": "success" if results else "no_hits",
                "strategy": "vector_related",
                "total": len(results),
                "results": results,
                "issues": [],
            })

        except Exception as exc:
            logger.error("get_related 向量搜索失败: kind=%s", _failure_kind(exc))
            if isinstance(exc, PKVRuntimeError):
                issue = _public_issue(
                    exc,
                    fallback_code=ErrorCode.RETRIEVAL_BACKEND_FAILED,
                    fallback_message="向量搜索不可用",
                    fallback_stage="vector_related",
                    fallback_recoverable=exc.recoverable,
                    fallback_severity="",
                )
            else:
                issue = _public_issue(
                    {},
                    fallback_code=ErrorCode.RETRIEVAL_BACKEND_FAILED,
                    fallback_message="向量搜索不可用",
                    fallback_stage="vector_related",
                    fallback_recoverable=True,
                    fallback_severity="",
                )
            return {
                "status": "error",
                "strategy": "vector_related",
                "total": 0,
                "results": [],
                "message": "向量搜索不可用",
                "error": "向量搜索不可用",
                "issues": [issue],
            }

    return await anyio.to_thread.run_sync(_impl)


# ============================================================
# Tool 9: query_subgraph — 获取关系子图 (Phase B)
# ============================================================

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def query_subgraph(
    knowledge_id: str,
    depth: int = 2,
    relation_types: Optional[list[str]] = None,
    max_nodes: int = 50,
) -> dict:
    """获取指定条目周围的关系子图。

    Args:
        knowledge_id: 种子知识条目 ID（数字字符串）
        depth: 查询跳数，默认 2，最大 4
        relation_types: 可选关系类型过滤列表
        max_nodes: 最多返回节点数，默认 50，最大 200

    Returns:
        子图结果，包含 nodes、edges、grouped_edges、truncated 等字段
    """

    def _impl():
        try:
            if isinstance(knowledge_id, bool):
                raise ValueError
            kid = int(knowledge_id)
            if kid <= 0:
                raise ValueError
        except (ValueError, TypeError):
            return _readonly_error_payload(
                status="invalid",
                code=ErrorCode.RETRIEVAL_INVALID_QUERY,
                message="无效的 knowledge_id，需要数字",
                stage="subgraph_id_validation",
                recoverable=True,
            )

        if not _is_positive_int(depth) or not _is_positive_int(max_nodes):
            return _readonly_error_payload(
                status="invalid",
                code=ErrorCode.RETRIEVAL_INVALID_QUERY,
                message="depth 和 max_nodes 必须是正整数",
                stage="subgraph_parameter_validation",
                recoverable=True,
            )
        depth_safe = min(depth, 4)
        max_nodes_safe = min(max_nodes, 200)
        max_edges_safe = max(max_nodes_safe * 4, 20)
        try:
            relation_types_safe = _normalize_relation_types(relation_types)
        except ValueError:
            return _readonly_error_payload(
                status="invalid",
                code=ErrorCode.RETRIEVAL_INVALID_QUERY,
                message="relation_types 必须是受支持的关系类型列表",
                stage="subgraph_relation_types_validation",
                recoverable=True,
            )

        try:
            relation_query_service = get_relation_query_service()
            result = relation_query_service.query_subgraph(
                seed_knowledge_id=kid,
                depth=depth_safe,
                relation_types=relation_types_safe,
                per_node_limit=max_nodes_safe,
                max_nodes=max_nodes_safe,
                max_edges=max_edges_safe,
            )
            payload = _strict_subgraph_payload(
                result,
                seed_knowledge_id=kid,
                max_depth=depth_safe,
                max_nodes=max_nodes_safe,
                max_edges=max_edges_safe,
                relation_types=relation_types_safe,
            )
            return _readonly_result_payload(
                payload,
                has_results=payload["total_edges"] > 0,
            )
        except Exception as exc:
            return _readonly_service_failure(
                exc,
                operation="关系子图查询",
                stage="query_subgraph",
            )

    return await anyio.to_thread.run_sync(_impl)


# ============================================================
# Tool 10: explain_relation — 解释条目关系 (Phase B)
# ============================================================

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def explain_relation(
    source_knowledge_id: str,
    target_knowledge_id: str,
    relation_types: Optional[list[str]] = None,
    max_depth: int = 2,
) -> dict:
    """解释两个知识条目之间为何相关。

    Args:
        source_knowledge_id: 起始知识条目 ID（数字字符串）
        target_knowledge_id: 目标知识条目 ID（数字字符串）
        relation_types: 可选关系类型过滤列表
        max_depth: 最多允许的解释跳数，默认 2，最大 4

    Returns:
        关系解释结果，包含 summary、path、evidence_items 等字段
    """

    def _impl():
        try:
            if isinstance(source_knowledge_id, bool) or isinstance(target_knowledge_id, bool):
                raise ValueError
            source_kid = int(source_knowledge_id)
            target_kid = int(target_knowledge_id)
            if (
                source_kid <= 0
                or target_kid <= 0
                or source_kid == target_kid
            ):
                raise ValueError
        except (ValueError, TypeError):
            return _readonly_error_payload(
                status="invalid",
                code=ErrorCode.RETRIEVAL_INVALID_QUERY,
                message="无效的 knowledge_id，需要数字",
                stage="relation_id_validation",
                recoverable=True,
            )

        if not _is_positive_int(max_depth):
            return _readonly_error_payload(
                status="invalid",
                code=ErrorCode.RETRIEVAL_INVALID_QUERY,
                message="max_depth 必须是正整数",
                stage="relation_parameter_validation",
                recoverable=True,
            )
        max_depth_safe = min(max_depth, 4)
        try:
            relation_types_safe = _normalize_relation_types(relation_types)
        except ValueError:
            return _readonly_error_payload(
                status="invalid",
                code=ErrorCode.RETRIEVAL_INVALID_QUERY,
                message="relation_types 必须是受支持的关系类型列表",
                stage="relation_types_validation",
                recoverable=True,
            )

        try:
            relation_query_service = get_relation_query_service()
            result = relation_query_service.explain_relation(
                source_knowledge_id=source_kid,
                target_knowledge_id=target_kid,
                relation_types=relation_types_safe,
                max_depth=max_depth_safe,
                per_node_limit=100,
            )
            payload = _strict_relation_explanation_payload(
                result,
                source_knowledge_id=source_kid,
                target_knowledge_id=target_kid,
                max_depth=max_depth_safe,
                relation_types=relation_types_safe,
            )
            return _readonly_result_payload(
                payload,
                has_results=payload["found"],
            )
        except Exception as exc:
            return _readonly_service_failure(
                exc,
                operation="关系解释",
                stage="explain_relation",
            )

    return await anyio.to_thread.run_sync(_impl)


# ============================================================
# Tool 11: collect_evidence — 聚合问题证据包 (Phase B)
# ============================================================

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def collect_evidence(
    question: str,
    top_k: int = 5,
    relation_max_depth: int = 2,
    include_chunks: bool = False,
) -> dict:
    """围绕问题聚合最小证据包。

    Args:
        question: 待回答的问题或主题
        top_k: 最多聚合的证据条目数，默认 5，最大 10
        relation_max_depth: 与种子条目解释关系时允许的最大跳数，默认 2，最大 4
        include_chunks: 是否显式返回 chunk 级证据字段，默认 False

    Returns:
        证据聚合结果；chunk 证据包含 citation_source 与稳定 citation_locator
    """

    def _impl():
        if not isinstance(question, str) or not question.strip():
            return _readonly_error_payload(
                status="invalid",
                code=ErrorCode.RETRIEVAL_INVALID_QUERY,
                message="question 不能为空",
                stage="evidence_question_validation",
                recoverable=True,
            )

        if not _is_positive_int(top_k) or not _is_positive_int(relation_max_depth):
            return _readonly_error_payload(
                status="invalid",
                code=ErrorCode.RETRIEVAL_INVALID_QUERY,
                message="top_k 和 relation_max_depth 必须是正整数",
                stage="evidence_parameter_validation",
                recoverable=True,
            )
        if not isinstance(include_chunks, bool):
            return _readonly_error_payload(
                status="invalid",
                code=ErrorCode.RETRIEVAL_INVALID_QUERY,
                message="include_chunks 必须是布尔值",
                stage="evidence_parameter_validation",
                recoverable=True,
            )
        top_k_safe = min(top_k, 10)
        relation_max_depth_safe = min(relation_max_depth, 4)

        try:
            evidence_collection_service = get_evidence_collection_service()
            result = evidence_collection_service.collect_evidence(
                question=question,
                top_k=top_k_safe,
                relation_max_depth=relation_max_depth_safe,
                include_chunks=include_chunks,
            )
            payload = _strict_collected_evidence_payload(
                result,
                question=question.strip(),
                top_k=top_k_safe,
                relation_max_depth=relation_max_depth_safe,
                include_chunks=include_chunks,
            )

            degraded_issue = None
            chunk_status = payload.get("chunk_retrieval_status")
            limitation_notes = payload.get("limitation_notes", [])
            document_degraded = (
                isinstance(limitation_notes, list)
                and any(
                    isinstance(note, str)
                    and note.startswith("document_retrieval_degraded[")
                    for note in limitation_notes
                )
            )
            if chunk_status == "path_unavailable":
                degraded_issue = _public_issue(
                    {},
                    fallback_code=ErrorCode.RETRIEVAL_INDEX_UNAVAILABLE,
                    fallback_message="chunk 检索路径不可用，已使用文档级证据",
                    fallback_stage="evidence_chunk_retrieval",
                    fallback_recoverable=True,
                    fallback_severity="",
                )
            elif chunk_status == "search_error" or document_degraded:
                degraded_issue = _public_issue(
                    {},
                    fallback_code=ErrorCode.RETRIEVAL_BACKEND_FAILED,
                    fallback_message="部分证据检索能力不可用",
                    fallback_stage="evidence_retrieval",
                    fallback_recoverable=True,
                    fallback_severity="",
                )
            return _readonly_result_payload(
                payload,
                has_results=payload["found"],
                degraded_issue=degraded_issue,
            )
        except Exception as exc:
            return _readonly_service_failure(
                exc,
                operation="证据聚合",
                stage="collect_evidence",
            )

    return await anyio.to_thread.run_sync(_impl)


# ============================================================
# Tool 12: find_bridges — 发现桥接节点 (Phase B partial)
# ============================================================

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def find_bridges(
    seed_knowledge_id: str,
    top_k: int = 5,
    max_depth: int = 2,
) -> dict:
    """发现 seed 周围关系子图中的桥接候选。

    注意：
        当前是 partial implementation，已引入局部图桥接信号与轻量文本重合。
        每个候选公开 seed 到 candidate 的逐跳 evidence_path。
        它适合作为桥接探索入口，不代表完整主题桥接发现。
    """

    def _impl():
        try:
            if isinstance(seed_knowledge_id, bool):
                raise ValueError
            seed_kid = int(seed_knowledge_id)
            if seed_kid <= 0:
                raise ValueError
        except (ValueError, TypeError):
            return _readonly_error_payload(
                status="invalid",
                code=ErrorCode.RETRIEVAL_INVALID_QUERY,
                message="无效的 seed_knowledge_id，需要数字",
                stage="bridge_seed_validation",
                recoverable=True,
            )

        if not _is_positive_int(top_k) or not _is_positive_int(max_depth):
            return _readonly_error_payload(
                status="invalid",
                code=ErrorCode.RETRIEVAL_INVALID_QUERY,
                message="top_k 和 max_depth 必须是正整数",
                stage="bridge_parameter_validation",
                recoverable=True,
            )
        top_k_safe = min(top_k, 10)
        max_depth_safe = min(max_depth, 4)
        try:
            exploration_service = get_exploration_service()
            result = exploration_service.find_bridges(
                seed_knowledge_id=seed_kid,
                top_k=top_k_safe,
                max_depth=max_depth_safe,
            )
            payload = _strict_bridge_payload(
                result,
                seed_knowledge_id=seed_kid,
                max_depth=max_depth_safe,
                top_k=top_k_safe,
            )
            return _readonly_result_payload(
                payload,
                has_results=payload["found"],
            )
        except Exception as exc:
            return _readonly_service_failure(
                exc,
                operation="桥接发现",
                stage="find_bridges",
            )

    return await anyio.to_thread.run_sync(_impl)


# ============================================================
# Tool 13: timeline_of — 重建弱时间线 (Phase B partial)
# ============================================================

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def timeline_of(
    topic: str,
    top_k: int = 8,
    sort_order: str = "asc",
) -> dict:
    """按结构化时间字段重建主题的弱时间线。

    注意：
        当前是 partial implementation，会优先使用 event_time > published_at > archived_at
        的结构化真实时间字段排序，缺失时才回退 archived_at。
        每个时间点公开 source 与定位所用时间字段的 citation_locator。
        它不代表正文中的完整真实事件时间，也还未接入 video_timestamps 或事件时间抽取。
    """

    def _impl():
        if not isinstance(topic, str) or not topic.strip():
            return _readonly_error_payload(
                status="invalid",
                code=ErrorCode.RETRIEVAL_INVALID_QUERY,
                message="topic 不能为空",
                stage="timeline_topic_validation",
                recoverable=True,
            )
        if not _is_positive_int(top_k):
            return _readonly_error_payload(
                status="invalid",
                code=ErrorCode.RETRIEVAL_INVALID_QUERY,
                message="top_k 必须是正整数",
                stage="timeline_parameter_validation",
                recoverable=True,
            )
        if type(sort_order) is not str:
            return _readonly_error_payload(
                status="invalid",
                code=ErrorCode.RETRIEVAL_INVALID_QUERY,
                message="sort_order 仅支持 asc 或 desc",
                stage="timeline_sort_validation",
                recoverable=True,
            )
        sort_order_safe = sort_order.lower()
        if sort_order_safe not in {"asc", "desc"}:
            return _readonly_error_payload(
                status="invalid",
                code=ErrorCode.RETRIEVAL_INVALID_QUERY,
                message="sort_order 仅支持 asc 或 desc",
                stage="timeline_sort_validation",
                recoverable=True,
            )

        top_k_safe = min(top_k, 20)
        try:
            exploration_service = get_exploration_service()
            result = exploration_service.timeline_of(
                topic=topic,
                top_k=top_k_safe,
                sort_order=sort_order_safe,
            )
            payload = _strict_timeline_payload(
                result,
                topic=topic.strip(),
                top_k=top_k_safe,
                sort_order=sort_order_safe,
            )
            return _readonly_result_payload(
                payload,
                has_results=payload["found"],
                degraded_issue=_retrieval_degraded_issue_from_notes(
                    payload,
                    operations=("timeline",),
                ),
            )
        except Exception as exc:
            return _readonly_service_failure(
                exc,
                operation="时间线查询",
                stage="timeline_of",
            )

    return await anyio.to_thread.run_sync(_impl)


# ============================================================
# Tool 14: contrast — 主题对比 (Phase B partial)
# ============================================================

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def contrast(
    topic_a: str,
    topic_b: str,
    top_k: int = 5,
) -> dict:
    """对比两个主题的检索候选与显式关系图信号。

    注意：
        当前是 partial implementation，已引入跨主题显式关系路径信号。
        comparison_dimensions.provenance 公开候选、来源与关系路径映射。
        它不代表完整语义对比，也未引入 contrast 关系类型。
    """

    def _impl():
        if not isinstance(topic_a, str) or not topic_a.strip():
            return _readonly_error_payload(
                status="invalid",
                code=ErrorCode.RETRIEVAL_INVALID_QUERY,
                message="topic_a 不能为空",
                stage="contrast_topic_validation",
                recoverable=True,
            )
        if not isinstance(topic_b, str) or not topic_b.strip():
            return _readonly_error_payload(
                status="invalid",
                code=ErrorCode.RETRIEVAL_INVALID_QUERY,
                message="topic_b 不能为空",
                stage="contrast_topic_validation",
                recoverable=True,
            )
        if not _is_positive_int(top_k):
            return _readonly_error_payload(
                status="invalid",
                code=ErrorCode.RETRIEVAL_INVALID_QUERY,
                message="top_k 必须是正整数",
                stage="contrast_parameter_validation",
                recoverable=True,
            )

        try:
            exploration_service = get_exploration_service()
            top_k_safe = min(top_k, 10)
            result = exploration_service.contrast(
                topic_a=topic_a,
                topic_b=topic_b,
                top_k=top_k_safe,
            )
            payload = _strict_contrast_payload(
                result,
                topic_a=topic_a.strip(),
                topic_b=topic_b.strip(),
                top_k=top_k_safe,
            )
            return _readonly_result_payload(
                payload,
                has_results=payload["found"],
                degraded_issue=_retrieval_degraded_issue_from_notes(
                    payload,
                    operations=("contrast_topic_a", "contrast_topic_b"),
                ),
            )
        except Exception as exc:
            return _readonly_service_failure(
                exc,
                operation="主题对比",
                stage="contrast",
            )

    return await anyio.to_thread.run_sync(_impl)
