"""
MCP Resource handler 实现

提供可引用的知识条目、chunk、字段、关系及知识库汇总 Resource。

Resource vs Tool 选择原则：
    - Resource：静态/准静态数据，客户端可缓存（如标签列表、统计信息）
    - Tool：需要参数、有副作用或结果动态变化的操作（如搜索、归档）
    - Resource 返回 str（文本），Tool 返回 dict（结构化数据），两者互补
"""

import json
import logging
import math
import re

import anyio

from src.mcp.server import (
    get_markdown_store,
    get_relation_query_service,
    get_sqlite_store,
    mcp,
)
from src.mcp.utils import parse_tags_string
from src.relations.citations import (
    build_chunk_locator,
    build_metadata_locator,
    build_relation_locator,
    read_persisted_metadata_field,
    resolve_vault_file_path,
    sanitize_public_evidence,
    serialize_relation_evidence,
)
from src.relations.models import (
    RelationDirection,
    RelationRecord,
    RelationSourceType,
    RelationType,
)
from src.runtime.errors import ErrorCode
from src.storage.markdown_store import Entry

logger = logging.getLogger("pkv.mcp")

TIMELINE_METADATA_FIELDS = {
    "event_time",
    "published_at",
    "published_time",
    "publish_time",
    "archived_at",
}
_RELATION_TYPE_VALUES = frozenset(item.value for item in RelationType)
_RELATION_SOURCE_TYPE_VALUES = frozenset(item.value for item in RelationSourceType)
_RESOURCE_BACKEND_ERROR = (
    f"{ErrorCode.RESOURCE_NOT_READABLE.value}: 请求的资源暂时不可用"
)
_UNSUPPORTED_TIMELINE_METADATA_FIELD = "不支持的 timeline 元数据字段"
_ENTRY_METADATA_FIELDS = (
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
    "word_count",
    "event_time",
    "published_at",
    "archived_at",
    "updated_at",
)
_CHUNK_FIELDS = (
    "chunk_id",
    "knowledge_id",
    "chunk_index",
    "chunk_text",
    "context_before",
    "context_after",
    "section_title",
    "created_at",
)
_POSITIVE_URI_INTEGER = re.compile(r"[1-9][0-9]*", re.ASCII)
_NONNEGATIVE_URI_INTEGER = re.compile(r"(?:0|[1-9][0-9]*)", re.ASCII)
_MAX_SQLITE_INTEGER = (1 << 63) - 1
_PERSISTED_METADATA_STORES = frozenset(
    {"knowledge_items", "markdown_frontmatter"}
)


def _positive_int(raw_value: str, field_name: str) -> int:
    if (
        type(raw_value) is not str
        or len(raw_value) > 19
        or _POSITIVE_URI_INTEGER.fullmatch(raw_value) is None
    ):
        raise ValueError(f"无效的 {field_name}")
    value = int(raw_value)
    if value > _MAX_SQLITE_INTEGER:
        raise ValueError(f"无效的 {field_name}")
    return value


def _nonnegative_int(raw_value: str, field_name: str) -> int:
    if (
        type(raw_value) is not str
        or len(raw_value) > 19
        or _NONNEGATIVE_URI_INTEGER.fullmatch(raw_value) is None
    ):
        raise ValueError(f"无效的 {field_name}")
    value = int(raw_value)
    if value > _MAX_SQLITE_INTEGER:
        raise ValueError(f"无效的 {field_name}")
    return value


def _exact_enum_value(
    raw_value: object,
    *,
    allowed_values: frozenset[str],
    field_name: str,
) -> str:
    """Accept only an exact public enum string without coercion or trimming."""

    if type(raw_value) is not str or raw_value not in allowed_values:
        raise ValueError(f"无效的 {field_name}")
    return raw_value


def _raise_resource_backend_error(exc: BaseException) -> None:
    """Raise one public Resource failure without exposing backend diagnostics."""

    logger.error(
        "Resource backend failure: type=%s code=%s",
        type(exc).__name__,
        ErrorCode.RESOURCE_NOT_READABLE.value,
    )
    raise ValueError(_RESOURCE_BACKEND_ERROR) from None


def _resource_backend_call(callback):
    """Execute a factory/backend call behind the stable Resource error boundary."""

    try:
        return callback()
    except Exception as exc:
        _raise_resource_backend_error(exc)


def _is_positive_backend_id(value: object) -> bool:
    return type(value) is int and value > 0


def _is_nonnegative_backend_count(value: object) -> bool:
    return type(value) is int and value >= 0


def _is_positive_finite_backend_number(value: object) -> bool:
    try:
        return (
            type(value) in {int, float}
            and value > 0
            and math.isfinite(value)
        )
    except (OverflowError, TypeError, ValueError):
        return False


def _is_json_tree(
    value: object,
    *,
    depth: int = 0,
    ancestors: set[int] | None = None,
) -> bool:
    if value is None or type(value) in {str, bool, int}:
        return True
    if type(value) is float:
        return math.isfinite(value)
    if type(value) not in {list, dict} or depth >= 16 or len(value) > 4096:
        return False
    active = set() if ancestors is None else ancestors
    value_id = id(value)
    if value_id in active:
        return False
    active.add(value_id)
    try:
        if type(value) is list:
            return all(
                _is_json_tree(item, depth=depth + 1, ancestors=active)
                for item in value
            )
        return all(
            type(key) is str
            and _is_json_tree(item, depth=depth + 1, ancestors=active)
            for key, item in value.items()
        )
    except Exception:
        return False
    finally:
        active.remove(value_id)


def _project_persisted_metadata_field(
    result: object,
) -> tuple[bool, str | None, str]:
    """Validate the helper projection before tuple unpacking or publication."""

    if type(result) is not tuple or len(result) != 3:
        raise TypeError("metadata field backend contract invalid")
    found, value, storage_field = result
    if type(found) is not bool:
        raise TypeError("metadata field backend contract invalid")
    if found is False:
        if value is not None or storage_field != "":
            raise TypeError("metadata field backend contract invalid")
        return False, None, ""
    if (
        type(storage_field) is not str
        or storage_field not in _PERSISTED_METADATA_STORES
        or type(value) is not str
        or not value.strip()
    ):
        raise TypeError("metadata field backend contract invalid")
    return True, value, storage_field


def _is_relation_record(
    value: object,
    *,
    expected_relation_id: int | None = None,
    expected_source_id: int | None = None,
    expected_target_id: int | None = None,
    expected_relation_type: str | None = None,
    expected_relation_source_type: str | None = None,
) -> bool:
    """Validate the exact persisted relation contract before public projection."""

    if type(value) is not RelationRecord:
        return False
    try:
        relation_id = value.relation_id
        source_id = value.source_knowledge_id
        target_id = value.target_knowledge_id
        relation_type = value.relation_type
        relation_source_type = value.relation_source_type
        direction = value.direction
        weight = value.weight
        evidence_payload = value.evidence_payload
        created_at = value.created_at
        updated_at = value.updated_at
    except Exception:
        return False
    return (
        _is_positive_backend_id(relation_id)
        and _is_positive_backend_id(source_id)
        and _is_positive_backend_id(target_id)
        and source_id != target_id
        and type(relation_type) is RelationType
        and type(relation_source_type) is RelationSourceType
        and type(direction) is RelationDirection
        and _is_positive_finite_backend_number(weight)
        and type(evidence_payload) is dict
        and _is_json_tree(evidence_payload)
        and (created_at is None or type(created_at) is str)
        and (updated_at is None or type(updated_at) is str)
        and (
            expected_relation_id is None
            or relation_id == expected_relation_id
        )
        and (expected_source_id is None or source_id == expected_source_id)
        and (expected_target_id is None or target_id == expected_target_id)
        and (
            expected_relation_type is None
            or relation_type.value == expected_relation_type
        )
        and (
            expected_relation_source_type is None
            or relation_source_type.value == expected_relation_source_type
        )
    )


def _is_vault_entry_row(value: object, *, expected_id: int) -> bool:
    return (
        type(value) is dict
        and {"knowledge_id", "title", "source_type", "file_path"}.issubset(value)
        and _is_positive_backend_id(value["knowledge_id"])
        and value["knowledge_id"] == expected_id
        and type(value["title"]) is str
        and type(value["source_type"]) is str
        and bool(value["source_type"].strip())
        and type(value["file_path"]) is str
    )


def _is_tag_count_row(value: object) -> bool:
    return (
        type(value) is dict
        and {"name", "count"}.issubset(value)
        and type(value["name"]) is str
        and bool(value["name"].strip())
        and _is_nonnegative_backend_count(value["count"])
    )


def _is_source_count_row(value: object) -> bool:
    return (
        type(value) is tuple
        and len(value) == 2
        and type(value[0]) is str
        and bool(value[0].strip())
        and _is_positive_backend_id(value[1])
    )


def _is_tag_collection(value: object, *, max_items: int | None = None) -> bool:
    if (
        type(value) is not list
        or (max_items is not None and len(value) > max_items)
        or not all(_is_tag_count_row(item) for item in value)
    ):
        return False
    names = [item["name"] for item in value]
    counts = [item["count"] for item in value]
    return (
        len(names) == len(set(names))
        and counts == sorted(counts, reverse=True)
    )


def _is_statistics_payload(value: object) -> bool:
    if type(value) is not dict or not {
        "total_entries",
        "by_source_type",
        "top_tags",
    }.issubset(value):
        return False
    source_counts = value["by_source_type"]
    top_tags = value["top_tags"]
    total_entries = value["total_entries"]
    if (
        not _is_nonnegative_backend_count(total_entries)
        or type(source_counts) is not list
        or not all(_is_source_count_row(item) for item in source_counts)
        or not _is_tag_collection(top_tags, max_items=20)
    ):
        return False
    source_names = [item[0] for item in source_counts]
    return (
        len(source_names) == len(set(source_names))
        and sum(item[1] for item in source_counts) == total_entries
        and all(item["count"] <= total_entries for item in top_tags)
    )


def _project_statistics(stats: dict) -> dict:
    return {
        "total_entries": stats["total_entries"],
        "by_source_type": [list(item) for item in stats["by_source_type"]],
        "top_tags": [
            {"name": item["name"], "count": item["count"]}
            for item in stats["top_tags"]
        ],
    }


def _project_entry_metadata(entry: dict) -> dict:
    payload = {
        field: entry[field]
        for field in _ENTRY_METADATA_FIELDS
        if field in entry
    }
    text_fields = set(_ENTRY_METADATA_FIELDS) - {"knowledge_id", "word_count"}
    if (
        not _is_positive_backend_id(payload.get("knowledge_id"))
        or (
            "word_count" in payload
            and not _is_nonnegative_backend_count(payload["word_count"])
        )
        or any(
            payload.get(field) is not None and type(payload[field]) is not str
            for field in text_fields
            if field in payload
        )
    ):
        raise TypeError("entry metadata contract invalid")
    payload["tags"] = parse_tags_string(payload.get("tags", ""))
    payload["keywords"] = parse_tags_string(payload.get("keywords", ""))
    return payload


def _project_chunk(
    chunk: object,
    *,
    expected_knowledge_id: int | None = None,
    expected_chunk_id: int | None = None,
    expected_chunk_index: int | None = None,
) -> dict:
    if type(chunk) is not dict or not {
        "chunk_id",
        "knowledge_id",
        "chunk_index",
        "chunk_text",
    }.issubset(chunk):
        raise TypeError("chunk backend contract invalid")
    if (
        not _is_positive_backend_id(chunk["chunk_id"])
        or not _is_positive_backend_id(chunk["knowledge_id"])
        or (
            expected_knowledge_id is not None
            and chunk["knowledge_id"] != expected_knowledge_id
        )
        or not _is_nonnegative_backend_count(chunk["chunk_index"])
        or type(chunk["chunk_text"]) is not str
        or (
            expected_chunk_id is not None
            and chunk["chunk_id"] != expected_chunk_id
        )
        or (
            expected_chunk_index is not None
            and chunk["chunk_index"] != expected_chunk_index
        )
    ):
        raise TypeError("chunk backend contract invalid")
    payload = {field: chunk[field] for field in _CHUNK_FIELDS if field in chunk}
    if any(
        payload.get(field) is not None and type(payload[field]) is not str
        for field in (
            "context_before",
            "context_after",
            "section_title",
            "created_at",
        )
        if field in payload
    ):
        raise TypeError("chunk backend contract invalid")
    return payload


def _json_resource(payload: dict) -> str:
    try:
        if type(payload) is not dict or not _is_json_tree(payload):
            raise TypeError("resource payload is not a strict JSON tree")
        sanitized = sanitize_public_evidence(payload)
        if type(sanitized) is not dict or not _is_json_tree(sanitized):
            raise TypeError("sanitized resource payload is not a strict JSON tree")
        return json.dumps(
            sanitized,
            ensure_ascii=False,
            indent=2,
            allow_nan=False,
        )
    except Exception as exc:
        _raise_resource_backend_error(exc)


def _require_vault_entry(knowledge_id: int) -> dict:
    """Return a parent entry only when its canonical source is inside the vault."""
    entry = _resource_backend_call(
        lambda: get_sqlite_store().query_by_id(knowledge_id)
    )
    if entry is None:
        raise ValueError("未找到条目")
    if not _is_vault_entry_row(entry, expected_id=knowledge_id):
        _raise_resource_backend_error(TypeError("entry backend contract invalid"))
    md_store = _resource_backend_call(get_markdown_store)
    try:
        resolve_vault_file_path(
            entry.get("file_path"),
            md_store.vault_dir,
        )
    except Exception:
        raise ValueError("条目内容不可用") from None
    return entry


# ============================================================
# Resource 1: pkv://entries/{knowledge_id} — 条目全文
# ============================================================

@mcp.resource("pkv://entries/{knowledge_id}")
async def get_entry_content(knowledge_id: str) -> str:
    """获取知识条目的 Markdown 全文。

    Args:
        knowledge_id: 知识条目 ID

    Returns:
        Markdown 格式的全文内容
    """
    def _impl():
        kid = _positive_int(knowledge_id, "knowledge_id")

        entry = _require_vault_entry(kid)

        try:
            md_store = _resource_backend_call(get_markdown_store)
            safe_path = resolve_vault_file_path(
                entry.get("file_path"),
                md_store.vault_dir,
            )
            loaded_entry = _resource_backend_call(lambda: md_store.load(safe_path))
            if type(loaded_entry) is not Entry:
                _raise_resource_backend_error(
                    TypeError("entry content backend contract invalid")
                )
            try:
                content = loaded_entry.content
            except Exception as exc:
                _raise_resource_backend_error(exc)
            if type(content) is not str or not content:
                _raise_resource_backend_error(
                    TypeError("entry content backend contract invalid")
                )
            return content
        except ValueError as exc:
            if str(exc) == _RESOURCE_BACKEND_ERROR:
                raise
            logger.warning("读取 entry Resource 失败: knowledge_id=%s", kid)
            raise ValueError("条目内容不可用") from None
        except Exception:
            logger.warning("读取 entry Resource 失败: knowledge_id=%s", kid)
            raise ValueError("条目内容不可用") from None
        raise ValueError("条目内容不可用")

    return await anyio.to_thread.run_sync(_impl)


# ============================================================
# Resource 2: pkv://entries/{knowledge_id}/metadata — 条目元数据
# ============================================================

@mcp.resource("pkv://entries/{knowledge_id}/metadata")
async def get_entry_metadata(knowledge_id: str) -> str:
    """获取知识条目的元数据（JSON 格式）。

    Args:
        knowledge_id: 知识条目 ID

    Returns:
        JSON 格式的元数据字符串
    """
    def _impl():
        kid = _positive_int(knowledge_id, "knowledge_id")

        entry = _require_vault_entry(kid)

        # 转换 tags/keywords 为列表后再序列化
        entry_dict = _resource_backend_call(lambda: _project_entry_metadata(entry))

        return _json_resource(entry_dict)

    return await anyio.to_thread.run_sync(_impl)


@mcp.resource("pkv://entries/{knowledge_id}/chunks/{chunk_id}")
async def get_entry_chunk(knowledge_id: str, chunk_id: str) -> str:
    """按持久化 chunk_id 读取精确的条目片段。"""
    def _impl() -> str:
        kid = _positive_int(knowledge_id, "knowledge_id")
        cid = _positive_int(chunk_id, "chunk_id")
        _require_vault_entry(kid)
        chunk = _resource_backend_call(
            lambda: get_sqlite_store().get_chunk_by_id(cid)
        )
        if chunk is None:
            raise ValueError(f"未找到条目 {kid} 的 chunk_id={cid}")
        payload = _resource_backend_call(
            lambda: _project_chunk(chunk, expected_chunk_id=cid)
        )
        chunk_knowledge_id = payload["knowledge_id"]
        if chunk_knowledge_id != kid:
            raise ValueError(f"未找到条目 {kid} 的 chunk_id={cid}")
        payload["citation_locator"] = build_chunk_locator(kid, chunk_id=cid)
        return _json_resource(payload)

    return await anyio.to_thread.run_sync(_impl)


@mcp.resource("pkv://entries/{knowledge_id}/chunk-index/{chunk_index}")
async def get_entry_chunk_by_index(knowledge_id: str, chunk_index: str) -> str:
    """按条目内稳定 chunk_index 读取精确片段。"""
    def _impl() -> str:
        kid = _positive_int(knowledge_id, "knowledge_id")
        index = _nonnegative_int(chunk_index, "chunk_index")
        _require_vault_entry(kid)
        chunk = _resource_backend_call(
            lambda: get_sqlite_store().get_chunk_by_index(kid, index)
        )
        if chunk is None:
            raise ValueError(f"未找到条目 {kid} 的 chunk_index={index}")
        payload = _resource_backend_call(
            lambda: _project_chunk(
                chunk,
                expected_knowledge_id=kid,
                expected_chunk_index=index,
            )
        )
        payload["citation_locator"] = build_chunk_locator(
            kid,
            chunk_index=index,
        )
        return _json_resource(payload)

    return await anyio.to_thread.run_sync(_impl)


@mcp.resource("pkv://entries/{knowledge_id}/metadata/{field_name}")
async def get_entry_metadata_field(knowledge_id: str, field_name: str) -> str:
    """读取一个精确元数据字段并回显其物理/存储字段。"""
    def _impl() -> str:
        kid = _positive_int(knowledge_id, "knowledge_id")
        if type(field_name) is not str or not field_name:
            raise ValueError("field_name 不能为空")
        requested_field = field_name
        if requested_field not in TIMELINE_METADATA_FIELDS:
            raise ValueError(_UNSUPPORTED_TIMELINE_METADATA_FIELD)
        entry = _resource_backend_call(lambda: get_sqlite_store().query_by_id(kid))
        if entry is None:
            raise ValueError(f"未找到条目: {kid}")
        if not _is_vault_entry_row(entry, expected_id=kid):
            _raise_resource_backend_error(
                TypeError("entry backend contract invalid")
            )

        md_store = _resource_backend_call(get_markdown_store)
        found, value, storage_field = _resource_backend_call(
            lambda: _project_persisted_metadata_field(
                read_persisted_metadata_field(
                    entry,
                    requested_field,
                    vault_dir=md_store.vault_dir,
                )
            )
        )
        if not found:
            raise ValueError(f"条目 {kid} 不存在元数据字段: {requested_field}")

        return _json_resource(
            {
                "knowledge_id": kid,
                "field": requested_field,
                "physical_source_field": requested_field,
                "storage_field": storage_field,
                "value": value,
                "citation_locator": build_metadata_locator(kid, requested_field),
            }
        )

    return await anyio.to_thread.run_sync(_impl)


def _relation_payload(record: RelationRecord) -> str:
    if not _is_relation_record(record):
        _raise_resource_backend_error(
            TypeError("relation backend contract invalid")
        )
    payload = serialize_relation_evidence(record)
    payload["citation_locator"] = build_relation_locator(
        relation_id=record.relation_id,
        source_knowledge_id=record.source_knowledge_id,
        target_knowledge_id=record.target_knowledge_id,
        relation_type=record.relation_type.value,
        relation_source_type=record.relation_source_type.value,
    )
    return _json_resource(payload)


@mcp.resource("pkv://relations/{relation_id}")
async def get_relation_resource(relation_id: str) -> str:
    """按持久化 relation_id 读取精确关系边。"""
    def _impl() -> str:
        rid = _positive_int(relation_id, "relation_id")
        relation_store = _resource_backend_call(
            lambda: get_relation_query_service().relation_store
        )
        record = _resource_backend_call(lambda: relation_store.get_relation(rid))
        if record is None:
            raise ValueError(f"未找到关系: {rid}")
        if not _is_relation_record(record, expected_relation_id=rid):
            _raise_resource_backend_error(
                TypeError("relation backend contract invalid")
            )
        source_id = record.source_knowledge_id
        target_id = record.target_knowledge_id
        _require_vault_entry(source_id)
        _require_vault_entry(target_id)
        return _resource_backend_call(lambda: _relation_payload(record))

    return await anyio.to_thread.run_sync(_impl)


@mcp.resource(
    "pkv://relations/by-edge/{source_knowledge_id}/{target_knowledge_id}/"
    "{relation_type}/{relation_source_type}"
)
async def get_relation_by_edge_resource(
    source_knowledge_id: str,
    target_knowledge_id: str,
    relation_type: str,
    relation_source_type: str,
) -> str:
    """按唯一边字段读取关系，供缺少 relation_id 的记录生成稳定引用。"""
    def _impl() -> str:
        source_id = _positive_int(source_knowledge_id, "source_knowledge_id")
        target_id = _positive_int(target_knowledge_id, "target_knowledge_id")
        relation_type_safe = _exact_enum_value(
            relation_type,
            allowed_values=_RELATION_TYPE_VALUES,
            field_name="relation_type",
        )
        relation_source_type_safe = _exact_enum_value(
            relation_source_type,
            allowed_values=_RELATION_SOURCE_TYPE_VALUES,
            field_name="relation_source_type",
        )
        relation_store = _resource_backend_call(
            lambda: get_relation_query_service().relation_store
        )
        records = _resource_backend_call(
            lambda: relation_store.list_relations_between(source_id, target_id)
        )
        if type(records) is not list or not all(
            _is_relation_record(
                record,
                expected_source_id=source_id,
                expected_target_id=target_id,
            )
            for record in records
        ):
            _raise_resource_backend_error(
                TypeError("relation backend contract invalid")
            )
        matches = [
            record
            for record in records
            if record.relation_type.value == relation_type_safe
            and record.relation_source_type.value == relation_source_type_safe
        ]
        if len(matches) != 1:
            raise ValueError(
                "关系边无法唯一解析: "
                f"{source_id}->{target_id}/{relation_type_safe}/{relation_source_type_safe}"
            )
        record = matches[0]
        if not _is_relation_record(
            record,
            expected_source_id=source_id,
            expected_target_id=target_id,
            expected_relation_type=relation_type_safe,
            expected_relation_source_type=relation_source_type_safe,
        ):
            _raise_resource_backend_error(
                TypeError("relation backend contract invalid")
            )
        _require_vault_entry(source_id)
        _require_vault_entry(target_id)
        return _resource_backend_call(lambda: _relation_payload(record))

    return await anyio.to_thread.run_sync(_impl)


# ============================================================
# Resource 8: pkv://tags — 标签列表
# ============================================================

@mcp.resource("pkv://tags")
async def get_tags_resource() -> str:
    """获取所有标签列表（Resource 版，返回 JSON 字符串）。

    Returns:
        JSON 格式的标签列表，每项包含 name 和 count
    """
    def _impl():
        tags = _resource_backend_call(
            lambda: get_sqlite_store().get_all_tags_with_count()
        )
        if not _is_tag_collection(tags):
            _raise_resource_backend_error(TypeError("tag backend contract invalid"))
        payload = {
            "total_tags": len(tags),
            "tags": [
                {"name": item["name"], "count": item["count"]}
                for item in tags
            ],
        }
        return _json_resource(payload)

    return await anyio.to_thread.run_sync(_impl)


# ============================================================
# Resource 9: pkv://stats — 统计信息
# ============================================================

@mcp.resource("pkv://stats")
async def get_stats_resource() -> str:
    """获取知识库统计信息（Resource 版，返回 JSON 字符串）。

    Returns:
        JSON 格式的统计数据
    """
    def _impl():
        stats = _resource_backend_call(lambda: get_sqlite_store().get_statistics())
        if not _is_statistics_payload(stats):
            _raise_resource_backend_error(
                TypeError("statistics backend contract invalid")
            )
        return _json_resource(_project_statistics(stats))

    return await anyio.to_thread.run_sync(_impl)
