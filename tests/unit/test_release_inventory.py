"""Pure contract tests for the PyInstaller release inventory."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import marshal
import os
import struct
import sys
import types
import zlib
from pathlib import Path, PurePosixPath
from typing import Any

import pytest

from scripts import release_inventory


pytestmark = pytest.mark.packaging_contract
PROJECT_ROOT = Path(__file__).parents[2]


class _FakeDistribution:
    def __init__(self, name: str, version: str, prefix: Path, files: list[str]) -> None:
        self.metadata = {"Name": name}
        self.version = version
        self._prefix = prefix
        self.files = [PurePosixPath(value) for value in files]

    def locate_file(self, declared: PurePosixPath) -> Path:
        return self._prefix.joinpath(*declared.parts)


def _write(path: Path, content: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _empty_toc(source: Path) -> list[Any]:
    hooks = source / "hooks"
    hooks.mkdir(exist_ok=True)
    return [
        [str(source / "app.py")],
        [str(source)],
        [],
        [(str(hooks), 1000)],
        {},
        [],
        [],
        False,
        {},
        0,
        [],
        [],
        "3.11-test",
        [],
        [],
        [],
        [],
        [],
        [],
        [],
    ]


def _write_toc(path: Path, value: list[Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(repr(tuple(value)), encoding="utf-8", newline="\n")
    return path


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


def _pyc_bytes(source: bytes, *, filename: str) -> bytes:
    code = compile(
        source,
        filename,
        "exec",
        flags=0,
        dont_inherit=True,
        optimize=0,
    )
    return importlib.util.MAGIC_NUMBER + (b"\0" * 12) + marshal.dumps(code)


def _source_archive_bytes(source: bytes, *, destination: str) -> bytes:
    code = compile(
        source,
        destination + ".py",
        "exec",
        flags=0,
        dont_inherit=True,
        optimize=0,
    )
    return marshal.dumps(_replace_code_filename(code, destination + ".py"))


def _module_archive_bytes(pyc: bytes, *, destination: str) -> bytes:
    code = marshal.loads(pyc[16:])
    assert isinstance(code, types.CodeType)
    return marshal.dumps(_replace_code_filename(code, destination + ".py"))


def _write_carchive(
    package: Path,
    *,
    entries: list[tuple[str, bytes, str, bool]],
    python_library: str = "python311.dll",
    python_version: int = 311,
    compressed_trailing: bytes = b"",
) -> bytes:
    """Write the subset of the PyInstaller 6.21 CArchive wire format we consume."""

    data_parts: list[bytes] = []
    toc_parts: list[bytes] = []
    offset = 0
    header_format = "!IIIIBc"
    header_size = struct.calcsize(header_format)
    for name, content, typecode, compressed in entries:
        stored = (
            zlib.compress(content, 9) + compressed_trailing if compressed else content
        )
        encoded_name = name.encode("utf-8") + b"\0"
        entry_length = ((header_size + len(encoded_name) + 15) // 16) * 16
        name_field = encoded_name + (
            b"\0" * (entry_length - header_size - len(encoded_name))
        )
        toc_parts.append(
            struct.pack(
                header_format,
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
    library_field = python_library.encode("ascii") + b"\0"
    library_field += b"\0" * (64 - len(library_field))
    cookie_format = "!8sIIII64s"
    archive_length = len(data) + len(toc) + struct.calcsize(cookie_format)
    cookie = struct.pack(
        cookie_format,
        b"MEI\x0c\x0b\x0a\x0b\x0e",
        archive_length,
        len(data),
        len(toc),
        python_version,
        library_field,
    )
    package_bytes = data + toc + cookie
    _write(package, package_bytes)
    return package_bytes


def _write_pyz(
    path: Path,
    *,
    members: list[tuple[str, Path | None, str, bool]],
) -> Path:
    data_parts: list[bytes] = []
    toc: list[tuple[str, tuple[int, int, int]]] = []
    source_toc: list[tuple[str, str, str]] = []
    offset = release_inventory.PYZ_HEADER_SIZE
    for name, source_path, source_kind, package in members:
        if source_path is None:
            toc.append((name, (3, offset, 0)))
            source_toc.append((name, "-", source_kind))
            continue
        source_bytes = source_path.read_bytes()
        optimize = {"PYMODULE": 0, "PYMODULE-1": 1, "PYMODULE-2": 2}[source_kind]
        code = compile(
            source_bytes,
            os.fspath(source_path),
            "exec",
            flags=0,
            dont_inherit=True,
            optimize=optimize,
        )
        relative = os.path.join(*name.split("."))
        filename = (
            os.path.join(relative, "__init__.py") if package else relative + ".py"
        )
        content = marshal.dumps(_replace_code_filename(code, filename))
        stored = zlib.compress(content, 6)
        data_parts.append(stored)
        toc.append((name, (1 if package else 0, offset, len(stored))))
        source_toc.append((name, str(source_path), source_kind))
        offset += len(stored)
    header = (
        b"PYZ\0" + importlib.util.MAGIC_NUMBER + struct.pack("!i", offset) + b"\0" * 5
    )
    _write(path, header + b"".join(data_parts) + marshal.dumps(toc))
    _write_toc(path.with_suffix(".toc"), [str(path), source_toc])
    return path


def test_carchive_rejects_trailing_compressed_entry_bytes(tmp_path: Path) -> None:
    package = tmp_path / "bad.pkg"
    executable = tmp_path / "bad.exe"
    package_bytes = _write_carchive(
        package,
        entries=[("payload", b"bounded", "x", True)],
        compressed_trailing=b"hidden",
    )
    executable.write_bytes(b"bootloader" + package_bytes)

    with pytest.raises(
        release_inventory.InventoryError,
        match="trailing/incomplete compressed data",
    ):
        release_inventory._parse_carchive(executable, package)


@pytest.mark.parametrize(
    ("limit_name", "limit_value", "message"),
    [
        (
            "MAX_BOOTLOADER_PREFIX_BYTES",
            4,
            "bootloader prefix size is outside accepted bounds",
        ),
        ("MAX_CARCHIVE_TOC_BYTES", 1, "CArchive TOC bounds are invalid"),
        ("MAX_CARCHIVE_ENTRIES", 0, "CArchive has too many entries"),
    ],
)
def test_carchive_enforces_structural_resource_limits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    limit_name: str,
    limit_value: int,
    message: str,
) -> None:
    package = tmp_path / "bounded.pkg"
    executable = tmp_path / "bounded.exe"
    package_bytes = _write_carchive(
        package,
        entries=[("payload", b"bounded", "x", True)],
    )
    executable.write_bytes(b"bootloader" + package_bytes)
    monkeypatch.setattr(release_inventory, limit_name, limit_value)

    with pytest.raises(release_inventory.InventoryError, match=message):
        release_inventory._parse_carchive(executable, package)


def test_pyz_namespaces_are_exactly_bound_to_analysis_virtual_entries(
    tmp_path: Path,
) -> None:
    prefix = tmp_path / "python"
    child = _write(
        prefix / "Lib" / "site-packages" / "demo_namespace" / "child.py",
        b"VALUE = 1\n",
    )
    pyz = _write_pyz(
        tmp_path / "PYZ-00.pyz",
        members=[
            ("demo_namespace", None, "PYMODULE", False),
            ("demo_namespace.child", child, "PYMODULE", False),
        ],
    )
    child_bytes = child.read_bytes()
    portable_entries = [
        {
            "component_ids": ["python-distribution:demo"],
            "conda_component_ids": [],
            "destination": "demo_namespace.child",
            "distribution_names": ["demo"],
            "slot": "pure-modules",
            "source_ref": "python-prefix/Lib/site-packages/demo_namespace/child.py",
            "source_sha256": hashlib.sha256(child_bytes).hexdigest(),
            "source_size": len(child_bytes),
        }
    ]
    virtual_entries = [
        {
            "destination": "demo_namespace",
            "slot": "pure-modules",
            "type": "PYMODULE",
        }
    ]

    members, component_ids, _ = release_inventory._bind_pyz_members(
        pyz_path=pyz,
        pyz_bytes=pyz.read_bytes(),
        portable_entries=portable_entries,
        virtual_entries=virtual_entries,
        bootstrap_module_names=set(),
        roots=[("python-prefix", prefix)],
        embedded_path="app/pkv.exe!/PYZ.pyz",
    )
    assert [item["name"] for item in members] == [
        "demo_namespace",
        "demo_namespace.child",
    ]
    assert members[0]["component_ids"] == ["python-distribution:demo"]
    assert component_ids == {"python-distribution:demo"}

    with pytest.raises(
        release_inventory.InventoryError,
        match="Analysis virtual pure modules differ from PYZ namespace members",
    ):
        release_inventory._bind_pyz_members(
            pyz_path=pyz,
            pyz_bytes=pyz.read_bytes(),
            portable_entries=portable_entries,
            virtual_entries=[],
            bootstrap_module_names=set(),
            roots=[("python-prefix", prefix)],
            embedded_path="app/pkv.exe!/PYZ.pyz",
        )


def test_pyz_rejects_excessive_total_uncompressed_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _write(tmp_path / "large.py", b"VALUE = 'bounded'\n")
    pyz = _write_pyz(
        tmp_path / "PYZ-00.pyz",
        members=[("large", source, "PYMODULE", False)],
    )
    monkeypatch.setattr(release_inventory, "MAX_ARCHIVE_TOTAL_UNCOMPRESSED_BYTES", 8)

    with pytest.raises(
        release_inventory.InventoryError,
        match="PyInstaller PYZ exceeds the total size limit",
    ):
        release_inventory._parse_pyz_archive(pyz.read_bytes())


def test_marshaled_pkg_content_rejects_hidden_trailing_bytes() -> None:
    source = b"VALUE = 1\n"
    code = compile(source, "module.py", "exec", flags=0, dont_inherit=True, optimize=0)
    content = marshal.dumps(code)

    assert release_inventory._pkg_content_matches(content, code)
    assert not release_inventory._pkg_content_matches(content + b"hidden", code)


def _write_conda_record(
    prefix: Path,
    *,
    name: str,
    version: str,
    build: str,
    license_value: str,
    files: list[str],
    digest_overrides: dict[str, str] | None = None,
) -> Path:
    digest_overrides = digest_overrides or {}
    package_digest = hashlib.sha256(f"{name}-{version}-{build}".encode()).hexdigest()
    paths = []
    for relative in files:
        physical = prefix.joinpath(*PurePosixPath(relative).parts)
        paths.append(
            {
                "_path": relative,
                "path_type": "hardlink",
                "sha256": digest_overrides.get(
                    relative, release_inventory.sha256_file(physical)
                ),
                "size_in_bytes": physical.stat().st_size,
            }
        )
    filename = f"{name}-{version}-{build}.json"
    record = {
        "build": build,
        "build_number": 0,
        "channel": "https://repo.example.invalid/win-64",
        "files": files,
        "license": license_value,
        "name": name,
        "paths_data": {"paths": paths, "paths_version": 1},
        "sha256": package_digest,
        "subdir": "win-64",
        "url": f"https://repo.example.invalid/win-64/{name}-{version}-{build}.conda",
        "version": version,
    }
    path = prefix / "conda-meta" / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, sort_keys=True), encoding="utf-8", newline="\n")
    return path


def _fixture(tmp_path: Path) -> dict[str, Any]:
    prefix = tmp_path / "python"
    source = tmp_path / "source"
    work = tmp_path / "work"
    payload = tmp_path / "payload"
    for directory in (prefix, source, work, payload):
        directory.mkdir()

    app_source = b"print('app')\n"
    app = _write(source / "app.py", app_source)
    icon = _write(source / "app.ico", b"synthetic-icon")
    config = _write(source / "config" / "config.yaml", b"provider: null\n")
    demo_module = _write(
        prefix / "Lib" / "site-packages" / "demo" / "__init__.py",
        b"NAME = 'demo'\n",
    )
    demo_extension = _write(
        prefix / "Lib" / "site-packages" / "demo" / "ext.pyd", b"demo-ext"
    )
    runtime_hook = _write(
        prefix
        / "Lib"
        / "site-packages"
        / "PyInstaller"
        / "hooks"
        / "rthooks"
        / "pyi_rth_demo.py",
        b"# runtime hook\n",
    )
    pyinstaller_bootstrap = _write(
        prefix
        / "Lib"
        / "site-packages"
        / "PyInstaller"
        / "loader"
        / "pyiboot01_bootstrap.py",
        b"# bootstrap\n",
    )
    native_sources = {
        "python311.dll": _write(prefix / "python311.dll", b"python"),
        "_sqlite3.pyd": _write(prefix / "DLLs" / "_sqlite3.pyd", b"sqlite-ext"),
        "libssl-3-x64.dll": _write(
            prefix / "Library" / "bin" / "libssl-3-x64.dll", b"openssl"
        ),
        "sqlite3.dll": _write(prefix / "Library" / "bin" / "sqlite3.dll", b"sqlite"),
        "zlib.dll": _write(prefix / "Library" / "bin" / "zlib.dll", b"zlib"),
        "msvcp140.dll": _write(prefix / "Library" / "bin" / "msvcp140.dll", b"msvc"),
    }
    base_library = _write(work / "base_library.zip", b"stdlib-zip")
    stdlib_module = _write(prefix / "Lib" / "pathlib.py", b"# stdlib\n")
    struct_source = _write(prefix / "Lib" / "struct.py", b"STRUCT_MARKER = 1\n")
    struct_pyc = _write(
        work / "localpycs" / "struct.pyc",
        _pyc_bytes(b"STRUCT_MARKER = 1\n", filename="struct.py"),
    )
    pyz = _write_pyz(
        work / "PYZ-00.pyz",
        members=[("demo", demo_module, "PYMODULE", True)],
    )
    bootloader = _write(work / "bootloader" / "run.exe", b"MZ-synthetic-bootloader")
    _write_conda_record(
        prefix,
        name="openssl",
        version="3.6.3",
        build="test_0",
        license_value="Apache-2.0",
        files=["Library/bin/libssl-3-x64.dll"],
    )
    _write_conda_record(
        prefix,
        name="sqlite",
        version="3.50.0",
        build="test_0",
        license_value="blessing",
        files=["Library/bin/sqlite3.dll"],
    )
    _write_conda_record(
        prefix,
        name="zlib",
        version="1.3.1",
        build="test_0",
        license_value="Zlib",
        files=["Library/bin/zlib.dll"],
    )
    _write_conda_record(
        prefix,
        name="vs2015_runtime",
        version="14.44.35208",
        build="test_0",
        license_value="LicenseRef-Microsoft-VCRuntime",
        files=["Library/bin/msvcp140.dll"],
    )
    conda_registry = release_inventory.generate_conda_native_registry(
        prefix, prefix / "conda-meta"
    )

    toc = _empty_toc(source)
    toc[11] = [("config/config.yaml", str(config), "DATA")]
    toc[13] = [
        ("app", str(app), "PYSOURCE"),
        ("pyi_rth_demo", str(runtime_hook), "PYSOURCE"),
    ]
    # PyInstaller lists bootstrap modules such as struct in Analysis pure, but
    # moves them out of PYZ into the outer CArchive. The inventory must bind
    # the real PYZ rather than copying the Analysis slot wholesale.
    toc[14] = [
        ("demo", str(demo_module), "PYMODULE"),
        ("struct", str(struct_source), "PYMODULE"),
    ]
    toc[15] = [
        (name, str(path), "EXTENSION" if name.endswith(".pyd") else "BINARY")
        for name, path in native_sources.items()
    ]
    toc[15].extend(
        [
            ("demo/ext.pyd", str(demo_extension), "EXTENSION"),
            ("base_library.zip", str(base_library), "DATA"),
        ]
    )
    toc[18] = [("config/config.yaml", str(config), "DATA")]
    toc[19] = [("pathlib", str(stdlib_module), "PYMODULE")]
    toc_path = _write_toc(work / "Analysis-00.toc", toc)

    pkg_path = work / "pkv.pkg"
    pkg_entries = [
        ("X utf8", None, "OPTION"),
        ("PYZ-00.pyz", str(pyz), "PYZ"),
        ("struct", str(struct_pyc), "PYMODULE"),
        ("pyiboot01_bootstrap", str(pyinstaller_bootstrap), "PYSOURCE"),
        ("pyi_rth_demo", str(runtime_hook), "PYSOURCE"),
        ("app", str(app), "PYSOURCE"),
    ]
    pkg_compression = {
        "BINARY": True,
        "DATA": True,
        "EXECUTABLE": True,
        "EXTENSION": True,
        "PYMODULE": True,
        "PYSOURCE": True,
        "PYZ": False,
        "SPLASH": True,
        "SYMLINK": False,
    }
    package_bytes = _write_carchive(
        pkg_path,
        entries=[
            (
                "struct",
                _module_archive_bytes(struct_pyc.read_bytes(), destination="struct"),
                "m",
                True,
            ),
            (
                "pyiboot01_bootstrap",
                _source_archive_bytes(
                    pyinstaller_bootstrap.read_bytes(),
                    destination="pyiboot01_bootstrap",
                ),
                "s",
                True,
            ),
            (
                "pyi_rth_demo",
                _source_archive_bytes(
                    runtime_hook.read_bytes(), destination="pyi_rth_demo"
                ),
                "s",
                True,
            ),
            ("app", _source_archive_bytes(app_source, destination="app"), "s", True),
            ("X utf8", b"", "o", False),
            ("PYZ.pyz", pyz.read_bytes(), "z", False),
        ],
    )
    pkg_toc = [
        str(pkg_path),
        pkg_compression,
        pkg_entries,
        "python311.dll",
        True,
        False,
        False,
        [],
        None,
        None,
        None,
    ]
    pkg_toc_path = _write_toc(work / "PKG-00.toc", pkg_toc)
    exe_toc = [
        str(work / "pkv.exe"),
        True,
        False,
        True,
        str(icon),
        None,
        False,
        False,
        b"<assembly/>",
        True,
        False,
        None,
        None,
        None,
        str(pkg_path),
        pkg_entries,
        [],
        False,
        False,
        0,
        [("run.exe", str(bootloader), "EXECUTABLE")],
        str(native_sources["python311.dll"]),
    ]
    exe_toc_path = _write_toc(work / "EXE-00.toc", exe_toc)

    for name, source_path in native_sources.items():
        _write(payload / "_internal" / name, source_path.read_bytes())
    _write(payload / "_internal" / "demo" / "ext.pyd", demo_extension.read_bytes())
    _write(payload / "_internal" / "base_library.zip", base_library.read_bytes())
    _write(payload / "_internal" / "config" / "config.yaml", config.read_bytes())
    _write(payload / "pkv.exe", bootloader.read_bytes() + package_bytes)

    distributions = [
        _FakeDistribution(
            "demo_pkg",
            "1.2.3",
            prefix,
            [
                "Lib/site-packages/demo/__init__.py",
                "Lib/site-packages/demo/ext.pyd",
            ],
        ),
        _FakeDistribution(
            "PyInstaller",
            "6.21.0",
            prefix,
            [
                "Lib/site-packages/PyInstaller/hooks/rthooks/pyi_rth_demo.py",
                "Lib/site-packages/PyInstaller/loader/pyiboot01_bootstrap.py",
            ],
        ),
    ]
    executable_pkg_tocs = release_inventory.discover_executable_pkg_tocs(
        work, ["pkv.exe"]
    )
    return {
        "app": app,
        "bootloader": bootloader,
        "distributions": distributions,
        "conda_meta": prefix / "conda-meta",
        "conda_registry": conda_registry,
        "demo_module": demo_module,
        "exe_toc": exe_toc,
        "exe_toc_path": exe_toc_path,
        "executable_pkg_tocs": executable_pkg_tocs,
        "payload": payload,
        "pkg_entries": pkg_entries,
        "pkg_path": pkg_path,
        "pkg_toc": pkg_toc,
        "pkg_toc_path": pkg_toc_path,
        "prefix": prefix,
        "roots": {
            "python-prefix": prefix,
            "source": source,
            "build-work": work,
        },
        "source": source,
        "toc": toc,
        "toc_path": toc_path,
        "work": work,
    }


def _inventory(fixture: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    arguments = {
        "source_roots": fixture["roots"],
        "bootloader_executables": ["pkv.exe"],
        "executable_pkg_tocs": fixture["executable_pkg_tocs"],
        "conda_meta_root": fixture["conda_meta"],
        "conda_native_registry": fixture["conda_registry"],
        "distributions": fixture["distributions"],
        "fail_on_unattributed_native": False,
        "fail_on_unresolved_components": False,
        "python_version": "3.11.15",
    }
    arguments.update(overrides)
    return release_inventory.build_release_inventory(
        fixture["toc_path"], fixture["payload"], **arguments
    )


def test_inventory_binds_actual_closure_and_explicit_runtime_components(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)

    first = _inventory(fixture)
    second = _inventory(fixture)

    assert first == second
    assert first["schema_version"] == "pkv.release-inventory.v1"
    assert first["payload"]["file_count"] == 10
    assert len(first["payload"]["tree_sha256"]) == 64
    assert "analysis_toc_sha256" not in first["bindings"]
    assert "raw_toc_sha256" not in first["analysis"]
    assert first["bindings"]["payload_tree_sha256"] == first["payload"]["tree_sha256"]
    assert len(first["bindings"]["embedded_archives_sha256"]) == 64
    assert len(first["bindings"]["closure_sha256"]) == 64
    assert first["coverage"]["embedded_archive_count"] == 1
    assert first["coverage"]["embedded_entry_count"] == 6
    archive = first["embedded_archives"][0]
    assert archive["executable_artifact_path"] == "app/pkv.exe"
    assert archive["entry_count"] == 6
    pyz_entry = archive["entries"][5]
    assert pyz_entry["pyz_member_count"] == 1
    assert [item["name"] for item in pyz_entry["pyz_members"]] == ["demo"]
    assert pyz_entry["component_ids"] == sorted(
        {
            component_id
            for member in pyz_entry["pyz_members"]
            for component_id in member["component_ids"]
        }
    )
    assert "runtime:cpython" not in pyz_entry["component_ids"]
    assert [item["name"] for item in archive["entries"]] == [
        "struct",
        "pyiboot01_bootstrap",
        "pyi_rth_demo",
        "app",
        "X utf8",
        "PYZ.pyz",
    ]
    assert [item["kind"] for item in archive["entries"]] == [
        "PYMODULE",
        "PYSOURCE",
        "PYSOURCE",
        "PYSOURCE",
        "OPTION",
        "PYZ",
    ]
    executable = next(
        item for item in first["payload"]["files"] if item["path"] == "pkv.exe"
    )
    assert (
        executable["embedded_archive_graph_sha256"] == archive["portable_graph_sha256"]
    )
    assert executable["embedded_entry_count"] == 6
    assert executable["embedded_pkg_sha256"] == archive["pkg_sha256"]
    assert executable["embedded_pkg_size"] == archive["pkg_size"]
    assert "application:project" in executable["embedded_component_ids"]
    assert "runtime:cpython" in executable["embedded_component_ids"]
    assert "native:msvc-runtime" not in executable["embedded_component_ids"]
    components = {item["id"]: item for item in first["components"]}
    assert "app/pkv.exe!/app" in components["application:project"]["embedded_paths"]
    assert "app/pkv.exe!/struct" in components["runtime:cpython"]["embedded_paths"]
    assert (
        "app/pkv.exe!/<bootloader-prefix>"
        in components["build-runtime:pyinstaller-bootloader"]["embedded_paths"]
    )
    assert components["python-distribution:demo-pkg"]["classification_ids"] == []
    assert all(
        "contains_native_payload" in item
        for item in components.values()
        if item["identity_status"] == "complete"
    )
    assert all(
        "contains_native_payload" not in item
        for item in components.values()
        if item["identity_status"] != "complete"
    )
    assert components["application:project"]["contains_native_payload"] is False
    assert (
        components["build-runtime:pyinstaller-hooks"]["contains_native_payload"]
        is False
    )
    assert (
        components["build-runtime:pyinstaller-bootloader"]["contains_native_payload"]
        is True
    )
    assert components["python-distribution:demo-pkg"]["contains_native_payload"] is True
    assert components["runtime:cpython"]["contains_native_payload"] is True
    assert [item["name"] for item in first["included_distributions"]] == [
        "demo-pkg",
        "pyinstaller",
    ]

    component_ids = {item["id"] for item in first["components"]}
    assert "python-distribution:pyinstaller" not in component_ids
    assert {
        "application:project",
        "build-runtime:pyinstaller-bootloader",
        "build-runtime:pyinstaller-hooks",
        "native:msvc-runtime",
        "native:openssl",
        "native:sqlite",
        "native:zlib",
        "conda-package:openssl",
        "conda-package:sqlite",
        "conda-package:vs2015-runtime",
        "conda-package:zlib",
        "python-distribution:demo-pkg",
        "runtime:cpython",
    } <= component_ids
    assert first["coverage"]["unattributed_native_file_count"] == 0
    assert first["coverage"]["unresolved_component_ids"] == []
    assert first["payload"]["path_base"] == "app"
    assert all(
        item["artifact_path"] == f"app/{item['path']}"
        for item in first["payload"]["files"]
    )
    assert "source/app.py" in first["coverage"]["unowned_source_paths"]
    assert "python-prefix/python311.dll" in first["coverage"]["unowned_source_paths"]

    serialized = release_inventory.canonical_json_bytes(first)
    assert str(tmp_path).encode("utf-8") not in serialized
    assert serialized.endswith(b"\n")
    assert all(len(item["sha256"]) == 64 for item in first["payload"]["files"])


def test_literal_eval_rejects_executable_toc_text(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    fixture["toc_path"].write_text(
        "__import__('pathlib').Path('owned').write_text('bad')",
        encoding="utf-8",
    )

    with pytest.raises(release_inventory.InventoryError, match="safe Python literal"):
        _inventory(fixture)

    assert not (tmp_path / "owned").exists()


def test_inventory_requires_a_complete_executable_pkg_mapping(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)

    with pytest.raises(
        release_inventory.InventoryError,
        match="PKG executable mapping is incomplete",
    ):
        _inventory(fixture, executable_pkg_tocs={})


@pytest.mark.parametrize("tamper_target", ["pkg", "exe"])
def test_inventory_rejects_pkg_executable_suffix_tampering(
    tmp_path: Path, tamper_target: str
) -> None:
    fixture = _fixture(tmp_path)
    pkg_size = fixture["pkg_path"].stat().st_size
    target = (
        fixture["pkg_path"]
        if tamper_target == "pkg"
        else fixture["payload"] / "pkv.exe"
    )
    tampered = bytearray(target.read_bytes())
    offset = 0 if tamper_target == "pkg" else len(tampered) - pkg_size
    tampered[offset] ^= 0x01
    target.write_bytes(tampered)

    with pytest.raises(
        release_inventory.InventoryError,
        match="executable suffix differs from its PKG archive",
    ):
        _inventory(fixture)


def test_inventory_rejects_pkg_toc_tampering(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    tampered_toc = list(fixture["pkg_toc"])
    tampered_entries = list(tampered_toc[2])
    tampered_entries[0] = ("X ascii", None, "OPTION")
    tampered_toc[2] = tampered_entries
    _write_toc(fixture["pkg_toc_path"], tampered_toc)

    with pytest.raises(
        release_inventory.InventoryError,
        match="EXE/PKG TOCs disagree",
    ):
        _inventory(fixture)


def test_inventory_rejects_pkg_source_tampering(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    fixture["app"].write_bytes(b"print('tampered')\n")

    with pytest.raises(
        release_inventory.InventoryError,
        match="CArchive content differs from the PKG source",
    ):
        _inventory(fixture)


def test_inventory_rejects_pyz_member_source_tampering(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    fixture["demo_module"].write_bytes(b"NAME = 'tampered'\n")

    with pytest.raises(
        release_inventory.InventoryError,
        match="PYZ member code differs from source",
    ):
        _inventory(fixture)


@pytest.mark.parametrize(
    ("slot", "destination", "root_key", "relative"),
    [
        (13, "app", "source", "alternate_app.py"),
        (14, "struct", "prefix", "Lib/alternate_struct.py"),
    ],
)
def test_analysis_executable_sources_must_match_embedded_pkg_code(
    tmp_path: Path,
    slot: int,
    destination: str,
    root_key: str,
    relative: str,
) -> None:
    fixture = _fixture(tmp_path)
    alternate = _write(fixture[root_key] / relative, b"DIFFERENT = True\n")
    fixture["toc"][slot] = [
        (name, str(alternate) if name == destination else source, kind)
        for name, source, kind in fixture["toc"][slot]
    ]
    _write_toc(fixture["toc_path"], fixture["toc"])

    with pytest.raises(
        release_inventory.InventoryError,
        match="Analysis executable source differs from embedded PKG code",
    ):
        _inventory(fixture)


def test_unbound_pyinstaller_bootstrap_source_is_rejected(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    alternate = _write(
        fixture["prefix"]
        / "Lib"
        / "site-packages"
        / "PyInstaller"
        / "loader"
        / "alternate_bootstrap.py",
        b"# alternate\n",
    )
    fixture["pkg_entries"][:] = [
        (
            name,
            str(alternate) if name == "pyiboot01_bootstrap" else source,
            kind,
        )
        for name, source, kind in fixture["pkg_entries"]
    ]
    _write_toc(fixture["pkg_toc_path"], fixture["pkg_toc"])
    _write_toc(fixture["exe_toc_path"], fixture["exe_toc"])

    with pytest.raises(
        release_inventory.InventoryError,
        match="executable PKG has an unbound PYSOURCE entry",
    ):
        _inventory(fixture)


def test_portable_closure_ignores_a_b_physical_root_names(tmp_path: Path) -> None:
    left_root = tmp_path / "physical-a"
    right_root = tmp_path / "physical-b"
    left_root.mkdir()
    right_root.mkdir()
    left_fixture = _fixture(left_root)
    right_fixture = _fixture(right_root)
    left_raw_hash = release_inventory.sha256_file(left_fixture["toc_path"])
    right_raw_hash = release_inventory.sha256_file(right_fixture["toc_path"])
    left = _inventory(left_fixture)
    right = _inventory(right_fixture)

    assert left_raw_hash != right_raw_hash
    assert (
        left["analysis"]["portable_graph_sha256"]
        == right["analysis"]["portable_graph_sha256"]
    )
    assert left["embedded_archives"] == right["embedded_archives"]
    assert (
        left["bindings"]["embedded_archives_sha256"]
        == right["bindings"]["embedded_archives_sha256"]
    )
    assert left["payload"]["tree_sha256"] == right["payload"]["tree_sha256"]
    assert left["bindings"]["closure_sha256"] == right["bindings"]["closure_sha256"]
    assert left == right


def test_root_artifact_path_base_has_no_fictional_prefix(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)

    inventory = _inventory(fixture, artifact_path_base="")
    product_inventory = _inventory(fixture, artifact_path_base="app")

    assert inventory["payload"]["path_base"] == "."
    assert all(
        item["artifact_path"] == item["path"] for item in inventory["payload"]["files"]
    )
    assert inventory["bindings"]["artifact_path_base"] == "."
    assert (
        inventory["bindings"]["closure_sha256"]
        != product_inventory["bindings"]["closure_sha256"]
    )


def test_toc_rejects_source_outside_named_roots(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    outside = _write(tmp_path / "outside.dll", b"outside")
    fixture["toc"][15].append(("outside.dll", str(outside), "BINARY"))
    _write_toc(fixture["toc_path"], fixture["toc"])
    _write(fixture["payload"] / "_internal" / "outside.dll", b"outside")

    with pytest.raises(
        release_inventory.InventoryError, match="escapes all allowed roots"
    ):
        _inventory(fixture)


@pytest.mark.parametrize(
    "left,right",
    [("duplicate.dll", "duplicate.dll"), ("Plugin.dll", "plugin.dll")],
)
def test_toc_rejects_duplicate_and_windows_case_colliding_destinations(
    tmp_path: Path, left: str, right: str
) -> None:
    fixture = _fixture(tmp_path)
    left_source = _write(fixture["prefix"] / "DLLs" / "left.pyd", b"left")
    right_source = _write(fixture["prefix"] / "DLLs" / "right.pyd", b"right")
    fixture["toc"][15].extend(
        [(left, str(left_source), "BINARY"), (right, str(right_source), "BINARY")]
    )
    _write_toc(fixture["toc_path"], fixture["toc"])

    with pytest.raises(
        release_inventory.InventoryError, match="duplicate/case-colliding"
    ):
        _inventory(fixture)


def test_payload_native_without_toc_mapping_fails_closed(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    _write(fixture["payload"] / "_internal" / "injected.dll", b"not-in-analysis")

    with pytest.raises(
        release_inventory.InventoryError, match="no final Analysis TOC mapping"
    ):
        _inventory(fixture)


def test_source_payload_byte_mismatch_fails_closed(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    (fixture["payload"] / "_internal" / "zlib.dll").write_bytes(b"tampered")

    with pytest.raises(release_inventory.InventoryError, match="bytes differ"):
        _inventory(fixture)


def test_unattributed_native_is_explicit_and_formal_gate_can_reject_it(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    mystery = _write(fixture["prefix"] / "Library" / "bin" / "mystery.dll", b"mystery")
    fixture["toc"][15].append(("mystery.dll", str(mystery), "BINARY"))
    _write_toc(fixture["toc_path"], fixture["toc"])
    _write(fixture["payload"] / "_internal" / "mystery.dll", b"mystery")

    diagnostic = _inventory(fixture)
    assert diagnostic["coverage"]["unattributed_native_paths"] == [
        "app/_internal/mystery.dll"
    ]
    mystery_record = next(
        item
        for item in diagnostic["payload"]["files"]
        if item["path"] == "_internal/mystery.dll"
    )
    assert "native:unattributed" in mystery_record["component_ids"]

    with pytest.raises(
        release_inventory.InventoryError, match="lacks a resolved component"
    ):
        _inventory(fixture, fail_on_unattributed_native=True)


def test_analysis_only_module_cannot_masquerade_as_pyz_member(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    orphan = _write(
        fixture["prefix"] / "Lib" / "site-packages" / "orphan" / "module.py",
        b"VALUE = 1\n",
    )
    fixture["toc"][14].append(("orphan.module", str(orphan), "PYMODULE"))
    _write_toc(fixture["toc_path"], fixture["toc"])

    with pytest.raises(
        release_inventory.InventoryError,
        match="Analysis pure modules do not equal PYZ members plus bootstrap modules",
    ):
        _inventory(fixture)


def test_expected_bootloader_set_requires_an_exact_pkg_mapping(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)

    with pytest.raises(
        release_inventory.InventoryError, match="PKG executable mapping is incomplete"
    ):
        _inventory(fixture, bootloader_executables=["pkv.exe", "pkv-mcp.exe"])


def test_default_release_mode_requires_frozen_conda_authority(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)

    with pytest.raises(
        release_inventory.InventoryError,
        match="requires a conda native registry",
    ):
        release_inventory.build_release_inventory(
            fixture["toc_path"],
            fixture["payload"],
            source_roots=fixture["roots"],
            bootloader_executables=["pkv.exe"],
            executable_pkg_tocs=fixture["executable_pkg_tocs"],
            distributions=fixture["distributions"],
            python_version="3.11.15",
        )


def test_frozen_conda_registry_allows_zero_unknown_formal_inventory(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)

    inventory = _inventory(
        fixture,
        fail_on_unattributed_native=True,
        fail_on_unresolved_components=True,
    )

    assert inventory["coverage"]["unattributed_native_file_count"] == 0
    assert inventory["coverage"]["unresolved_component_ids"] == []
    assert len(inventory["included_conda_packages"]) == 4
    assert (
        inventory["bindings"]["conda_native_registry_sha256"]
        == hashlib.sha256(
            release_inventory.canonical_json_bytes(fixture["conda_registry"])
        ).hexdigest()
    )
    serialized_registry = release_inventory.canonical_json_bytes(
        fixture["conda_registry"]
    )
    assert str(fixture["prefix"]).encode("utf-8") not in serialized_registry


def test_conda_registry_rejects_record_drift(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    record = next(fixture["conda_meta"].glob("openssl-*.json"))
    record.write_bytes(record.read_bytes() + b"\n")

    with pytest.raises(
        release_inventory.InventoryError,
        match="differs from the physical authority",
    ):
        _inventory(fixture)


def test_conda_registry_rejects_two_byte_matching_owners(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    _write_conda_record(
        fixture["prefix"],
        name="zlib-duplicate",
        version="1.3.1",
        build="test_0",
        license_value="Zlib",
        files=["Library/bin/zlib.dll"],
    )

    with pytest.raises(
        release_inventory.InventoryError,
        match="exactly one byte-matching owner",
    ):
        release_inventory.generate_conda_native_registry(
            fixture["prefix"], fixture["conda_meta"]
        )


def test_conda_registry_freezes_shadowed_stale_record_claim(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    _write_conda_record(
        fixture["prefix"],
        name="xz",
        version="5.8.2",
        build="test_0",
        license_value="0BSD",
        files=["Library/bin/zlib.dll"],
        digest_overrides={"Library/bin/zlib.dll": "0" * 64},
    )

    registry = release_inventory.generate_conda_native_registry(
        fixture["prefix"], fixture["conda_meta"]
    )

    assert registry["shadowed_file_claim_count"] == 1
    assert registry["shadowed_file_claims"] == [
        {
            "active_owners": ["conda-package:zlib"],
            "path": "Library/bin/zlib.dll",
            "sha256": release_inventory.sha256_file(
                fixture["prefix"] / "Library" / "bin" / "zlib.dll"
            ),
            "shadowed_claimants": ["conda-package:xz"],
            "size": len(b"zlib"),
        }
    ]


@pytest.mark.windows_release_env
@pytest.mark.skipif(os.name != "nt", reason="the release registry targets Windows")
def test_committed_conda_registry_matches_release_environment() -> None:
    path = PROJECT_ROOT / "packaging" / "locks" / "conda-native-registry.v1.json"
    raw = path.read_bytes()
    registry = json.loads(raw.decode("utf-8"))

    assert raw == release_inventory.canonical_json_bytes(registry)
    owners, packages, registry_hash = release_inventory.validate_conda_native_registry(
        registry,
        python_prefix=Path(sys.prefix),
        conda_meta_root=Path(sys.prefix) / "conda-meta",
        target="windows-x86_64",
        python_prefix_label="python-prefix",
    )

    assert registry_hash == release_inventory.sha256_file(path)
    assert len(packages) == registry["package_count"] == 21
    assert len(owners) == 2030
    assert registry["file_count"] == 2031
    assert registry["shadowed_file_claim_count"] == 1
