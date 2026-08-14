#!/usr/bin/env python3
"""Build a fast, non-release PKV onedir package for local self-tests only.

This deliberately has no relationship to ``build_release.py``.  It uses the
same frozen application graph so the three public entrypoints are exercised,
but writes only below ``dist/internal`` and emits neither an installer nor a
release candidate.  The generated package is labelled **INTERNAL TEST ONLY**.

Use ``scripts/build-internal-package.ps1`` rather than invoking this file
directly.  The PowerShell entrypoint can optionally seed a synthetic
``.data-test`` root and run the external-artifact smoke checks.
"""

from __future__ import annotations

import argparse
import io
import importlib.util
import json
import marshal
import os
import platform
import re
import shutil
import stat
import struct
import subprocess
import sys
import types
import uuid
import zipfile
import zlib
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path
from typing import Any, Iterable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
INTERNAL_OUTPUT_RELATIVE = Path("dist") / "internal"
RUNTIME_MANIFEST_RELATIVE = Path("packaging") / "runtime-resources.json"
SPEC_RELATIVE = Path("packaging") / "pkv.spec"
CLASSIFICATION = "INTERNAL TEST ONLY"
INFO_FILENAME = "internal-build-info.json"
MARKER_FILENAME = "INTERNAL-TEST-ONLY.txt"

_FORBIDDEN_SEGMENTS = frozenset(
    {
        ".data",
        ".data-test",
        "vault",
        "logs",
        "fixtures",
        "fixture",
        "tests",
        "test-fixtures",
    }
)
_FORBIDDEN_FILENAMES = frozenset(
    {
        ".env",
        "local.yaml",
        "conftest.py",
    }
)
_FORBIDDEN_SUFFIXES = frozenset(
    {
        ".db",
        ".sqlite",
        ".sqlite3",
        ".log",
        ".idx",
    }
)
_SECRET_PATH_TERMS = frozenset(
    {
        "credential",
        "credentials",
        "secret",
        "secrets",
        "apikey",
        "api_key",
        "private_key",
        "password",
    }
)
# These are byte patterns deliberately applied to every regular payload file,
# including frozen binaries.  Decoding first is unsafe: a NUL or a non-UTF-8
# byte must not exempt embedded credential material from inspection.
_SECRET_VALUE_BYTE_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        rb"\bsk-[A-Za-z0-9_-]{20,}\b",
        rb"\bgh[pousr]_[A-Za-z0-9_]{20,}\b",
        rb"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b",
        rb"(?:api[_-]?key|access[_-]?key(?:[_-]?id)?|secret(?:[_-]?key)?|password|(?:access|auth|refresh|id)[_-]?token)\s*[:=]\s*['\"]?[A-Za-z0-9_./+=-]{16,}",
    )
)
_PEM_PRIVATE_KEY_HEADER = re.compile(
    rb"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----", re.IGNORECASE
)
_PEM_PRIVATE_KEY_FOOTER = re.compile(
    rb"-----END (?:[A-Z0-9]+ )*PRIVATE KEY-----", re.IGNORECASE
)
_PEM_BODY_BYTES = frozenset(
    b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=\t\r\n "
)
_MAX_PEM_PRIVATE_KEY_BYTES = 2 * 1024 * 1024
_SECRET_SCAN_CHUNK_BYTES = 64 * 1024
_SECRET_SCAN_OVERLAP_BYTES = 16 * 1024
# This is a per-file / per-ZIP-member fail-closed inspection limit, rather
# than a text-only threshold.  It bounds memory and decompression work while
# refusing an input too large to inspect in full.
_MAX_SECRET_SCAN_BYTES_PER_INPUT = 256 * 1024 * 1024
_WINDOWS_FILE_ATTRIBUTE_REPARSE_POINT = 0x0400

# Frozen payloads contain compressed CArchive/PYZ modules and base_library.zip
# members.  Their raw bytes cannot prove the absence of a source-level secret,
# so recognized formats are parsed with explicit resource limits and then their
# decompressed members are passed through the same byte scanner.
_MAX_FROZEN_CONTAINER_BYTES = 64 * 1024 * 1024
_MAX_FROZEN_CONTAINER_TOC_BYTES = 16 * 1024 * 1024
_MAX_FROZEN_CONTAINER_MEMBERS = 100_000
_MAX_FROZEN_MEMBER_BYTES = 64 * 1024 * 1024
_MAX_FROZEN_CONTAINER_TOTAL_BYTES = 64 * 1024 * 1024
_MAX_FROZEN_CONTAINER_DEPTH = 4
# A recursive traversal must not reset its aggregate allowance at every ZIP,
# CArchive, or PYZ boundary.  The outer package is intentionally larger than a
# frozen sub-container, so it receives an explicit, still finite, allowance.
_MAX_FROZEN_RECURSIVE_TOTAL_BYTES = 512 * 1024 * 1024
_MAX_FROZEN_RECURSIVE_MEMBERS = 200_000
_MAX_FINAL_PACKAGE_ZIP_BYTES = 1024 * 1024 * 1024
_MAX_FINAL_PACKAGE_MEMBER_BYTES = _MAX_SECRET_SCAN_BYTES_PER_INPUT
_MAX_FINAL_PACKAGE_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
_CARCHIVE_COOKIE_MAGIC = b"MEI\x0c\x0b\x0a\x0b\x0e"
_CARCHIVE_COOKIE_FORMAT = "!8sIIII64s"
_CARCHIVE_COOKIE_SIZE = struct.calcsize(_CARCHIVE_COOKIE_FORMAT)
_CARCHIVE_TOC_FORMAT = "!IIIIBc"
_CARCHIVE_TOC_HEADER_SIZE = struct.calcsize(_CARCHIVE_TOC_FORMAT)
_CARCHIVE_TYPECODES = frozenset({"b", "d", "z", "Z", "M", "m", "s", "x", "o", "l"})
_PYZ_MAGIC = b"PYZ\0"
_PYZ_HEADER_SIZE = 17
_PYINSTALLER_ENTRYPOINTS = frozenset({"pkv.exe", "pkv-mcp.exe"})

_ZIP_LOCAL_SIGNATURE = b"PK\x03\x04"
_ZIP_CENTRAL_SIGNATURE = b"PK\x01\x02"
_ZIP_EOCD_SIGNATURE = b"PK\x05\x06"
_ZIP_LOCAL_HEADER = struct.Struct("<IHHHHHIIIHH")
_ZIP_CENTRAL_HEADER = struct.Struct("<IHHHHHHIIIHHHHHII")
_ZIP_EOCD = struct.Struct("<IHHHHIIH")
_ZIP_ALLOWED_FLAGS = 0x0800  # UTF-8; descriptors/encryption are rejected.
_ZIP64_U16 = 0xFFFF
_ZIP64_U32 = 0xFFFFFFFF
_ZIP_LOCAL_SIGNATURE_VALUE = int.from_bytes(_ZIP_LOCAL_SIGNATURE, "little")
_ZIP_CENTRAL_SIGNATURE_VALUE = int.from_bytes(_ZIP_CENTRAL_SIGNATURE, "little")
_ZIP_EOCD_SIGNATURE_VALUE = int.from_bytes(_ZIP_EOCD_SIGNATURE, "little")


class _FrozenScanBudget:
    """One bounded allowance shared by all recursively expanded containers."""

    def __init__(self, *, total_limit: int, member_limit: int) -> None:
        self.total_limit = total_limit
        self.member_limit = member_limit
        self.total_bytes = 0
        self.members = 0

    def add_member(self, *, label: str) -> None:
        self.members += 1
        if self.members > self.member_limit:
            raise InternalPackageError(
                f"{label} exceeds the recursive frozen-member count limit"
            )

    def add_bytes(self, amount: int, *, label: str) -> None:
        if amount < 0:
            raise InternalPackageError(f"{label} has an invalid recursive scan size")
        self.total_bytes += amount
        if self.total_bytes > self.total_limit:
            raise InternalPackageError(
                f"{label} exceeds the recursive frozen scan size limit"
            )


def _new_frozen_scan_budget(
    *, total_limit: int | None = None, member_limit: int | None = None
) -> _FrozenScanBudget:
    return _FrozenScanBudget(
        total_limit=(
            _MAX_FROZEN_RECURSIVE_TOTAL_BYTES if total_limit is None else total_limit
        ),
        member_limit=(
            _MAX_FROZEN_RECURSIVE_MEMBERS if member_limit is None else member_limit
        ),
    )


class InternalPackageError(RuntimeError):
    """A fail-closed internal-package preflight or build failure."""


def _is_relative_to(candidate: Path, root: Path) -> bool:
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


def _lexical_path(path: Path) -> Path:
    """Return an absolute lexical path without reading an arbitrary payload."""

    return Path(os.path.abspath(os.path.normpath(path)))


def _existing_lstat(path: Path) -> os.stat_result | None:
    """Inspect one path component without following a link or junction."""

    try:
        return os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise InternalPackageError(f"cannot inspect internal package path {path}: {exc}") from exc


def _is_link_or_reparse(status: os.stat_result) -> bool:
    """Recognize POSIX symlinks and Windows junction/reparse-point entries."""

    attributes = getattr(status, "st_file_attributes", 0)
    return stat.S_ISLNK(status.st_mode) or bool(
        attributes & _WINDOWS_FILE_ATTRIBUTE_REPARSE_POINT
    )


def _path_chain(root: Path, candidate: Path) -> list[Path]:
    """Return lexical path components from ``root`` through ``candidate``."""

    relative = candidate.relative_to(root)
    chain = [root]
    current = root
    for part in relative.parts:
        current = current / part
        chain.append(current)
    return chain


def assert_safe_internal_output_path(
    project_root: Path,
    candidate: Path,
    *,
    label: str,
    require_directory: bool,
) -> Path:
    """Reject reparse-point output paths before an internal build writes them.

    Lexical containment alone is not enough on Windows: an existing ``dist``
    component can be a junction that redirects an otherwise-valid looking
    ``dist/internal`` path outside the checkout.  Check every existing path
    component with ``lstat`` so the check does not traverse the link itself.
    """

    project_root = _lexical_path(project_root)
    authority = _lexical_path(project_root / INTERNAL_OUTPUT_RELATIVE)
    candidate = _lexical_path(candidate)
    if not _is_relative_to(candidate, authority):
        raise InternalPackageError(
            f"{label} must stay below {authority}; refusing {candidate}"
        )

    chain = _path_chain(project_root, candidate)
    for index, current in enumerate(chain):
        status = _existing_lstat(current)
        if status is None:
            continue
        if _is_link_or_reparse(status):
            raise InternalPackageError(
                f"{label} contains a symlink, junction, or reparse point: {current}"
            )
        is_leaf = index == len(chain) - 1
        if not is_leaf and not stat.S_ISDIR(status.st_mode):
            raise InternalPackageError(f"{label} has a non-directory parent: {current}")
        if is_leaf and require_directory and not stat.S_ISDIR(status.st_mode):
            raise InternalPackageError(f"{label} must be a directory when it exists: {current}")
    return candidate


def resolve_project_root(value: str | None) -> Path:
    root = PROJECT_ROOT if value is None else Path(value)
    root = _lexical_path(root)
    if not (root / SPEC_RELATIVE).is_file():
        raise InternalPackageError(
            f"project root does not contain {SPEC_RELATIVE.as_posix()}: {root}"
        )
    return root


def resolve_internal_output_root(project_root: Path, value: str | None) -> Path:
    """Allow output only under this checkout's ignored ``dist/internal`` tree."""

    authority = _lexical_path(project_root / INTERNAL_OUTPUT_RELATIVE)
    requested = authority if value is None else Path(value)
    if not requested.is_absolute():
        requested = project_root / requested
    requested = _lexical_path(requested)
    if not _is_relative_to(requested, authority):
        raise InternalPackageError(
            "internal package output must stay below "
            f"{authority}; refusing {requested}"
        )
    if requested == authority.parent:
        raise InternalPackageError("internal package output cannot be dist itself")
    return assert_safe_internal_output_path(
        project_root,
        requested,
        label="internal package output",
        require_directory=True,
    )


def _run_git(project_root: Path, *arguments: str) -> str | None:
    try:
        completed = subprocess.run(
            ["git", *arguments],
            cwd=project_root,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()


def source_identity(project_root: Path) -> dict[str, Any]:
    """Record revision and dirty state without exposing changed-file names."""

    revision = _run_git(project_root, "rev-parse", "HEAD")
    porcelain = _run_git(project_root, "status", "--porcelain=v1")
    if revision is None and porcelain is None:
        return {"state": "unavailable", "revision": None, "dirty": None}
    return {
        "state": "dirty" if porcelain else "clean",
        "revision": revision,
        "dirty": bool(porcelain),
    }


def _declared_dependency_names(requirements_path: Path) -> list[str]:
    names: set[str] = {"pyinstaller"}
    for raw_line in requirements_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        match = re.match(r"^([A-Za-z0-9][A-Za-z0-9_.-]*)", line)
        if match:
            names.add(match.group(1).lower())
    return sorted(names)


def dependency_summary(project_root: Path) -> dict[str, Any]:
    """Return a compact build-machine dependency summary, not a lockfile."""

    versions: list[dict[str, str | None]] = []
    for name in _declared_dependency_names(project_root / "requirements.txt"):
        try:
            version = metadata.version(name)
        except metadata.PackageNotFoundError:
            version = None
        versions.append({"name": name, "version": version})
    return {
        "requirements_file": "requirements.txt",
        "requirements_sha256": _sha256_file(project_root / "requirements.txt"),
        "resolved": versions,
    }


def _sha256_file(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_runtime_manifest(project_root: Path) -> None:
    """Check that the build graph still has an explicit data-free allowlist."""

    manifest_path = project_root / RUNTIME_MANIFEST_RELATIVE
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InternalPackageError(
            f"cannot read runtime resource manifest: {manifest_path}"
        ) from exc

    if manifest.get("schema_version") != 2:
        raise InternalPackageError("unsupported runtime resource manifest schema")
    include_globs = manifest.get("include_globs")
    forbidden_globs = manifest.get("forbidden_globs")
    if not isinstance(include_globs, list) or not isinstance(forbidden_globs, list):
        raise InternalPackageError("runtime resource manifest is missing allow/deny lists")
    normalized_includes = [str(item).replace("\\", "/").casefold() for item in include_globs]
    if any("local.yaml" in item or ".env" in item for item in normalized_includes):
        raise InternalPackageError("runtime resource allowlist must never include local secrets")
    normalized_forbidden = [
        str(item).replace("\\", "/").casefold() for item in forbidden_globs
    ]
    required_denials = {"**/local.yaml", ".data/**", ".data-test/**", "vault/**", "logs/**"}
    if not required_denials.issubset(set(normalized_forbidden)):
        raise InternalPackageError(
            "runtime resource manifest is missing a required user-data denial"
        )


def _forbidden_reason(relative: Path) -> str | None:
    parts = [part.casefold() for part in relative.parts]
    filename = relative.name.casefold()
    stem = relative.stem.casefold()

    if any(part in _FORBIDDEN_SEGMENTS for part in parts):
        return "user-data, logs, or test-fixture path"
    if filename in _FORBIDDEN_FILENAMES or filename.startswith(".env."):
        return "local configuration or environment secret path"
    if relative.suffix.casefold() in _FORBIDDEN_SUFFIXES:
        return "database, index, or log payload"
    if filename.startswith("test_") or filename.endswith("_test.py"):
        return "test payload"
    if "fixture" in filename:
        return "test fixture payload"
    if stem in _SECRET_PATH_TERMS or any(
        term in filename for term in _SECRET_PATH_TERMS
    ):
        return "secret-bearing file path"
    return None


def _advance_pem_private_key_candidates(
    candidates: list[bytes], chunk: bytes, *, label: str
) -> list[bytes]:
    """Track complete textual PEM blocks across bounded stream chunks.

    A frozen dependency can legitimately carry the literal BEGIN/END markers
    used by its parser.  Treat only a marker pair enclosing an actual base64
    body as key material; malformed/non-textual candidates are discarded as
    parser strings, while an unreasonably large all-base64 candidate fails
    closed rather than escaping the overlap window.
    """

    remaining: list[bytes] = []
    for candidate in candidates:
        candidate += chunk
        if len(candidate) > _MAX_PEM_PRIVATE_KEY_BYTES:
            raise InternalPackageError(
                f"refusing probable credential material in {label}: PEM block exceeds limit"
            )
        footer = _PEM_PRIVATE_KEY_FOOTER.search(candidate)
        body = candidate[: footer.start()] if footer else candidate
        if any(byte not in _PEM_BODY_BYTES for byte in body):
            continue
        if footer:
            compact = bytes(
                byte for byte in body if byte not in b"\t\r\n "
            )
            if len(compact) >= 24:
                raise InternalPackageError(f"refusing probable credential material in {label}")
            continue
        remaining.append(candidate)
    return remaining


def _scan_stream_for_secret(stream: Any, *, label: str, initial: bytes = b"") -> None:
    """Search a byte stream in bounded memory, refusing uninspectable inputs."""

    scanned = 0
    overlap = b""
    pending = initial
    pem_candidates: list[bytes] = []
    while True:
        remaining = _MAX_SECRET_SCAN_BYTES_PER_INPUT - scanned
        if remaining <= 0:
            if stream.read(1):
                raise InternalPackageError(
                    f"refusing {label}: it exceeds the bounded credential scan limit"
                )
            return
        if pending:
            chunk = pending
            pending = b""
            if len(chunk) > remaining:
                raise InternalPackageError(
                    f"refusing {label}: it exceeds the bounded credential scan limit"
                )
        else:
            try:
                chunk = stream.read(min(_SECRET_SCAN_CHUNK_BYTES, remaining))
            except OSError as exc:
                raise InternalPackageError(f"cannot inspect {label}: {exc}") from exc
        if not chunk:
            return
        scanned += len(chunk)
        window = overlap + chunk
        if any(pattern.search(window) for pattern in _SECRET_VALUE_BYTE_PATTERNS):
            raise InternalPackageError(f"refusing probable credential material in {label}")
        pem_candidates = _advance_pem_private_key_candidates(
            pem_candidates, chunk, label=label
        )
        for header in _PEM_PRIVATE_KEY_HEADER.finditer(window):
            candidate = window[header.end() :]
            if len(candidate) > _MAX_PEM_PRIVATE_KEY_BYTES:
                raise InternalPackageError(
                    f"refusing probable credential material in {label}: PEM block exceeds limit"
                )
            footer = _PEM_PRIVATE_KEY_FOOTER.search(candidate)
            body = candidate[: footer.start()] if footer else candidate
            if any(byte not in _PEM_BODY_BYTES for byte in body):
                continue
            if footer:
                compact = bytes(
                    byte for byte in body if byte not in b"\t\r\n "
                )
                if len(compact) >= 24:
                    raise InternalPackageError(
                        f"refusing probable credential material in {label}"
                    )
            else:
                pem_candidates.append(candidate)
        overlap = window[-_SECRET_SCAN_OVERLAP_BYTES:]


def _scan_bytes_for_secret(data: bytes, *, label: str, limit: int) -> None:
    if len(data) > limit:
        raise InternalPackageError(f"refusing {label}: it exceeds the bounded scan limit")
    _scan_stream_for_secret(io.BytesIO(data), label=label)


def _scan_metadata_for_secret(data: bytes, *, label: str) -> None:
    """Apply the same credential policy to names, comments, and extra fields."""

    _scan_bytes_for_secret(
        data,
        label=label,
        limit=_MAX_FROZEN_CONTAINER_TOC_BYTES,
    )


def _bounded_zlib_decompress(
    stored: bytes,
    *,
    label: str,
    wbits: int | None = None,
    stored_limit: int | None = None,
    content_limit: int | None = None,
) -> bytes:
    """Inflate one known archive member without accepting trailing/bomb data."""

    stored_limit = _MAX_FROZEN_CONTAINER_BYTES if stored_limit is None else stored_limit
    content_limit = _MAX_FROZEN_MEMBER_BYTES if content_limit is None else content_limit
    if len(stored) > stored_limit:
        raise InternalPackageError(f"{label} exceeds the stored size limit")
    try:
        decompressor = zlib.decompressobj() if wbits is None else zlib.decompressobj(wbits)
        content = decompressor.decompress(stored, content_limit + 1)
        if len(content) > content_limit:
            raise InternalPackageError(f"{label} exceeds the uncompressed size limit")
        content += decompressor.flush(content_limit + 1 - len(content))
    except zlib.error as exc:
        raise InternalPackageError(f"{label} compression is invalid") from exc
    if (
        len(content) > content_limit
        or not decompressor.eof
        or decompressor.unused_data
        or decompressor.unconsumed_tail
    ):
        raise InternalPackageError(f"{label} has trailing, incomplete, or oversized data")
    return content


def _marshal_load_exact(data: bytes, *, label: str) -> Any:
    stream = io.BytesIO(data)
    try:
        value = marshal.load(stream)
    except (EOFError, TypeError, ValueError, MemoryError, RecursionError) as exc:
        raise InternalPackageError(f"{label} is not valid marshal data") from exc
    if stream.tell() != len(data):
        raise InternalPackageError(f"{label} has trailing marshal data")
    return value


class _PyzTocMarshalReader:
    """Small, schema-limited marshal reader for an untrusted PYZ TOC.

    ``marshal.load`` trusts collection lengths before the caller can validate
    them.  PYZ uses only a list of ``(name, (flag, offset, size))`` records, so
    accept the narrow scalar/list/tuple/reference subset needed for that
    format and reject every allocation-heavy marshal type.
    """

    _TYPE_REF = ord("r")
    _TYPE_INT = ord("i")
    _TYPE_INT64 = ord("I")
    _TYPE_TUPLE = ord("(")
    _TYPE_SMALL_TUPLE = ord(")")
    _TYPE_LIST = ord("[")
    _TYPE_UNICODE = ord("u")
    _TYPE_ASCII = ord("a")
    _TYPE_ASCII_INTERNED = ord("A")
    _TYPE_SHORT_ASCII = ord("z")
    _TYPE_SHORT_ASCII_INTERNED = ord("Z")

    def __init__(self, data: bytes, *, label: str) -> None:
        self._data = memoryview(data)
        self._label = label
        self._position = 0
        self._references: list[Any] = []
        self._nodes = 0
        self._max_nodes = _MAX_FROZEN_CONTAINER_MEMBERS * 8 + 1

    def _fail(self, message: str) -> None:
        raise InternalPackageError(f"{self._label} {message}")

    def _read(self, size: int) -> bytes:
        if size < 0 or self._position + size > len(self._data):
            self._fail("is truncated")
        result = self._data[self._position : self._position + size].tobytes()
        self._position += size
        return result

    def _read_i32(self) -> int:
        return struct.unpack("<i", self._read(4))[0]

    def _add_reference(self, value: Any) -> int:
        if len(self._references) >= self._max_nodes:
            self._fail("has too many marshal references")
        self._references.append(value)
        return len(self._references) - 1

    def _read_string(self, typecode: int) -> str:
        if typecode in {self._TYPE_SHORT_ASCII, self._TYPE_SHORT_ASCII_INTERNED}:
            size = self._read(1)[0]
        else:
            size = self._read_i32()
        if size < 0 or size > 4096:
            self._fail("has an oversized marshal string")
        raw = self._read(size)
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise InternalPackageError(f"{self._label} has a non-UTF-8 marshal string") from exc

    def read_object(self, *, depth: int = 0) -> Any:
        if depth > 32:
            self._fail("exceeds the marshal nesting limit")
        self._nodes += 1
        if self._nodes > self._max_nodes:
            self._fail("has too many marshal nodes")
        tag = self._read(1)[0]
        wants_reference = bool(tag & 0x80)
        typecode = tag & 0x7F
        if typecode == self._TYPE_REF:
            if wants_reference:
                self._fail("has an invalid marshal reference tag")
            index = self._read_i32()
            if index < 0 or index >= len(self._references):
                self._fail("has an invalid marshal reference")
            return self._references[index]
        if typecode == self._TYPE_INT:
            value: Any = self._read_i32()
            if wants_reference:
                self._add_reference(value)
            return value
        if typecode == self._TYPE_INT64:
            value = struct.unpack("<q", self._read(8))[0]
            if wants_reference:
                self._add_reference(value)
            return value
        if typecode in {
            self._TYPE_UNICODE,
            self._TYPE_ASCII,
            self._TYPE_ASCII_INTERNED,
            self._TYPE_SHORT_ASCII,
            self._TYPE_SHORT_ASCII_INTERNED,
        }:
            value = self._read_string(typecode)
            if wants_reference:
                self._add_reference(value)
            return value
        if typecode in {self._TYPE_TUPLE, self._TYPE_SMALL_TUPLE, self._TYPE_LIST}:
            size = self._read_i32() if typecode != self._TYPE_SMALL_TUPLE else self._read(1)[0]
            if size < 0 or size > _MAX_FROZEN_CONTAINER_MEMBERS:
                self._fail("has an invalid marshal collection length")
            values: list[Any] = []
            reference_index = self._add_reference(values) if wants_reference else None
            for _ in range(size):
                values.append(self.read_object(depth=depth + 1))
            value = values if typecode == self._TYPE_LIST else tuple(values)
            if reference_index is not None:
                self._references[reference_index] = value
            return value
        self._fail("uses an unsupported marshal type")

    def read_toc(self) -> Any:
        value = self.read_object()
        if self._position != len(self._data):
            self._fail("has trailing marshal data")
        return value


def _parse_pyz_toc(data: bytes, *, label: str) -> Any:
    """Parse a PYZ TOC without giving untrusted counts to ``marshal.load``."""

    return _PyzTocMarshalReader(data, label=label).read_toc()


def _normalize_frozen_member_name(name: str, *, label: str) -> str:
    """Validate a container member name using Windows-safe archive semantics."""

    normalized = name
    parts = normalized.split("/")
    if (
        not name
        or "\x00" in name
        or "\\" in name
        or normalized.startswith("/")
        or re.match(r"^[A-Za-z]:", normalized)
        or any(
            part in {"", ".", ".."}
            or ":" in part
            or part.endswith((".", " "))
            for part in parts
        )
    ):
        raise InternalPackageError(f"{label} has an unsafe member name")
    return normalized


def _scan_pyz_archive_bytes(
    data: bytes,
    *,
    label: str,
    depth: int,
    budget: _FrozenScanBudget | None = None,
) -> None:
    """Parse a PyInstaller PYZ and inspect every zlib-inflated marshal member."""

    if depth > _MAX_FROZEN_CONTAINER_DEPTH:
        raise InternalPackageError(f"{label} exceeds the nested archive depth limit")
    if len(data) < _PYZ_HEADER_SIZE or len(data) > _MAX_FROZEN_CONTAINER_BYTES:
        raise InternalPackageError(f"{label} size is outside accepted bounds")
    budget = budget or _new_frozen_scan_budget()
    budget.add_bytes(len(data), label=label)
    if (
        data[:4] != _PYZ_MAGIC
        or data[4:8] != importlib.util.MAGIC_NUMBER
        or data[12:_PYZ_HEADER_SIZE] != b"\0" * 5
    ):
        raise InternalPackageError(f"{label} header is invalid")
    try:
        toc_offset = struct.unpack("!i", data[8:12])[0]
    except struct.error as exc:
        raise InternalPackageError(f"{label} TOC offset is invalid") from exc
    toc_size = len(data) - toc_offset
    if (
        toc_offset < _PYZ_HEADER_SIZE
        or toc_offset >= len(data)
        or toc_size <= 0
        or toc_size > _MAX_FROZEN_CONTAINER_TOC_BYTES
    ):
        raise InternalPackageError(f"{label} TOC bounds are invalid")
    _scan_metadata_for_secret(data[toc_offset:], label=f"{label} TOC metadata")
    raw_toc = _parse_pyz_toc(data[toc_offset:], label=f"{label} TOC")
    if type(raw_toc) is not list or not raw_toc or len(raw_toc) > _MAX_FROZEN_CONTAINER_MEMBERS:
        raise InternalPackageError(f"{label} TOC root is invalid")

    expected_offset = _PYZ_HEADER_SIZE
    total_uncompressed = 0
    names: list[str] = []
    folded_names: set[str] = set()
    for raw_entry in raw_toc:
        if (
            not isinstance(raw_entry, tuple)
            or len(raw_entry) != 2
            or type(raw_entry[0]) is not str
            or not isinstance(raw_entry[1], tuple)
            or len(raw_entry[1]) != 3
            or any(type(value) is not int for value in raw_entry[1])
        ):
            raise InternalPackageError(f"{label} TOC entry is invalid")
        _scan_metadata_for_secret(
            raw_entry[0].encode("utf-8"), label=f"{label} TOC member name"
        )
        name = _normalize_frozen_member_name(raw_entry[0], label=label)
        flag, offset, stored_size = raw_entry[1]
        if (
            len(name.encode("utf-8")) > 4096
            or name.casefold() in folded_names
            or flag not in {0, 1, 3}
            or offset != expected_offset
            or stored_size < 0
            or stored_size > _MAX_FROZEN_MEMBER_BYTES
            or offset + stored_size > toc_offset
            or (flag == 3 and stored_size != 0)
            or (flag != 3 and stored_size == 0)
        ):
            raise InternalPackageError(f"{label} TOC entry bounds/name is invalid")
        stored = data[offset : offset + stored_size]
        budget.add_member(label=label)
        if flag == 3:
            content = b""
        else:
            content = _bounded_zlib_decompress(
                stored, label=f"{label} member {name!r}"
            )
            code = _marshal_load_exact(content, label=f"{label} member {name!r}")
            if not isinstance(code, types.CodeType):
                raise InternalPackageError(f"{label} member is not a code object")
            _scan_bytes_for_secret(
                content,
                label=f"{label} member: {name}",
                limit=_MAX_FROZEN_MEMBER_BYTES,
            )
        budget.add_bytes(len(content), label=label)
        total_uncompressed += len(content)
        if total_uncompressed > _MAX_FROZEN_CONTAINER_TOTAL_BYTES:
            raise InternalPackageError(f"{label} exceeds the total size limit")
        names.append(name)
        folded_names.add(name.casefold())
        expected_offset += stored_size
    if names != sorted(names, key=lambda item: item.encode("utf-8")) or expected_offset != toc_offset:
        raise InternalPackageError(f"{label} member ordering/data boundary is invalid")


def _scan_carchive_bytes(
    data: bytes,
    *,
    label: str,
    depth: int,
    budget: _FrozenScanBudget | None = None,
) -> None:
    """Parse a terminal PyInstaller CArchive and recursively inspect members."""

    if depth > _MAX_FROZEN_CONTAINER_DEPTH:
        raise InternalPackageError(f"{label} exceeds the nested archive depth limit")
    if len(data) <= _CARCHIVE_COOKIE_SIZE or len(data) > _MAX_FROZEN_CONTAINER_BYTES:
        raise InternalPackageError(
            f"{label} PyInstaller CArchive size is outside accepted bounds"
        )
    budget = budget or _new_frozen_scan_budget()
    budget.add_bytes(len(data), label=label)
    cookie_start = len(data) - _CARCHIVE_COOKIE_SIZE
    if data[cookie_start : cookie_start + 8] != _CARCHIVE_COOKIE_MAGIC:
        raise InternalPackageError(f"{label} has no terminal PyInstaller CArchive cookie")
    try:
        magic, archive_length, toc_offset, toc_length, _pyvers, raw_pylib = struct.unpack(
            _CARCHIVE_COOKIE_FORMAT, data[cookie_start:]
        )
    except struct.error as exc:
        raise InternalPackageError(f"{label} CArchive cookie is malformed") from exc
    archive_start = len(data) - archive_length
    if (
        magic != _CARCHIVE_COOKIE_MAGIC
        or archive_length <= _CARCHIVE_COOKIE_SIZE
        or archive_length > len(data)
        or archive_start <= 0
        or toc_offset < 0
        or toc_length <= 0
        or toc_length > _MAX_FROZEN_CONTAINER_TOC_BYTES
        or toc_offset + toc_length != archive_length - _CARCHIVE_COOKIE_SIZE
    ):
        raise InternalPackageError(f"{label} CArchive bounds are invalid")
    if not raw_pylib or b"\0" not in raw_pylib:
        raise InternalPackageError(f"{label} CArchive Python library field is invalid")
    pylib, padding = raw_pylib.split(b"\0", 1)
    if not pylib or any(padding):
        raise InternalPackageError(f"{label} CArchive Python library field is malformed")
    try:
        pylib.decode("ascii")
    except UnicodeDecodeError as exc:
        raise InternalPackageError(f"{label} CArchive Python library is not ASCII") from exc
    _scan_metadata_for_secret(pylib, label=f"{label} CArchive Python library")

    archive = data[archive_start:]
    toc_bytes = archive[toc_offset : toc_offset + toc_length]
    position = 0
    expected_offset = 0
    total_uncompressed = 0
    seen: set[str] = set()
    entries = 0
    while position < len(toc_bytes):
        if entries >= _MAX_FROZEN_CONTAINER_MEMBERS:
            raise InternalPackageError(f"{label} has too many CArchive entries")
        if len(toc_bytes) - position < _CARCHIVE_TOC_HEADER_SIZE:
            raise InternalPackageError(f"{label} CArchive TOC is truncated")
        try:
            entry_length, offset, stored_size, content_size, compressed, raw_type = struct.unpack(
                _CARCHIVE_TOC_FORMAT,
                toc_bytes[position : position + _CARCHIVE_TOC_HEADER_SIZE],
            )
        except struct.error as exc:
            raise InternalPackageError(f"{label} CArchive TOC entry is malformed") from exc
        if (
            entry_length < _CARCHIVE_TOC_HEADER_SIZE + 1
            or entry_length % 16 != 0
            or position + entry_length > len(toc_bytes)
            or compressed not in {0, 1}
            or offset != expected_offset
            or stored_size < 0
            or stored_size > toc_offset - offset
            or content_size > _MAX_FROZEN_MEMBER_BYTES
            or total_uncompressed + content_size > _MAX_FROZEN_CONTAINER_TOTAL_BYTES
        ):
            raise InternalPackageError(f"{label} CArchive TOC entry bounds are invalid")
        name_field = toc_bytes[
            position + _CARCHIVE_TOC_HEADER_SIZE : position + entry_length
        ]
        terminator = name_field.find(b"\0")
        if (
            terminator <= 0
            or terminator > 4096
            or any(name_field[terminator:])
        ):
            raise InternalPackageError(f"{label} CArchive TOC name padding is invalid")
        try:
            _scan_metadata_for_secret(
                name_field[:terminator], label=f"{label} CArchive member name"
            )
            name = _normalize_frozen_member_name(
                name_field[:terminator].decode("utf-8"), label=label
            )
            typecode = raw_type.decode("ascii")
        except UnicodeDecodeError as exc:
            raise InternalPackageError(f"{label} CArchive TOC text is invalid") from exc
        if typecode not in _CARCHIVE_TYPECODES:
            raise InternalPackageError(f"{label} CArchive has an unsupported entry type")
        if typecode != "o" and name.casefold() in seen:
            raise InternalPackageError(f"{label} CArchive has duplicate member names")
        seen.add(name.casefold())
        stored = archive[offset : offset + stored_size]
        if len(stored) != stored_size:
            raise InternalPackageError(f"{label} CArchive member is truncated")
        if compressed:
            content = _bounded_zlib_decompress(
                stored, label=f"{label} member {name!r}"
            )
        else:
            content = stored
        if len(content) != content_size:
            raise InternalPackageError(f"{label} CArchive member content length is invalid")
        budget.add_member(label=label)
        budget.add_bytes(len(content), label=label)
        _scan_bytes_for_secret(
            content,
            label=f"{label} member: {name}",
            limit=_MAX_FROZEN_MEMBER_BYTES,
        )
        if typecode in {"z", "Z"} and not content.startswith(_PYZ_MAGIC):
            raise InternalPackageError(
                f"{label} CArchive PYZ member has an invalid header"
            )
        # CArchive type codes describe PyInstaller's intended payload role,
        # not the only possible nested container.  Inspect every member by its
        # content/name so a data (x) entry cannot hide a ZIP/CArchive/PYZ.
        _scan_nested_frozen_container(
            content,
            name=name,
            label=f"{label} member {name!r}",
            depth=depth + 1,
            budget=budget,
        )
        total_uncompressed += len(content)
        expected_offset += stored_size
        position += entry_length
        entries += 1
    if position != len(toc_bytes) or expected_offset != toc_offset:
        raise InternalPackageError(f"{label} CArchive data/TOC boundary is invalid")


def _zip_terminal_eocd_offset(data: bytes, *, label: str) -> int:
    """Return one terminal EOCD offset; reject appended/ambiguous ZIP layouts."""

    if len(data) < _ZIP_EOCD.size:
        raise InternalPackageError(f"{label} ZIP data is truncated")
    start = max(0, len(data) - (_ZIP_EOCD.size + _ZIP64_U16))
    candidates: list[int] = []
    position = data.find(_ZIP_EOCD_SIGNATURE, start)
    while position >= 0:
        if position + _ZIP_EOCD.size <= len(data):
            try:
                fields = _ZIP_EOCD.unpack_from(data, position)
            except struct.error:
                fields = ()
            if fields and position + _ZIP_EOCD.size + fields[-1] == len(data):
                candidates.append(position)
        position = data.find(_ZIP_EOCD_SIGNATURE, position + 1)
    if len(candidates) != 1:
        raise InternalPackageError(f"{label} ZIP has no unique terminal end record")
    return candidates[0]


def _decode_zip_member_name(raw_name: bytes, *, flags: int, label: str) -> tuple[str, bool]:
    _scan_metadata_for_secret(raw_name, label=f"{label} ZIP member name")
    try:
        name = raw_name.decode("utf-8" if flags & 0x0800 else "cp437")
    except UnicodeDecodeError as exc:
        raise InternalPackageError(f"{label} ZIP member name is not decodable") from exc
    directory = name.endswith("/")
    normalized = _normalize_frozen_member_name(
        name[:-1] if directory else name, label=label
    )
    return normalized, directory


def _scan_zip_archive_bytes(
    data: bytes,
    *,
    label: str,
    depth: int,
    budget: _FrozenScanBudget | None = None,
    container_limit: int | None = None,
    member_limit: int | None = None,
    total_limit: int | None = None,
) -> None:
    """Strictly parse and inspect a ZIP without trusting ``zipfile`` offsets.

    CPython's ``zipfile`` deliberately adjusts offsets for self-extracting ZIPs
    and clips output to central-directory sizes.  Both behaviours can hide
    bytes from a content scanner, so verify the physical local/central layout
    and inflate raw DEFLATE streams ourselves before accepting a member.
    """

    if depth > _MAX_FROZEN_CONTAINER_DEPTH:
        raise InternalPackageError(f"{label} exceeds the nested archive depth limit")
    container_limit = (
        _MAX_FROZEN_CONTAINER_BYTES if container_limit is None else container_limit
    )
    member_limit = _MAX_FROZEN_MEMBER_BYTES if member_limit is None else member_limit
    total_limit = (
        _MAX_FROZEN_CONTAINER_TOTAL_BYTES if total_limit is None else total_limit
    )
    if len(data) > container_limit:
        raise InternalPackageError(f"{label} exceeds the ZIP size limit")
    budget = budget or _new_frozen_scan_budget()
    budget.add_bytes(len(data), label=label)

    eocd_offset = _zip_terminal_eocd_offset(data, label=label)
    try:
        (
            signature,
            disk_number,
            central_disk,
            entries_on_disk,
            entries_total,
            central_size,
            central_offset,
            comment_size,
        ) = _ZIP_EOCD.unpack_from(data, eocd_offset)
    except struct.error as exc:
        raise InternalPackageError(f"{label} ZIP end record is malformed") from exc
    if (
        signature != _ZIP_EOCD_SIGNATURE_VALUE
        or comment_size != len(data) - eocd_offset - _ZIP_EOCD.size
    ):
        raise InternalPackageError(f"{label} ZIP end record is invalid")
    _scan_metadata_for_secret(
        data[eocd_offset + _ZIP_EOCD.size :], label=f"{label} ZIP archive comment"
    )
    if central_offset + central_size != eocd_offset:
        raise InternalPackageError(
            f"{label} ZIP has a prefix, concatenated data, or non-canonical end-directory layout"
        )
    if (
        disk_number
        or central_disk
        or entries_on_disk != entries_total
        or entries_total in {_ZIP64_U16}
        or central_size == _ZIP64_U32
        or central_offset == _ZIP64_U32
        or not entries_total
        or entries_total > _MAX_FROZEN_CONTAINER_MEMBERS
        or central_size <= 0
        or central_size > _MAX_FROZEN_CONTAINER_TOC_BYTES
    ):
        raise InternalPackageError(f"{label} ZIP end-directory bounds are invalid")

    records: list[dict[str, Any]] = []
    central_cursor = central_offset
    seen: set[str] = set()
    total_declared = 0
    for _ in range(entries_total):
        if central_cursor + _ZIP_CENTRAL_HEADER.size > eocd_offset:
            raise InternalPackageError(f"{label} ZIP central directory is truncated")
        try:
            (
                signature,
                _version_made,
                version_needed,
                flags,
                method,
                mod_time,
                mod_date,
                crc32,
                compressed_size,
                uncompressed_size,
                name_size,
                extra_size,
                entry_comment_size,
                disk_start,
                _internal_attr,
                external_attr,
                local_offset,
            ) = _ZIP_CENTRAL_HEADER.unpack_from(data, central_cursor)
        except struct.error as exc:
            raise InternalPackageError(f"{label} ZIP central entry is malformed") from exc
        entry_end = (
            central_cursor
            + _ZIP_CENTRAL_HEADER.size
            + name_size
            + extra_size
            + entry_comment_size
        )
        if signature != _ZIP_CENTRAL_SIGNATURE_VALUE or entry_end > eocd_offset:
            raise InternalPackageError(f"{label} ZIP central entry bounds are invalid")
        raw_name_start = central_cursor + _ZIP_CENTRAL_HEADER.size
        raw_name = data[raw_name_start : raw_name_start + name_size]
        central_extra = data[
            raw_name_start + name_size : raw_name_start + name_size + extra_size
        ]
        entry_comment = data[
            raw_name_start + name_size + extra_size : entry_end
        ]
        _scan_metadata_for_secret(central_extra, label=f"{label} ZIP central extra")
        _scan_metadata_for_secret(entry_comment, label=f"{label} ZIP entry comment")
        if (
            flags & 0x1
            or flags & ~_ZIP_ALLOWED_FLAGS
            or method not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
            or disk_start not in {0}
            or compressed_size == _ZIP64_U32
            or uncompressed_size == _ZIP64_U32
            or local_offset == _ZIP64_U32
        ):
            if flags & 0x1:
                raise InternalPackageError(f"{label} ZIP contains an encrypted member")
            raise InternalPackageError(f"{label} ZIP uses unsupported flags, ZIP64, or compression")
        name, directory = _decode_zip_member_name(raw_name, flags=flags, label=label)
        if name.casefold() in seen:
            raise InternalPackageError(f"{label} ZIP has duplicate member names")
        seen.add(name.casefold())
        if stat.S_ISLNK(external_attr >> 16):
            raise InternalPackageError(f"{label} ZIP contains a link")
        if directory and (
            method != zipfile.ZIP_STORED
            or crc32
            or compressed_size
            or uncompressed_size
        ):
            raise InternalPackageError(f"{label} ZIP has a nonempty directory entry")
        if not directory:
            if uncompressed_size > member_limit:
                raise InternalPackageError(f"{label} ZIP member exceeds the size limit")
            total_declared += uncompressed_size
            if total_declared > total_limit:
                raise InternalPackageError(f"{label} ZIP exceeds the total size limit")
        records.append(
            {
                "central_offset": central_cursor,
                "version_needed": version_needed,
                "flags": flags,
                "method": method,
                "mod_time": mod_time,
                "mod_date": mod_date,
                "crc32": crc32,
                "compressed_size": compressed_size,
                "uncompressed_size": uncompressed_size,
                "raw_name": raw_name,
                "local_offset": local_offset,
                "name": name,
                "directory": directory,
            }
        )
        central_cursor = entry_end
    if central_cursor != eocd_offset:
        raise InternalPackageError(f"{label} ZIP central directory has unaccounted bytes")

    physical_cursor = 0
    for record in sorted(records, key=lambda item: int(item["local_offset"])):
        local_offset = int(record["local_offset"])
        if local_offset != physical_cursor or local_offset + _ZIP_LOCAL_HEADER.size > central_offset:
            raise InternalPackageError(
                f"{label} ZIP has a prefix, concatenated data, or non-canonical local layout"
            )
        try:
            (
                signature,
                local_version_needed,
                local_flags,
                local_method,
                local_mod_time,
                local_mod_date,
                local_crc32,
                local_compressed_size,
                local_uncompressed_size,
                local_name_size,
                local_extra_size,
            ) = _ZIP_LOCAL_HEADER.unpack_from(data, local_offset)
        except struct.error as exc:
            raise InternalPackageError(f"{label} ZIP local entry is malformed") from exc
        local_name_start = local_offset + _ZIP_LOCAL_HEADER.size
        data_start = local_name_start + local_name_size + local_extra_size
        data_end = data_start + int(record["compressed_size"])
        if (
            signature != _ZIP_LOCAL_SIGNATURE_VALUE
            or data_start > central_offset
            or data_end > central_offset
            or local_version_needed != record["version_needed"]
            or local_flags != record["flags"]
            or local_method != record["method"]
            or local_mod_time != record["mod_time"]
            or local_mod_date != record["mod_date"]
            or local_crc32 != record["crc32"]
            or local_compressed_size != record["compressed_size"]
            or local_uncompressed_size != record["uncompressed_size"]
            or data[local_name_start : local_name_start + local_name_size] != record["raw_name"]
        ):
            raise InternalPackageError(f"{label} ZIP local/central metadata is inconsistent")
        _scan_metadata_for_secret(
            data[local_name_start + local_name_size : data_start],
            label=f"{label} ZIP local extra",
        )
        record["data_start"] = data_start
        record["data_end"] = data_end
        physical_cursor = data_end
    if physical_cursor != central_offset:
        raise InternalPackageError(
            f"{label} ZIP has a prefix, concatenated data, or hidden local data"
        )

    for record in records:
        budget.add_member(label=label)
        if record["directory"]:
            continue
        data_start = int(record["data_start"])
        data_end = int(record["data_end"])
        stored = data[data_start:data_end]
        if record["method"] == zipfile.ZIP_STORED:
            content = stored
        else:
            content = _bounded_zlib_decompress(
                stored,
                label=f"{label} ZIP member {record['name']!r}",
                wbits=-zlib.MAX_WBITS,
                stored_limit=member_limit,
                content_limit=member_limit,
            )
        if len(content) != record["uncompressed_size"]:
            raise InternalPackageError(f"{label} ZIP member content length is invalid")
        if zlib.crc32(content) & 0xFFFFFFFF != record["crc32"]:
            raise InternalPackageError(f"{label} ZIP member checksum is invalid")
        budget.add_bytes(len(content), label=label)
        _scan_bytes_for_secret(
            content,
            label=f"{label} ZIP member: {record['name']}",
            limit=member_limit,
        )
        _scan_nested_frozen_container(
            content,
            name=str(record["name"]),
            label=f"{label} ZIP member {record['name']!r}",
            depth=depth + 1,
            budget=budget,
        )


def _scan_nested_frozen_container(
    data: bytes,
    *,
    name: str,
    label: str,
    depth: int,
    budget: _FrozenScanBudget | None = None,
) -> None:
    """Dispatch only documented/recognized frozen container formats."""

    filename = name.rsplit("/", 1)[-1].casefold()
    named_entrypoint = filename in _PYINSTALLER_ENTRYPOINTS
    terminal_carchive = _has_terminal_carchive_cookie(data)
    if named_entrypoint or terminal_carchive:
        _scan_carchive_bytes(data, label=label, depth=depth, budget=budget)
    elif data.startswith(_PYZ_MAGIC):
        _scan_pyz_archive_bytes(data, label=label, depth=depth, budget=budget)
    elif filename.endswith(".zip") or _looks_like_zip_container(data):
        _scan_zip_archive_bytes(data, label=label, depth=depth, budget=budget)


def _has_terminal_carchive_cookie(data: bytes) -> bool:
    """Recognize a CArchive by its terminal cookie without arbitrary zlib hunting."""

    return (
        len(data) >= _CARCHIVE_COOKIE_SIZE
        and data[-_CARCHIVE_COOKIE_SIZE : -_CARCHIVE_COOKIE_SIZE + 8]
        == _CARCHIVE_COOKIE_MAGIC
    )


def _looks_like_zip_container(data: bytes) -> bool:
    """Recognize a ZIP by terminal metadata too, including SFX/concatenations.

    Recognition is deliberately broader than acceptance: strict physical layout
    validation in ``_scan_zip_archive_bytes`` rejects prefixed/concatenated
    data instead of allowing ``zipfile`` to adjust offsets silently.
    """

    if data.startswith((_ZIP_LOCAL_SIGNATURE, _ZIP_EOCD_SIGNATURE)):
        return True
    try:
        return zipfile.is_zipfile(io.BytesIO(data))
    except (OSError, RuntimeError):
        return False


def _scan_regular_file_for_secret(path: Path, relative: Path) -> None:
    status = _existing_lstat(path)
    if status is None or not stat.S_ISREG(status.st_mode):
        raise InternalPackageError(
            f"package payload changed during inspection: {relative.as_posix()}"
        )
    if status.st_size > _MAX_SECRET_SCAN_BYTES_PER_INPUT:
        raise InternalPackageError(
            "refusing package file that exceeds the bounded credential scan limit: "
            f"{relative.as_posix()}"
        )
    try:
        with path.open("rb") as stream:
            _scan_stream_for_secret(
                stream,
                label=f"package file: {relative.as_posix()}",
            )
    except OSError as exc:
        raise InternalPackageError(f"cannot inspect package file {path}: {exc}") from exc


def _iter_payload_paths(payload_root: Path) -> Iterable[tuple[Path, Path, os.stat_result]]:
    """Walk a payload without traversing symlinks, junctions, or special files."""

    root_status = _existing_lstat(payload_root)
    if root_status is None or not stat.S_ISDIR(root_status.st_mode):
        raise InternalPackageError(f"package payload is missing: {payload_root}")
    if _is_link_or_reparse(root_status):
        raise InternalPackageError(f"package payload contains a link: {payload_root}")

    pending = [payload_root]
    while pending:
        current = pending.pop()
        try:
            children = sorted(current.iterdir(), key=lambda item: item.as_posix())
        except OSError as exc:
            raise InternalPackageError(f"cannot inspect package directory {current}: {exc}") from exc
        for path in children:
            relative = path.relative_to(payload_root)
            status = _existing_lstat(path)
            if status is None:
                raise InternalPackageError(
                    f"package payload changed during inspection: {relative.as_posix()}"
                )
            if _is_link_or_reparse(status):
                raise InternalPackageError(f"package payload contains a link: {relative}")
            if stat.S_ISDIR(status.st_mode):
                pending.append(path)
            elif not stat.S_ISREG(status.st_mode):
                raise InternalPackageError(
                    f"package payload contains a non-regular file: {relative.as_posix()}"
                )
            yield path, relative, status


def assert_payload_safe(payload_root: Path) -> None:
    """Fail closed if the final payload has private data, logs, or test inputs."""

    budget = _new_frozen_scan_budget()
    for path, relative, status in _iter_payload_paths(payload_root):
        _scan_metadata_for_secret(
            relative.as_posix().encode("utf-8"),
            label=f"package path metadata: {relative.as_posix()}",
        )
        reason = _forbidden_reason(relative)
        if reason is not None:
            raise InternalPackageError(f"refusing {reason}: {relative.as_posix()}")
        if stat.S_ISREG(status.st_mode):
            _scan_regular_file_for_secret(path, relative)
            filename = relative.name.casefold()
            if (
                filename in _PYINSTALLER_ENTRYPOINTS
                or filename.endswith(".zip")
                or _file_has_frozen_container_magic(path)
                or _file_has_terminal_carchive_cookie(path)
                or _file_looks_like_zip_container(path)
            ):
                if status.st_size > _MAX_FROZEN_CONTAINER_BYTES:
                    raise InternalPackageError(
                        "refusing frozen package file that exceeds the bounded archive scan limit: "
                        f"{relative.as_posix()}"
                    )
                try:
                    data = path.read_bytes()
                except OSError as exc:
                    raise InternalPackageError(
                        f"cannot inspect frozen package file {relative.as_posix()}"
                    ) from exc
                _scan_nested_frozen_container(
                    data,
                    name=relative.as_posix(),
                    label=f"package file: {relative.as_posix()}",
                    depth=0,
                    budget=budget,
                )


def _file_has_frozen_container_magic(path: Path) -> bool:
    try:
        with path.open("rb") as stream:
            return stream.read(4) in {_PYZ_MAGIC, _ZIP_LOCAL_SIGNATURE, _ZIP_EOCD_SIGNATURE}
    except OSError as exc:
        raise InternalPackageError(f"cannot inspect package file {path}: {exc}") from exc


def _file_has_terminal_carchive_cookie(path: Path) -> bool:
    try:
        if path.stat().st_size < _CARCHIVE_COOKIE_SIZE:
            return False
        with path.open("rb") as stream:
            stream.seek(-_CARCHIVE_COOKIE_SIZE, os.SEEK_END)
            return stream.read(8) == _CARCHIVE_COOKIE_MAGIC
    except OSError as exc:
        raise InternalPackageError(f"cannot inspect package file {path}: {exc}") from exc


def _file_looks_like_zip_container(path: Path) -> bool:
    """Recognize SFX-like ZIPs on disk so they are strictly rejected/scanned."""

    try:
        with path.open("rb") as stream:
            # ``is_zipfile`` seeks only to ZIP metadata; do this before the
            # bounded full-read gate so an oversized arbitrary-name SFX/ZIP is
            # recognized and rejected rather than silently treated as opaque.
            return zipfile.is_zipfile(stream)
    except (OSError, RuntimeError) as exc:
        raise InternalPackageError(f"cannot inspect package file {path}: {exc}") from exc


def _marker_text() -> str:
    return """INTERNAL TEST ONLY

This onedir package and its ZIP are for the maintainer's local synthetic-data
self-test.  They are not a release, release candidate, installer, or
distribution artifact.  Do not put config/local.yaml, credentials, a Vault,
logs, databases, or test fixtures into this package.
"""


def build_metadata(
    *, project_root: Path, package_id: str, built_at: datetime, source: dict[str, Any]
) -> dict[str, Any]:
    return {
        "schema_version": "pkv.internal-build-info.v1",
        "classification": CLASSIFICATION,
        "package_id": package_id,
        "built_at_utc": built_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "source": source,
        "build_machine": {
            "python": sys.version,
            "implementation": platform.python_implementation(),
            "platform": platform.platform(),
            "machine": platform.machine(),
        },
        "dependencies": dependency_summary(project_root),
        "payload_policy": {
            "local_yaml": "forbidden",
            "credentials": "forbidden",
            "vault": "forbidden",
            "logs": "forbidden",
            "test_fixtures": "forbidden",
            "credential_content_scan": "bounded binary and nested PyInstaller/ZIP scan",
        },
    }


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _new_package_id(built_at: datetime, source: dict[str, Any]) -> str:
    revision = source.get("revision")
    suffix = str(revision)[:8] if isinstance(revision, str) and revision else "unknown"
    if source.get("dirty") is True:
        suffix = f"{suffix}-dirty"
    return (
        f"pkv-internal-{built_at.astimezone(UTC).strftime('%Y%m%dT%H%M%SZ')}"
        f"-{suffix}-{uuid.uuid4().hex[:8]}"
    )


def _sanitized_build_environment(work_root: Path) -> dict[str, str]:
    environment = dict(os.environ)
    for key in list(environment):
        folded = key.casefold()
        if (
            any(term in folded for term in ("api_key", "apikey", "token", "secret", "password", "credential"))
            or folded in {"http_proxy", "https_proxy", "all_proxy", "no_proxy"}
        ):
            environment.pop(key, None)
    environment["PYINSTALLER_CONFIG_DIR"] = str(work_root / "pyinstaller-config")
    return environment


def invoke_pyinstaller(
    *, project_root: Path, spec_path: Path, work_root: Path, stage_dist_root: Path
) -> None:
    """Use the checked-in onedir spec without invoking release tooling."""

    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--clean",
        "--noconfirm",
        "--workpath",
        str(work_root),
        "--distpath",
        str(stage_dist_root),
        str(spec_path),
    ]
    try:
        completed = subprocess.run(
            command,
            cwd=project_root,
            env=_sanitized_build_environment(work_root),
            check=False,
        )
    except OSError as exc:
        raise InternalPackageError(f"cannot launch PyInstaller: {exc}") from exc
    if completed.returncode != 0:
        raise InternalPackageError(f"PyInstaller failed with exit code {completed.returncode}")


def _archive_directory(source_root: Path, archive_path: Path) -> None:
    """Create a ZIP with exactly one named root, preserving the onedir layout."""

    root_name = source_root.name
    with zipfile.ZipFile(
        archive_path,
        mode="x",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=6,
    ) as archive:
        for path in sorted(source_root.rglob("*"), key=lambda item: item.as_posix()):
            relative = path.relative_to(source_root).as_posix()
            if path.is_dir():
                continue
            archive.write(path, arcname=f"{root_name}/{relative}")


def assert_zip_safe(archive_path: Path, package_id: str) -> None:
    """Verify ZIP paths and bytes cannot reintroduce prohibited material."""

    try:
        archive_bytes = archive_path.read_bytes()
    except OSError as exc:
        raise InternalPackageError(f"cannot verify internal package ZIP: {exc}") from exc
    budget = _new_frozen_scan_budget(
        total_limit=_MAX_FINAL_PACKAGE_TOTAL_BYTES,
        member_limit=_MAX_FROZEN_RECURSIVE_MEMBERS,
    )
    _scan_zip_archive_bytes(
        archive_bytes,
        label="internal package ZIP",
        depth=0,
        budget=budget,
        container_limit=_MAX_FINAL_PACKAGE_ZIP_BYTES,
        member_limit=_MAX_FINAL_PACKAGE_MEMBER_BYTES,
        total_limit=_MAX_FINAL_PACKAGE_TOTAL_BYTES,
    )
    try:
        archive = zipfile.ZipFile(io.BytesIO(archive_bytes), mode="r")
    except (OSError, zipfile.BadZipFile) as exc:
        raise InternalPackageError(f"cannot verify internal package ZIP: {exc}") from exc
    with archive:
        entries = archive.infolist()
        names = [entry.filename for entry in entries]
        if not entries:
            raise InternalPackageError("internal package ZIP is empty")
        seen: set[str] = set()
        for entry in entries:
            name = entry.filename
            directory = name.endswith("/")
            normalized = _normalize_frozen_member_name(
                name[:-1] if directory else name,
                label="internal package ZIP",
            )
            _scan_metadata_for_secret(
                name.encode("utf-8"), label="internal package ZIP path metadata"
            )
            parts = normalized.split("/")
            if not parts or parts[0] != package_id or any(
                part in {".", ".."} for part in parts
            ):
                raise InternalPackageError(
                    f"internal package ZIP violates its single-root contract: {name}"
                )
            folded = (normalized + ("/" if directory else "")).casefold()
            if folded in seen:
                raise InternalPackageError(
                    f"internal package ZIP has duplicate case-insensitive path: {name}"
                )
            seen.add(folded)
            relative = Path(*parts[1:])
            if relative.parts:
                reason = _forbidden_reason(relative)
                if reason is not None:
                    raise InternalPackageError(
                        f"refusing {reason} in ZIP: {relative.as_posix()}"
                    )
            mode = entry.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise InternalPackageError(f"internal package ZIP contains a link: {name}")
        expected = {
            f"{package_id}/{MARKER_FILENAME}",
            f"{package_id}/{INFO_FILENAME}",
            f"{package_id}/pkv/pkv.exe",
            f"{package_id}/pkv/pkv-mcp.exe",
        }
        if not expected.issubset(set(names)):
            raise InternalPackageError("internal package ZIP is missing a required onedir entrypoint")


def build_internal_package(
    *, project_root: Path, output_root: Path, now: datetime | None = None
) -> dict[str, Any]:
    """Build and verify an internal-only onedir tree and its companion ZIP."""

    validate_runtime_manifest(project_root)
    output_root = assert_safe_internal_output_path(
        project_root,
        output_root,
        label="internal package output",
        require_directory=True,
    )
    built_at = now or datetime.now(UTC)
    source = source_identity(project_root)
    package_id = _new_package_id(built_at, source)
    package_root = output_root / package_id
    assert_safe_internal_output_path(
        project_root,
        package_root,
        label="internal package output path",
        require_directory=False,
    )
    if package_root.exists():
        raise InternalPackageError(f"internal output path already exists: {package_root}")
    archive_path = output_root / f"{package_id}.zip"
    assert_safe_internal_output_path(
        project_root,
        archive_path,
        label="internal package ZIP path",
        require_directory=False,
    )
    if archive_path.exists():
        raise InternalPackageError(f"internal ZIP path already exists: {archive_path}")

    work_root = output_root / ".work" / package_id
    stage_dist_root = work_root / "dist"
    assert_safe_internal_output_path(
        project_root,
        work_root,
        label="internal package work path",
        require_directory=False,
    )
    assert_safe_internal_output_path(
        project_root,
        stage_dist_root,
        label="internal package staging path",
        require_directory=False,
    )
    output_root.mkdir(parents=True, exist_ok=True)
    # Re-check after creating the authority path and immediately before the
    # external builder receives a writable work/dist location.
    assert_safe_internal_output_path(
        project_root,
        output_root,
        label="internal package output",
        require_directory=True,
    )
    assert_safe_internal_output_path(
        project_root,
        work_root,
        label="internal package work path",
        require_directory=False,
    )
    invoke_pyinstaller(
        project_root=project_root,
        spec_path=project_root / SPEC_RELATIVE,
        work_root=work_root,
        stage_dist_root=stage_dist_root,
    )
    collected_root = stage_dist_root / "pkv"
    assert_safe_internal_output_path(
        project_root,
        collected_root,
        label="PyInstaller collected path",
        require_directory=True,
    )
    required_entrypoints = ("pkv.exe", "pkv-mcp.exe")
    if not collected_root.is_dir() or any(
        not (collected_root / executable).is_file() for executable in required_entrypoints
    ):
        raise InternalPackageError(
            "PyInstaller did not produce the required headless onedir tree"
        )

    # Inspect the builder's tree before copytree can follow a link/junction.
    for _path, _relative, _status in _iter_payload_paths(collected_root):
        pass
    assert_safe_internal_output_path(
        project_root,
        package_root,
        label="internal package output path",
        require_directory=False,
    )
    shutil.copytree(collected_root, package_root / "pkv")
    (package_root / MARKER_FILENAME).write_text(_marker_text(), encoding="utf-8")
    _write_json(
        package_root / INFO_FILENAME,
        build_metadata(
            project_root=project_root,
            package_id=package_id,
            built_at=built_at,
            source=source,
        ),
    )
    assert_payload_safe(package_root)
    assert_safe_internal_output_path(
        project_root,
        archive_path,
        label="internal package ZIP path",
        require_directory=False,
    )
    _archive_directory(package_root, archive_path)
    assert_zip_safe(archive_path, package_id)

    return {
        "classification": CLASSIFICATION,
        "package_id": package_id,
        "package_root": str(package_root),
        "zip_path": str(archive_path),
        "metadata_path": str(package_root / INFO_FILENAME),
        "payload_verified": True,
    }


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--project-root",
        default=None,
        help="repository root (defaults to this script's parent)",
    )
    parser.add_argument(
        "--output-root",
        default=None,
        help="must stay below dist/internal (defaults to dist/internal)",
    )
    return parser.parse_args(argv)


def main(argv: Iterable[str] | None = None) -> int:
    try:
        args = parse_args(argv)
        project_root = resolve_project_root(args.project_root)
        output_root = resolve_internal_output_root(project_root, args.output_root)
        result = build_internal_package(
            project_root=project_root,
            output_root=output_root,
        )
    except InternalPackageError as exc:
        print(f"internal package build failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
