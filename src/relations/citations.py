"""Stable citation locators shared by relation and evidence services."""

from __future__ import annotations

from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any
from typing import Optional
from urllib.parse import unquote, urlparse

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
    return sanitize_public_source_url(source_url) or build_entry_locator(knowledge_id)


_LOCAL_PATH_KEYS = {
    "file_path",
    "source_file_path",
    "target_file_path",
}


def is_local_reference(value: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    decoded = unquote(text)
    if urlparse(decoded).scheme.lower() == "file":
        return True
    windows_path = PureWindowsPath(decoded)
    return (
        PurePosixPath(decoded).is_absolute()
        or windows_path.is_absolute()
        # Windows root-relative (\\Windows\\...) and NT namespace paths
        # (\\??\\C:\\...) have no drive, so is_absolute() alone misses them.
        or bool(windows_path.root)
    )


def sanitize_public_source_url(value: Any) -> str:
    """Return a remote-safe source URL, clearing filesystem-backed references."""
    text = str(value or "").strip()
    if not text or is_local_reference(text):
        return ""
    return text


def _fallback_knowledge_id(value: dict[str, Any]) -> Optional[int]:
    for key in ("knowledge_id", "source_knowledge_id", "target_knowledge_id"):
        raw_id = value.get(key)
        try:
            knowledge_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        if knowledge_id > 0:
            return knowledge_id
    return None


def _sanitize_public_dict_key(key: Any, existing: dict[Any, Any]) -> Any:
    """Redact filesystem-valued dynamic keys without silently overwriting peers."""
    sanitized_key = (
        "[redacted-local-reference]"
        if isinstance(key, str) and is_local_reference(key)
        else key
    )
    if sanitized_key not in existing:
        return sanitized_key
    if not isinstance(sanitized_key, str):
        return sanitized_key

    base_key = sanitized_key
    index = 2
    while sanitized_key in existing:
        sanitized_key = f"{base_key}#{index}"
        index += 1
    return sanitized_key


def sanitize_public_evidence(value: Any) -> Any:
    """Recursively remove local filesystem locations from public evidence."""
    if isinstance(value, dict):
        knowledge_id = _fallback_knowledge_id(value)
        sanitized: dict[Any, Any] = {}
        for key, item in value.items():
            if key in _LOCAL_PATH_KEYS:
                continue
            public_key = _sanitize_public_dict_key(key, sanitized)
            if key == "source_url":
                sanitized[public_key] = sanitize_public_source_url(item)
                continue
            if (
                key in {"source", "citation_source"}
                and isinstance(item, str)
                and is_local_reference(item)
            ):
                sanitized[public_key] = (
                    build_entry_locator(knowledge_id)
                    if knowledge_id is not None
                    else "[redacted-local-reference]"
                )
                continue
            sanitized[public_key] = sanitize_public_evidence(item)
        return sanitized
    if isinstance(value, list):
        return [sanitize_public_evidence(item) for item in value]
    if isinstance(value, tuple):
        return [sanitize_public_evidence(item) for item in value]
    if isinstance(value, str) and is_local_reference(value):
        return "[redacted-local-reference]"
    return value


def serialize_relation_evidence(record: Any) -> dict[str, Any]:
    """Serialize one relation edge without leaking extractor-local paths."""
    return sanitize_public_evidence(record.to_dict())


def resolve_vault_file_path(file_path: Any, vault_dir: Any) -> Path:
    """Resolve one regular file and prove that it remains inside the vault."""
    raw_path = str(file_path or "").strip()
    if not raw_path or not vault_dir:
        raise ValueError("条目文件不可用")
    if raw_path.startswith(("\\\\", "//")):
        raise ValueError("条目文件不可用")

    try:
        resolved_vault = Path(vault_dir).resolve(strict=True)
        candidate = Path(raw_path)
        if not candidate.is_absolute():
            candidate = resolved_vault / candidate
        resolved_file = candidate.resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        raise ValueError("条目文件不可用") from None

    if not resolved_vault.is_dir():
        raise ValueError("条目文件不可用")
    try:
        resolved_file.relative_to(resolved_vault)
    except ValueError:
        raise ValueError("条目文件不可用") from None
    if resolved_file == resolved_vault or not resolved_file.is_file():
        raise ValueError("条目文件不可用")
    return resolved_file


def read_persisted_metadata_field(
    entry: dict[str, Any],
    field_name: str,
    vault_dir: Any = None,
) -> tuple[bool, Any, str]:
    """Read a metadata field only from a persistent entry or Markdown source."""
    resolved_path: Optional[Path] = None
    if vault_dir:
        try:
            resolved_path = resolve_vault_file_path(
                entry.get("file_path"),
                vault_dir,
            )
        except Exception:
            return False, None, ""

    if field_name in entry and entry.get(field_name) not in (None, ""):
        return True, entry[field_name], "knowledge_items"

    if resolved_path is not None:
        try:
            post = frontmatter.load(resolved_path)
            value = post.metadata.get(field_name)
            if value not in (None, ""):
                return True, value, "markdown_frontmatter"
        except Exception:
            return False, None, ""
    return False, None, ""
