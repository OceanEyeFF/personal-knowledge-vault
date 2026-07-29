"""Stable citation locators shared by relation and evidence services."""

from __future__ import annotations

from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any
from typing import Optional

import frontmatter


def build_entry_locator(knowledge_id: int) -> str:
    """Return the canonical MCP resource locator for one knowledge entry."""
    return f"pkv://entries/{int(knowledge_id)}"


def build_entry_metadata_locator(knowledge_id: int) -> str:
    """Return the canonical metadata Resource locator for one entry."""
    return f"{build_entry_locator(knowledge_id)}/metadata"


def build_chunk_locator(
    knowledge_id: int,
    *,
    chunk_id: Optional[int] = None,
    chunk_index: Optional[int] = None,
) -> str:
    """Return a stable locator for a chunk, preferring its persistent id."""
    entry_locator = build_entry_locator(knowledge_id)
    if chunk_id is not None:
        return f"{entry_locator}/chunks/{int(chunk_id)}"
    if chunk_index is not None:
        return f"{entry_locator}/chunk-index/{int(chunk_index)}"
    return entry_locator


def build_metadata_locator(knowledge_id: int, field_name: str) -> str:
    """Return a locator for a structured metadata field on an entry."""
    normalized_field = str(field_name or "metadata").strip() or "metadata"
    return f"{build_entry_locator(knowledge_id)}/metadata/{normalized_field}"


def build_relation_locator(
    *,
    relation_id: Optional[int],
    source_knowledge_id: int,
    target_knowledge_id: int,
    relation_type: str,
    relation_source_type: str,
) -> str:
    """Return a resolvable locator for a persisted relation edge.

    Persisted relation ids are preferred.  The composite form remains precise
    for callers that construct a record without carrying its database id.
    """
    if relation_id is not None:
        return f"pkv://relations/{int(relation_id)}"
    return (
        "pkv://relations/by-edge/"
        f"{int(source_knowledge_id)}/{int(target_knowledge_id)}/"
        f"{str(relation_type)}/{str(relation_source_type)}"
    )


def resolve_citation_source(
    knowledge_id: int,
    *,
    source_url: str = "",
    file_path: str = "",
) -> str:
    """Choose a remote-usable source without exposing a local file path."""
    del file_path
    return str(source_url or "").strip() or build_entry_locator(knowledge_id)


_LOCAL_PATH_KEYS = {
    "file_path",
    "source_file_path",
    "target_file_path",
}


def _is_absolute_path_text(value: str) -> bool:
    return PurePosixPath(value).is_absolute() or PureWindowsPath(value).is_absolute()


def sanitize_public_evidence(value: Any) -> Any:
    """Recursively remove local filesystem locations from public evidence."""
    if isinstance(value, dict):
        return {
            key: sanitize_public_evidence(item)
            for key, item in value.items()
            if key not in _LOCAL_PATH_KEYS
        }
    if isinstance(value, list):
        return [sanitize_public_evidence(item) for item in value]
    if isinstance(value, tuple):
        return [sanitize_public_evidence(item) for item in value]
    if isinstance(value, str) and _is_absolute_path_text(value):
        return "[redacted-local-path]"
    return value


def serialize_relation_evidence(record: Any) -> dict[str, Any]:
    """Serialize one relation edge without leaking extractor-local paths."""
    return sanitize_public_evidence(record.to_dict())


def read_persisted_metadata_field(
    entry: dict[str, Any],
    field_name: str,
) -> tuple[bool, Any, str]:
    """Read a metadata field only from a persistent entry or Markdown source."""
    if field_name in entry and entry.get(field_name) not in (None, ""):
        return True, entry[field_name], "knowledge_items"

    file_path = str(entry.get("file_path") or "").strip()
    if file_path:
        path = Path(file_path)
        try:
            if path.is_file():
                post = frontmatter.load(path)
                value = post.metadata.get(field_name)
                if value not in (None, ""):
                    return True, value, "markdown_frontmatter"
        except Exception:
            return False, None, ""
    return False, None, ""
