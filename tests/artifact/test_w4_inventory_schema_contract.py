"""Focused PowerShell 5.1 contracts for the W3 inventory consumed by W4."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess

import pytest


pytestmark = pytest.mark.artifact

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DRIVER_MODULE = REPOSITORY_ROOT / "packaging" / "w4_driver" / "W4.Driver.psm1"
SCENARIO_MODULE = REPOSITORY_ROOT / "packaging" / "w4_driver" / "W4.Scenarios.psm1"


def _canonical_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _windows_powershell() -> Path:
    system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    executable = (
        system_root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    )
    assert executable.is_file()
    return executable


def _ps_quote(value: Path) -> str:
    return str(value).replace("'", "''")


def _run_inventory_validator(
    fixture: dict[str, object], *, expected_success: bool
) -> subprocess.CompletedProcess[str]:
    root = Path(fixture["root"])
    inventory_path = root / "inventory.json"
    build_path = root / "build.json"
    provenance_path = root / "provenance.json"
    dependency_path = root / "dependency.json"
    for path, key in (
        (inventory_path, "inventory"),
        (build_path, "build"),
        (provenance_path, "provenance"),
        (dependency_path, "dependency"),
    ):
        path.write_bytes(_canonical_bytes(fixture[key]))
    command = (
        "$ErrorActionPreference='Stop';"
        f"Import-Module '{_ps_quote(DRIVER_MODULE)}' -Force;"
        f"$module=Import-Module '{_ps_quote(SCENARIO_MODULE)}' -Force -PassThru;"
        f"$inventory=[IO.File]::ReadAllText('{_ps_quote(inventory_path)}')|ConvertFrom-Json;"
        f"$build=[IO.File]::ReadAllText('{_ps_quote(build_path)}')|ConvertFrom-Json;"
        f"$provenance=[IO.File]::ReadAllText('{_ps_quote(provenance_path)}')|ConvertFrom-Json;"
        f"$dependency=[IO.File]::ReadAllText('{_ps_quote(dependency_path)}')|ConvertFrom-Json;"
        "& $module {param($inventory,$root,$build,$provenance,$dependency) "
        "(Assert-W4ReleaseInventory -Inventory $inventory -ArtifactRoot $root "
        "-BuildInfo $build -Provenance $provenance -DependencyManifest $dependency "
        "-ExpectedExecutablePaths @('app/pkv.exe')).PayloadTreeSha256} "
        f"$inventory '{_ps_quote(root)}' $build $provenance $dependency"
    )
    result = subprocess.run(
        [
            str(_windows_powershell()),
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            command,
        ],
        cwd=REPOSITORY_ROOT,
        env=os.environ.copy(),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
    )
    if expected_success:
        assert result.returncode == 0, result.stderr
    else:
        assert result.returncode != 0
    return result


def _run_distribution_owner_validator(
    tmp_path: Path,
    *,
    distribution_names: list[str],
    component_ids: list[str],
    source_ref: str,
    destinations: list[str],
    allow_bootloader: bool = False,
) -> subprocess.CompletedProcess[str]:
    payload_path = tmp_path / "distribution-owner.json"
    payload_path.write_bytes(
        _canonical_bytes(
            {
                "allow_bootloader": allow_bootloader,
                "component_ids": component_ids,
                "destinations": destinations,
                "distribution_names": distribution_names,
                "source_ref": source_ref,
            }
        )
    )
    command = (
        "$ErrorActionPreference='Stop';"
        f"Import-Module '{_ps_quote(DRIVER_MODULE)}' -Force;"
        f"$module=Import-Module '{_ps_quote(SCENARIO_MODULE)}' -Force -PassThru;"
        f"$value=[IO.File]::ReadAllText('{_ps_quote(payload_path)}')|ConvertFrom-Json;"
        "& $module {param($value) Assert-W4DistributionOwnerSet "
        "-DistributionNames @($value.distribution_names) "
        "-ComponentIds @($value.component_ids) -SourceRef ([string]$value.source_ref) "
        "-LogicalDestinations @($value.destinations) "
        "-AllowPyInstallerBootloader:([bool]$value.allow_bootloader) "
        "-Label 'test distribution owner'} $value"
    )
    return subprocess.run(
        [
            str(_windows_powershell()),
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            command,
        ],
        cwd=REPOSITORY_ROOT,
        env=os.environ.copy(),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
    )


def _run_license_status_validator(
    tmp_path: Path,
    *,
    component_id: str,
    contains_native_payload: bool,
    classifications: list[str],
    license_index_status: str,
    sbom_status: str,
    license_files: list[dict[str, str]],
) -> subprocess.CompletedProcess[str]:
    payload_path = tmp_path / "license-status.json"
    payload_path.write_bytes(
        _canonical_bytes(
            {
                "component_id": component_id,
                "inventory_component": {
                    "classification_ids": classifications,
                    "contains_native_payload": contains_native_payload,
                },
                "license_index_component": {
                    "license_files": license_files,
                    "license_material_status": license_index_status,
                },
                "sbom_status": sbom_status,
            }
        )
    )
    command = (
        "$ErrorActionPreference='Stop';"
        f"Import-Module '{_ps_quote(DRIVER_MODULE)}' -Force;"
        f"$module=Import-Module '{_ps_quote(SCENARIO_MODULE)}' -Force -PassThru;"
        f"$value=[IO.File]::ReadAllText('{_ps_quote(payload_path)}')|ConvertFrom-Json;"
        "& $module {param($value) Assert-W4LicenseMaterialStatusBinding "
        "-ComponentId ([string]$value.component_id) "
        "-InventoryComponent $value.inventory_component "
        "-LicenseIndexComponent $value.license_index_component "
        "-SbomStatus ([string]$value.sbom_status)} $value"
    )
    return subprocess.run(
        [
            str(_windows_powershell()),
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            command,
        ],
        cwd=REPOSITORY_ROOT,
        env=os.environ.copy(),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
    )


def _inventory_fixture(root: Path) -> dict[str, object]:
    app = root / "app"
    internal = app / "_internal"
    internal.mkdir(parents=True)
    prefix = b"MZ-synthetic-bootloader-prefix"
    pkg = b"synthetic-carchive-pkg-suffix"
    executable = app / "pkv.exe"
    executable.write_bytes(prefix + pkg)
    data = internal / "data.bin"
    data.write_bytes(b"collected-runtime-data")

    member = {
        "component_ids": ["application:project"],
        "conda_component_ids": [],
        "content_sha256": "a" * 64,
        "content_size": 8,
        "distribution_names": [],
        "kind": "module",
        "name": "demo",
        "source_kind": "PYMODULE",
        "source_ref": "source/demo.py",
        "source_sha256": _sha(b""),
        "source_size": 0,
        "stored_sha256": "b" * 64,
        "stored_size": 4,
    }
    pyz_members = [member]
    pyz_entry = {
        "component_ids": ["application:project"],
        "compressed": True,
        "conda_component_ids": [],
        "content_sha256": "2" * 64,
        "distribution_names": [],
        "kind": "PYZ",
        "name": "PYZ.pyz",
        "pyz_member_count": 1,
        "pyz_members": pyz_members,
        "pyz_members_sha256": _sha(_canonical_bytes(pyz_members)),
        "pyz_python_magic_sha256": "c" * 64,
        "pyz_toc_sha256": "d" * 64,
        "pyz_toc_size": 2,
        "source_ref": "build-work/PYZ.pyz",
        "source_sha256": "3" * 64,
        "source_size": 23,
        "stored_sha256": "4" * 64,
        "stored_size": 19,
        "typecode": "z",
        "uncompressed_size": 23,
    }
    bootstrap_content = b"pyinstaller-bootstrap"
    bootstrap_entry = {
        "component_ids": ["build-runtime:pyinstaller-bootloader"],
        "compressed": False,
        "conda_component_ids": [],
        "content_sha256": _sha(bootstrap_content),
        "distribution_names": ["pyinstaller"],
        "kind": "PYSOURCE",
        "name": "pyiboot01_bootstrap",
        "source_ref": (
            "python-prefix/Lib/site-packages/PyInstaller/loader/"
            "pyiboot01_bootstrap.py"
        ),
        "source_sha256": _sha(bootstrap_content),
        "source_size": len(bootstrap_content),
        "stored_sha256": _sha(bootstrap_content),
        "stored_size": len(bootstrap_content),
        "typecode": "s",
        "uncompressed_size": len(bootstrap_content),
    }
    option_entry = {
        "component_ids": ["build-runtime:pyinstaller-bootloader"],
        "compressed": False,
        "content_sha256": _sha(b""),
        "kind": "OPTION",
        "name": "pyi-contents-directory _internal",
        "stored_sha256": _sha(b""),
        "stored_size": 0,
        "typecode": "o",
        "uncompressed_size": 0,
    }
    archive_material = {
        "bootloader_input": {
            "source_ref": "python-prefix/Lib/site-packages/PyInstaller/bootloader/run.exe",
            "source_sha256": "5" * 64,
            "source_size": len(prefix),
        },
        "bootloader_prefix_sha256": _sha(prefix),
        "bootloader_prefix_size": len(prefix),
        "component_ids": [
            "application:project",
            "build-runtime:pyinstaller-bootloader",
        ],
        "entries": [pyz_entry, bootstrap_entry, option_entry],
        "entry_count": 3,
        "executable_artifact_path": "app/pkv.exe",
        "executable_sha256": _sha(prefix + pkg),
        "executable_size": len(prefix + pkg),
        "pkg_sha256": _sha(pkg),
        "pkg_size": len(pkg),
        "python_library": "python311.dll",
        "python_version": 311,
    }
    archive = {
        **archive_material,
        "portable_graph_sha256": _sha(_canonical_bytes(archive_material)),
    }
    components = [
        {
            "classification_ids": [],
            "contains_native_payload": False,
            "embedded_paths": ["app/pkv.exe!/PYZ.pyz#/demo"],
            "id": "application:project",
            "identity_status": "complete",
            "name": "Personal Knowledge Vault application code",
            "payload_paths": ["app/_internal/data.bin"],
            "source_paths": ["source/data.bin", "source/demo.py"],
            "type": "application",
        },
        {
            "classification_ids": [],
            "contains_native_payload": True,
            "embedded_paths": [
                "app/pkv.exe!/<bootloader-prefix>",
                "app/pkv.exe!/pyi-contents-directory _internal",
                "app/pkv.exe!/pyiboot01_bootstrap",
            ],
            "id": "build-runtime:pyinstaller-bootloader",
            "identity_status": "complete",
            "name": "PyInstaller bootloader",
            "payload_paths": ["app/pkv.exe"],
            "source_paths": [
                "python-prefix/Lib/site-packages/PyInstaller/bootloader/run.exe",
                "python-prefix/Lib/site-packages/PyInstaller/loader/pyiboot01_bootstrap.py",
            ],
            "type": "runtime",
            "version": "6.21.0",
        },
    ]
    data_row = {
        "artifact_path": "app/_internal/data.bin",
        "component_ids": ["application:project"],
        "conda_component_ids": [],
        "distribution_names": [],
        "kind": "DATA",
        "path": "_internal/data.bin",
        "sha256": _sha(data.read_bytes()),
        "size": data.stat().st_size,
        "source_ref": "source/data.bin",
        "source_sha256": _sha(data.read_bytes()),
        "toc_destination": "data.bin",
    }
    executable_row = {
        "artifact_path": "app/pkv.exe",
        "component_ids": archive["component_ids"],
        "embedded_archive_graph_sha256": archive["portable_graph_sha256"],
        "embedded_component_ids": archive["component_ids"],
        "embedded_entry_count": 3,
        "embedded_pkg_sha256": archive["pkg_sha256"],
        "embedded_pkg_size": archive["pkg_size"],
        "kind": "PYINSTALLER_BOOTLOADER_EXECUTABLE",
        "path": "pkv.exe",
        "sha256": archive["executable_sha256"],
        "size": archive["executable_size"],
    }
    payload_files = [data_row, executable_row]
    payload_tree = _sha(
        "".join(
            f"{row['path']}\0{row['size']}\0{row['sha256']}\n" for row in payload_files
        ).encode("utf-8")
    )
    analysis_sha = "6" * 64
    registry_sha = "7" * 64
    lock_sha = "8" * 64
    blockers: list[str] = []
    blocker_authority: list[dict[str, str]] = []
    blocker_authority_sha = _sha(_canonical_bytes(blocker_authority))
    authority = {
        "artifact_kind": "test_candidate",
        "artifact_status": "test-candidate-on-compliance-hold",
        "build_fingerprint": "9" * 64,
        "conda_native_registry_path": "packaging/locks/conda-native-registry.v1.json",
        "conda_native_registry_sha256": registry_sha,
        "environment_lock_path": "packaging/locks/release-environment.v2.json",
        "environment_lock_sha256": lock_sha,
        "release_blocker_authority": blocker_authority,
        "release_blocker_authority_sha256": blocker_authority_sha,
        "release_blockers": blockers,
        "release_eligible": False,
        "source_revision": "a" * 40,
    }
    embedded_sha = _sha(_canonical_bytes([archive]))
    portable_binding = {
        "analysis_graph_sha256": analysis_sha,
        "artifact_path_base": "app",
        "conda_native_registry_sha256": registry_sha,
        "embedded_archives_sha256": embedded_sha,
        "payload_tree_sha256": payload_tree,
    }
    closure_sha = _sha(_canonical_bytes(portable_binding))
    artifact_closure_sha = _sha(
        _canonical_bytes(
            {
                "authority": authority,
                "inventory_closure_sha256": closure_sha,
            }
        )
    )
    inventory = {
        "analysis": {
            "entry_count": 3,
            "portable_graph_sha256": analysis_sha,
            "source_count": 4,
            "sources": [
                {
                    "component_ids": ["build-runtime:pyinstaller-bootloader"],
                    "conda_component_ids": [],
                    "distribution_names": [],
                    "occurrences": [
                        {
                            "destination": "pkv.exe",
                            "slot": "bootloader-prefix:pkv.exe",
                            "type": "EXECUTABLE",
                        }
                    ],
                    "path": "python-prefix/Lib/site-packages/PyInstaller/bootloader/run.exe",
                    "sha256": "5" * 64,
                    "size": len(prefix),
                },
                {
                    "component_ids": ["application:project"],
                    "conda_component_ids": [],
                    "distribution_names": [],
                    "occurrences": [
                        {"destination": "data.bin", "slot": "datas", "type": "DATA"}
                    ],
                    "path": "source/data.bin",
                    "sha256": _sha(data.read_bytes()),
                    "size": data.stat().st_size,
                },
                {
                    "component_ids": ["build-runtime:pyinstaller-bootloader"],
                    "conda_component_ids": [],
                    "distribution_names": ["pyinstaller"],
                    "occurrences": [
                        {
                            "destination": "pyiboot01_bootstrap",
                            "slot": "embedded:pkv.exe",
                            "type": "PYSOURCE",
                        }
                    ],
                    "path": (
                        "python-prefix/Lib/site-packages/PyInstaller/loader/"
                        "pyiboot01_bootstrap.py"
                    ),
                    "sha256": _sha(bootstrap_content),
                    "size": len(bootstrap_content),
                },
                {
                    "component_ids": ["application:project"],
                    "conda_component_ids": [],
                    "distribution_names": [],
                    "occurrences": [
                        {
                            "destination": "demo",
                            "slot": "pure-modules",
                            "type": "PYMODULE",
                        }
                    ],
                    "path": "source/demo.py",
                    "sha256": _sha(b""),
                    "size": 0,
                },
            ],
            "virtual_entries": [],
        },
        "authority": authority,
        "bindings": {
            **portable_binding,
            "artifact_closure_sha256": artifact_closure_sha,
            "closure_sha256": closure_sha,
        },
        "components": components,
        "coverage": {
            "conda_native_registry_sha256": registry_sha,
            "embedded_archive_count": 1,
            "embedded_entry_count": 3,
            "payload_file_count": 2,
            "unattributed_native_file_count": 0,
            "unattributed_native_paths": [],
            "unowned_source_path_count": 3,
            "unowned_source_paths": [
                "python-prefix/Lib/site-packages/PyInstaller/bootloader/run.exe",
                "source/data.bin",
                "source/demo.py",
            ],
            "unresolved_component_ids": [],
        },
        "embedded_archives": [archive],
        "included_conda_packages": [],
        "included_distributions": [
            {
                "name": "pyinstaller",
                "source_paths": [
                    "python-prefix/Lib/site-packages/PyInstaller/loader/pyiboot01_bootstrap.py"
                ],
                "version": "6.21.0",
            }
        ],
        "payload": {
            "file_count": 2,
            "files": payload_files,
            "path_base": "app",
            "tree_sha256": payload_tree,
        },
        "schema_version": "pkv.release-inventory.v1",
    }
    inventory["analysis"]["sources"].sort(
        key=lambda item: str(item["path"]).encode("utf-8")
    )
    build = {
        "artifact_kind": authority["artifact_kind"],
        "artifact_status": authority["artifact_status"],
        "build_fingerprint": authority["build_fingerprint"],
        "inputs": {
            "packaging/locks/conda-native-registry.v1.json": registry_sha,
        },
        "release_inventory_artifact_closure_sha256": artifact_closure_sha,
        "release_inventory_closure_sha256": closure_sha,
        "source_revision": authority["source_revision"],
    }
    provenance = {
        "release_blocker_authority": blocker_authority,
        "release_blocker_authority_sha256": blocker_authority_sha,
        "release_blockers": blockers,
        "release_inventory_artifact_closure_sha256": artifact_closure_sha,
        "release_inventory_closure_sha256": closure_sha,
    }
    dependency = {"environment_lock_sha256": lock_sha}
    return {
        "root": root,
        "inventory": inventory,
        "build": build,
        "provenance": provenance,
        "dependency": dependency,
    }


def test_w4_accepts_exact_onedir_payload_and_pyz_member_closure(tmp_path: Path) -> None:
    fixture = _inventory_fixture(tmp_path)

    result = _run_inventory_validator(fixture, expected_success=True)

    assert result.stdout.strip() == fixture["inventory"]["payload"]["tree_sha256"]


@pytest.mark.parametrize(
    ("distribution_names", "component_ids", "source_ref", "destinations", "allow"),
    [
        (
            ["requests"],
            ["python-distribution:requests"],
            "python-prefix/Lib/site-packages/requests/__init__.py",
            ["requests"],
            False,
        ),
        (
            ["pyinstaller"],
            ["build-runtime:pyinstaller-bootloader"],
            "python-prefix/Lib/site-packages/PyInstaller/loader/pyiboot01_bootstrap.py",
            ["pyiboot01_bootstrap"],
            True,
        ),
        (
            ["pyinstaller"],
            ["build-runtime:pyinstaller-hooks"],
            "python-prefix/Lib/site-packages/PyInstaller/hooks/rthooks/pyi_rth_inspect.py",
            ["pyi_rth_inspect"],
            False,
        ),
        (
            ["pyinstaller-hooks-contrib"],
            ["build-runtime:pyinstaller-hooks-contrib"],
            "python-prefix/Lib/site-packages/_pyinstaller_hooks_contrib/rthooks/pyi_rth_certifi.py",
            ["pyi_rth_certifi"],
            False,
        ),
    ],
)
def test_w4_distribution_owner_predicate_accepts_exact_special_bindings(
    tmp_path: Path,
    distribution_names: list[str],
    component_ids: list[str],
    source_ref: str,
    destinations: list[str],
    allow: bool,
) -> None:
    result = _run_distribution_owner_validator(
        tmp_path,
        distribution_names=distribution_names,
        component_ids=component_ids,
        source_ref=source_ref,
        destinations=destinations,
        allow_bootloader=allow,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("distribution_names", "component_ids", "source_ref", "destinations", "allow"),
    [
        (
            ["pyinstaller"],
            ["python-distribution:pyinstaller"],
            "python-prefix/Lib/site-packages/PyInstaller/loader/pyiboot01_bootstrap.py",
            ["pyiboot01_bootstrap"],
            True,
        ),
        (
            ["pyinstaller"],
            ["build-runtime:pyinstaller-bootloader"],
            "source/Lib/site-packages/PyInstaller/loader/pyiboot01_bootstrap.py",
            ["pyiboot01_bootstrap"],
            True,
        ),
        (
            ["pyinstaller"],
            ["build-runtime:pyinstaller-bootloader"],
            "python-prefix/Lib/site-packages/PyInstaller/hooks/rthooks/pyi_rth_inspect.py",
            ["pyi_rth_inspect"],
            False,
        ),
        (
            ["pyinstaller-hooks-contrib"],
            ["python-distribution:pyinstaller-hooks-contrib"],
            "python-prefix/Lib/site-packages/_pyinstaller_hooks_contrib/rthooks/pyi_rth_certifi.py",
            ["pyi_rth_certifi"],
            False,
        ),
        (
            ["pyinstaller"],
            [
                "build-runtime:pyinstaller-hooks",
                "python-distribution:requests",
            ],
            "python-prefix/Lib/site-packages/PyInstaller/hooks/rthooks/pyi_rth_inspect.py",
            ["pyi_rth_inspect"],
            False,
        ),
        (
            ["pyinstaller"],
            [
                "build-runtime:pyinstaller-hooks",
                "build-runtime:pyinstaller-hooks-contrib",
            ],
            "python-prefix/Lib/site-packages/PyInstaller/hooks/rthooks/pyi_rth_inspect.py",
            ["pyi_rth_inspect"],
            False,
        ),
    ],
)
def test_w4_distribution_owner_predicate_rejects_generic_or_mismatched_owners(
    tmp_path: Path,
    distribution_names: list[str],
    component_ids: list[str],
    source_ref: str,
    destinations: list[str],
    allow: bool,
) -> None:
    result = _run_distribution_owner_validator(
        tmp_path,
        distribution_names=distribution_names,
        component_ids=component_ids,
        source_ref=source_ref,
        destinations=destinations,
        allow_bootloader=allow,
    )
    assert result.returncode != 0


@pytest.mark.parametrize(
    (
        "component_id",
        "native",
        "classifications",
        "license_status",
        "sbom_status",
        "license_files",
    ),
    [
        (
            "python-distribution:requests",
            False,
            [],
            "bound",
            "bound",
            [{"path": "licenses/requests.txt", "sha256": "a" * 64}],
        ),
        (
            "python-distribution:unlicensed-placeholder",
            False,
            [],
            "metadata-only-compliance-hold",
            "metadata-only-compliance-hold",
            [],
        ),
        (
            "python-distribution:cffi",
            True,
            [],
            "top-level-only-compliance-hold",
            "top-level-only-compliance-hold",
            [],
        ),
        (
            "conda-package:zlib",
            True,
            [],
            "metadata-only-compliance-hold",
            "metadata-only-compliance-hold",
            [],
        ),
    ],
)
def test_w4_license_status_matches_license_index_and_independent_hold_derivation(
    tmp_path: Path,
    component_id: str,
    native: bool,
    classifications: list[str],
    license_status: str,
    sbom_status: str,
    license_files: list[dict[str, str]],
) -> None:
    result = _run_license_status_validator(
        tmp_path,
        component_id=component_id,
        contains_native_payload=native,
        classifications=classifications,
        license_index_status=license_status,
        sbom_status=sbom_status,
        license_files=license_files,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("license_status", "sbom_status", "license_files"),
    [
        (
            "requires-license-index-binding",
            "requires-license-index-binding",
            [{"path": "licenses/requests.txt", "sha256": "a" * 64}],
        ),
        (
            "bound",
            "requires-license-index-binding",
            [{"path": "licenses/requests.txt", "sha256": "a" * 64}],
        ),
        ("bound", "bound", []),
    ],
)
def test_w4_license_status_rejects_stale_or_unbound_ordinary_component(
    tmp_path: Path,
    license_status: str,
    sbom_status: str,
    license_files: list[dict[str, str]],
) -> None:
    result = _run_license_status_validator(
        tmp_path,
        component_id="python-distribution:requests",
        contains_native_payload=False,
        classifications=[],
        license_index_status=license_status,
        sbom_status=sbom_status,
        license_files=license_files,
    )
    assert result.returncode != 0


def test_w4_canonical_hash_matches_python_without_corrupting_literal_escape(
    tmp_path: Path,
) -> None:
    value = {
        "ampersand": "A&B",
        "apostrophe": "owner's",
        "literal_escape": r"\u003c",
        "nel": "left\u0085right",
        "path": "app/pkv.exe!/<bootloader-prefix>",
        "separators": "left\u2028middle\u2029right",
    }
    source = tmp_path / "canonical.json"
    source.write_bytes(_canonical_bytes(value))
    command = (
        "$ErrorActionPreference='Stop';"
        f"Import-Module '{_ps_quote(DRIVER_MODULE)}' -Force;"
        f"$value=[IO.File]::ReadAllText('{_ps_quote(source)}')|ConvertFrom-Json;"
        "Get-W4CanonicalJsonSha256 -Value $value"
    )
    result = subprocess.run(
        [
            str(_windows_powershell()),
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            command,
        ],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == _sha(_canonical_bytes(value))


def test_w4_uses_python_utf8_byte_order_for_canonical_inventory_paths(
    tmp_path: Path,
) -> None:
    values = [
        "_internal/_asyncio.pyd",
        "_internal/native-framework/plugins/platforms/qwindows.dll",
        "_internal/native-framework/bin/core.dll",
        "\U0001f600-supplementary",
        "\ue000-private-use",
    ]
    source = tmp_path / "paths.json"
    source.write_text(json.dumps(values, ensure_ascii=False), encoding="utf-8")
    command = (
        "$ErrorActionPreference='Stop';"
        f"Import-Module '{_ps_quote(DRIVER_MODULE)}' -Force;"
        f"$module=Import-Module '{_ps_quote(SCENARIO_MODULE)}' -Force -PassThru;"
        f"$items=[IO.File]::ReadAllText('{_ps_quote(source)}')|ConvertFrom-Json;"
        "& $module {param($value) @(Get-W4Utf8SortedStrings -Values $value) | "
        "ConvertTo-Json -Compress} $items"
    )

    result = subprocess.run(
        [
            str(_windows_powershell()),
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            command,
        ],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == sorted(
        values, key=lambda item: item.encode("utf-8")
    )


@pytest.mark.parametrize(
    ("component_index", "incorrect_value"),
    ((0, True), (1, False)),
    ids=("pure-pyz-is-not-native", "bootloader-is-native"),
)
def test_w4_recomputes_component_native_payload_state(
    tmp_path: Path, component_index: int, incorrect_value: bool
) -> None:
    fixture = _inventory_fixture(tmp_path)
    fixture["inventory"]["components"][component_index][
        "contains_native_payload"
    ] = incorrect_value

    result = _run_inventory_validator(fixture, expected_success=False)

    assert "native-payload binding" in result.stderr


def test_w4_native_state_uses_collected_logical_kind_not_outer_exe(
    tmp_path: Path,
) -> None:
    fixture = _inventory_fixture(tmp_path)
    fixture["inventory"]["payload"]["files"][0]["kind"] = "EXTENSION"

    result = _run_inventory_validator(fixture, expected_success=False)

    assert "native-payload binding" in result.stderr


def test_w4_rejects_executable_whose_physical_pkg_suffix_changed(
    tmp_path: Path,
) -> None:
    fixture = _inventory_fixture(tmp_path)
    executable = tmp_path / "app" / "pkv.exe"
    data = bytearray(executable.read_bytes())
    data[-1] ^= 0x01
    executable.write_bytes(data)

    result = _run_inventory_validator(fixture, expected_success=False)

    assert "physical app tree" in result.stderr or "suffix/prefix" in result.stderr


def test_w4_rejects_pyz_member_graph_hash_tampering(tmp_path: Path) -> None:
    fixture = _inventory_fixture(tmp_path)
    fixture["inventory"]["embedded_archives"][0]["entries"][0]["pyz_members_sha256"] = (
        "f" * 64
    )
    # Keep every enclosing declared hash self-consistent; W4 must still derive the
    # member graph rather than trusting an attacker-controlled hash chain.
    archive = fixture["inventory"]["embedded_archives"][0]
    material = {
        key: value for key, value in archive.items() if key != "portable_graph_sha256"
    }
    archive["portable_graph_sha256"] = _sha(_canonical_bytes(material))
    fixture["inventory"]["bindings"]["embedded_archives_sha256"] = _sha(
        _canonical_bytes(fixture["inventory"]["embedded_archives"])
    )

    result = _run_inventory_validator(fixture, expected_success=False)

    assert "PYZ member graph is invalid" in result.stderr


def test_w4_rejects_zero_size_pyz_source_with_nonempty_hash(tmp_path: Path) -> None:
    fixture = _inventory_fixture(tmp_path)
    archive = fixture["inventory"]["embedded_archives"][0]
    pyz_entry = archive["entries"][0]
    pyz_entry["pyz_members"][0]["source_sha256"] = "f" * 64
    pyz_entry["pyz_members_sha256"] = _sha(_canonical_bytes(pyz_entry["pyz_members"]))
    material = {
        key: value for key, value in archive.items() if key != "portable_graph_sha256"
    }
    archive["portable_graph_sha256"] = _sha(_canonical_bytes(material))
    fixture["inventory"]["bindings"]["embedded_archives_sha256"] = _sha(
        _canonical_bytes(fixture["inventory"]["embedded_archives"])
    )

    result = _run_inventory_validator(fixture, expected_success=False)

    assert "PYZ physical member binding is invalid" in result.stderr


def test_hardlink_evidence_rejects_incomplete_revalidation_scope() -> None:
    evidence = {
        "schema_version": "pkv.conda-hardlink-threat-evidence.v1",
        "anchors": [
            {
                "hardlink_count": 2,
                "label": label,
                "path": path,
                "sha256": "a" * 64,
                "size": 1,
            }
            for label, path in (
                ("numpy-package-anchor", "Lib/site-packages/numpy/__init__.py"),
                ("python-dll", "python311.dll"),
                ("python-executable", "python.exe"),
            )
        ],
        "observed_hardlink_anchor_count": 3,
        "release_eligible_environment_requirement": "copy-only-no-hardlinks",
        "threat_model": "accepted_for_test_candidate",
        "validation_scope": ["before-build-a", "after-build-a"],
    }
    text = json.dumps(evidence, separators=(",", ":")).replace("'", "''")
    command = (
        "$ErrorActionPreference='Stop';"
        f"Import-Module '{_ps_quote(DRIVER_MODULE)}' -Force;"
        f"$module=Import-Module '{_ps_quote(SCENARIO_MODULE)}' -Force -PassThru;"
        f"$value='{text}'|ConvertFrom-Json;"
        "& $module {param($item) Assert-W4CondaHardlinkThreatEvidence -Evidence $item} $value"
    )
    result = subprocess.run(
        [
            str(_windows_powershell()),
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            command,
        ],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )

    assert result.returncode != 0
    assert "revalidation scope" in result.stderr
