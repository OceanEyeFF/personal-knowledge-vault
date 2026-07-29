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

logger = logging.getLogger("pkv.mcp")

TIMELINE_METADATA_FIELDS = {
    "event_time",
    "published_at",
    "published_time",
    "publish_time",
    "archived_at",
}


def _positive_int(raw_value: str, field_name: str) -> int:
    try:
        value = int(raw_value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"无效的 {field_name}") from exc
    if value <= 0:
        raise ValueError(f"{field_name} 必须为正整数")
    return value


def _json_resource(payload: dict) -> str:
    return json.dumps(
        sanitize_public_evidence(payload),
        ensure_ascii=False,
        indent=2,
        default=str,
    )


def _require_vault_entry(knowledge_id: int) -> dict:
    """Return a parent entry only when its canonical source is inside the vault."""
    entry = get_sqlite_store().query_by_id(knowledge_id)
    if not entry:
        raise ValueError("未找到条目")
    try:
        resolve_vault_file_path(
            entry.get("file_path"),
            get_markdown_store().vault_dir,
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
            md_store = get_markdown_store()
            safe_path = resolve_vault_file_path(
                entry.get("file_path"),
                md_store.vault_dir,
            )
            loaded_entry = md_store.load(safe_path)
            if loaded_entry and loaded_entry.content:
                return loaded_entry.content
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
        try:
            kid = int(knowledge_id)
        except (ValueError, TypeError):
            raise ValueError("无效的 knowledge_id") from None

        entry = _require_vault_entry(kid)

        # 转换 tags/keywords 为列表后再序列化
        entry_dict = dict(entry)
        entry_dict.pop("file_path", None)
        entry_dict["tags"] = parse_tags_string(entry_dict.get("tags", ""))
        entry_dict["keywords"] = parse_tags_string(entry_dict.get("keywords", ""))

        return _json_resource(entry_dict)

    return await anyio.to_thread.run_sync(_impl)


@mcp.resource("pkv://entries/{knowledge_id}/chunks/{chunk_id}")
async def get_entry_chunk(knowledge_id: str, chunk_id: str) -> str:
    """按持久化 chunk_id 读取精确的条目片段。"""
    def _impl() -> str:
        kid = _positive_int(knowledge_id, "knowledge_id")
        cid = _positive_int(chunk_id, "chunk_id")
        _require_vault_entry(kid)
        chunk = get_sqlite_store().get_chunk_by_id(cid)
        if not chunk or int(chunk["knowledge_id"]) != kid:
            raise ValueError(f"未找到条目 {kid} 的 chunk_id={cid}")
        payload = dict(chunk)
        payload["citation_locator"] = build_chunk_locator(kid, chunk_id=cid)
        return _json_resource(payload)

    return await anyio.to_thread.run_sync(_impl)


@mcp.resource("pkv://entries/{knowledge_id}/chunk-index/{chunk_index}")
async def get_entry_chunk_by_index(knowledge_id: str, chunk_index: str) -> str:
    """按条目内稳定 chunk_index 读取精确片段。"""
    def _impl() -> str:
        kid = _positive_int(knowledge_id, "knowledge_id")
        try:
            index = int(chunk_index)
        except (TypeError, ValueError):
            raise ValueError("无效的 chunk_index") from None
        if index < 0:
            raise ValueError("chunk_index 不能为负数")
        _require_vault_entry(kid)
        chunk = get_sqlite_store().get_chunk_by_index(kid, index)
        if not chunk:
            raise ValueError(f"未找到条目 {kid} 的 chunk_index={index}")
        payload = dict(chunk)
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
        requested_field = str(field_name or "").strip()
        if not requested_field:
            raise ValueError("field_name 不能为空")
        if requested_field not in TIMELINE_METADATA_FIELDS:
            raise ValueError(f"不支持的 timeline 元数据字段: {requested_field}")
        entry = get_sqlite_store().query_by_id(kid)
        if not entry:
            raise ValueError(f"未找到条目: {kid}")

        found, value, storage_field = read_persisted_metadata_field(
            entry,
            requested_field,
            vault_dir=get_markdown_store().vault_dir,
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


def _relation_payload(record) -> str:
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
        relation_store = get_relation_query_service().relation_store
        record = relation_store.get_relation(rid)
        if not record:
            raise ValueError(f"未找到关系: {rid}")
        _require_vault_entry(record.source_knowledge_id)
        _require_vault_entry(record.target_knowledge_id)
        return _relation_payload(record)

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
        relation_store = get_relation_query_service().relation_store
        matches = [
            record
            for record in relation_store.list_relations_between(source_id, target_id)
            if record.relation_type.value == relation_type
            and record.relation_source_type.value == relation_source_type
        ]
        if len(matches) != 1:
            raise ValueError(
                "关系边无法唯一解析: "
                f"{source_id}->{target_id}/{relation_type}/{relation_source_type}"
            )
        record = matches[0]
        _require_vault_entry(record.source_knowledge_id)
        _require_vault_entry(record.target_knowledge_id)
        return _relation_payload(record)

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
        store = get_sqlite_store()
        tags = store.get_all_tags_with_count()
        return _json_resource({"total_tags": len(tags), "tags": tags})

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
        store = get_sqlite_store()
        stats = store.get_statistics()
        return _json_resource(stats)

    return await anyio.to_thread.run_sync(_impl)
