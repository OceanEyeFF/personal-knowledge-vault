"""Contracts for the P1-A internal-only package helper.

These tests never invoke PyInstaller or an Artifact.  They lock down the
internal output authority and the fail-closed payload/ZIP refusal rules; the
actual onedir smoke is intentionally opt-in through the PowerShell entrypoint.
"""

from __future__ import annotations

import io
import importlib.util
import marshal
import os
import subprocess
import struct
import zipfile
import zlib
from pathlib import Path
from types import SimpleNamespace

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = PROJECT_ROOT / "scripts" / "build_internal_package.py"
SMOKE_SCRIPT_PATH = PROJECT_ROOT / "scripts" / "run-internal-package-smoke.ps1"
WORKSPACE_HELPER_PATH = PROJECT_ROOT / "scripts" / "internal-package-workspace.ps1"
SPEC = importlib.util.spec_from_file_location("build_internal_package", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
internal_package = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(internal_package)


def _pyz_bytes(source: bytes) -> bytes:
    code = compile(source, "embedded_secret.py", "exec", dont_inherit=True, optimize=0)
    content = marshal.dumps(code)
    stored = zlib.compress(content, 9)
    toc_offset = internal_package._PYZ_HEADER_SIZE + len(stored)
    toc = [("embedded_secret", (0, internal_package._PYZ_HEADER_SIZE, len(stored)))]
    return (
        internal_package._PYZ_MAGIC
        + importlib.util.MAGIC_NUMBER
        + struct.pack("!i", toc_offset)
        + b"\0" * 5
        + stored
        + marshal.dumps(toc)
    )


def _carchive_bytes(
    entries: list[tuple[str, bytes, str, bool]], *, compressed_trailing: bytes = b""
) -> bytes:
    """Write the bounded PyInstaller CArchive subset used by the scanner."""

    data_parts: list[bytes] = []
    toc_parts: list[bytes] = []
    offset = 0
    for name, content, typecode, compressed in entries:
        stored = (
            zlib.compress(content, 9) + compressed_trailing if compressed else content
        )
        encoded_name = name.encode("utf-8") + b"\0"
        entry_length = (
            (
                internal_package._CARCHIVE_TOC_HEADER_SIZE + len(encoded_name) + 15
            )
            // 16
            * 16
        )
        name_field = encoded_name + b"\0" * (
            entry_length - internal_package._CARCHIVE_TOC_HEADER_SIZE - len(encoded_name)
        )
        toc_parts.append(
            struct.pack(
                internal_package._CARCHIVE_TOC_FORMAT,
                entry_length,
                offset,
                len(stored),
                len(content),
                int(compressed),
                typecode.encode("ascii"),
            )
            + name_field
        )
        data_parts.append(stored)
        offset += len(stored)
    data = b"".join(data_parts)
    toc = b"".join(toc_parts)
    library = b"python311.dll\0" + b"\0" * (64 - len(b"python311.dll\0"))
    archive_length = len(data) + len(toc) + internal_package._CARCHIVE_COOKIE_SIZE
    cookie = struct.pack(
        internal_package._CARCHIVE_COOKIE_FORMAT,
        internal_package._CARCHIVE_COOKIE_MAGIC,
        archive_length,
        len(data),
        len(toc),
        311,
        library,
    )
    return b"MZ synthetic bootloader" + data + toc + cookie


def _innocuous_entrypoint() -> bytes:
    return _carchive_bytes(
        [("bootstrap", marshal.dumps(compile("answer = 42", "boot.py", "exec")), "m", True)]
    )


def _compressed_secret_carchive() -> bytes:
    secret = b"api_key=sk-abcdefghijklmnopqrstuvwx"
    pyz = _pyz_bytes(b"embedded = " + repr(secret).encode("ascii") + b"\n")
    frozen = _carchive_bytes([("PYZ.pyz", pyz, "z", True)])
    assert not any(
        pattern.search(frozen) for pattern in internal_package._SECRET_VALUE_BYTE_PATTERNS
    )
    return frozen


def _nested_zip_bytes(content: bytes, *, name: str = "hidden.pyc") -> bytes:
    nested = io.BytesIO()
    with zipfile.ZipFile(nested, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(name, content)
    result = nested.getvalue()
    assert not any(
        pattern.search(result) for pattern in internal_package._SECRET_VALUE_BYTE_PATTERNS
    )
    return result


def _zip_with_entries(
    entries: list[tuple[str, bytes]], *, compression: int = zipfile.ZIP_DEFLATED
) -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=compression) as archive:
        for name, content in entries:
            archive.writestr(name, content)
    return stream.getvalue()


def _concatenated_secret_zip() -> bytes:
    secret = b"api_key=sk-abcdefghijklmnopqrstuvwx"
    hidden = _nested_zip_bytes(b"\0" + secret + b"\0", name="hidden.py")
    clean = _nested_zip_bytes(b"clean", name="clean.py")
    combined = hidden + clean
    assert not any(
        pattern.search(combined) for pattern in internal_package._SECRET_VALUE_BYTE_PATTERNS
    )
    return combined


def _nonempty_directory_zip() -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("hidden/", b"not-a-directory")
    return stream.getvalue()


def _forged_stored_zip_with_hidden_suffix() -> bytes:
    secret = b"api_key=sk-abcdefghijklmnopqrstuvwx"
    original = b"SAFE" + secret
    data = bytearray(_zip_with_entries([("visible.bin", original)]))
    central = data.rfind(internal_package._ZIP_CENTRAL_SIGNATURE)
    assert central > 0
    safe_crc = zlib.crc32(b"SAFE") & 0xFFFFFFFF
    # Retain the full DEFLATE member, but lie about its expanded size and CRC.
    # ZipExtFile inflates then clips to the claimed four-byte size.
    struct.pack_into("<I", data, 14, safe_crc)
    struct.pack_into("<I", data, 22, 4)
    struct.pack_into("<I", data, central + 16, safe_crc)
    struct.pack_into("<I", data, central + 24, 4)
    assert not any(
        pattern.search(data) for pattern in internal_package._SECRET_VALUE_BYTE_PATTERNS
    )
    return bytes(data)


def _minimal_package(root: Path, package_id: str = "pkv-internal-test") -> Path:
    package = root / package_id
    app = package / "pkv"
    app.mkdir(parents=True)
    (package / internal_package.MARKER_FILENAME).write_text(
        "INTERNAL TEST ONLY\n", encoding="utf-8"
    )
    (package / internal_package.INFO_FILENAME).write_text("{}\n", encoding="utf-8")
    for name in ("pkv.exe", "pkv-mcp.exe"):
        (app / name).write_bytes(_innocuous_entrypoint())
    return package


def test_internal_output_root_rejects_release_and_external_paths(tmp_path: Path) -> None:
    expected = PROJECT_ROOT / "dist" / "internal"

    assert (
        internal_package.resolve_internal_output_root(PROJECT_ROOT, None) == expected
    )
    assert internal_package.resolve_internal_output_root(
        PROJECT_ROOT, "dist/internal/subtree"
    ) == expected / "subtree"

    for candidate in ("dist/release", str(tmp_path)):
        with pytest.raises(internal_package.InternalPackageError):
            internal_package.resolve_internal_output_root(PROJECT_ROOT, candidate)


def _pretend_reparse_point(monkeypatch: pytest.MonkeyPatch, target: Path) -> None:
    original_lstat = os.lstat
    target = internal_package._lexical_path(target)

    def fake_lstat(path: str | os.PathLike[str]) -> os.stat_result | SimpleNamespace:
        result = original_lstat(path)
        if internal_package._lexical_path(Path(path)) == target:
            return SimpleNamespace(
                st_mode=result.st_mode,
                st_size=result.st_size,
                st_file_attributes=internal_package._WINDOWS_FILE_ATTRIBUTE_REPARSE_POINT,
            )
        return result

    monkeypatch.setattr(internal_package.os, "lstat", fake_lstat)


def test_internal_output_root_rejects_reparse_point_ancestor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    internal = project / "dist" / "internal"
    internal.mkdir(parents=True)
    _pretend_reparse_point(monkeypatch, internal)

    with pytest.raises(internal_package.InternalPackageError, match="reparse point"):
        internal_package.resolve_internal_output_root(project, None)


def test_internal_work_path_rejects_existing_reparse_point(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    work_parent = project / "dist" / "internal" / ".work"
    work_parent.mkdir(parents=True)
    _pretend_reparse_point(monkeypatch, work_parent)

    with pytest.raises(internal_package.InternalPackageError, match="reparse point"):
        internal_package.assert_safe_internal_output_path(
            project,
            work_parent / "next-build",
            label="internal package work path",
            require_directory=False,
        )


def test_runtime_resource_manifest_keeps_private_data_denials() -> None:
    internal_package.validate_runtime_manifest(PROJECT_ROOT)


@pytest.mark.parametrize(
    "relative",
    [
        "local.yaml",
        ".env",
        "vault/real-note.md",
        "logs/application.log",
        "fixtures/sample.json",
        "tests/test_hidden.py",
        "test_payload.py",
        "db/knowledge.sqlite",
    ],
)
def test_payload_safety_refuses_private_or_test_material(
    tmp_path: Path, relative: str
) -> None:
    payload = _minimal_package(tmp_path)
    target = payload / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("synthetic", encoding="utf-8")

    with pytest.raises(internal_package.InternalPackageError):
        internal_package.assert_payload_safe(payload)


def test_payload_safety_refuses_probable_secret_value(tmp_path: Path) -> None:
    payload = _minimal_package(tmp_path)
    (payload / "config.txt").write_text(
        "api_key = sk-abcdefghijklmnopqrstuvwx\n", encoding="utf-8"
    )

    with pytest.raises(internal_package.InternalPackageError, match="credential"):
        internal_package.assert_payload_safe(payload)


def test_payload_safety_scans_binary_content_for_private_keys(tmp_path: Path) -> None:
    payload = _minimal_package(tmp_path)
    (payload / "pkv" / "frozen-data.bin").write_bytes(
        b"MZ\x00\xffembedded\x00-----BEGIN PRIVATE KEY-----\n"
        b"QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVo=\n"
        b"-----END PRIVATE KEY-----\x00"
    )

    with pytest.raises(internal_package.InternalPackageError, match="credential"):
        internal_package.assert_payload_safe(payload)


def test_secret_scanner_tracks_pem_body_beyond_chunk_overlap() -> None:
    pem = (
        b"-----BEGIN PRIVATE KEY-----\n"
        + b"A" * (internal_package._SECRET_SCAN_CHUNK_BYTES * 2)
        + b"\n-----END PRIVATE KEY-----"
    )

    with pytest.raises(internal_package.InternalPackageError, match="credential"):
        internal_package._scan_stream_for_secret(io.BytesIO(pem), label="large PEM")


def test_secret_scanner_allows_library_pem_marker_literals_without_body() -> None:
    parser_literals = (
        b"-----BEGIN OPENSSH PRIVATE KEY-----\xf3parser-token\xf3"
        b"-----END OPENSSH PRIVATE KEY-----"
    )

    internal_package._scan_stream_for_secret(io.BytesIO(parser_literals), label="parser literals")


@pytest.mark.parametrize(
    "entrypoint", ("pkv.exe", "pkv-mcp.exe")
)
def test_deep_scans_reject_compressed_secret_hidden_in_each_pyz_carchive(
    tmp_path: Path, entrypoint: str
) -> None:
    payload = _minimal_package(tmp_path)
    (payload / "pkv" / entrypoint).write_bytes(_compressed_secret_carchive())

    with pytest.raises(internal_package.InternalPackageError, match="credential"):
        internal_package.assert_payload_safe(payload)

    archive = tmp_path / f"{entrypoint}.zip"
    internal_package._archive_directory(payload, archive)
    with pytest.raises(internal_package.InternalPackageError, match="credential"):
        internal_package.assert_zip_safe(archive, payload.name)


def test_payload_safety_rejects_secret_hidden_in_nested_zip(tmp_path: Path) -> None:
    payload = _minimal_package(tmp_path)
    secret = b"api_key=sk-abcdefghijklmnopqrstuvwx"
    (payload / "pkv" / "base_library.zip").write_bytes(
        _nested_zip_bytes(b"\0" + secret + b"\0")
    )

    with pytest.raises(internal_package.InternalPackageError, match="credential"):
        internal_package.assert_payload_safe(payload)


@pytest.mark.parametrize("helper_name", ("helper.exe", "helper.bin"))
def test_deep_scanner_dispatches_terminal_carchive_for_any_filename(
    tmp_path: Path, helper_name: str
) -> None:
    package = _minimal_package(tmp_path)
    (package / "pkv" / helper_name).write_bytes(_compressed_secret_carchive())

    with pytest.raises(internal_package.InternalPackageError, match="credential"):
        internal_package.assert_payload_safe(package)

    archive = tmp_path / f"{helper_name}.zip"
    internal_package._archive_directory(package, archive)
    with pytest.raises(internal_package.InternalPackageError, match="credential"):
        internal_package.assert_zip_safe(archive, package.name)


def test_deep_scanner_expands_zip_in_carchive_data_member(tmp_path: Path) -> None:
    package = _minimal_package(tmp_path)
    secret = b"api_key=sk-abcdefghijklmnopqrstuvwx"
    nested = _nested_zip_bytes(b"\0" + secret + b"\0")
    # Type x is data, not a PYZ.  Content recognition must still recurse.
    (package / "pkv" / "helper.bin").write_bytes(
        _carchive_bytes([("opaque.bin", nested, "x", True)])
    )

    with pytest.raises(internal_package.InternalPackageError, match="credential"):
        internal_package.assert_payload_safe(package)

    archive = tmp_path / "nested-data.zip"
    internal_package._archive_directory(package, archive)
    with pytest.raises(internal_package.InternalPackageError, match="credential"):
        internal_package.assert_zip_safe(archive, package.name)


def test_deep_scanner_rejects_concatenated_zip_in_payload_and_outer_zip(
    tmp_path: Path,
) -> None:
    package = _minimal_package(tmp_path)
    (package / "pkv" / "base_library.zip").write_bytes(_concatenated_secret_zip())

    with pytest.raises(internal_package.InternalPackageError, match="ZIP"):
        internal_package.assert_payload_safe(package)

    archive = tmp_path / "concatenated-inner.zip"
    internal_package._archive_directory(package, archive)
    with pytest.raises(internal_package.InternalPackageError, match="ZIP"):
        internal_package.assert_zip_safe(archive, package.name)


def test_deep_scanner_rejects_prefixed_zip_under_arbitrary_filename(tmp_path: Path) -> None:
    package = _minimal_package(tmp_path)
    (package / "pkv" / "opaque.bin").write_bytes(
        b"MZ synthetic prefix" + _nested_zip_bytes(b"safe", name="safe.py")
    )

    with pytest.raises(internal_package.InternalPackageError, match="prefix"):
        internal_package.assert_payload_safe(package)

    archive = tmp_path / "prefixed-inner.zip"
    internal_package._archive_directory(package, archive)
    with pytest.raises(internal_package.InternalPackageError, match="prefix"):
        internal_package.assert_zip_safe(archive, package.name)


def test_oversized_arbitrary_name_sfx_zip_is_recognized_then_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = tmp_path / "payload"
    payload.mkdir()
    opaque = b"MZ synthetic prefix" + _nested_zip_bytes(b"safe", name="safe.py")
    (payload / "opaque.bin").write_bytes(opaque)
    monkeypatch.setattr(internal_package, "_MAX_FROZEN_CONTAINER_BYTES", len(opaque) - 1)

    with pytest.raises(internal_package.InternalPackageError, match="frozen package file"):
        internal_package.assert_payload_safe(payload)


def test_deep_scanner_rejects_nonempty_zip_directory_in_payload_and_outer_zip(
    tmp_path: Path,
) -> None:
    package = _minimal_package(tmp_path)
    (package / "pkv" / "base_library.zip").write_bytes(_nonempty_directory_zip())

    with pytest.raises(internal_package.InternalPackageError, match="nonempty directory"):
        internal_package.assert_payload_safe(package)

    archive = tmp_path / "directory-inner.zip"
    internal_package._archive_directory(package, archive)
    with pytest.raises(internal_package.InternalPackageError, match="nonempty directory"):
        internal_package.assert_zip_safe(archive, package.name)


def test_deep_scanner_rejects_zip_size_clipping_hidden_suffix(tmp_path: Path) -> None:
    package = _minimal_package(tmp_path)
    (package / "pkv" / "base_library.zip").write_bytes(_forged_stored_zip_with_hidden_suffix())

    with pytest.raises(internal_package.InternalPackageError, match="content length"):
        internal_package.assert_payload_safe(package)

    archive = tmp_path / "forged-inner.zip"
    internal_package._archive_directory(package, archive)
    with pytest.raises(internal_package.InternalPackageError, match="content length"):
        internal_package.assert_zip_safe(archive, package.name)


def test_zip_contract_rejects_secret_hidden_in_nested_base_library_zip(
    tmp_path: Path,
) -> None:
    package = _minimal_package(tmp_path)
    secret = b"api_key=sk-abcdefghijklmnopqrstuvwx"
    (package / "pkv" / "base_library.zip").write_bytes(
        _nested_zip_bytes(b"\0" + secret + b"\0")
    )
    archive = tmp_path / "package.zip"
    internal_package._archive_directory(package, archive)

    with pytest.raises(internal_package.InternalPackageError, match="credential"):
        internal_package.assert_zip_safe(archive, package.name)


def test_payload_safety_rejects_non_pyinstaller_entrypoint(tmp_path: Path) -> None:
    payload = _minimal_package(tmp_path)
    (payload / "pkv" / "pkv.exe").write_bytes(b"MZ not a PyInstaller executable")

    with pytest.raises(internal_package.InternalPackageError, match="CArchive"):
        internal_package.assert_payload_safe(payload)


def test_payload_safety_rejects_malformed_recognized_nested_zip(tmp_path: Path) -> None:
    payload = _minimal_package(tmp_path)
    (payload / "pkv" / "base_library.zip").write_bytes(b"PK\x03\x04truncated")

    with pytest.raises(internal_package.InternalPackageError, match="ZIP data is truncated"):
        internal_package.assert_payload_safe(payload)


def test_deep_scanner_rejects_corrupt_carchive_toc(tmp_path: Path) -> None:
    payload = _minimal_package(tmp_path)
    frozen = bytearray(_innocuous_entrypoint())
    cookie = len(frozen) - internal_package._CARCHIVE_COOKIE_SIZE
    struct.pack_into("!I", frozen, cookie + 16, 1)
    (payload / "pkv" / "pkv.exe").write_bytes(frozen)

    with pytest.raises(internal_package.InternalPackageError, match="CArchive bounds"):
        internal_package.assert_payload_safe(payload)


def test_deep_scanner_rejects_corrupt_pyz_marshal_toc(tmp_path: Path) -> None:
    payload = _minimal_package(tmp_path)
    pyz = _pyz_bytes(b"answer = 42\n")
    toc_offset = struct.unpack("!i", pyz[8:12])[0]
    malformed_pyz = pyz[:toc_offset] + b"c"
    (payload / "pkv" / "pkv.exe").write_bytes(
        _carchive_bytes([("PYZ.pyz", malformed_pyz, "z", False)])
    )

    with pytest.raises(internal_package.InternalPackageError, match="unsupported marshal"):
        internal_package.assert_payload_safe(payload)


def test_deep_scanner_rejects_trailing_carchive_compressed_member(
    tmp_path: Path,
) -> None:
    payload = _minimal_package(tmp_path)
    bootstrap = marshal.dumps(compile("answer = 42", "boot.py", "exec"))
    (payload / "pkv" / "pkv-mcp.exe").write_bytes(
        _carchive_bytes(
            [("bootstrap", bootstrap, "m", True)], compressed_trailing=b"hidden"
        )
    )

    with pytest.raises(internal_package.InternalPackageError, match="trailing, incomplete"):
        internal_package.assert_payload_safe(payload)


def test_deep_scanner_enforces_lowered_member_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frozen = _carchive_bytes([("payload", b"bounded", "x", False)])
    monkeypatch.setattr(internal_package, "_MAX_FROZEN_MEMBER_BYTES", 4)

    with pytest.raises(internal_package.InternalPackageError, match="TOC entry bounds"):
        internal_package._scan_carchive_bytes(frozen, label="limit fixture", depth=0)


def test_deep_scanner_enforces_lowered_nested_depth_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    nested = _nested_zip_bytes(b"safe")
    outer = _nested_zip_bytes(nested, name="inner.zip")
    monkeypatch.setattr(internal_package, "_MAX_FROZEN_CONTAINER_DEPTH", 0)

    with pytest.raises(internal_package.InternalPackageError, match="nested archive depth"):
        internal_package._scan_zip_archive_bytes(outer, label="depth fixture", depth=0)


def test_deep_scanner_enforces_shared_recursive_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frozen = _carchive_bytes(
        [("first", b"one", "x", False), ("second", b"two", "x", False)]
    )
    # One CArchive accepts two entries locally.  A single shared budget must
    # still stop the second sibling rather than resetting per member/container.
    monkeypatch.setattr(internal_package, "_MAX_FROZEN_RECURSIVE_MEMBERS", 1)

    with pytest.raises(internal_package.InternalPackageError, match="recursive frozen-member"):
        internal_package._scan_carchive_bytes(frozen, label="shared budget", depth=0)


def test_deep_scanner_enforces_shared_recursive_byte_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frozen = _carchive_bytes(
        [("first", b"one", "x", False), ("second", b"two", "x", False)]
    )
    # The archive input itself plus one member fits; the second sibling exceeds
    # the same traversal's allowance instead of starting a fresh total.
    monkeypatch.setattr(
        internal_package, "_MAX_FROZEN_RECURSIVE_TOTAL_BYTES", len(frozen) + 3
    )

    with pytest.raises(internal_package.InternalPackageError, match="recursive frozen scan"):
        internal_package._scan_carchive_bytes(frozen, label="shared byte budget", depth=0)


def test_pyz_toc_reader_rejects_huge_collection_before_marshal_load() -> None:
    malformed = bytes([ord("[") | 0x80]) + struct.pack(
        "<i", internal_package._MAX_FROZEN_CONTAINER_MEMBERS + 1
    )

    with pytest.raises(internal_package.InternalPackageError, match="collection length"):
        internal_package._parse_pyz_toc(malformed, label="oversized PYZ TOC")


def test_deep_scanner_rejects_unsupported_carchive_type() -> None:
    frozen = _carchive_bytes([("unsupported", b"safe", "q", False)])

    with pytest.raises(internal_package.InternalPackageError, match="unsupported entry type"):
        internal_package._scan_carchive_bytes(frozen, label="type fixture", depth=0)


def test_deep_scanner_rejects_non_pyz_carchive_pyz_type() -> None:
    frozen = _carchive_bytes([("PYZ.pyz", b"not a PYZ", "z", False)])

    with pytest.raises(internal_package.InternalPackageError, match="PYZ member has an invalid header"):
        internal_package._scan_carchive_bytes(frozen, label="PYZ type fixture", depth=0)


def test_zip_contract_requires_one_safe_internal_root(tmp_path: Path) -> None:
    package = _minimal_package(tmp_path)
    internal_package.assert_payload_safe(package)
    archive = tmp_path / "package.zip"
    internal_package._archive_directory(package, archive)

    internal_package.assert_zip_safe(archive, package.name)

    unsafe = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(unsafe, "w") as stream:
        stream.writestr(f"{package.name}/local.yaml", "not allowed")
    with pytest.raises(internal_package.InternalPackageError, match="local configuration"):
        internal_package.assert_zip_safe(unsafe, package.name)


def test_zip_contract_scans_binary_entries_for_private_keys(tmp_path: Path) -> None:
    package = _minimal_package(tmp_path)
    archive = tmp_path / "package.zip"
    internal_package._archive_directory(package, archive)
    with zipfile.ZipFile(archive, "a") as stream:
        stream.writestr(
            f"{package.name}/pkv/frozen-data.bin",
            b"MZ\x00\x80-----BEGIN PRIVATE KEY-----\n"
            b"QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVo=\n"
            b"-----END PRIVATE KEY-----\x00",
        )

    with pytest.raises(internal_package.InternalPackageError, match="credential"):
        internal_package.assert_zip_safe(archive, package.name)


def test_payload_and_zip_refuse_secret_in_filename_metadata(tmp_path: Path) -> None:
    package = _minimal_package(tmp_path)
    (package / "pkv" / "sk-abcdefghijklmnopqrstuvwx.bin").write_bytes(b"safe")

    with pytest.raises(internal_package.InternalPackageError, match="credential"):
        internal_package.assert_payload_safe(package)

    archive = tmp_path / "secret-name.zip"
    internal_package._archive_directory(package, archive)
    with pytest.raises(internal_package.InternalPackageError, match="credential"):
        internal_package.assert_zip_safe(archive, package.name)


def test_deep_scanner_refuses_secret_in_carchive_member_metadata() -> None:
    frozen = _carchive_bytes(
        [("sk-abcdefghijklmnopqrstuvwx.bin", b"safe", "x", False)]
    )

    with pytest.raises(internal_package.InternalPackageError, match="credential"):
        internal_package._scan_carchive_bytes(frozen, label="CArchive name", depth=0)


def test_zip_contract_scans_archive_comment_metadata(tmp_path: Path) -> None:
    package = _minimal_package(tmp_path)
    archive = tmp_path / "comment.zip"
    internal_package._archive_directory(package, archive)
    with zipfile.ZipFile(archive, "a") as stream:
        stream.comment = b"api_key=sk-abcdefghijklmnopqrstuvwx"

    with pytest.raises(internal_package.InternalPackageError, match="credential"):
        internal_package.assert_zip_safe(archive, package.name)


@pytest.mark.parametrize("metadata_field", ("comment", "extra"))
def test_zip_contract_scans_entry_comment_and_extra_metadata(
    tmp_path: Path, metadata_field: str
) -> None:
    package = _minimal_package(tmp_path)
    archive = tmp_path / f"entry-{metadata_field}.zip"
    internal_package._archive_directory(package, archive)
    secret = b"api_key=sk-abcdefghijklmnopqrstuvwx"
    info = zipfile.ZipInfo(f"{package.name}/pkv/safe.bin")
    if metadata_field == "comment":
        info.comment = secret
    else:
        info.extra = struct.pack("<HH", 0xCAFE, len(secret)) + secret
    with zipfile.ZipFile(archive, "a") as stream:
        stream.writestr(info, b"safe")

    with pytest.raises(internal_package.InternalPackageError, match="credential"):
        internal_package.assert_zip_safe(archive, package.name)


@pytest.mark.parametrize("unsafe_name", ("local.yaml:stream", "local.yaml.", "local.yaml "))
def test_zip_contract_rejects_windows_normalization_aliases(
    tmp_path: Path, unsafe_name: str
) -> None:
    package = _minimal_package(tmp_path)
    archive = tmp_path / "unsafe-name.zip"
    internal_package._archive_directory(package, archive)
    with zipfile.ZipFile(archive, "a") as stream:
        stream.writestr(f"{package.name}/pkv/{unsafe_name}", b"safe")

    with pytest.raises(internal_package.InternalPackageError, match="unsafe member name"):
        internal_package.assert_zip_safe(archive, package.name)


def test_smoke_child_environment_is_explicit_and_credential_free() -> None:
    source = SMOKE_SCRIPT_PATH.read_text(encoding="utf-8-sig")
    start = source.index("function New-IsolatedProcessStartInfo")
    end = source.index("\nfunction Get-ShortProcessOutput", start)
    block = source[start:end]

    clear = "$info.EnvironmentVariables.Clear()"
    assert clear in block
    assert block.index(clear) < block.index("$info.EnvironmentVariables['PKV_TEST_OFFLINE']")
    assert "foreach ($key in @($info.EnvironmentVariables.Keys))" not in block
    assert "EnvironmentVariables.Remove" not in block
    assert "PKV_TEST_OFFLINE" in block
    assert "PKV_TEST_LOAD_LOCAL" in block
    assert "PKV_RUN_LIVE" in block
    assert "SystemRoot" in block
    assert "ComSpec" in block
    assert "PATHEXT" in block
    assert "PATH" in block
    assert "TMPDIR" in block

    for forbidden in (
        "HOME",
        "HOMEDRIVE",
        "HOMEPATH",
        "PKV_DATA_ROOT",
        "AWS_ACCESS_KEY_ID",
        "AZURE_OPENAI_KEY",
        "QT_PLUGIN_PATH",
        "_PYI_APPLICATION_HOME_DIR",
    ):
        assert f"$info.EnvironmentVariables['{forbidden}']" not in block


def test_workspace_cleanup_is_no_follow_and_fail_closed() -> None:
    helper = WORKSPACE_HELPER_PATH.read_text(encoding="utf-8-sig")
    smoke = SMOKE_SCRIPT_PATH.read_text(encoding="utf-8-sig")

    assert "Remove-Item -LiteralPath $target -Recurse" not in helper
    assert "[System.IO.Directory]::Delete($candidate, $false)" in helper
    assert "FileAttributes]::ReparsePoint" in helper
    assert "Remove-InternalWorkspaceSafely" in smoke
    assert "SilentlyContinue" not in smoke[smoke.index("} finally {") :]


def _ps_literal(path: Path) -> str:
    return "'" + str(path).replace("'", "''") + "'"


def _run_workspace_command(command: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            command,
        ],
        check=False,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


@pytest.mark.skipif(os.name != "nt", reason="PowerShell parser contract is Windows-only")
def test_internal_package_powershell_helpers_parse() -> None:
    """Keep the opt-in smoke lane parseable before a costly package build."""

    for script_path in (SMOKE_SCRIPT_PATH, WORKSPACE_HELPER_PATH):
        command = (
            "$tokens = $null; $errors = $null; "
            "[void][System.Management.Automation.Language.Parser]::ParseFile("
            f"{_ps_literal(script_path)}, [ref]$tokens, [ref]$errors); "
            "if ($errors.Count -gt 0) { "
            "$errors | ForEach-Object { Write-Error $_.Message }; exit 1 }"
        )
        result = _run_workspace_command(command)
        assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(os.name != "nt", reason="PowerShell reparse contract is Windows-only")
def test_workspace_cleanup_refuses_junction_descendant_without_touching_target(
    tmp_path: Path,
) -> None:
    forbidden = tmp_path / "repository"
    workspace = tmp_path / ".pkv-internal-smoke-junction"
    target = tmp_path / "junction-target"
    forbidden.mkdir()
    workspace.mkdir()
    target.mkdir()
    canary = target / "must-survive.txt"
    canary.write_text("synthetic", encoding="utf-8")
    junction = workspace / "child-junction"
    helper = _ps_literal(WORKSPACE_HELPER_PATH)
    command = (
        f". {helper}; "
        f"$link={_ps_literal(junction)}; $target={_ps_literal(target)}; "
        "try { "
        "[void](New-Item -ItemType Junction -Path $link -Target $target -ErrorAction Stop); "
        f"Remove-InternalWorkspaceSafely -Path {_ps_literal(workspace)} "
        f"-ForbiddenRoot {_ps_literal(forbidden)} "
        "-RequiredLeafPrefix '.pkv-internal-smoke-'; exit 9 "
        "} catch { Write-Output $_.Exception.Message; exit 0 } "
        "finally { "
        "$item=Get-Item -LiteralPath $link -Force -ErrorAction SilentlyContinue; "
        "if($null -ne $item -and "
        "($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) { "
        "[System.IO.Directory]::Delete($link, $false) } }"
    )

    result = _run_workspace_command(command)

    if "privilege" in (result.stdout + result.stderr).lower():
        pytest.skip("junction creation is unavailable on this Windows runner")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "reparse point" in (result.stdout + result.stderr).lower()
    assert workspace.is_dir()
    assert canary.read_text(encoding="utf-8") == "synthetic"

    cleanup = _run_workspace_command(
        f". {helper}; Remove-InternalWorkspaceSafely -Path {_ps_literal(workspace)} "
        f"-ForbiddenRoot {_ps_literal(forbidden)} "
        "-RequiredLeafPrefix '.pkv-internal-smoke-'"
    )
    assert cleanup.returncode == 0, cleanup.stdout + cleanup.stderr
    assert not workspace.exists()
    assert canary.exists()


def test_metadata_records_classification_source_and_toolchain() -> None:
    metadata = internal_package.build_metadata(
        project_root=PROJECT_ROOT,
        package_id="pkv-internal-test",
        built_at=internal_package.datetime(2026, 8, 13, tzinfo=internal_package.UTC),
        source={"state": "dirty", "revision": "a" * 40, "dirty": True},
    )

    assert metadata["classification"] == "INTERNAL TEST ONLY"
    assert metadata["source"] == {
        "state": "dirty",
        "revision": "a" * 40,
        "dirty": True,
    }
    assert metadata["build_machine"]["python"]
    assert metadata["dependencies"]["resolved"]
    assert metadata["payload_policy"]["credentials"] == "forbidden"
