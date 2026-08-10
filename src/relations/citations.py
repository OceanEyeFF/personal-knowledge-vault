"""Stable citation locators shared by relation and evidence services."""

from __future__ import annotations

from pathlib import Path, PurePosixPath, PureWindowsPath
import re
from typing import Any
from typing import Optional
from urllib.parse import unquote, unquote_plus, urlparse, urlsplit, urlunsplit

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

_PUBLIC_URL_SENSITIVE_QUERY_NAMES = frozenset(
    {
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
        "code",
        "cookie",
        "credential",
        "credentials",
        "id_token",
        "jsession_id",
        "jsessionid",
        "jsessionidsso",
        "jwt",
        "jwt_token",
        "key",
        "oauth_token",
        "pass",
        "passcode",
        "passphrase",
        "passwd",
        "password",
        "phpsessid",
        "private_key",
        "pwd",
        "refresh_token",
        "secret",
        "session",
        "session_id",
        "session_key",
        "sessionid",
        "session_token",
        "sid",
        "sig",
        "signature",
        "subscription_key",
        "token",
        "x_amz_credential",
        "x_amz_security_token",
        "x_amz_signature",
        "x_api_key",
        "x_auth_token",
        "x_goog_credential",
        "x_goog_signature",
    }
)
_PUBLIC_URL_SENSITIVE_QUERY_MARKERS = frozenset(
    {
        "api_key",
        "apikey",
        "auth",
        "authorization",
        "bearer",
        "cookie",
        "credential",
        "jwt",
        "password",
        "secret",
        "session",
        "sig",
        "signature",
        "token",
    }
)
_PUBLIC_URL_REDACTED_VALUE = "redacted"
_NESTED_URL_USERINFO_RE = re.compile(
    # WHATWG special-scheme URLs may omit one or both authority slashes and
    # are still normalized into credential-bearing URLs by browsers/clients.
    r"(?:(?:https?|ftp|wss?):[\\/]*|//)[^/?#&;]*@",
    re.IGNORECASE,
)


def _decode_bounded(value: str, *, plus: bool = False) -> tuple[str, bool]:
    """Decode at most eight layers and report an unsafe remaining layer."""

    decoder = unquote_plus if plus else unquote
    decoded = str(value)
    for _ in range(8):
        next_value = decoder(decoded)
        if next_value == decoded:
            return decoded, False
        decoded = next_value
    return decoded, decoder(decoded) != decoded


def _has_unsafe_decoded_transport_residual(value: str) -> bool:
    """Reject decoded URL sub-values that can be reinterpreted as private data.

    Query and matrix values may hide another URL or filesystem path behind one
    or more percent-encoding layers.  Checking the raw URL alone is therefore
    insufficient: browsers and downstream clients commonly decode these values
    again before logging, redirecting, or displaying them.
    """

    if "\\" in value or any(ord(char) < 32 or ord(char) == 127 for char in value):
        return True
    if re.search(r"(?i)(?:^|[/?&;=#])file:", value):
        return True
    for component in re.split(r"[?&;#]", value):
        _key, separator, nested_value = component.partition("=")
        if separator and is_local_reference(nested_value):
            return True
    return False


def is_local_reference(value: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    decoded, decode_exhausted = _decode_bounded(text)
    if decode_exhausted:
        return True
    try:
        parsed_scheme = urlparse(decoded).scheme.lower()
    except ValueError:
        return True
    if parsed_scheme == "file":
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
    """Return a public HTTP(S) citation URL without embedded credentials."""
    text = str(value or "").strip()
    if not text or is_local_reference(text):
        return ""
    if "\\" in text or any(ord(char) <= 32 or ord(char) == 127 for char in text):
        return ""

    try:
        parsed = urlsplit(text)
        scheme = parsed.scheme.lower()
        hostname = parsed.hostname
        port = parsed.port
    except (TypeError, ValueError):
        return ""
    if scheme not in {"http", "https"} or not hostname or "%" in hostname:
        return ""
    try:
        hostname = hostname.encode("idna").decode("ascii")
    except UnicodeError:
        return ""

    host = f"[{hostname}]" if ":" in hostname else hostname
    default_port = 443 if scheme == "https" else 80
    netloc = host if port in (None, default_port) else f"{host}:{port}"
    path = _redact_public_url_path_matrix(parsed.path or "/")
    query = _redact_public_url_query(parsed.query)
    # Fragments are neither sent to the source server nor stable MCP locators;
    # dropping them also prevents a second credential-bearing parameter surface.
    return urlunsplit((scheme, netloc, path, query, ""))


def _redact_public_url_path_matrix(path: str) -> str:
    """Replace credential-bearing ``;name=value`` matrix values in URL paths."""

    def replace(match: re.Match[str]) -> str:
        if not _is_sensitive_public_query_key(match.group("key")):
            return match.group(0)
        return (
            f"{match.group('prefix')}{match.group('key')}="
            f"{_PUBLIC_URL_REDACTED_VALUE}"
        )

    sanitized = re.sub(
        (
            r"(?P<prefix>;|%3[bB])"
            r"(?P<key>[^/;=?#]+)=(?P<value>[^/;]*)"
        ),
        replace,
        path,
    )
    decoded, decode_exhausted = _decode_bounded(sanitized)
    if decode_exhausted:
        return "/"
    if _has_unsafe_decoded_transport_residual(decoded):
        return "/"
    if _NESTED_URL_USERINFO_RE.search(decoded):
        return "/"
    for match in re.finditer(r";(?P<key>[^/;=?#]+)=(?P<value>[^/;]*)", decoded):
        if (
            _is_sensitive_public_query_key(match.group("key"))
            and match.group("value") != _PUBLIC_URL_REDACTED_VALUE
        ):
            # Ambiguous/double-encoded matrix syntax cannot be rewritten while
            # preserving exact path semantics, so retain only the safe origin.
            return "/"
    return sanitized


def _redact_public_url_query(query: str) -> str:
    """Preserve query order and safe values while replacing credential values."""

    parts = re.split(r"([&;])", query)
    sanitized_parts: list[str] = []
    pending_separator = ""
    for index in range(0, len(parts), 2):
        parameter = parts[index]
        decoded_parameter, decode_exhausted = _decode_bounded(
            parameter,
            plus=True,
        )
        if decode_exhausted:
            return ""
        decoded_key, decoded_separator, _decoded_value = decoded_parameter.partition("=")

        if "=" in parameter:
            key, _separator, _value = parameter.partition("=")
            if _is_sensitive_public_query_key(key):
                parameter = f"{key}={_PUBLIC_URL_REDACTED_VALUE}"
        elif decoded_separator and _is_sensitive_public_query_key(decoded_key):
            # An encoded/double-encoded ``=`` is ambiguous across downstream
            # parsers.  Drop the complete credential-bearing component.
            parameter = ""

        if parameter:
            if sanitized_parts:
                sanitized_parts.append(pending_separator or "&")
            sanitized_parts.append(parameter)
        if index + 1 < len(parts):
            pending_separator = parts[index + 1]
        else:
            pending_separator = ""

    sanitized = "".join(sanitized_parts)
    decoded, decode_exhausted = _decode_bounded(sanitized, plus=True)
    if decode_exhausted:
        return ""
    if _has_unsafe_decoded_transport_residual(decoded):
        return ""
    if _NESTED_URL_USERINFO_RE.search(decoded):
        return ""
    for match in re.finditer(
        r"(?:^|[?&;#])(?P<key>[^?&;=#]+)=(?P<value>[^?&;#]*)",
        decoded,
    ):
        if (
            _is_sensitive_public_query_key(match.group("key"))
            and match.group("value") != _PUBLIC_URL_REDACTED_VALUE
        ):
            # Nested encoded separators can make a credential appear only after
            # downstream decoding.  Dropping the query is the only unambiguous
            # public representation.
            return ""
    return sanitized


def _is_sensitive_public_query_key(key: str) -> bool:
    decoded, decode_exhausted = _decode_bounded(str(key), plus=True)
    if decode_exhausted:
        return True
    camel_normalized = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", decoded)
    camel_normalized = re.sub(
        r"([a-z0-9])([A-Z])", r"\1_\2", camel_normalized
    )
    normalized_candidates = {
        re.sub(r"[^A-Za-z0-9]+", "_", candidate).strip("_").lower()
        for candidate in (decoded, camel_normalized)
    }
    for normalized in normalized_candidates:
        if normalized in _PUBLIC_URL_SENSITIVE_QUERY_NAMES:
            return True
        if any(
            normalized.startswith(f"{marker}_")
            or normalized.endswith(f"_{marker}")
            or f"_{marker}_" in normalized
            for marker in _PUBLIC_URL_SENSITIVE_QUERY_MARKERS
        ):
            return True
    return False


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


def _sanitize_public_source_reference(
    value: str,
    *,
    knowledge_id: Optional[int],
) -> str:
    """Sanitize citation ``source`` fields while preserving canonical locators."""

    text = str(value or "").strip()
    fallback = (
        build_entry_locator(knowledge_id)
        if knowledge_id is not None
        else "[redacted-source-reference]"
    )
    if is_local_reference(text):
        return (
            build_entry_locator(knowledge_id)
            if knowledge_id is not None
            else "[redacted-local-reference]"
        )
    public_url = sanitize_public_source_url(text)
    if public_url:
        return public_url
    if _is_stable_pkv_locator(text):
        return text
    return fallback if _looks_url_like_source_reference(text) else text


def _looks_url_like_source_reference(value: str) -> bool:
    """Fail closed for malformed/relative URL shapes, preserving plain labels."""

    decoded, decode_exhausted = _decode_bounded(
        str(value or "").strip(),
        plus=True,
    )
    if decode_exhausted:
        return True
    lowered = decoded.lower()
    if (
        not decoded
        or "://" in decoded
        or decoded.startswith("//")
        or lowered.startswith("www.")
        or any(marker in decoded for marker in ("@", "?", "#", "\\"))
        or any(ord(char) <= 32 or ord(char) == 127 for char in decoded)
        or re.match(
            r"^[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?::[0-9]+)?(?:/|$)",
            decoded,
        )
    ):
        return True
    try:
        if urlsplit(decoded).scheme:
            return True
    except ValueError:
        return True
    return any(
        _is_sensitive_public_query_key(match.group("key"))
        for match in re.finditer(
            r"(?:^|[?&;])(?P<key>[^?&;=#]+)=",
            decoded,
        )
    )


def _is_stable_pkv_locator(value: str) -> bool:
    """Accept only locator forms generated by this module's public builders."""

    return bool(
        re.fullmatch(
            (
                r"pkv://entries/[1-9][0-9]*"
                r"(?:/(?:metadata(?:/[A-Za-z0-9._~-]+)?"
                r"|chunks/[1-9][0-9]*|chunk-index/[0-9]+))?"
                r"|pkv://relations/(?:[1-9][0-9]*"
                r"|by-edge/[1-9][0-9]*/[1-9][0-9]*/"
                r"[A-Za-z0-9._~-]+/[A-Za-z0-9._~-]+)"
            ),
            value,
        )
    )


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
            if key in {"source", "citation_source"}:
                if isinstance(item, str):
                    sanitized[public_key] = _sanitize_public_source_reference(
                        item,
                        knowledge_id=knowledge_id,
                    )
                else:
                    sanitized[public_key] = (
                        build_entry_locator(knowledge_id)
                        if knowledge_id is not None
                        else "[redacted-source-reference]"
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
    """Resolve through the single W1 Vault containment implementation."""
    raw_path = str(file_path or "").strip()
    if not raw_path or not vault_dir:
        raise ValueError("条目文件不可用")
    try:
        from src.storage.vault_paths import VaultPathGateway

        return VaultPathGateway(Path(vault_dir), create=False).resolve(
            raw_path,
            must_exist=True,
            require_file=True,
        )
    except Exception:
        raise ValueError("条目文件不可用") from None


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
            from src.storage.vault_paths import VaultPathGateway

            gateway = VaultPathGateway(Path(vault_dir), create=False)
            post = frontmatter.loads(gateway.read_text(resolved_path))
            value = post.metadata.get(field_name)
            if value not in (None, ""):
                return True, value, "markdown_frontmatter"
        except Exception:
            return False, None, ""
    return False, None, ""
