"""Stable citation locators shared by relation and evidence services."""

from __future__ import annotations

from typing import Optional


def build_entry_locator(knowledge_id: int) -> str:
    """Return the canonical MCP resource locator for one knowledge entry."""
    return f"pkv://entries/{int(knowledge_id)}"


def build_chunk_locator(
    knowledge_id: int,
    *,
    chunk_id: Optional[int] = None,
    chunk_index: Optional[int] = None,
) -> str:
    """Return a stable locator for a chunk, preferring its persistent id."""
    entry_locator = build_entry_locator(knowledge_id)
    if chunk_id is not None:
        return f"{entry_locator}#chunk-id:{int(chunk_id)}"
    if chunk_index is not None:
        return f"{entry_locator}#chunk-index:{int(chunk_index)}"
    return entry_locator


def build_metadata_locator(knowledge_id: int, field_name: str) -> str:
    """Return a locator for a structured metadata field on an entry."""
    normalized_field = str(field_name or "metadata").strip() or "metadata"
    return f"{build_entry_locator(knowledge_id)}/metadata#{normalized_field}"


def resolve_citation_source(
    knowledge_id: int,
    *,
    source_url: str = "",
    file_path: str = "",
) -> str:
    """Choose the most direct available source, with the entry URI as fallback."""
    return (
        str(source_url or "").strip()
        or str(file_path or "").strip()
        or build_entry_locator(knowledge_id)
    )
