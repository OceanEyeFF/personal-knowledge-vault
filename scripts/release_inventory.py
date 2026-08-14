"""Build a fail-closed inventory from a PyInstaller Analysis and COLLECT tree.

The inventory is deliberately independent from PyInstaller's Python API.  The
``Analysis-00.toc`` file is parsed with :func:`ast.literal_eval`, physical
sources are constrained to caller-named roots, and every file in the COLLECT
tree is bound to either an Analysis destination or an explicitly named
PyInstaller bootloader executable.

Absolute build paths and the raw TOC hash are never emitted.  Instead, source
paths use stable ``<root-label>/<relative-path>`` references and the returned
TOC identity is a portable graph hash.  A caller that needs a local-only raw
diagnostic may hash the TOC separately, outside the reproducible artifact.
"""

from __future__ import annotations

import ast
import argparse
import hashlib
import io
import importlib.util
import importlib.metadata
import json
import marshal
import os
import platform
import re
import sqlite3
import ssl
import stat
import struct
import sys
import types
import zlib
from collections import defaultdict
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urlsplit


SCHEMA_VERSION = "pkv.release-inventory.v1"
CONDA_REGISTRY_SCHEMA_VERSION = "pkv.conda-native-registry.v1"
CONDA_REGISTRY_GENERATOR = {
    "algorithm": "conda-meta-paths-data-exact-v1",
    "file_selection": (
        "all paths_data files from every installed conda package owning at least "
        "one native Library/bin file"
    ),
    "package_selection": (
        "conda-meta records with a .dll/.pyd/.so/.dylib path below Library/bin"
    ),
    "version": 1,
}
MAX_TOC_BYTES = 64 * 1024 * 1024
MAX_CARCHIVE_PACKAGE_BYTES = 256 * 1024 * 1024
MAX_BOOTLOADER_PREFIX_BYTES = 32 * 1024 * 1024
MAX_EXECUTABLE_BYTES = MAX_CARCHIVE_PACKAGE_BYTES + MAX_BOOTLOADER_PREFIX_BYTES
MAX_CARCHIVE_TOC_BYTES = 16 * 1024 * 1024
MAX_CARCHIVE_ENTRIES = 100_000
MAX_CARCHIVE_NAME_BYTES = 4096
MAX_ARCHIVE_MEMBER_BYTES = 64 * 1024 * 1024
MAX_ARCHIVE_TOTAL_UNCOMPRESSED_BYTES = 64 * 1024 * 1024
MAX_PYZ_BYTES = 256 * 1024 * 1024
MAX_PYZ_TOC_BYTES = 16 * 1024 * 1024
MAX_PYZ_MEMBERS = 100_000
MAX_PYZ_NAME_BYTES = 4096
WINDOWS_REPARSE_POINT = 0x400

CARCHIVE_COOKIE_MAGIC = b"MEI\x0c\x0b\x0a\x0b\x0e"
CARCHIVE_COOKIE_FORMAT = "!8sIIII64s"
CARCHIVE_COOKIE_SIZE = struct.calcsize(CARCHIVE_COOKIE_FORMAT)
CARCHIVE_TOC_FORMAT = "!IIIIBc"
CARCHIVE_TOC_HEADER_SIZE = struct.calcsize(CARCHIVE_TOC_FORMAT)
PYZ_MAGIC = b"PYZ\0"
PYZ_HEADER_SIZE = 17
CARCHIVE_TYPE_BY_PKG_TYPE = {
    "BINARY": "b",
    "DATA": "x",
    "EXTENSION": "b",
    "OPTION": "o",
    "PYMODULE": "m",
    "PYSOURCE": "s",
    "PYZ": "z",
}
PKG_TOC_TYPES = frozenset(CARCHIVE_TYPE_BY_PKG_TYPE)

TOC_ENTRY_SLOTS = {
    11: "input-data",
    13: "scripts",
    14: "pure-modules",
    15: "binaries",
    18: "data",
    19: "stdlib-modules",
}
TOC_SLOT_TYPES = {
    11: frozenset({"DATA"}),
    13: frozenset({"PYSOURCE"}),
    14: frozenset({"PYMODULE"}),
    15: frozenset({"BINARY", "DATA", "EXTENSION"}),
    18: frozenset({"DATA"}),
    19: frozenset({"PYMODULE"}),
}
FINAL_PAYLOAD_SLOTS = (15, 18)
NATIVE_SUFFIXES = frozenset({".dll", ".dylib", ".pyd", ".so"})
ROOT_LABEL = re.compile(r"[a-z][a-z0-9-]{0,63}\Z")


class InventoryError(ValueError):
    """Raised when the Analysis or payload cannot be inventoried safely."""


def canonical_json_bytes(value: Any) -> bytes:
    """Return the project's canonical UTF-8 JSON representation."""

    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _marshal_load_exact(data: bytes, *, label: str) -> Any:
    stream = io.BytesIO(data)
    try:
        value = marshal.load(stream)
    except (EOFError, TypeError, ValueError, MemoryError, RecursionError) as exc:
        raise InventoryError(f"{label} is not valid marshal data") from exc
    if stream.tell() != len(data):
        raise InventoryError(f"{label} has trailing marshal data")
    return value


def _is_reparse(path: Path, details: os.stat_result) -> bool:
    attributes = getattr(details, "st_file_attributes", 0)
    return bool(attributes & WINDOWS_REPARSE_POINT)


def _absolute_unresolved(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _reject_link_chain(path: Path, *, label: str) -> None:
    absolute = _absolute_unresolved(path)
    chain = [absolute, *absolute.parents]
    for candidate in reversed(chain):
        if not candidate.exists() and not candidate.is_symlink():
            continue
        try:
            details = candidate.lstat()
        except OSError as exc:
            raise InventoryError(f"cannot inspect {label}: {candidate}") from exc
        if stat.S_ISLNK(details.st_mode) or _is_reparse(candidate, details):
            raise InventoryError(f"{label} contains a link/reparse point: {candidate}")


def _regular_file(
    path: Path,
    *,
    label: str,
    reject_hardlinks: bool,
) -> os.stat_result:
    _reject_link_chain(path, label=label)
    try:
        details = path.lstat()
    except OSError as exc:
        raise InventoryError(f"cannot inspect {label}: {path}") from exc
    if not stat.S_ISREG(details.st_mode):
        raise InventoryError(f"{label} is not a regular file: {path}")
    if reject_hardlinks and details.st_nlink > 1:
        raise InventoryError(f"{label} hardlinks are forbidden: {path}")
    return details


def _normal_directory(path: Path, *, label: str) -> Path:
    _reject_link_chain(path, label=label)
    try:
        details = path.lstat()
    except OSError as exc:
        raise InventoryError(f"cannot inspect {label}: {path}") from exc
    if not stat.S_ISDIR(details.st_mode):
        raise InventoryError(f"{label} is not a normal directory: {path}")
    return path.resolve(strict=True)


def _windows_path_key(path: Path) -> str:
    """Use Windows collision semantics even when unit tests run elsewhere."""

    return path.as_posix().casefold()


def _normalize_destination(value: str, *, label: str) -> str:
    if not value or "\x00" in value or ":" in value:
        raise InventoryError(f"unsafe {label}: {value!r}")
    normalized = value.replace("\\", "/")
    pure = PurePosixPath(normalized)
    if (
        pure.is_absolute()
        or not pure.parts
        or any(part in {"", ".", ".."} for part in pure.parts)
    ):
        raise InventoryError(f"unsafe {label}: {value!r}")
    return pure.as_posix()


def _prepare_source_roots(source_roots: Mapping[str, Path]) -> list[tuple[str, Path]]:
    if not source_roots:
        raise InventoryError("at least one named source root is required")
    prepared: list[tuple[str, Path]] = []
    folded_labels: set[str] = set()
    for label, raw_root in sorted(source_roots.items()):
        if not ROOT_LABEL.fullmatch(label) or label.casefold() in folded_labels:
            raise InventoryError(f"invalid or duplicate source root label: {label!r}")
        folded_labels.add(label.casefold())
        root = _normal_directory(Path(raw_root), label=f"source root {label!r}")
        prepared.append((label, root))
    # Prefer the most specific root when roots overlap.  The label is the stable
    # tiebreaker, so A/B physical paths never influence portable references.
    prepared.sort(key=lambda item: (-len(item[1].parts), item[0].encode("utf-8")))
    return prepared


def _source_reference(path: Path, roots: Sequence[tuple[str, Path]]) -> str:
    resolved = path.resolve(strict=True)
    for label, root in roots:
        try:
            relative = resolved.relative_to(root)
        except ValueError:
            continue
        if not relative.parts:
            raise InventoryError(
                f"TOC source unexpectedly names a root directory: {path}"
            )
        return f"{label}/{relative.as_posix()}"
    raise InventoryError(f"TOC source escapes all allowed roots: {path}")


def _physical_source(
    source: str,
    *,
    roots: Sequence[tuple[str, Path]],
) -> tuple[Path, str, int, str]:
    if not source or "\x00" in source:
        raise InventoryError(f"unsafe TOC source path: {source!r}")
    path = Path(source)
    if not path.is_absolute():
        raise InventoryError(f"TOC source path is not absolute: {source!r}")
    details = _regular_file(
        path,
        label="TOC source",
        # Conda environments may legitimately hardlink immutable package files.
        reject_hardlinks=False,
    )
    reference = _source_reference(path, roots)
    return path.resolve(strict=True), reference, details.st_size, sha256_file(path)


def _physical_source_from_reference(
    reference: str,
    *,
    roots: Sequence[tuple[str, Path]],
) -> tuple[Path, int, str]:
    label, separator, raw_relative = reference.partition("/")
    if not separator:
        raise InventoryError(f"invalid portable source reference: {reference!r}")
    relative = _normalize_destination(raw_relative, label="portable source reference")
    for root_label, root in roots:
        if root_label != label:
            continue
        physical, actual_reference, size, digest = _physical_source(
            os.fspath(root.joinpath(*PurePosixPath(relative).parts)), roots=roots
        )
        if actual_reference != reference:
            raise InventoryError("portable source reference is not canonical")
        return physical, size, digest
    raise InventoryError(f"portable source reference has no named root: {reference!r}")


def _validate_literal_tree(value: Any, *, depth: int = 0) -> None:
    if depth > 64:
        raise InventoryError("PyInstaller TOC nesting exceeds the safety limit")
    if value is None or isinstance(value, (str, int, bool)):
        return
    if isinstance(value, bytes):
        if len(value) > MAX_TOC_BYTES:
            raise InventoryError(
                "PyInstaller TOC byte literal exceeds the safety limit"
            )
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _validate_literal_tree(item, depth=depth + 1)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, (str, int)):
                raise InventoryError("PyInstaller TOC contains an unsupported dict key")
            _validate_literal_tree(item, depth=depth + 1)
        return
    raise InventoryError(
        f"PyInstaller TOC contains an unsupported literal type: {type(value).__name__}"
    )


def _read_literal_toc(path: Path, *, label: str) -> tuple[Any, ...]:
    details = _regular_file(
        path,
        label=label,
        reject_hardlinks=True,
    )
    if details.st_size <= 0 or details.st_size > MAX_TOC_BYTES:
        raise InventoryError(f"{label} size is outside the accepted bounds")
    try:
        text = path.read_text(encoding="utf-8")
        value = ast.literal_eval(text)
    except (
        OSError,
        UnicodeError,
        SyntaxError,
        ValueError,
        MemoryError,
        RecursionError,
    ) as exc:
        raise InventoryError(f"{label} is not a safe Python literal") from exc
    _validate_literal_tree(value)
    if not isinstance(value, tuple):
        raise InventoryError(f"{label} root must be a tuple")
    return value


def _read_toc(path: Path) -> tuple[Any, ...]:
    value = _read_literal_toc(path, label="Analysis TOC")
    if len(value) != 20:
        raise InventoryError("Analysis TOC root must be the expected 20-item tuple")
    expected_types = {
        0: list,
        1: list,
        2: list,
        3: list,
        4: dict,
        5: list,
        6: list,
        7: bool,
        8: dict,
        9: int,
        10: list,
        11: list,
        12: str,
        13: list,
        14: list,
        15: list,
        16: list,
        17: list,
        18: list,
        19: list,
    }
    for index, expected in expected_types.items():
        if type(value[index]) is not expected:
            raise InventoryError(
                f"Analysis TOC slot {index} has an unexpected structure"
            )
    return value


def _read_pkg_toc(path: Path) -> tuple[Any, ...]:
    value = _read_literal_toc(path, label="PyInstaller PKG TOC")
    expected_types = (
        str,
        dict,
        list,
        str,
        bool,
        bool,
        bool,
        list,
        type(None),
        type(None),
        type(None),
    )
    if len(value) != len(expected_types) or any(
        type(item) is not expected
        for item, expected in zip(value, expected_types, strict=True)
    ):
        raise InventoryError("PyInstaller PKG TOC root has an unexpected structure")
    if not Path(value[0]).is_absolute():
        raise InventoryError("PyInstaller PKG TOC contains a non-absolute output path")
    if (
        not value[3]
        or Path(value[3]).name != value[3]
        or not value[3].casefold().endswith(".dll")
    ):
        raise InventoryError("PyInstaller PKG Python library name is invalid")
    compression = value[1]
    if any(
        type(key) is not str or type(flag) is not bool
        for key, flag in compression.items()
    ):
        raise InventoryError("PyInstaller PKG compression contract is invalid")
    for item in value[2]:
        if not isinstance(item, tuple) or len(item) != 3:
            raise InventoryError("PyInstaller PKG entry has an unexpected structure")
        destination, source, kind = item
        if (
            type(destination) is not str
            or type(kind) is not str
            or kind not in PKG_TOC_TYPES
        ):
            raise InventoryError("PyInstaller PKG entry has an unsupported type")
        _normalize_destination(destination, label="PKG destination")
        if kind == "OPTION":
            if source not in {None, ""}:
                raise InventoryError("PyInstaller OPTION entry has a physical source")
        elif type(source) is not str or not Path(source).is_absolute():
            raise InventoryError("PyInstaller PKG source is not an absolute path")
        if kind not in compression and kind != "OPTION":
            raise InventoryError(f"PyInstaller PKG compression policy omits {kind}")
    return value


def _read_exe_toc(path: Path) -> tuple[Any, ...]:
    value = _read_literal_toc(path, label="PyInstaller EXE TOC")
    expected_types: tuple[type[Any], ...] = (
        str,
        bool,
        bool,
        bool,
        str,
        type(None),
        bool,
        bool,
        bytes,
        bool,
        bool,
        type(None),
        type(None),
        type(None),
        str,
        list,
        list,
        bool,
        bool,
        int,
        list,
        str,
    )
    if len(value) != len(expected_types) or any(
        type(item) is not expected
        for item, expected in zip(value, expected_types, strict=True)
    ):
        raise InventoryError("PyInstaller EXE TOC root has an unexpected structure")
    for index in (0, 4, 14, 21):
        if not Path(value[index]).is_absolute():
            raise InventoryError("PyInstaller EXE TOC contains a non-absolute path")
    if len(value[20]) != 1:
        raise InventoryError("PyInstaller EXE TOC must bind exactly one bootloader")
    bootloader = value[20][0]
    if (
        not isinstance(bootloader, tuple)
        or len(bootloader) != 3
        or type(bootloader[0]) is not str
        or type(bootloader[1]) is not str
        or bootloader[2] != "EXECUTABLE"
        or not Path(bootloader[1]).is_absolute()
    ):
        raise InventoryError("PyInstaller EXE TOC bootloader binding is invalid")
    return value


def discover_executable_pkg_tocs(
    work_root: Path,
    executable_names: Sequence[str],
) -> dict[str, Path]:
    """Discover and cross-bind each EXE TOC to exactly one PKG TOC.

    The returned mapping is intentionally explicit and is consumed again by
    :func:`build_release_inventory`; discovery is not treated as a trust step.
    """

    resolved_work = _normal_directory(Path(work_root), label="PyInstaller work root")
    expected: dict[str, str] = {}
    for raw_name in executable_names:
        name = _normalize_destination(raw_name, label="bootloader executable")
        if "/" in name or not name.casefold().endswith(".exe"):
            raise InventoryError(f"invalid bootloader executable name: {raw_name!r}")
        prior = expected.setdefault(name.casefold(), name)
        if prior != name:
            raise InventoryError("case-colliding bootloader executable names")
    if not expected:
        raise InventoryError("at least one bootloader executable name is required")

    exe_tocs = sorted(
        resolved_work.rglob("EXE-*.toc"),
        key=lambda item: item.as_posix().encode("utf-8"),
    )
    pkg_tocs = sorted(
        resolved_work.rglob("PKG-*.toc"),
        key=lambda item: item.as_posix().encode("utf-8"),
    )
    pkg_by_output: dict[str, tuple[Path, tuple[Any, ...]]] = {}
    for path in pkg_tocs:
        toc = _read_pkg_toc(path)
        pkg_path = Path(toc[0]).resolve(strict=True)
        if resolved_work not in pkg_path.parents:
            raise InventoryError("PyInstaller PKG output escapes the work root")
        key = _windows_path_key(pkg_path)
        if key in pkg_by_output:
            raise InventoryError("duplicate/case-colliding PyInstaller PKG output")
        pkg_by_output[key] = (path.resolve(strict=True), toc)

    discovered: dict[str, Path] = {}
    for path in exe_tocs:
        toc = _read_exe_toc(path)
        output = Path(toc[0])
        name = output.name
        expected_name = expected.get(name.casefold())
        if expected_name is None:
            raise InventoryError(f"unexpected PyInstaller EXE TOC output: {name}")
        if name != expected_name:
            raise InventoryError(
                "PyInstaller EXE TOC output case differs from the contract"
            )
        pkg_output = Path(toc[14]).resolve(strict=True)
        try:
            pkg_toc_path, pkg_toc = pkg_by_output[_windows_path_key(pkg_output)]
        except KeyError as exc:
            raise InventoryError(
                f"PyInstaller EXE {name} has no matching PKG TOC"
            ) from exc
        if toc[15] != pkg_toc[2] or Path(toc[21]).name != pkg_toc[3]:
            raise InventoryError(f"PyInstaller EXE/PKG TOCs disagree for {name}")
        if name.casefold() in discovered:
            raise InventoryError(f"duplicate PyInstaller EXE TOC output: {name}")
        discovered[name.casefold()] = pkg_toc_path
    if set(discovered) != set(expected):
        missing = sorted(set(expected) - set(discovered))
        raise InventoryError(
            f"PyInstaller executable/PKG mappings are incomplete: {missing}"
        )
    if len(pkg_by_output) != len(discovered):
        raise InventoryError(
            "unexpected PyInstaller PKG TOC is present in the work root"
        )
    return {expected[key]: discovered[key] for key in sorted(expected)}


def _replace_code_filename(code: types.CodeType, filename: str) -> types.CodeType:
    constants = tuple(
        (
            _replace_code_filename(item, filename)
            if isinstance(item, types.CodeType)
            else item
        )
        for item in code.co_consts
    )
    return code.replace(co_consts=constants, co_filename=filename)


def _expected_pkg_entry_content(
    *,
    destination: str,
    source_path: Path,
    source_bytes: bytes,
    kind: str,
) -> bytes | types.CodeType:
    data = source_bytes
    if kind == "PYSOURCE":
        source_name = os.fspath(source_path)
        if source_path.suffix.casefold() == ".pyc":
            if len(data) < 16 or data[:4] != importlib.util.MAGIC_NUMBER:
                raise InventoryError("PyInstaller PYSOURCE .pyc has an invalid header")
            code = _marshal_load_exact(
                data[16:], label="PyInstaller PYSOURCE .pyc code"
            )
        else:
            compile_name = source_name if source_path.suffix else source_name + ".py"
            try:
                code = compile(
                    data,
                    compile_name,
                    "exec",
                    flags=0,
                    dont_inherit=True,
                    optimize=0,
                )
            except (SyntaxError, ValueError) as exc:
                raise InventoryError("PyInstaller PYSOURCE cannot be compiled") from exc
        if not isinstance(code, types.CodeType):
            raise InventoryError("PyInstaller PYSOURCE did not yield a code object")
        normalized_destination = os.path.normpath(destination)
        code_filename = (
            os.path.splitext(normalized_destination)[0]
            + os.path.splitext(code.co_filename)[1]
        )
        return _replace_code_filename(code, code_filename)
    if kind == "PYMODULE":
        if len(data) < 16 or data[:4] != importlib.util.MAGIC_NUMBER:
            raise InventoryError("PyInstaller PYMODULE has an invalid .pyc header")
        code = _marshal_load_exact(data[16:], label="PyInstaller PYMODULE .pyc code")
        if not isinstance(code, types.CodeType):
            raise InventoryError("PyInstaller PYMODULE did not yield a code object")
        return _replace_code_filename(code, os.path.normpath(destination) + ".py")
    return data


def _code_objects_equal(left: types.CodeType, right: types.CodeType) -> bool:
    scalar_attributes = (
        "co_argcount",
        "co_posonlyargcount",
        "co_kwonlyargcount",
        "co_nlocals",
        "co_stacksize",
        "co_flags",
        "co_code",
        "co_names",
        "co_varnames",
        "co_filename",
        "co_name",
        "co_qualname",
        "co_firstlineno",
        "co_linetable",
        "co_exceptiontable",
        "co_freevars",
        "co_cellvars",
    )
    if any(getattr(left, name) != getattr(right, name) for name in scalar_attributes):
        return False
    if len(left.co_consts) != len(right.co_consts):
        return False
    for left_value, right_value in zip(left.co_consts, right.co_consts, strict=True):
        if isinstance(left_value, types.CodeType) or isinstance(
            right_value, types.CodeType
        ):
            if not (
                isinstance(left_value, types.CodeType)
                and isinstance(right_value, types.CodeType)
                and _code_objects_equal(left_value, right_value)
            ):
                return False
        elif not _marshal_constant_equal(left_value, right_value):
            return False
    return True


def _marshal_constant_equal(left: Any, right: Any) -> bool:
    if type(left) is not type(right):
        return False
    if isinstance(left, types.CodeType):
        return _code_objects_equal(left, right)
    if isinstance(left, tuple):
        return len(left) == len(right) and all(
            _marshal_constant_equal(first, second)
            for first, second in zip(left, right, strict=True)
        )
    if isinstance(left, frozenset):
        unmatched = list(right)
        for first in left:
            for index, second in enumerate(unmatched):
                if _marshal_constant_equal(first, second):
                    unmatched.pop(index)
                    break
            else:
                return False
        return not unmatched
    if isinstance(left, float):
        return struct.pack("!d", left) == struct.pack("!d", right)
    if isinstance(left, complex):
        return struct.pack("!dd", left.real, left.imag) == struct.pack(
            "!dd", right.real, right.imag
        )
    return left == right


def _pkg_content_matches(actual: bytes, expected: bytes | types.CodeType) -> bool:
    if isinstance(expected, bytes):
        return actual == expected
    try:
        actual_code = _marshal_load_exact(actual, label="CArchive Python entry")
    except InventoryError:
        return False
    return isinstance(actual_code, types.CodeType) and _code_objects_equal(
        actual_code, expected
    )


def _read_pyz_source_toc(pyz_path: Path) -> list[tuple[str, str, str]]:
    toc_path = pyz_path.with_suffix(".toc")
    value = _read_literal_toc(toc_path, label="PyInstaller PYZ TOC")
    if (
        len(value) != 2
        or type(value[0]) is not str
        or type(value[1]) is not list
        or not Path(value[0]).is_absolute()
        or Path(value[0]).resolve(strict=True) != pyz_path.resolve(strict=True)
    ):
        raise InventoryError("PyInstaller PYZ TOC root is invalid")
    if not value[1] or len(value[1]) > MAX_PYZ_MEMBERS:
        raise InventoryError("PyInstaller PYZ TOC member count is invalid")
    result: list[tuple[str, str, str]] = []
    names: list[str] = []
    folded_names: set[str] = set()
    for raw_entry in value[1]:
        if (
            not isinstance(raw_entry, tuple)
            or len(raw_entry) != 3
            or type(raw_entry[0]) is not str
            or type(raw_entry[1]) is not str
            or raw_entry[2] not in {"PYMODULE", "PYMODULE-1", "PYMODULE-2"}
        ):
            raise InventoryError("PyInstaller PYZ TOC member is invalid")
        name, source, kind = raw_entry
        encoded_name = name.encode("utf-8")
        if (
            not name
            or len(encoded_name) > MAX_PYZ_NAME_BYTES
            or "\x00" in name
            or "/" in name
            or "\\" in name
            or name.startswith(".")
            or name.endswith(".")
            or ".." in name
            or name.casefold() in folded_names
        ):
            raise InventoryError("PyInstaller PYZ TOC member name is invalid")
        if source != "-" and not Path(source).is_absolute():
            raise InventoryError("PyInstaller PYZ TOC source is not absolute")
        folded_names.add(name.casefold())
        names.append(name)
        result.append((name, source, kind))
    if names != sorted(names, key=lambda item: item.encode("utf-8")):
        raise InventoryError("PyInstaller PYZ TOC members are not canonically sorted")
    return result


def _bounded_zlib_decompress(stored: bytes, *, label: str) -> bytes:
    try:
        decompressor = zlib.decompressobj()
        content = decompressor.decompress(stored, MAX_ARCHIVE_MEMBER_BYTES + 1)
        if len(content) > MAX_ARCHIVE_MEMBER_BYTES:
            raise InventoryError(f"{label} exceeds the uncompressed size limit")
        remaining = MAX_ARCHIVE_MEMBER_BYTES + 1 - len(content)
        content += decompressor.flush(remaining)
    except zlib.error as exc:
        raise InventoryError(f"{label} compression is invalid") from exc
    if (
        len(content) > MAX_ARCHIVE_MEMBER_BYTES
        or not decompressor.eof
        or decompressor.unused_data
        or decompressor.unconsumed_tail
    ):
        raise InventoryError(f"{label} has trailing, incomplete, or oversized data")
    return content


def _parse_pyz_archive(data: bytes) -> dict[str, Any]:
    if len(data) < PYZ_HEADER_SIZE or len(data) > MAX_PYZ_BYTES:
        raise InventoryError("PyInstaller PYZ size is outside accepted bounds")
    if (
        data[:4] != PYZ_MAGIC
        or data[4:8] != importlib.util.MAGIC_NUMBER
        or data[12:PYZ_HEADER_SIZE] != b"\0" * 5
    ):
        raise InventoryError("PyInstaller PYZ header is invalid")
    try:
        toc_offset = struct.unpack("!i", data[8:12])[0]
    except struct.error as exc:
        raise InventoryError("PyInstaller PYZ TOC offset is invalid") from exc
    toc_size = len(data) - toc_offset
    if (
        toc_offset < PYZ_HEADER_SIZE
        or toc_offset >= len(data)
        or toc_size <= 0
        or toc_size > MAX_PYZ_TOC_BYTES
    ):
        raise InventoryError("PyInstaller PYZ TOC bounds are invalid")
    raw_toc = _marshal_load_exact(data[toc_offset:], label="PyInstaller PYZ TOC")
    if type(raw_toc) is not list or not raw_toc or len(raw_toc) > MAX_PYZ_MEMBERS:
        raise InventoryError("PyInstaller PYZ TOC root is invalid")

    names: list[str] = []
    folded_names: set[str] = set()
    expected_offset = PYZ_HEADER_SIZE
    total_uncompressed = 0
    records: list[dict[str, Any]] = []
    for raw_entry in raw_toc:
        if (
            not isinstance(raw_entry, tuple)
            or len(raw_entry) != 2
            or type(raw_entry[0]) is not str
            or not isinstance(raw_entry[1], tuple)
            or len(raw_entry[1]) != 3
            or any(type(value) is not int for value in raw_entry[1])
        ):
            raise InventoryError("PyInstaller PYZ TOC entry is invalid")
        name = raw_entry[0]
        flag, offset, stored_size = raw_entry[1]
        encoded_name = name.encode("utf-8")
        if (
            not name
            or len(encoded_name) > MAX_PYZ_NAME_BYTES
            or "\x00" in name
            or "/" in name
            or "\\" in name
            or name.startswith(".")
            or name.endswith(".")
            or ".." in name
            or name.casefold() in folded_names
            or flag not in {0, 1, 3}
            or offset != expected_offset
            or stored_size < 0
            or offset + stored_size > toc_offset
            or (flag == 3 and stored_size != 0)
            or (flag != 3 and stored_size == 0)
        ):
            raise InventoryError("PyInstaller PYZ TOC entry bounds/name is invalid")
        stored = data[offset : offset + stored_size]
        if flag == 3:
            content = b""
            kind = "namespace"
        else:
            content = _bounded_zlib_decompress(
                stored, label=f"PyInstaller PYZ member {name!r}"
            )
            code = _marshal_load_exact(
                content, label=f"PyInstaller PYZ member {name!r}"
            )
            if not isinstance(code, types.CodeType):
                raise InventoryError("PyInstaller PYZ member is not a code object")
            kind = "package" if flag == 1 else "module"
        total_uncompressed += len(content)
        if total_uncompressed > MAX_ARCHIVE_TOTAL_UNCOMPRESSED_BYTES:
            raise InventoryError("PyInstaller PYZ exceeds the total size limit")
        folded_names.add(name.casefold())
        names.append(name)
        records.append(
            {
                "content": content,
                "content_sha256": hashlib.sha256(content).hexdigest(),
                "content_size": len(content),
                "flag": flag,
                "kind": kind,
                "name": name,
                "stored_sha256": hashlib.sha256(stored).hexdigest(),
                "stored_size": stored_size,
            }
        )
        expected_offset += stored_size
    if (
        names != sorted(names, key=lambda item: item.encode("utf-8"))
        or expected_offset != toc_offset
    ):
        raise InventoryError("PyInstaller PYZ member ordering/data boundary is invalid")
    return {
        "members": records,
        "python_magic_sha256": hashlib.sha256(data[4:8]).hexdigest(),
        "toc_sha256": hashlib.sha256(data[toc_offset:]).hexdigest(),
        "toc_size": toc_size,
    }


def _expected_pyz_code(
    *,
    name: str,
    source_path: Path,
    source_bytes: bytes,
    source_kind: str,
    package: bool,
) -> types.CodeType:
    optimize = {"PYMODULE": 0, "PYMODULE-1": 1, "PYMODULE-2": 2}[source_kind]
    if source_path.suffix.casefold() == ".pyc":
        if len(source_bytes) < 16 or source_bytes[:4] != importlib.util.MAGIC_NUMBER:
            raise InventoryError("PyInstaller PYZ source .pyc has an invalid header")
        code = _marshal_load_exact(
            source_bytes[16:], label="PyInstaller PYZ source .pyc code"
        )
    else:
        try:
            code = compile(
                source_bytes,
                os.fspath(source_path),
                "exec",
                flags=0,
                dont_inherit=True,
                optimize=optimize,
            )
        except (SyntaxError, ValueError) as exc:
            raise InventoryError("PyInstaller PYZ source cannot be compiled") from exc
    if not isinstance(code, types.CodeType):
        raise InventoryError("PyInstaller PYZ source did not yield a code object")
    relative = os.path.join(*name.split("."))
    filename = os.path.join(relative, "__init__.py") if package else relative + ".py"
    return _replace_code_filename(code, filename)


def _bind_pyz_members(
    *,
    pyz_path: Path,
    pyz_bytes: bytes,
    portable_entries: Sequence[Mapping[str, Any]],
    virtual_entries: Sequence[Mapping[str, Any]],
    bootstrap_module_names: set[str],
    roots: Sequence[tuple[str, Path]],
    embedded_path: str,
) -> tuple[list[dict[str, Any]], set[str], dict[str, Any]]:
    source_toc = _read_pyz_source_toc(pyz_path)
    parsed = _parse_pyz_archive(pyz_bytes)
    actual_members = parsed["members"]
    source_names = [item[0] for item in source_toc]
    actual_names = [item["name"] for item in actual_members]
    if source_names != actual_names:
        raise InventoryError("PyInstaller PYZ bytes differ from the PYZ source TOC")

    pure_by_name: dict[str, Mapping[str, Any]] = {}
    for item in portable_entries:
        if item["slot"] != "pure-modules":
            continue
        name = str(item["destination"])
        if name in pure_by_name:
            raise InventoryError("duplicate Analysis pure-module destination")
        pure_by_name[name] = item
    physical_source_names = {name for name, source, _ in source_toc if source != "-"}
    namespace_source_names = {name for name, source, _ in source_toc if source == "-"}
    analysis_namespace_names = {
        str(item["destination"])
        for item in virtual_entries
        if item["slot"] == "pure-modules"
    }
    if namespace_source_names != analysis_namespace_names:
        raise InventoryError(
            "Analysis virtual pure modules differ from PYZ namespace members"
        )
    expected_analysis_names = physical_source_names | (
        bootstrap_module_names & set(pure_by_name)
    )
    if set(pure_by_name) != expected_analysis_names:
        raise InventoryError(
            "Analysis pure modules do not equal PYZ members plus bootstrap modules"
        )

    result: list[dict[str, Any]] = []
    component_ids: set[str] = set()
    for source_entry, actual in zip(source_toc, actual_members, strict=True):
        name, raw_source, source_kind = source_entry
        if raw_source == "-":
            if actual["kind"] != "namespace":
                raise InventoryError("physical PYZ member is declared as a namespace")
            dotted_prefix = name + "."
            path_prefix = name.replace(".", "/") + "/"
            related = [
                item
                for item in portable_entries
                if str(item["destination"]).startswith(dotted_prefix)
                or str(item["destination"]).replace("\\", "/").startswith(path_prefix)
            ]
            components = sorted(
                {
                    str(component)
                    for item in related
                    for component in item["component_ids"]
                }
            )
            conda_components = sorted(
                {
                    str(component)
                    for item in related
                    for component in item["conda_component_ids"]
                }
            )
            owners = sorted(
                {str(owner) for item in related for owner in item["distribution_names"]}
            )
            if not components:
                raise InventoryError(
                    f"PyInstaller PYZ namespace has no Analysis owner: {name}"
                )
            source_ref = f"virtual-namespace/{name.replace('.', '/')}"
            source_size = 0
            source_sha256 = hashlib.sha256(b"").hexdigest()
        else:
            if actual["kind"] == "namespace":
                raise InventoryError("PYZ namespace unexpectedly has a physical source")
            try:
                analysis_item = pure_by_name[name]
            except KeyError as exc:
                raise InventoryError(
                    f"PYZ member is absent from Analysis pure modules: {name}"
                ) from exc
            physical, source_ref, source_size, source_sha256 = _physical_source(
                raw_source, roots=roots
            )
            source_bytes = physical.read_bytes()
            if (
                len(source_bytes) != source_size
                or hashlib.sha256(source_bytes).hexdigest() != source_sha256
                or analysis_item["source_ref"] != source_ref
                or analysis_item["source_size"] != source_size
                or analysis_item["source_sha256"] != source_sha256
            ):
                raise InventoryError(
                    f"PYZ source changed or differs from Analysis: {name}"
                )
            expected_package = physical.stem == "__init__"
            if (actual["kind"] == "package") != expected_package:
                raise InventoryError(
                    f"PYZ package/module flag differs from source: {name}"
                )
            expected_code = _expected_pyz_code(
                name=name,
                source_path=physical,
                source_bytes=source_bytes,
                source_kind=source_kind,
                package=expected_package,
            )
            if not _pkg_content_matches(actual["content"], expected_code):
                raise InventoryError(f"PYZ member code differs from source: {name}")
            components = list(analysis_item["component_ids"])
            conda_components = list(analysis_item["conda_component_ids"])
            owners = list(analysis_item["distribution_names"])
        component_ids.update(components)
        result.append(
            {
                "component_ids": components,
                "conda_component_ids": conda_components,
                "content_sha256": actual["content_sha256"],
                "content_size": actual["content_size"],
                "distribution_names": owners,
                "kind": actual["kind"],
                "name": name,
                "source_kind": source_kind,
                "source_ref": source_ref,
                "source_sha256": source_sha256,
                "source_size": source_size,
                "stored_sha256": actual["stored_sha256"],
                "stored_size": actual["stored_size"],
            }
        )
    portable = [
        {key: value for key, value in item.items() if key != "content"}
        for item in result
    ]
    metadata = {
        "pyz_member_count": len(portable),
        "pyz_members": portable,
        "pyz_members_sha256": hashlib.sha256(
            canonical_json_bytes(portable)
        ).hexdigest(),
        "pyz_python_magic_sha256": parsed["python_magic_sha256"],
        "pyz_toc_sha256": parsed["toc_sha256"],
        "pyz_toc_size": parsed["toc_size"],
    }
    return portable, component_ids, metadata


def _parse_carchive(executable: Path, package: Path) -> dict[str, Any]:
    executable_details = _regular_file(
        executable, label="PyInstaller executable", reject_hardlinks=True
    )
    package_details = _regular_file(
        package, label="PyInstaller PKG archive", reject_hardlinks=True
    )
    if (
        executable_details.st_size <= CARCHIVE_COOKIE_SIZE
        or executable_details.st_size > MAX_EXECUTABLE_BYTES
        or package_details.st_size <= CARCHIVE_COOKIE_SIZE
        or package_details.st_size > MAX_CARCHIVE_PACKAGE_BYTES
    ):
        raise InventoryError(
            "PyInstaller executable/PKG size is outside accepted bounds"
        )
    executable_bytes = executable.read_bytes()
    package_bytes = package.read_bytes()
    cookie_start = executable_bytes.rfind(CARCHIVE_COOKIE_MAGIC)
    if cookie_start < 0 or cookie_start + CARCHIVE_COOKIE_SIZE != len(executable_bytes):
        raise InventoryError("PyInstaller executable has no terminal CArchive cookie")
    try:
        magic, archive_length, toc_offset, toc_length, pyvers, raw_pylib = (
            struct.unpack(
                CARCHIVE_COOKIE_FORMAT,
                executable_bytes[cookie_start : cookie_start + CARCHIVE_COOKIE_SIZE],
            )
        )
    except struct.error as exc:
        raise InventoryError("PyInstaller CArchive cookie is malformed") from exc
    if magic != CARCHIVE_COOKIE_MAGIC or archive_length != len(package_bytes):
        raise InventoryError("PyInstaller CArchive cookie length is invalid")
    archive_start = len(executable_bytes) - archive_length
    if archive_start <= 0 or archive_start > MAX_BOOTLOADER_PREFIX_BYTES:
        raise InventoryError(
            "PyInstaller bootloader prefix size is outside accepted bounds"
        )
    if executable_bytes[archive_start:] != package_bytes:
        raise InventoryError(
            "PyInstaller executable suffix differs from its PKG archive"
        )
    if (
        toc_offset < 0
        or toc_length <= 0
        or toc_length > MAX_CARCHIVE_TOC_BYTES
        or toc_offset + toc_length != archive_length - CARCHIVE_COOKIE_SIZE
    ):
        raise InventoryError("PyInstaller CArchive TOC bounds are invalid")
    if not raw_pylib or b"\0" not in raw_pylib:
        raise InventoryError("PyInstaller CArchive Python library name is invalid")
    pylib_raw, padding = raw_pylib.split(b"\0", 1)
    if not pylib_raw or any(padding):
        raise InventoryError("PyInstaller CArchive Python library field is malformed")
    try:
        python_library = pylib_raw.decode("ascii")
    except UnicodeDecodeError as exc:
        raise InventoryError(
            "PyInstaller CArchive Python library name is not ASCII"
        ) from exc

    toc_bytes = package_bytes[toc_offset : toc_offset + toc_length]
    position = 0
    expected_offset = 0
    total_uncompressed = 0
    seen: dict[str, str] = {}
    records: list[dict[str, Any]] = []
    while position < len(toc_bytes):
        if len(records) >= MAX_CARCHIVE_ENTRIES:
            raise InventoryError("PyInstaller CArchive has too many entries")
        if len(toc_bytes) - position < CARCHIVE_TOC_HEADER_SIZE:
            raise InventoryError("PyInstaller CArchive TOC is truncated")
        try:
            entry_length, offset, stored_size, content_size, compressed, raw_type = (
                struct.unpack(
                    CARCHIVE_TOC_FORMAT,
                    toc_bytes[position : position + CARCHIVE_TOC_HEADER_SIZE],
                )
            )
        except struct.error as exc:
            raise InventoryError("PyInstaller CArchive TOC entry is malformed") from exc
        if (
            entry_length < CARCHIVE_TOC_HEADER_SIZE + 1
            or entry_length % 16 != 0
            or position + entry_length > len(toc_bytes)
            or compressed not in {0, 1}
            or offset != expected_offset
            or stored_size > toc_offset - offset
            or content_size > MAX_ARCHIVE_MEMBER_BYTES
            or total_uncompressed + content_size > MAX_ARCHIVE_TOTAL_UNCOMPRESSED_BYTES
        ):
            raise InventoryError("PyInstaller CArchive TOC entry bounds are invalid")
        name_field = toc_bytes[
            position + CARCHIVE_TOC_HEADER_SIZE : position + entry_length
        ]
        terminator = name_field.find(b"\0")
        if (
            terminator <= 0
            or terminator > MAX_CARCHIVE_NAME_BYTES
            or any(name_field[terminator:])
        ):
            raise InventoryError("PyInstaller CArchive TOC name padding is invalid")
        try:
            name = name_field[:terminator].decode("utf-8")
            typecode = raw_type.decode("ascii")
        except UnicodeDecodeError as exc:
            raise InventoryError("PyInstaller CArchive TOC text is invalid") from exc
        normalized_name = _normalize_destination(name, label="CArchive entry name")
        if typecode != "o":
            folded_name = normalized_name.casefold()
            if folded_name in seen:
                raise InventoryError("duplicate/case-colliding CArchive entry name")
            seen[folded_name] = normalized_name
        stored = package_bytes[offset : offset + stored_size]
        try:
            if compressed:
                decompressor = zlib.decompressobj()
                content = decompressor.decompress(stored, content_size + 1)
                content += decompressor.flush(content_size + 1 - len(content))
                if (
                    not decompressor.eof
                    or decompressor.unused_data
                    or decompressor.unconsumed_tail
                ):
                    raise InventoryError(
                        "PyInstaller CArchive entry has trailing/incomplete compressed data"
                    )
            else:
                content = stored
        except zlib.error as exc:
            raise InventoryError(
                "PyInstaller CArchive entry compression is invalid"
            ) from exc
        if len(content) != content_size:
            raise InventoryError("PyInstaller CArchive entry content length is invalid")
        records.append(
            {
                "compressed": bool(compressed),
                "content": content,
                "content_sha256": hashlib.sha256(content).hexdigest(),
                "name": normalized_name,
                "stored_sha256": hashlib.sha256(stored).hexdigest(),
                "stored_size": stored_size,
                "typecode": typecode,
                "uncompressed_size": content_size,
            }
        )
        total_uncompressed += content_size
        expected_offset += stored_size
        position += entry_length
    if position != len(toc_bytes) or expected_offset != toc_offset:
        raise InventoryError("PyInstaller CArchive data/TOC boundary is invalid")
    expected_pyvers = sys.version_info.major * 100 + sys.version_info.minor
    expected_python_library = (
        f"python{sys.version_info.major}{sys.version_info.minor}.dll"
    )
    if (
        pyvers != expected_pyvers
        or python_library.casefold() != expected_python_library
    ):
        raise InventoryError("PyInstaller CArchive Python runtime identity is invalid")
    return {
        "bootloader_prefix_sha256": hashlib.sha256(
            executable_bytes[:archive_start]
        ).hexdigest(),
        "bootloader_prefix_size": archive_start,
        "entries": records,
        "executable_sha256": hashlib.sha256(executable_bytes).hexdigest(),
        "executable_size": len(executable_bytes),
        "pkg_sha256": hashlib.sha256(package_bytes).hexdigest(),
        "pkg_size": len(package_bytes),
        "python_library": python_library,
        "python_version": pyvers,
    }


def _pkg_entry_contracts(pkg_toc: tuple[Any, ...]) -> list[dict[str, Any]]:
    compression = pkg_toc[1]
    bootstrap: list[dict[str, Any]] = []
    archive: list[dict[str, Any]] = []
    for raw_destination, raw_source, kind in pkg_toc[2]:
        destination = _normalize_destination(raw_destination, label="PKG destination")
        if kind == "PYZ":
            archive_name = "PYZ.pyz"
        else:
            archive_name = _normalize_destination(
                os.path.normpath(raw_destination), label="CArchive destination"
            )
        typecode = CARCHIVE_TYPE_BY_PKG_TYPE[kind]
        # PyInstaller promotes executable DATA to a binary CArchive entry.  On
        # Windows os.access(X_OK) is normally true for ordinary files, including
        # base_library.zip in a onefile executable.
        if kind == "DATA" and raw_source and os.access(raw_source, os.X_OK):
            typecode = "b"
        item = {
            "archive_name": archive_name,
            "compressed": (
                bool(compression.get(kind, False)) if kind != "OPTION" else False
            ),
            "destination": destination,
            "kind": kind,
            "source": raw_source,
            "typecode": typecode,
        }
        if kind in {"PYMODULE", "PYSOURCE"}:
            bootstrap.append(item)
        else:
            archive.append(item)
    archive.sort(
        key=lambda item: (
            item["typecode"].encode("ascii"),
            item["archive_name"].encode("utf-8"),
        )
    )
    return [*bootstrap, *archive]


def _build_embedded_archives(
    *,
    work_root: Path,
    payload_root: Path,
    executable_pkg_tocs: Mapping[str, Path],
    executable_names: Mapping[str, str],
    roots: Sequence[tuple[str, Path]],
    source_owners: Mapping[str, Sequence[str]],
    conda_source_owners: Mapping[str, Sequence[str]],
    python_root_label: str,
    application_root_labels: frozenset[str],
    generated_root_labels: frozenset[str],
    portable_entries: Sequence[Mapping[str, Any]],
    virtual_entries: Sequence[Mapping[str, Any]],
    artifact_path_base: str,
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    dict[str, set[str]],
    dict[str, dict[str, Any]],
    list[str],
]:
    supplied: dict[str, Path] = {}
    for raw_name, raw_path in executable_pkg_tocs.items():
        name = _normalize_destination(raw_name, label="PKG executable mapping")
        if "/" in name:
            raise InventoryError("PKG executable mapping must use a basename")
        key = name.casefold()
        if (
            key not in executable_names
            or executable_names[key] != name
            or key in supplied
        ):
            raise InventoryError("PKG executable mapping key set/case is invalid")
        supplied[key] = Path(raw_path).resolve(strict=True)
    if set(supplied) != set(executable_names):
        raise InventoryError("PKG executable mapping is incomplete")
    discovered = discover_executable_pkg_tocs(
        work_root, list(executable_names.values())
    )
    discovered_folded = {
        name.casefold(): path.resolve(strict=True) for name, path in discovered.items()
    }
    if supplied != discovered_folded:
        raise InventoryError(
            "supplied PKG executable mapping differs from EXE/PKG TOCs"
        )

    exe_contracts: dict[str, tuple[Path, tuple[Any, ...]]] = {}
    for exe_toc_path in Path(work_root).resolve(strict=True).rglob("EXE-*.toc"):
        exe_toc = _read_exe_toc(exe_toc_path)
        name = Path(exe_toc[0]).name
        if name.casefold() in executable_names:
            exe_contracts[name.casefold()] = (
                exe_toc_path.resolve(strict=True),
                exe_toc,
            )

    analysis_entry_by_destination: dict[str, Mapping[str, Any]] = {}
    for item in portable_entries:
        if item["slot"] not in {"scripts", "pure-modules"}:
            continue
        destination = str(item["destination"])
        folded_destination = destination.casefold()
        prior = analysis_entry_by_destination.setdefault(folded_destination, item)
        if prior["destination"] != destination:
            raise InventoryError("Analysis script/pure destinations collide by case")
    archives: list[dict[str, Any]] = []
    source_items: list[dict[str, Any]] = []
    component_embedded_paths: dict[str, set[str]] = defaultdict(set)
    by_executable: dict[str, dict[str, Any]] = {}
    unattributed_native: list[str] = []

    for folded_name in sorted(executable_names):
        name = executable_names[folded_name]
        pkg_toc_path = supplied[folded_name]
        pkg_toc = _read_pkg_toc(pkg_toc_path)
        try:
            _, exe_toc = exe_contracts[folded_name]
        except KeyError as exc:  # defensive; discovery already rejects this
            raise InventoryError(f"missing EXE TOC for {name}") from exc
        if (
            Path(exe_toc[14]).resolve(strict=True)
            != Path(pkg_toc[0]).resolve(strict=True)
            or exe_toc[15] != pkg_toc[2]
            or Path(exe_toc[21]).name != pkg_toc[3]
        ):
            raise InventoryError(f"EXE/PKG authority changed while inventorying {name}")

        payload_executable = Path(payload_root) / name
        parsed = _parse_carchive(payload_executable, Path(pkg_toc[0]))
        if parsed["python_library"] != pkg_toc[3]:
            raise InventoryError(
                f"CArchive Python library differs from PKG TOC for {name}"
            )
        expected_entries = _pkg_entry_contracts(pkg_toc)
        actual_entries = parsed["entries"]
        if len(actual_entries) != len(expected_entries):
            raise InventoryError(f"CArchive/PKG entry count differs for {name}")
        analysis_script_names = {
            str(item["destination"])
            for item in portable_entries
            if item["slot"] == "scripts"
        }
        pkg_pysource_names = {
            str(item["destination"])
            for item in expected_entries
            if item["kind"] == "PYSOURCE"
        }
        if not analysis_script_names <= pkg_pysource_names:
            raise InventoryError(
                f"Analysis scripts are absent from the executable PKG for {name}"
            )
        for expected in expected_entries:
            if (
                expected["kind"] != "PYSOURCE"
                or expected["destination"] in analysis_script_names
            ):
                continue
            physical, source_ref, _, _ = _physical_source(
                expected["source"], roots=roots
            )
            source_distribution_owners = {
                _normalize_distribution_name(owner)
                for owner in source_owners.get(_windows_path_key(physical), [])
            }
            is_locked_pyinstaller_bootstrap = (
                expected["destination"] == "pyiboot01_bootstrap"
                and source_ref.casefold()
                == (
                    f"{python_root_label}/Lib/site-packages/PyInstaller/loader/"
                    "pyiboot01_bootstrap.py"
                ).casefold()
                and source_distribution_owners == {"pyinstaller"}
            )
            if not is_locked_pyinstaller_bootstrap:
                raise InventoryError(
                    f"executable PKG has an unbound PYSOURCE entry for {name}"
                )

        artifact_executable = (
            f"{artifact_path_base}/{name}" if artifact_path_base else name
        )
        portable_archive_entries: list[dict[str, Any]] = []
        archive_component_ids: set[str] = set()
        for expected, actual in zip(expected_entries, actual_entries, strict=True):
            if (
                expected["archive_name"] != actual["name"]
                or expected["typecode"] != actual["typecode"]
                or expected["compressed"] != actual["compressed"]
            ):
                raise InventoryError(f"CArchive/PKG entry contract differs for {name}")
            kind = expected["kind"]
            embedded_path = f"{artifact_executable}!/{actual['name']}"
            entry_extra: dict[str, Any] = {}
            if kind == "OPTION":
                if actual["content"] != b"":
                    raise InventoryError(
                        "PyInstaller OPTION CArchive entry is not empty"
                    )
                components = ["build-runtime:pyinstaller-bootloader"]
                source_fields: dict[str, Any] = {}
            else:
                physical, source_ref, source_size, source_sha256 = _physical_source(
                    expected["source"], roots=roots
                )
                current_bytes = physical.read_bytes()
                if (
                    len(current_bytes) != source_size
                    or hashlib.sha256(current_bytes).hexdigest() != source_sha256
                ):
                    raise InventoryError(
                        f"PKG source changed while reading: {source_ref}"
                    )
                expected_content = _expected_pkg_entry_content(
                    destination=expected["destination"],
                    source_path=physical,
                    source_bytes=current_bytes,
                    kind=kind,
                )
                if not _pkg_content_matches(actual["content"], expected_content):
                    raise InventoryError(
                        f"CArchive content differs from the PKG source: {name}!/{actual['name']}"
                    )
                analysis_item = analysis_entry_by_destination.get(
                    expected["destination"].casefold()
                )
                if analysis_item is not None and kind in {"PYMODULE", "PYSOURCE"}:
                    if (
                        analysis_item["destination"] != expected["destination"]
                        or analysis_item["type"] != kind
                    ):
                        raise InventoryError(
                            f"Analysis/PKG executable entry contract differs: {name}!/{actual['name']}"
                        )
                    (
                        analysis_physical,
                        analysis_size,
                        analysis_sha256,
                    ) = _physical_source_from_reference(
                        str(analysis_item["source_ref"]), roots=roots
                    )
                    analysis_bytes = analysis_physical.read_bytes()
                    if (
                        len(analysis_bytes) != analysis_size
                        or hashlib.sha256(analysis_bytes).hexdigest() != analysis_sha256
                        or analysis_size != analysis_item["source_size"]
                        or analysis_sha256 != analysis_item["source_sha256"]
                    ):
                        raise InventoryError(
                            f"Analysis executable source changed while reading: {name}!/{actual['name']}"
                        )
                    if kind == "PYMODULE":
                        analysis_expected = _expected_pyz_code(
                            name=expected["destination"],
                            source_path=analysis_physical,
                            source_bytes=analysis_bytes,
                            source_kind="PYMODULE",
                            package=False,
                        )
                    else:
                        analysis_expected = _expected_pkg_entry_content(
                            destination=expected["destination"],
                            source_path=analysis_physical,
                            source_bytes=analysis_bytes,
                            kind=kind,
                        )
                    if not _pkg_content_matches(actual["content"], analysis_expected):
                        raise InventoryError(
                            f"Analysis executable source differs from embedded PKG code: {name}!/{actual['name']}"
                        )
                source_key = _windows_path_key(physical)
                owners = sorted(source_owners.get(source_key, []))
                conda_component_ids = sorted(conda_source_owners.get(source_key, []))
                components = _classify_components(
                    destination=expected["destination"],
                    source_ref=source_ref,
                    kind=kind,
                    distribution_names=owners,
                    conda_component_ids=conda_component_ids,
                    python_root_label=python_root_label,
                    application_root_labels=application_root_labels,
                    generated_root_labels=generated_root_labels,
                )
                source_label = source_ref.split("/", 1)[0]
                destination_folded = expected["destination"].casefold()
                if source_label in generated_root_labels:
                    generated_components: set[str] = set()
                    if kind == "PYMODULE" and destination_folded == "struct":
                        generated_components.add("runtime:cpython")
                    elif destination_folded.startswith("pyimod"):
                        generated_components.add("build-runtime:pyinstaller-bootloader")
                    if generated_components:
                        components = sorted(
                            (set(components) - {"build-input:unattributed"})
                            | generated_components
                        )
                if destination_folded.startswith("pyiboot"):
                    components = sorted(
                        (set(components) - {"python-distribution:pyinstaller"})
                        | {"build-runtime:pyinstaller-bootloader"}
                    )
                if "native:unattributed" in components:
                    unattributed_native.append(embedded_path)
                source_fields = {
                    "conda_component_ids": conda_component_ids,
                    "distribution_names": owners,
                    "source_ref": source_ref,
                    "source_sha256": source_sha256,
                    "source_size": source_size,
                }
                if kind != "PYZ":
                    # PYZ is a generated aggregate. Its physical source/hash is
                    # bound by the archive entry and its fine-grained members
                    # below; merging the aggregate into per-component source
                    # paths would alias every pure distribution/classification.
                    source_items.append(
                        {
                            "component_ids": components,
                            "conda_component_ids": conda_component_ids,
                            "destination": expected["destination"],
                            "distribution_names": owners,
                            "slot": f"embedded:{name}",
                            "source_ref": source_ref,
                            "source_sha256": source_sha256,
                            "source_size": source_size,
                            "type": kind,
                        }
                    )
            if kind == "PYZ":
                bootstrap_module_names = {
                    str(item["destination"])
                    for item in expected_entries
                    if item["kind"] == "PYMODULE"
                }
                pyz_members, pyz_components, entry_extra = _bind_pyz_members(
                    pyz_path=physical,
                    pyz_bytes=actual["content"],
                    portable_entries=portable_entries,
                    virtual_entries=virtual_entries,
                    bootstrap_module_names=bootstrap_module_names,
                    roots=roots,
                    embedded_path=embedded_path,
                )
                components = sorted(pyz_components)
                for member in pyz_members:
                    member_path = f"{embedded_path}#/{member['name']}"
                    for component in member["component_ids"]:
                        component_embedded_paths[component].add(member_path)
            else:
                for component in components:
                    component_embedded_paths[component].add(embedded_path)
            archive_component_ids.update(components)
            portable_archive_entries.append(
                {
                    "component_ids": components,
                    "compressed": actual["compressed"],
                    "content_sha256": actual["content_sha256"],
                    "kind": kind,
                    "name": actual["name"],
                    **entry_extra,
                    **source_fields,
                    "stored_sha256": actual["stored_sha256"],
                    "stored_size": actual["stored_size"],
                    "typecode": actual["typecode"],
                    "uncompressed_size": actual["uncompressed_size"],
                }
            )

        bootloader_tuple = exe_toc[20][0]
        bootloader_path, bootloader_ref, bootloader_size, bootloader_sha = (
            _physical_source(bootloader_tuple[1], roots=roots)
        )
        if sha256_file(bootloader_path) != bootloader_sha:
            raise InventoryError("PyInstaller bootloader changed while inventorying")
        source_items.append(
            {
                "component_ids": ["build-runtime:pyinstaller-bootloader"],
                "conda_component_ids": [],
                "destination": name,
                "distribution_names": [],
                "slot": f"bootloader-prefix:{name}",
                "source_ref": bootloader_ref,
                "source_sha256": bootloader_sha,
                "source_size": bootloader_size,
                "type": "EXECUTABLE",
            }
        )
        component_embedded_paths["build-runtime:pyinstaller-bootloader"].add(
            f"{artifact_executable}!/<bootloader-prefix>"
        )
        archive_component_ids.add("build-runtime:pyinstaller-bootloader")
        archive_material = {
            "bootloader_input": {
                "source_ref": bootloader_ref,
                "source_sha256": bootloader_sha,
                "source_size": bootloader_size,
            },
            "bootloader_prefix_sha256": parsed["bootloader_prefix_sha256"],
            "bootloader_prefix_size": parsed["bootloader_prefix_size"],
            "component_ids": sorted(archive_component_ids),
            "entries": portable_archive_entries,
            "entry_count": len(portable_archive_entries),
            "executable_artifact_path": artifact_executable,
            "executable_sha256": parsed["executable_sha256"],
            "executable_size": parsed["executable_size"],
            "pkg_sha256": parsed["pkg_sha256"],
            "pkg_size": parsed["pkg_size"],
            "python_library": parsed["python_library"],
            "python_version": parsed["python_version"],
        }
        archive_graph_sha256 = hashlib.sha256(
            canonical_json_bytes(archive_material)
        ).hexdigest()
        archive_record = {
            **archive_material,
            "portable_graph_sha256": archive_graph_sha256,
        }
        archives.append(archive_record)
        by_executable[folded_name] = archive_record

    archives.sort(key=lambda item: item["executable_artifact_path"].encode("utf-8"))
    return (
        archives,
        source_items,
        component_embedded_paths,
        by_executable,
        sorted(unattributed_native, key=lambda value: value.encode("utf-8")),
    )


def _validate_auxiliary_toc_paths(
    toc: tuple[Any, ...], roots: Sequence[tuple[str, Path]]
) -> None:
    for slot in (0, 1):
        for raw_path in toc[slot]:
            if not isinstance(raw_path, str) or not Path(raw_path).is_absolute():
                raise InventoryError(
                    f"Analysis TOC slot {slot} contains an unsafe path"
                )
            path = Path(raw_path)
            if slot == 0:
                _regular_file(
                    path,
                    label=f"Analysis TOC slot {slot} path",
                    reject_hardlinks=False,
                )
                _source_reference(path, roots)
            else:
                resolved = _normal_directory(
                    path, label=f"Analysis TOC slot {slot} path"
                )
                if not any(
                    resolved == root or root in resolved.parents for _, root in roots
                ):
                    raise InventoryError(
                        f"Analysis TOC slot {slot} path escapes all allowed roots"
                    )
    for item in toc[3]:
        if (
            not isinstance(item, (tuple, list))
            or len(item) != 2
            or not isinstance(item[0], str)
            or type(item[1]) is not int
        ):
            raise InventoryError("Analysis TOC hook path entry is malformed")
        hook_root = _normal_directory(Path(item[0]), label="Analysis hook path")
        if not any(hook_root == root or root in hook_root.parents for _, root in roots):
            raise InventoryError("Analysis hook path escapes all allowed roots")


def _parse_entries(
    toc: tuple[Any, ...],
    *,
    roots: Sequence[tuple[str, Path]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    entries: list[dict[str, Any]] = []
    virtual_entries: list[dict[str, Any]] = []
    per_slot_destinations: dict[int, dict[str, str]] = defaultdict(dict)
    final_destinations: dict[str, tuple[int, str]] = {}

    for slot, slot_name in TOC_ENTRY_SLOTS.items():
        for raw_entry in toc[slot]:
            if (
                not isinstance(raw_entry, (tuple, list))
                or len(raw_entry) != 3
                or not all(isinstance(item, str) for item in raw_entry)
            ):
                raise InventoryError(f"Analysis TOC slot {slot} entry is malformed")
            raw_destination, raw_source, kind = raw_entry
            if kind not in TOC_SLOT_TYPES[slot]:
                raise InventoryError(
                    f"Analysis TOC slot {slot} contains unexpected type {kind!r}"
                )
            destination = _normalize_destination(
                raw_destination, label=f"TOC slot {slot} destination"
            )
            destination_key = destination.casefold()
            prior = per_slot_destinations[slot].get(destination_key)
            if prior is not None:
                raise InventoryError(
                    f"duplicate/case-colliding TOC destination in slot {slot}: "
                    f"{prior!r} / {destination!r}"
                )
            per_slot_destinations[slot][destination_key] = destination
            if slot in FINAL_PAYLOAD_SLOTS:
                final_prior = final_destinations.setdefault(
                    destination_key, (slot, destination)
                )
                if final_prior != (slot, destination):
                    raise InventoryError(
                        "duplicate/case-colliding final TOC destination: "
                        f"{final_prior[1]!r} / {destination!r}"
                    )

            if raw_source == "-":
                if kind != "PYMODULE":
                    raise InventoryError(
                        "only PYMODULE entries may use virtual source '-'"
                    )
                virtual_entries.append(
                    {
                        "destination": destination,
                        "slot": slot_name,
                        "type": kind,
                    }
                )
                continue
            source_path, source_ref, source_size, source_sha256 = _physical_source(
                raw_source, roots=roots
            )
            entries.append(
                {
                    "destination": destination,
                    "slot": slot_name,
                    "slot_index": slot,
                    "source_key": _windows_path_key(source_path),
                    "source_path": source_path,
                    "source_ref": source_ref,
                    "source_sha256": source_sha256,
                    "source_size": source_size,
                    "type": kind,
                }
            )

    initial_data = {
        item["destination"].casefold(): item
        for item in entries
        if item["slot_index"] == 11
    }
    final_data = {
        item["destination"].casefold(): item
        for item in entries
        if item["slot_index"] == 18
    }
    for destination, initial in initial_data.items():
        final = final_data.get(destination)
        if final is None or (final["source_key"], final["type"]) != (
            initial["source_key"],
            initial["type"],
        ):
            raise InventoryError(
                "input-data TOC entry is not preserved in final data: "
                f"{initial['destination']}"
            )
    entries.sort(
        key=lambda item: (
            item["slot_index"],
            item["destination"].encode("utf-8"),
            item["source_ref"].encode("utf-8"),
        )
    )
    virtual_entries.sort(
        key=lambda item: (
            item["slot"].encode("utf-8"),
            item["destination"].encode("utf-8"),
        )
    )
    return entries, virtual_entries


def _normalize_distribution_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _distribution_file_owners(
    distributions: Iterable[importlib.metadata.Distribution],
) -> tuple[dict[str, list[str]], dict[str, dict[str, str]]]:
    owners: dict[str, set[str]] = defaultdict(set)
    records: dict[str, dict[str, str]] = {}
    for distribution in distributions:
        raw_name = distribution.metadata.get("Name")
        if not isinstance(raw_name, str) or not raw_name.strip():
            raise InventoryError("installed distribution is missing its Name metadata")
        name = _normalize_distribution_name(raw_name)
        version = str(distribution.version)
        prior = records.setdefault(name, {"name": name, "version": version})
        if prior["version"] != version:
            raise InventoryError(f"duplicate installed distribution name: {name}")
        for declared in distribution.files or ():
            try:
                located = Path(distribution.locate_file(declared))
            except (OSError, TypeError, ValueError) as exc:
                raise InventoryError(
                    f"cannot resolve installed file for distribution {name}"
                ) from exc
            if not located.exists() or not located.is_file():
                continue
            try:
                resolved = located.resolve(strict=True)
            except OSError as exc:
                raise InventoryError(
                    f"cannot resolve installed file for distribution {name}"
                ) from exc
            owners[_windows_path_key(resolved)].add(name)
    return (
        {key: sorted(value) for key, value in owners.items()},
        records,
    )


def _load_conda_record(path: Path) -> tuple[dict[str, Any], str, int]:
    details = _regular_file(
        path,
        label="conda-meta record",
        reject_hardlinks=True,
    )
    if details.st_size <= 0 or details.st_size > 32 * 1024 * 1024:
        raise InventoryError(f"conda-meta record size is unsafe: {path.name}")
    try:
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise InventoryError(f"conda-meta record is invalid: {path.name}") from exc
    if not isinstance(value, dict):
        raise InventoryError(f"conda-meta record root is not an object: {path.name}")
    return value, hashlib.sha256(raw).hexdigest(), len(raw)


def _conda_text(record: Mapping[str, Any], key: str, *, record_file: str) -> str:
    value = record.get(key)
    if (
        not isinstance(value, str)
        or not value.strip()
        or any(character in value for character in ("\x00", "\r", "\n"))
    ):
        raise InventoryError(f"conda-meta {record_file} has invalid {key}")
    return value


def _safe_conda_url(value: str, *, label: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise InventoryError(f"unsafe {label} URL in conda-meta")
    return value


def _conda_paths(
    record: Mapping[str, Any],
    *,
    record_file: str,
) -> list[dict[str, Any]]:
    raw_files = record.get("files")
    paths_data = record.get("paths_data")
    if (
        not isinstance(raw_files, list)
        or not all(isinstance(item, str) for item in raw_files)
        or not isinstance(paths_data, dict)
        or paths_data.get("paths_version") != 1
        or not isinstance(paths_data.get("paths"), list)
    ):
        raise InventoryError(f"conda-meta {record_file} file inventory is invalid")
    file_paths: dict[str, str] = {}
    for raw_path in raw_files:
        path = _normalize_destination(raw_path, label="conda package file path")
        key = path.casefold()
        prior = file_paths.setdefault(key, path)
        if prior != path:
            raise InventoryError(
                f"conda-meta {record_file} has a Windows path collision"
            )

    result: list[dict[str, Any]] = []
    paths_data_keys: set[str] = set()
    for raw_entry in paths_data["paths"]:
        if not isinstance(raw_entry, dict):
            raise InventoryError(f"conda-meta {record_file} path entry is invalid")
        raw_path = raw_entry.get("_path")
        if not isinstance(raw_path, str):
            raise InventoryError(f"conda-meta {record_file} path is invalid")
        path = _normalize_destination(raw_path, label="conda paths_data path")
        key = path.casefold()
        if key in paths_data_keys:
            raise InventoryError(f"conda-meta {record_file} path is duplicated")
        paths_data_keys.add(key)
        if file_paths.get(key) != path:
            raise InventoryError(
                f"conda-meta {record_file} files/paths_data differ: {path}"
            )
        path_type = raw_entry.get("path_type")
        if path_type != "hardlink":
            raise InventoryError(
                f"conda-meta {record_file} contains unsupported path type: {path_type!r}"
            )
        in_prefix_digest = raw_entry.get("sha256_in_prefix")
        digest = in_prefix_digest or raw_entry.get("sha256")
        size = raw_entry.get("size_in_bytes")
        if (
            not isinstance(digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
            or type(size) is not int
            or size < 0
        ):
            raise InventoryError(
                f"conda-meta {record_file} lacks an exact path hash/size: {path}"
            )
        result.append(
            {
                "path": path,
                "path_type": path_type,
                "prefix_replaced": in_prefix_digest is not None,
                "record_sha256": digest,
                "record_size": size,
            }
        )
    if paths_data_keys != set(file_paths):
        raise InventoryError(f"conda-meta {record_file} paths_data is incomplete")
    result.sort(key=lambda item: item["path"].encode("utf-8"))
    return result


def _is_selected_conda_native_path(path: str) -> bool:
    folded = path.casefold()
    return (
        folded.startswith("library/bin/")
        and PurePosixPath(folded).suffix in NATIVE_SUFFIXES
    )


def _tree_hash(entries: Iterable[tuple[str, int, str]]) -> str:
    digest = hashlib.sha256()
    for path, size, file_hash in sorted(
        entries, key=lambda item: item[0].encode("utf-8")
    ):
        digest.update(path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(size).encode("ascii"))
        digest.update(b"\0")
        digest.update(file_hash.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def generate_conda_native_registry(
    python_prefix: Path,
    conda_meta_root: Path,
    *,
    target: str = "windows-x86_64",
    python_prefix_label: str = "python-prefix",
) -> dict[str, Any]:
    """Generate the exact lock authority for conda-owned native inputs."""

    if target != "windows-x86_64":
        raise InventoryError("conda native registry target must be windows-x86_64")
    if not ROOT_LABEL.fullmatch(python_prefix_label):
        raise InventoryError("conda native registry python prefix label is invalid")
    prefix = _normal_directory(Path(python_prefix), label="conda Python prefix")
    meta_root = _normal_directory(Path(conda_meta_root), label="conda-meta root")
    try:
        meta_root.relative_to(prefix)
    except ValueError as exc:
        raise InventoryError("conda-meta root escapes the Python prefix") from exc
    if meta_root != prefix / "conda-meta":
        raise InventoryError("conda-meta root must be <python-prefix>/conda-meta")

    record_paths = sorted(
        meta_root.glob("*.json"), key=lambda path: path.name.encode("utf-8")
    )
    if not record_paths:
        raise InventoryError("conda-meta contains no package records")
    record_names: dict[str, str] = {}
    packages: list[dict[str, Any]] = []
    claim_groups: dict[str, dict[str, Any]] = {}
    for record_path in record_paths:
        prior_record = record_names.setdefault(
            record_path.name.casefold(), record_path.name
        )
        if prior_record != record_path.name:
            raise InventoryError("conda-meta record filenames collide under Windows")
        record, record_sha256, record_size = _load_conda_record(record_path)
        raw_record_files = record.get("files")
        if not isinstance(raw_record_files, list) or not all(
            isinstance(item, str) for item in raw_record_files
        ):
            raise InventoryError(
                f"conda-meta {record_path.name} file inventory is invalid"
            )
        if not any(
            _is_selected_conda_native_path(
                _normalize_destination(item, label="conda package file path")
            )
            for item in raw_record_files
        ):
            continue
        files = _conda_paths(record, record_file=record_path.name)

        raw_name = _conda_text(record, "name", record_file=record_path.name)
        name = _normalize_distribution_name(raw_name)
        if not re.fullmatch(r"[a-z0-9][a-z0-9.-]*", name):
            raise InventoryError(f"conda-meta package name is unsafe: {raw_name!r}")
        version = _conda_text(record, "version", record_file=record_path.name)
        build = _conda_text(record, "build", record_file=record_path.name)
        channel = _safe_conda_url(
            _conda_text(record, "channel", record_file=record_path.name),
            label="channel",
        )
        package_url = _safe_conda_url(
            _conda_text(record, "url", record_file=record_path.name),
            label="package",
        )
        license_value = _conda_text(record, "license", record_file=record_path.name)
        subdir = _conda_text(record, "subdir", record_file=record_path.name)
        package_sha256 = record.get("sha256")
        build_number = record.get("build_number")
        if (
            not re.fullmatch(r"[0-9a-f]{64}", str(package_sha256))
            or type(build_number) is not int
            or build_number < 0
            or subdir != "win-64"
        ):
            raise InventoryError(
                f"conda-meta package identity is invalid: {record_path.name}"
            )

        component_id = f"conda-package:{name}"
        for file_entry in files:
            relative = file_entry["path"]
            physical = prefix.joinpath(*PurePosixPath(relative).parts)
            details = _regular_file(
                physical,
                label=f"conda package file {relative!r}",
                reject_hardlinks=False,
            )
            actual_hash = sha256_file(physical)
            matches_record = actual_hash == file_entry["record_sha256"] and (
                file_entry["prefix_replaced"]
                or details.st_size == file_entry["record_size"]
            )
            frozen_file = {
                **file_entry,
                "claim_status": "pending",
                "installed_sha256": actual_hash,
                "installed_size": details.st_size,
            }
            claim = claim_groups.setdefault(
                relative.casefold(),
                {
                    "claims": [],
                    "installed_sha256": actual_hash,
                    "installed_size": details.st_size,
                    "path": relative,
                },
            )
            if (
                claim["path"] != relative
                or claim["installed_sha256"] != actual_hash
                or claim["installed_size"] != details.st_size
            ):
                raise InventoryError(
                    f"conda packages claim conflicting installed paths: {relative}"
                )
            claim["claims"].append(
                {
                    "component_id": component_id,
                    "file": frozen_file,
                    "matches_record": matches_record,
                }
            )
        packages.append(
            {
                "build": build,
                "build_number": build_number,
                "channel": channel,
                "component_id": component_id,
                "files": [
                    claim["file"]
                    for group in claim_groups.values()
                    for claim in group["claims"]
                    if claim["component_id"] == component_id
                ],
                "declared_license": license_value,
                "name": name,
                "package_sha256": package_sha256,
                "package_url": package_url,
                "record_file": record_path.name,
                "record_sha256": record_sha256,
                "record_size": record_size,
                "subdir": subdir,
                "version": version,
            }
        )

    packages.sort(key=lambda item: item["component_id"].encode("utf-8"))
    component_ids = [item["component_id"] for item in packages]
    if len(component_ids) != len(set(component_ids)):
        raise InventoryError("conda native registry contains duplicate package names")
    if not packages:
        raise InventoryError("conda native registry selects no packages")

    shadowed_file_claims = []
    for claim in sorted(
        claim_groups.values(), key=lambda item: item["path"].encode("utf-8")
    ):
        active = sorted(
            item["component_id"] for item in claim["claims"] if item["matches_record"]
        )
        shadowed = sorted(
            item["component_id"]
            for item in claim["claims"]
            if not item["matches_record"]
        )
        if len(active) != 1:
            raise InventoryError(
                "conda native path must have exactly one byte-matching owner: "
                + claim["path"]
            )
        for item in claim["claims"]:
            item["file"]["claim_status"] = (
                "active" if item["matches_record"] else "shadowed"
            )
        if shadowed:
            shadowed_file_claims.append(
                {
                    "active_owners": active,
                    "path": claim["path"],
                    "sha256": claim["installed_sha256"],
                    "shadowed_claimants": shadowed,
                    "size": claim["installed_size"],
                }
            )
    for package in packages:
        package["files"].sort(key=lambda item: item["path"].encode("utf-8"))

    all_files = [
        (
            f"{package['component_id']}/{item['path']}",
            item["installed_size"],
            item["installed_sha256"],
        )
        for package in packages
        for item in package["files"]
    ]
    record_entries = [
        (item["record_file"], item["record_size"], item["record_sha256"])
        for item in packages
    ]
    return {
        "declared_license_semantics": (
            "raw conda-meta license declaration; not asserted to be an SPDX identifier "
            "or expression and requires the release compliance mapping"
        ),
        "file_count": len(all_files),
        "files_tree_sha256": _tree_hash(all_files),
        "generator": dict(CONDA_REGISTRY_GENERATOR),
        "package_count": len(packages),
        "packages": packages,
        "python_prefix_label": python_prefix_label,
        "records_tree_sha256": _tree_hash(record_entries),
        "schema_version": CONDA_REGISTRY_SCHEMA_VERSION,
        "shadowed_file_claim_count": len(shadowed_file_claims),
        "shadowed_file_claims": shadowed_file_claims,
        "target": target,
    }


def validate_conda_native_registry(
    registry: Mapping[str, Any],
    *,
    python_prefix: Path,
    conda_meta_root: Path,
    target: str,
    python_prefix_label: str,
) -> tuple[dict[str, list[str]], dict[str, dict[str, Any]], str]:
    """Validate a frozen registry against every live record and package file."""

    actual = generate_conda_native_registry(
        python_prefix,
        conda_meta_root,
        target=target,
        python_prefix_label=python_prefix_label,
    )
    expected = dict(registry)
    if canonical_json_bytes(expected) != canonical_json_bytes(actual):
        raise InventoryError(
            "conda native registry differs from the physical authority"
        )
    registry_sha256 = hashlib.sha256(canonical_json_bytes(expected)).hexdigest()
    prefix = Path(python_prefix).resolve(strict=True)
    owners: dict[str, list[str]] = {}
    records: dict[str, dict[str, Any]] = {}
    for package in actual["packages"]:
        component_id = package["component_id"]
        records[component_id] = {
            key: value for key, value in package.items() if key != "files"
        }
        for item in package["files"]:
            if item["claim_status"] != "active":
                continue
            physical = prefix.joinpath(*PurePosixPath(item["path"]).parts)
            key = _windows_path_key(physical.resolve(strict=True))
            owners.setdefault(key, []).append(component_id)
    for key, component_ids in owners.items():
        owners[key] = sorted(set(component_ids))
    return owners, records, registry_sha256


def _native_name(value: str) -> str:
    return PurePosixPath(value.replace("\\", "/")).name.casefold()


def _classify_components(
    *,
    destination: str,
    source_ref: str,
    kind: str,
    distribution_names: Sequence[str],
    conda_component_ids: Sequence[str],
    python_root_label: str,
    application_root_labels: frozenset[str],
    generated_root_labels: frozenset[str],
) -> list[str]:
    components = {
        *[f"python-distribution:{name}" for name in distribution_names],
        *conda_component_ids,
    }
    folded_ref = f"/{source_ref.casefold()}"
    folded_destination = f"/{destination.casefold()}"
    basename = _native_name(destination)
    is_native = (
        kind in {"BINARY", "EXTENSION"}
        or PurePosixPath(destination).suffix.casefold() in NATIVE_SUFFIXES
    )

    prefix_marker = f"/{python_root_label.casefold()}/"
    prefix_relative = ""
    if folded_ref.startswith(prefix_marker):
        prefix_relative = folded_ref[len(prefix_marker) :]
    if (
        (
            prefix_relative.startswith("lib/")
            and not prefix_relative.startswith("lib/site-packages/")
        )
        or prefix_relative.startswith("dlls/")
        or re.fullmatch(r"python\d*\.(?:dll|exe)", basename)
    ):
        components.add("runtime:cpython")
    if destination.casefold() == "base_library.zip":
        components.add("runtime:cpython")

    if "/_pyinstaller_hooks_contrib/rthooks/" in folded_ref:
        components.add("build-runtime:pyinstaller-hooks-contrib")
        components.discard("python-distribution:pyinstaller-hooks-contrib")
    elif (
        "/pyinstaller/hooks/rthooks/" in folded_ref
        or "/pyinstaller/fake-modules/_pyi_rth_utils/" in folded_ref
    ):
        components.add("build-runtime:pyinstaller-hooks")
        components.discard("python-distribution:pyinstaller")
    if is_native and (
        basename.startswith(("libcrypto", "libssl"))
        or basename in {"crypto.dll", "openssl.exe", "ssl.dll"}
    ):
        components.add("native:openssl")
    if is_native and "sqlite" in basename:
        components.add("native:sqlite")
    if is_native and basename.startswith(("zlib", "zlib1")):
        components.add("native:zlib")
    if is_native and basename.startswith(
        (
            "api-ms-win-",
            "concrt",
            "msvcp",
            "ucrtbase",
            "vcamp",
            "vccorlib",
            "vcruntime",
        )
    ):
        components.add("native:msvc-runtime")

    has_authoritative_native_owner = bool(
        distribution_names or conda_component_ids or "runtime:cpython" in components
    )
    if is_native and not has_authoritative_native_owner:
        components.add("native:unattributed")
    source_label = source_ref.split("/", 1)[0]
    if not components:
        if source_label in application_root_labels:
            components.add("application:project")
        elif source_label in generated_root_labels:
            components.add("build-input:unattributed")
        elif source_label == python_root_label:
            # Physical site-packages files that Distribution.files does not own
            # are a real PyInstaller input, not evidence that may be discarded.
            components.add("python-distribution:unattributed")
        else:
            components.add("source:unattributed")
    return sorted(components)


def _component_descriptor(
    component_id: str,
    *,
    distribution_records: Mapping[str, Mapping[str, str]],
    conda_records: Mapping[str, Mapping[str, Any]],
    python_version: str,
) -> dict[str, Any]:
    if (
        component_id.startswith("python-distribution:")
        and component_id != "python-distribution:unattributed"
    ):
        name = component_id.split(":", 1)[1]
        record = distribution_records[name]
        return {
            "id": component_id,
            "identity_status": "complete",
            "name": name,
            "type": "python-distribution",
            "version": record["version"],
        }
    if component_id.startswith("conda-package:"):
        try:
            record = conda_records[component_id]
        except KeyError as exc:
            raise InventoryError(
                f"missing conda component authority: {component_id}"
            ) from exc
        return {
            "build": record["build"],
            "build_number": record["build_number"],
            "channel": record["channel"],
            "id": component_id,
            "identity_status": "complete",
            "declared_license": record["declared_license"],
            "name": record["name"],
            "package_sha256": record["package_sha256"],
            "package_url": record["package_url"],
            "record_file": record["record_file"],
            "record_sha256": record["record_sha256"],
            "record_size": record["record_size"],
            "subdir": record["subdir"],
            "type": "conda-native-package",
            "version": record["version"],
        }
    fixed: dict[str, tuple[str, str, str | None]] = {
        "application:project": (
            "Personal Knowledge Vault application code",
            "application",
            None,
        ),
        "build-input:unattributed": (
            "Unattributed generated build input",
            "build-input",
            None,
        ),
        "runtime:cpython": (
            "CPython runtime and standard library",
            "runtime",
            python_version,
        ),
        "build-runtime:pyinstaller-bootloader": (
            "PyInstaller bootloader",
            "runtime",
            distribution_records.get("pyinstaller", {}).get("version"),
        ),
        "build-runtime:pyinstaller-hooks": (
            "PyInstaller runtime hooks",
            "runtime",
            distribution_records.get("pyinstaller", {}).get("version"),
        ),
        "build-runtime:pyinstaller-hooks-contrib": (
            "PyInstaller hooks-contrib runtime hooks",
            "runtime",
            distribution_records.get("pyinstaller-hooks-contrib", {}).get("version"),
        ),
        "native:openssl": ("OpenSSL runtime", "native-library", ssl.OPENSSL_VERSION),
        "native:sqlite": ("SQLite runtime", "native-library", sqlite3.sqlite_version),
        "native:zlib": ("zlib runtime", "native-library", zlib.ZLIB_VERSION),
        "native:msvc-runtime": (
            "Microsoft Visual C++ runtime",
            "native-library",
            None,
        ),
        "native:unattributed": (
            "Unattributed native payload",
            "native-library",
            None,
        ),
        "python-distribution:unattributed": (
            "Unattributed Python distribution input",
            "python-distribution",
            None,
        ),
        "source:unattributed": (
            "Unattributed source input",
            "source",
            None,
        ),
    }
    try:
        name, component_type, version = fixed[component_id]
    except KeyError as exc:
        raise InventoryError(
            f"unknown component classification: {component_id}"
        ) from exc
    classification_only = component_id in {
        "native:msvc-runtime",
        "native:openssl",
        "native:sqlite",
        "native:zlib",
    }
    result: dict[str, Any] = {
        "id": component_id,
        "identity_status": (
            "classification-only"
            if classification_only
            else (
                "complete"
                if version or component_id == "application:project"
                else "requires-compliance-resolution"
            )
        ),
        "name": name,
        "type": component_type,
    }
    if version:
        result["version"] = version
    return result


def _walk_payload(root: Path) -> tuple[list[tuple[str, Path, int, str]], str]:
    resolved_root = _normal_directory(root, label="COLLECT payload root")
    files: list[tuple[str, Path, int, str]] = []
    folded: dict[str, str] = {}

    def visit(directory: Path) -> None:
        try:
            entries = sorted(
                os.scandir(directory), key=lambda item: item.name.encode("utf-8")
            )
        except OSError as exc:
            raise InventoryError(
                f"cannot enumerate COLLECT payload: {directory}"
            ) from exc
        for entry in entries:
            path = Path(entry.path)
            try:
                details = path.lstat()
            except OSError as exc:
                raise InventoryError(f"cannot inspect COLLECT payload: {path}") from exc
            if stat.S_ISLNK(details.st_mode) or _is_reparse(path, details):
                raise InventoryError(
                    f"COLLECT payload links/reparse points are forbidden: {path}"
                )
            if stat.S_ISDIR(details.st_mode):
                visit(path)
                continue
            if not stat.S_ISREG(details.st_mode):
                raise InventoryError(
                    f"COLLECT payload special file is forbidden: {path}"
                )
            if details.st_nlink > 1:
                raise InventoryError(f"COLLECT payload hardlinks are forbidden: {path}")
            relative = path.relative_to(resolved_root).as_posix()
            normalized = _normalize_destination(relative, label="payload path")
            prior = folded.setdefault(normalized.casefold(), normalized)
            if prior != normalized:
                raise InventoryError(
                    f"case-insensitive payload path collision: {prior!r} / {normalized!r}"
                )
            files.append((normalized, path, details.st_size, sha256_file(path)))

    visit(resolved_root)
    if not files:
        raise InventoryError("COLLECT payload is empty")
    files.sort(key=lambda item: item[0].encode("utf-8"))
    tree = hashlib.sha256()
    for relative, _, size, digest in files:
        tree.update(relative.encode("utf-8"))
        tree.update(b"\0")
        tree.update(str(size).encode("ascii"))
        tree.update(b"\0")
        tree.update(digest.encode("ascii"))
        tree.update(b"\n")
    return files, tree.hexdigest()


def build_release_inventory(
    analysis_toc: Path,
    payload_root: Path,
    *,
    source_roots: Mapping[str, Path],
    bootloader_executables: Sequence[str],
    executable_pkg_tocs: Mapping[str, Path],
    conda_native_registry: Mapping[str, Any] | None = None,
    conda_meta_root: Path | None = None,
    distributions: Iterable[importlib.metadata.Distribution] | None = None,
    contents_directory: str = "_internal",
    artifact_path_base: str = "app",
    python_root_label: str = "python-prefix",
    application_root_labels: Sequence[str] = ("source",),
    generated_root_labels: Sequence[str] = ("build-work",),
    python_version: str | None = None,
    target: str = "windows-x86_64",
    fail_on_unattributed_native: bool = True,
    fail_on_unresolved_components: bool = True,
) -> dict[str, Any]:
    """Return a canonical, JSON-serializable Analysis/COLLECT inventory.

    ``source_roots`` must use stable labels.  A typical release invocation is::

        {"python-prefix": Path(sys.prefix),
         "source": source_snapshot,
         "build-work": pyinstaller_work}

    Formal release behavior is the default: unattributed native files and any
    unresolved component identity fail closed.  Diagnostic callers may disable
    either gate explicitly; the unknown evidence remains file-level and visible.
    """

    roots = _prepare_source_roots(source_roots)
    roots_by_label = dict(roots)
    available_root_labels = {label for label, _ in roots}
    if python_root_label not in available_root_labels:
        raise InventoryError("python_root_label does not name a source root")
    application_labels = frozenset(application_root_labels)
    generated_labels = frozenset(generated_root_labels)
    if (
        not application_labels <= available_root_labels
        or not generated_labels <= available_root_labels
        or len(generated_labels) != 1
        or application_labels & generated_labels
        or python_root_label in application_labels | generated_labels
    ):
        raise InventoryError(
            "source root role labels are missing, overlapping, or unsafe"
        )
    contents_directory = _normalize_destination(
        contents_directory, label="PyInstaller contents directory"
    )
    if artifact_path_base in {"", "."}:
        normalized_artifact_path_base = ""
        artifact_path_base_label = "."
    else:
        normalized_artifact_path_base = _normalize_destination(
            artifact_path_base, label="artifact payload path base"
        )
        artifact_path_base_label = normalized_artifact_path_base
    executable_names: dict[str, str] = {}
    for raw_name in bootloader_executables:
        name = _normalize_destination(raw_name, label="bootloader executable")
        if "/" in name or not name.casefold().endswith(".exe"):
            raise InventoryError(f"invalid bootloader executable name: {raw_name!r}")
        prior = executable_names.setdefault(name.casefold(), name)
        if prior != name:
            raise InventoryError("case-colliding bootloader executable names")
    if not executable_names:
        raise InventoryError("at least one bootloader executable name is required")

    toc = _read_toc(Path(analysis_toc))
    _validate_auxiliary_toc_paths(toc, roots)
    entries, virtual_entries = _parse_entries(toc, roots=roots)
    if distributions is None:
        distributions = importlib.metadata.distributions()
    source_owners, distribution_records = _distribution_file_owners(distributions)
    conda_source_owners: dict[str, list[str]] = {}
    conda_records: dict[str, dict[str, Any]] = {}
    conda_registry_sha256: str | None = None
    if conda_native_registry is not None:
        python_prefix = roots_by_label[python_root_label]
        conda_source_owners, conda_records, conda_registry_sha256 = (
            validate_conda_native_registry(
                conda_native_registry,
                python_prefix=python_prefix,
                conda_meta_root=conda_meta_root or python_prefix / "conda-meta",
                target=target,
                python_prefix_label=python_root_label,
            )
        )
    elif fail_on_unattributed_native or fail_on_unresolved_components:
        raise InventoryError(
            "formal release inventory requires a conda native registry"
        )

    source_groups: dict[str, dict[str, Any]] = {}
    portable_entries: list[dict[str, Any]] = []
    final_destinations: dict[str, dict[str, Any]] = {}
    for entry in entries:
        owners = source_owners.get(entry["source_key"], [])
        conda_component_ids = conda_source_owners.get(entry["source_key"], [])
        components = _classify_components(
            destination=entry["destination"],
            source_ref=entry["source_ref"],
            kind=entry["type"],
            distribution_names=owners,
            conda_component_ids=conda_component_ids,
            python_root_label=python_root_label,
            application_root_labels=application_labels,
            generated_root_labels=generated_labels,
        )
        portable = {
            "component_ids": components,
            "conda_component_ids": conda_component_ids,
            "destination": entry["destination"],
            "distribution_names": owners,
            "slot": entry["slot"],
            "source_ref": entry["source_ref"],
            "source_sha256": entry["source_sha256"],
            "source_size": entry["source_size"],
            "type": entry["type"],
        }
        portable_entries.append(portable)
        if entry["slot_index"] in FINAL_PAYLOAD_SLOTS:
            final_destinations[entry["destination"].casefold()] = portable

        group = source_groups.setdefault(
            entry["source_ref"],
            {
                "component_ids": set(),
                "conda_component_ids": set(),
                "distribution_names": set(),
                "occurrences": [],
                "path": entry["source_ref"],
                "sha256": entry["source_sha256"],
                "size": entry["source_size"],
            },
        )
        if (group["sha256"], group["size"]) != (
            entry["source_sha256"],
            entry["source_size"],
        ):
            raise InventoryError(
                f"TOC source changed while inventorying: {entry['source_ref']}"
            )
        group["component_ids"].update(components)
        group["conda_component_ids"].update(conda_component_ids)
        group["distribution_names"].update(owners)
        group["occurrences"].append(
            {
                "destination": entry["destination"],
                "slot": entry["slot"],
                "type": entry["type"],
            }
        )

    portable_entries.sort(
        key=lambda item: (
            item["slot"].encode("utf-8"),
            item["destination"].encode("utf-8"),
            item["source_ref"].encode("utf-8"),
        )
    )
    portable_graph = {
        "entries": portable_entries,
        "virtual_entries": virtual_entries,
    }
    portable_graph_sha256 = hashlib.sha256(
        canonical_json_bytes(portable_graph)
    ).hexdigest()
    (
        embedded_archives,
        embedded_source_items,
        component_embedded_paths,
        embedded_by_executable,
        embedded_unattributed_native_paths,
    ) = _build_embedded_archives(
        work_root=roots_by_label[sorted(generated_labels)[0]],
        payload_root=Path(payload_root),
        executable_pkg_tocs=executable_pkg_tocs,
        executable_names=executable_names,
        roots=roots,
        source_owners=source_owners,
        conda_source_owners=conda_source_owners,
        python_root_label=python_root_label,
        application_root_labels=application_labels,
        generated_root_labels=generated_labels,
        portable_entries=portable_entries,
        virtual_entries=virtual_entries,
        artifact_path_base=normalized_artifact_path_base,
    )
    for entry in embedded_source_items:
        group = source_groups.setdefault(
            entry["source_ref"],
            {
                "component_ids": set(),
                "conda_component_ids": set(),
                "distribution_names": set(),
                "occurrences": [],
                "path": entry["source_ref"],
                "sha256": entry["source_sha256"],
                "size": entry["source_size"],
            },
        )
        if (group["sha256"], group["size"]) != (
            entry["source_sha256"],
            entry["source_size"],
        ):
            raise InventoryError(
                f"embedded source changed while inventorying: {entry['source_ref']}"
            )
        group["component_ids"].update(entry["component_ids"])
        group["conda_component_ids"].update(entry["conda_component_ids"])
        group["distribution_names"].update(entry["distribution_names"])
        group["occurrences"].append(
            {
                "destination": entry["destination"],
                "slot": entry["slot"],
                "type": entry["type"],
            }
        )

    payload_files, payload_tree_sha256 = _walk_payload(Path(payload_root))
    payload_records: list[dict[str, Any]] = []
    component_payload_paths: dict[str, set[str]] = defaultdict(set)
    unattributed_native_paths: list[str] = list(embedded_unattributed_native_paths)
    seen_executables: set[str] = set()
    prefix = f"{contents_directory}/"
    for relative, _, size, payload_sha256 in payload_files:
        artifact_path = (
            f"{normalized_artifact_path_base}/{relative}"
            if normalized_artifact_path_base
            else relative
        )
        if relative.casefold() in executable_names:
            if executable_names[relative.casefold()] != relative:
                raise InventoryError(
                    "bootloader executable case differs under Windows semantics: "
                    f"{relative!r} / {executable_names[relative.casefold()]!r}"
                )
            seen_executables.add(relative.casefold())
            archive_record = embedded_by_executable[relative.casefold()]
            if (
                archive_record["executable_sha256"] != payload_sha256
                or archive_record["executable_size"] != size
            ):
                raise InventoryError(
                    f"payload executable changed after CArchive validation: {relative}"
                )
            components = list(archive_record["component_ids"])
            record = {
                "component_ids": components,
                "embedded_archive_graph_sha256": archive_record[
                    "portable_graph_sha256"
                ],
                "embedded_component_ids": components,
                "embedded_entry_count": archive_record["entry_count"],
                "embedded_pkg_sha256": archive_record["pkg_sha256"],
                "embedded_pkg_size": archive_record["pkg_size"],
                "kind": "PYINSTALLER_BOOTLOADER_EXECUTABLE",
                "artifact_path": artifact_path,
                "path": relative,
                "sha256": payload_sha256,
                "size": size,
            }
        else:
            if not relative.casefold().startswith(prefix.casefold()):
                raise InventoryError(
                    f"payload file is outside the PyInstaller contents directory: {relative}"
                )
            destination = relative[len(prefix) :]
            toc_entry = final_destinations.get(destination.casefold())
            if toc_entry is None:
                raise InventoryError(
                    f"payload file has no final Analysis TOC mapping: {relative}"
                )
            if toc_entry["destination"] != destination:
                raise InventoryError(
                    "payload/TOC destination case differs under Windows semantics: "
                    f"{relative!r} / {toc_entry['destination']!r}"
                )
            if (
                payload_sha256 != toc_entry["source_sha256"]
                or size != toc_entry["source_size"]
            ):
                raise InventoryError(
                    f"payload bytes differ from the Analysis source: {relative}"
                )
            components = list(toc_entry["component_ids"])
            if not components:
                raise InventoryError(
                    f"payload file has no component mapping: {relative}"
                )
            if "native:unattributed" in components:
                unattributed_native_paths.append(artifact_path)
            record = {
                "artifact_path": artifact_path,
                "component_ids": components,
                "conda_component_ids": toc_entry["conda_component_ids"],
                "distribution_names": toc_entry["distribution_names"],
                "kind": toc_entry["type"],
                "path": relative,
                "sha256": payload_sha256,
                "size": size,
                "source_ref": toc_entry["source_ref"],
                "source_sha256": toc_entry["source_sha256"],
                "toc_destination": toc_entry["destination"],
            }
        payload_records.append(record)
        if relative.casefold() in executable_names:
            component_payload_paths["build-runtime:pyinstaller-bootloader"].add(
                artifact_path
            )
        else:
            for component in components:
                component_payload_paths[component].add(artifact_path)
    missing_executables = sorted(set(executable_names) - seen_executables)
    if missing_executables:
        raise InventoryError(
            f"expected bootloader executables are missing: {missing_executables}"
        )
    if fail_on_unattributed_native and unattributed_native_paths:
        raise InventoryError(
            "native payload lacks a resolved component identity: "
            + ", ".join(sorted(unattributed_native_paths))
        )

    source_records: list[dict[str, Any]] = []
    component_source_paths: dict[str, set[str]] = defaultdict(set)
    unowned_source_paths: list[str] = []
    included_distribution_sources: dict[str, set[str]] = defaultdict(set)
    included_conda_sources: dict[str, set[str]] = defaultdict(set)
    for source_ref, group in sorted(
        source_groups.items(), key=lambda item: item[0].encode("utf-8")
    ):
        components = sorted(group["component_ids"])
        conda_component_ids = sorted(group["conda_component_ids"])
        owners = sorted(group["distribution_names"])
        occurrences = sorted(
            group["occurrences"],
            key=lambda item: (
                item["slot"].encode("utf-8"),
                item["destination"].encode("utf-8"),
            ),
        )
        source_records.append(
            {
                "component_ids": components,
                "conda_component_ids": conda_component_ids,
                "distribution_names": owners,
                "occurrences": occurrences,
                "path": source_ref,
                "sha256": group["sha256"],
                "size": group["size"],
            }
        )
        if not owners and not conda_component_ids:
            unowned_source_paths.append(source_ref)
        for owner in owners:
            included_distribution_sources[owner].add(source_ref)
        for component_id in conda_component_ids:
            included_conda_sources[component_id].add(source_ref)
        for component in components:
            component_source_paths[component].add(source_ref)

    included_distributions = []
    for name, source_paths in sorted(included_distribution_sources.items()):
        record = distribution_records[name]
        included_distributions.append(
            {
                "name": name,
                "source_paths": sorted(
                    source_paths, key=lambda value: value.encode("utf-8")
                ),
                "version": record["version"],
            }
        )

    included_conda_packages = []
    for component_id, source_paths in sorted(included_conda_sources.items()):
        authority = conda_records[component_id]
        included_conda_packages.append(
            {
                "build": authority["build"],
                "channel": authority["channel"],
                "component_id": component_id,
                "declared_license": authority["declared_license"],
                "name": authority["name"],
                "package_sha256": authority["package_sha256"],
                "record_sha256": authority["record_sha256"],
                "record_size": authority["record_size"],
                "source_paths": sorted(
                    source_paths, key=lambda value: value.encode("utf-8")
                ),
                "version": authority["version"],
            }
        )

    all_component_ids = (
        set(component_source_paths)
        | set(component_payload_paths)
        | set(component_embedded_paths)
    )
    native_component_ids: set[str] = set()
    native_kinds = {"BINARY", "EXECUTABLE", "EXTENSION"}
    native_suffixes = (".dll", ".dylib", ".exe", ".pyd", ".so")
    for record in payload_records:
        if record["kind"] == "PYINSTALLER_BOOTLOADER_EXECUTABLE":
            native_component_ids.add("build-runtime:pyinstaller-bootloader")
            continue
        logical_path = str(
            record.get("toc_destination") or record.get("artifact_path") or ""
        ).casefold()
        if record["kind"] in native_kinds or logical_path.endswith(native_suffixes):
            native_component_ids.update(record["component_ids"])
    for archive in embedded_archives:
        for entry in archive["entries"]:
            logical_path = str(entry["name"]).casefold()
            if entry["kind"] in native_kinds or logical_path.endswith(native_suffixes):
                native_component_ids.update(entry["component_ids"])
    component_records = []
    for component_id in sorted(all_component_ids):
        descriptor = _component_descriptor(
            component_id,
            distribution_records=distribution_records,
            conda_records=conda_records,
            python_version=python_version or platform.python_version(),
        )
        descriptor["payload_paths"] = sorted(
            component_payload_paths.get(component_id, set()),
            key=lambda value: value.encode("utf-8"),
        )
        descriptor["embedded_paths"] = sorted(
            component_embedded_paths.get(component_id, set()),
            key=lambda value: value.encode("utf-8"),
        )
        descriptor["source_paths"] = sorted(
            component_source_paths.get(component_id, set()),
            key=lambda value: value.encode("utf-8"),
        )
        if descriptor["identity_status"] == "complete":
            descriptor["contains_native_payload"] = component_id in native_component_ids
        component_records.append(descriptor)

    classification_paths = {
        item["id"]: {
            *item["payload_paths"],
            *item["embedded_paths"],
            *item["source_paths"],
        }
        for item in component_records
        if item["identity_status"] == "classification-only"
    }
    for item in component_records:
        component_paths = {
            *item["payload_paths"],
            *item["embedded_paths"],
            *item["source_paths"],
        }
        item["classification_ids"] = sorted(
            (
                identifier
                for identifier, paths in classification_paths.items()
                if identifier != item["id"] and component_paths & paths
            ),
            key=lambda value: value.encode("utf-8"),
        )

    embedded_archives_sha256 = hashlib.sha256(
        canonical_json_bytes(embedded_archives)
    ).hexdigest()
    portable_binding_material = {
        "analysis_graph_sha256": portable_graph_sha256,
        "artifact_path_base": artifact_path_base_label,
        "conda_native_registry_sha256": conda_registry_sha256,
        "embedded_archives_sha256": embedded_archives_sha256,
        "payload_tree_sha256": payload_tree_sha256,
    }
    closure_sha256 = hashlib.sha256(
        canonical_json_bytes(portable_binding_material)
    ).hexdigest()
    unresolved_component_ids = sorted(
        item["id"]
        for item in component_records
        if item["identity_status"] == "requires-compliance-resolution"
    )
    if fail_on_unresolved_components and unresolved_component_ids:
        raise InventoryError(
            "payload closure has unresolved component identities: "
            + ", ".join(unresolved_component_ids)
        )
    return {
        "analysis": {
            "entry_count": len(portable_entries),
            "portable_graph_sha256": portable_graph_sha256,
            "source_count": len(source_records),
            "sources": source_records,
            "virtual_entries": virtual_entries,
        },
        "bindings": {
            **portable_binding_material,
            "closure_sha256": closure_sha256,
        },
        "components": component_records,
        "coverage": {
            "conda_native_registry_sha256": conda_registry_sha256,
            "embedded_archive_count": len(embedded_archives),
            "embedded_entry_count": sum(
                item["entry_count"] for item in embedded_archives
            ),
            "payload_file_count": len(payload_records),
            "unattributed_native_file_count": len(unattributed_native_paths),
            "unattributed_native_paths": sorted(unattributed_native_paths),
            "unowned_source_path_count": len(unowned_source_paths),
            "unowned_source_paths": unowned_source_paths,
            "unresolved_component_ids": unresolved_component_ids,
        },
        "included_conda_packages": included_conda_packages,
        "included_distributions": included_distributions,
        "embedded_archives": embedded_archives,
        "payload": {
            "file_count": len(payload_records),
            "files": payload_records,
            "path_base": artifact_path_base_label,
            "tree_sha256": payload_tree_sha256,
        },
        "schema_version": SCHEMA_VERSION,
    }


__all__ = [
    "CONDA_REGISTRY_SCHEMA_VERSION",
    "InventoryError",
    "SCHEMA_VERSION",
    "build_release_inventory",
    "canonical_json_bytes",
    "discover_executable_pkg_tocs",
    "generate_conda_native_registry",
    "sha256_file",
    "validate_conda_native_registry",
]


def _main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate the frozen conda native lock"
    )
    parser.add_argument("--generate-conda-registry", type=Path, metavar="OUTPUT")
    parser.add_argument("--python-prefix", type=Path, default=Path(sys.prefix))
    parser.add_argument("--conda-meta", type=Path)
    parser.add_argument("--target", default="windows-x86_64")
    arguments = parser.parse_args(argv)
    if arguments.generate_conda_registry is None:
        parser.error("--generate-conda-registry OUTPUT is required")
    prefix = arguments.python_prefix
    registry = generate_conda_native_registry(
        prefix,
        arguments.conda_meta or prefix / "conda-meta",
        target=arguments.target,
    )
    output = _absolute_unresolved(arguments.generate_conda_registry)
    parent = _normal_directory(output.parent, label="registry output directory")
    if output.parent.resolve(strict=True) != parent:
        raise InventoryError("registry output parent is unsafe")
    if output.exists() or output.is_symlink():
        _regular_file(
            output,
            label="registry output",
            reject_hardlinks=True,
        )
    temporary = output.with_name(output.name + ".tmp")
    if temporary.exists() or temporary.is_symlink():
        raise InventoryError(f"temporary registry output already exists: {temporary}")
    try:
        with temporary.open("xb") as stream:
            stream.write(canonical_json_bytes(registry))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)
    print(
        f"wrote {output} packages={registry['package_count']} "
        f"files={registry['file_count']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
