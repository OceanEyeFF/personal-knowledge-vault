"""Synthetic fail-closed contracts for the W4 Artifact E2E controller.

These tests inspect or execute only the external PowerShell driver boundary.
They do not import ``src`` and do not require a built product Artifact.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import uuid
import zipfile

import pytest


pytestmark = pytest.mark.artifact

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
RUNNER = REPOSITORY_ROOT / "scripts" / "run-artifact-e2e.ps1"
DRIVER_ROOT = REPOSITORY_ROOT / "packaging" / "w4_driver"
CONTROLLER = DRIVER_ROOT / "Invoke-W4ArtifactE2E.ps1"
DRIVER_MODULE = DRIVER_ROOT / "W4.Driver.psm1"
SCENARIO_MODULE = DRIVER_ROOT / "W4.Scenarios.psm1"
SCENARIO_CONTRACT = DRIVER_ROOT / "scenarios.v1.json"
ARTIFACT_ID = "PersonalKnowledgeVault-0.8.1-windows-x86_64"
EXPECTED_MATRIX_ROWS = {
    "payload_and_provenance",
    "installation_and_first_run",
    "gui_read_and_bm25",
    "offline_text_archive",
    "url_security_rejection",
    "semantic_provider_unavailable",
    "gui_chat_loopback",
    "mcp_stdio_lifecycle",
    "upgrade_rejection",
    "uninstall_and_data_boundary",
    "documentation_version_and_decision",
}
PROVENANCE_FIELDS = {
    "schema_version",
    "artifact_file",
    "artifact_kind",
    "artifact_status",
    "artifact_sha256",
    "artifact_size",
    "build_info_path",
    "build_info_sha256",
    "build_fingerprint",
    "compliance_manifest_sha256",
    "compliance_sources",
    "conda_hardlink_threat_evidence",
    "payload_manifest_path",
    "payload_manifest_sha256",
    "sbom_path",
    "sbom_sha256",
    "source_revision",
    "release_blockers",
    "release_blocker_authority",
    "release_blocker_authority_sha256",
    "release_eligible",
    "release_inventory_artifact_closure_sha256",
    "release_inventory_closure_sha256",
    "release_inventory_path",
    "release_inventory_sha256",
    "version",
}
EVIDENCE_FIELDS = {
    "scenario_id",
    "state",
    "producer_lane",
    "artifact_id",
    "artifact_sha256",
    "normalized_manifest_sha256",
    "build_fingerprint",
    "source_revision",
    "runner_version",
    "execution_id",
    "executed_at",
    "environment_fingerprint",
    "fixture_sha256",
    "harness_sha256",
    "evidence_manifest_sha256",
    "source_isolation_proof_sha256",
    "oracle_result",
    "evidence_paths",
}
BUILD_ENVIRONMENT_CONTRACT = {
    "conda_hardlink_threat_model": "accepted_for_test_candidate",
    "hardlink_sensitive_roots": [
        "python-prefix",
        "python-prefix/DLLs",
        "python-prefix/Lib",
        "python-prefix/Lib/site-packages",
        "python-prefix/Library/bin",
    ],
    "home_directory": "per-physical-build-root",
    "inherit_ambient": False,
    "live_environment_byte_revalidation": [
        "before-build-a",
        "after-build-a",
        "before-build-b",
        "after-build-b",
        "before-publication",
    ],
    "path_roles": [
        "python-prefix",
        "python-scripts",
        "python-library-bin",
        "python-dlls",
        "windows-system32",
        "locked-git-directory",
    ],
    "python_hash_seed": "0",
    "python_no_user_site": True,
    "release_eligible_environment_requirement": "copy-only-no-hardlinks",
    "source_date_epoch": "git-commit-timestamp",
    "temporary_directory": "per-physical-build-root",
    "timezone": "UTC",
}


def _windows_powershell() -> Path:
    system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    executable = (
        system_root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    )
    assert executable.is_file(), "W4 Artifact contracts require Windows PowerShell 5.1"
    return executable


def _is_within(candidate: Path, root: Path) -> bool:
    candidate_text = os.path.normcase(str(candidate.resolve(strict=False)))
    root_text = os.path.normcase(str(root.resolve(strict=False)))
    try:
        return os.path.commonpath([candidate_text, root_text]) == root_text
    except ValueError:
        return False


def _run_powershell(
    arguments: list[str], *, cwd: Path, timeout: int = 30
) -> subprocess.CompletedProcess[str]:
    assert cwd.is_dir()
    return subprocess.run(
        [
            str(_windows_powershell()),
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            *arguments,
        ],
        cwd=cwd,
        env=os.environ.copy(),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def _canonical_json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        + "\n"
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _ps_single_quoted(value: Path) -> str:
    return str(value).replace("'", "''")


def _run_build_environment_contract_validator(
    tmp_path: Path, contract: dict[str, object]
) -> subprocess.CompletedProcess[str]:
    contract_path = tmp_path / f"build-environment-{uuid.uuid4().hex}.json"
    contract_path.write_text(json.dumps(contract), encoding="utf-8")
    command = (
        "$ErrorActionPreference='Stop';"
        f"Import-Module '{_ps_single_quoted(DRIVER_MODULE)}' -Force;"
        f"$module=Import-Module '{_ps_single_quoted(SCENARIO_MODULE)}' "
        "-Force -PassThru;"
        f"$contract=[IO.File]::ReadAllText('{_ps_single_quoted(contract_path)}')|"
        "ConvertFrom-Json;"
        "& $module {param($value) Assert-W4BuildEnvironmentContract -Contract $value} "
        "$contract"
    )
    return _run_powershell(["-Command", command], cwd=REPOSITORY_ROOT)


def _extract_single_quoted_values(block: str) -> set[str]:
    return set(re.findall(r"'([^']+)'", block))


def _full_matrix_command(bundle: dict[str, Path]) -> list[str]:
    return [
        "-File",
        str(RUNNER),
        "-RunFullMatrix",
        "-CandidateRoot",
        str(bundle["candidate_root"]),
        "-DistributionZip",
        str(bundle["zip"]),
        "-DistributionSha256Path",
        str(bundle["sidecar"]),
        "-ProvenancePath",
        str(bundle["provenance"]),
        "-ComplianceSourcesRoot",
        str(bundle["compliance_root"]),
        "-ComplianceManifestPath",
        str(bundle["compliance_manifest"]),
        "-ComplianceProvenancePath",
        str(bundle["compliance_provenance"]),
        "-DriverRoot",
        str(bundle["driver_root"]),
        "-HarnessPath",
        str(bundle["harness"]),
        "-EvidenceRoot",
        str(bundle["evidence"]),
        "-WorkspaceRoot",
        str(bundle["workspace"]),
        "-HarnessWorkspaceRoot",
        str(bundle["harness_workspace"]),
        "-FullMatrixTimeoutSeconds",
        "60",
        "-RunId",
        bundle["run_id"].name,
    ]


def _write_driver_manifest(driver_root: Path) -> None:
    excluded = {"driver-manifest.json", "driver-manifest.sha256"}
    files: list[dict[str, object]] = []
    for path in sorted(
        (item for item in driver_root.rglob("*") if item.is_file()),
        key=lambda item: item.relative_to(driver_root).as_posix().casefold(),
    ):
        relative = path.relative_to(driver_root).as_posix()
        if relative in excluded:
            continue
        content = path.read_bytes()
        files.append(
            {
                "path": relative,
                "role": "w4-driver-contract",
                "sha256": hashlib.sha256(content).hexdigest(),
                "size": len(content),
            }
        )
    tree_text = "".join(
        f"{row['path']}\0{row['size']}\0{row['sha256']}\n" for row in files
    )
    manifest = {
        "schema_version": "pkv.m13.w4-driver-bundle.v1",
        "runner_version": "pkv.m13.artifact-runner.v2",
        "distribution": "e2e-only",
        "release_payload_membership": "forbidden",
        "self_excluded_paths": [
            "driver-manifest.json",
            "driver-manifest.sha256",
        ],
        "files": files,
        "tree_sha256": hashlib.sha256(tree_text.encode("utf-8")).hexdigest(),
    }
    manifest_path = driver_root / "driver-manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8"
    )
    manifest_sha = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    (driver_root / "driver-manifest.sha256").write_text(
        f"{manifest_sha}  driver-manifest.json\n", encoding="ascii"
    )


def _write_harness_package(
    package_root: Path, *, build_fingerprint: str, source_revision: str
) -> None:
    harness_id = "PKV-W4-LoopbackHarness-1.0.0-windows-x86_64"
    stage = package_root.parent / "harness-stage" / harness_id
    scripts = stage / "scripts"
    scripts.mkdir(parents=True)
    runtime = stage / "pkv-loopback-provider.exe"
    bootloader_prefix = b"MZ-synthetic-harness-bootloader"
    pkg_suffix = b"synthetic-harness-carchive-pkg"
    runtime.write_bytes(bootloader_prefix + pkg_suffix)
    contract = stage / "contract.v1.json"
    contract.write_text('{"synthetic":true}\n', encoding="utf-8")
    script_specs = [
        ("w3.chat.provider-error.v1", "provider-error.v1.json"),
        ("w3.chat.stop.v1", "stop.v1.json"),
        ("w3.chat.success.v1", "success.v1.json"),
        ("w4.chat.lifecycle.v1", "w4-chat-lifecycle.v1.json"),
    ]
    script_rows: list[dict[str, str]] = []
    for script_id, filename in script_specs:
        path = scripts / filename
        path.write_text(json.dumps({"script_id": script_id}) + "\n", encoding="utf-8")
        script_rows.append(
            {
                "script_id": script_id,
                "path": f"scripts/{filename}",
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    shutil.copy2(REPOSITORY_ROOT / "LICENSE", stage / "LICENSE")
    (stage / "THIRD-PARTY-NOTICES.txt").write_text(
        "Synthetic W4 harness legal notice\n", encoding="utf-8", newline="\n"
    )
    license_root = stage / "licenses"
    license_root.mkdir()
    cpython_license = license_root / "cpython-3.11.15-LICENSE.txt"
    pyinstaller_license = license_root / "pyinstaller-6.21.0-COPYING.txt"
    shutil.copy2(
        REPOSITORY_ROOT / "packaging" / "licenses" / cpython_license.name,
        cpython_license,
    )
    shutil.copy2(
        REPOSITORY_ROOT / "packaging" / "licenses" / pyinstaller_license.name,
        pyinstaller_license,
    )
    artifact_status = "internal-verification-only-on-native-compliance-hold"
    harness_blockers = ["harness-native-license-and-provenance"]
    harness_authority = [
        {
            "condition": (
                "Approved license, notice, redistribution provenance, and legal "
                "authorization for embedded native/runtime components are unresolved."
            ),
            "id": harness_blockers[0],
            "resolution": (
                "Bind approved redistribution/license evidence for the actual frozen "
                "harness runtime closure."
            ),
        }
    ]
    harness_authority_sha = _sha256_bytes(_canonical_json_bytes(harness_authority))
    toolchain_hash = "6" * 64

    component_id = "build-runtime:pyinstaller-bootloader"
    empty_sha = _sha256_bytes(b"")
    entries: list[dict[str, object]] = []
    embedded_paths: list[str] = []
    source_paths = ["python-prefix/Lib/site-packages/PyInstaller/bootloader/run.exe"]

    def add_entry(
        kind: str,
        name: str,
        typecode: str,
        index: int,
        *,
        distribution_names: list[str] | None = None,
        source_ref: str | None = None,
    ) -> None:
        content = f"{kind}:{index}:{name}".encode("utf-8")
        resolved_source_ref = source_ref or (
            f"python-prefix/synthetic/{kind.casefold()}-{index}.bin"
        )
        entries.append(
            {
                "component_ids": [component_id],
                "compressed": False,
                "conda_component_ids": [],
                "content_sha256": _sha256_bytes(content),
                "distribution_names": distribution_names or [],
                "kind": kind,
                "name": name,
                "source_ref": resolved_source_ref,
                "source_sha256": _sha256_bytes(content),
                "source_size": len(content),
                "stored_sha256": _sha256_bytes(content),
                "stored_size": len(content),
                "typecode": typecode,
                "uncompressed_size": len(content),
            }
        )
        embedded_paths.append(f"pkv-loopback-provider.exe!/{name}")
        source_paths.append(resolved_source_ref)

    for index in range(47):
        add_entry("BINARY", f"runtime/binary-{index}.dll", "b", index)
    add_entry("DATA", "base_library.zip", "b", 0)
    for index in range(8):
        add_entry("EXTENSION", f"runtime/extension-{index}.pyd", "b", index)
    option_name = "pyi-contents-directory _internal"
    entries.append(
        {
            "component_ids": [component_id],
            "compressed": False,
            "content_sha256": empty_sha,
            "kind": "OPTION",
            "name": option_name,
            "stored_sha256": empty_sha,
            "stored_size": 0,
            "typecode": "o",
            "uncompressed_size": 0,
        }
    )
    embedded_paths.append(f"pkv-loopback-provider.exe!/{option_name}")
    for index in range(5):
        add_entry("PYMODULE", f"pyimod{index:02d}", "m", index)
    add_entry(
        "PYSOURCE",
        "pyiboot01_bootstrap",
        "s",
        0,
        distribution_names=["pyinstaller"],
        source_ref=(
            "python-prefix/Lib/site-packages/PyInstaller/loader/"
            "pyiboot01_bootstrap.py"
        ),
    )
    for index in range(1, 3):
        add_entry("PYSOURCE", f"synthetic_source_{index}", "s", index)
    # The outer gate re-derives the frozen PYZ byte partition:
    # 17-byte header + 4 stored member bytes + 2-byte TOC.
    pyz_content = b"x" * 23
    pyz_member = {
        "component_ids": [component_id],
        "conda_component_ids": [],
        "content_sha256": _sha256_bytes(b"compiled"),
        "content_size": len(b"compiled"),
        "distribution_names": [],
        "kind": "module",
        "name": "synthetic_module",
        "source_kind": "PYMODULE",
        "source_ref": "python-prefix/synthetic/synthetic_module.py",
        "source_sha256": _sha256_bytes(b""),
        "source_size": 0,
        "stored_sha256": _sha256_bytes(b"pyzc"),
        "stored_size": len(b"pyzc"),
    }
    pyz_members = [pyz_member]
    entries.append(
        {
            "component_ids": [component_id],
            "compressed": False,
            "conda_component_ids": [],
            "content_sha256": _sha256_bytes(pyz_content),
            "distribution_names": [],
            "kind": "PYZ",
            "name": "PYZ.pyz",
            "pyz_member_count": 1,
            "pyz_members": pyz_members,
            "pyz_members_sha256": _sha256_bytes(_canonical_json_bytes(pyz_members)),
            "pyz_python_magic_sha256": _sha256_bytes(b"magic"),
            "pyz_toc_sha256": _sha256_bytes(b"{}"),
            "pyz_toc_size": len(b"{}"),
            "source_ref": "python-prefix/synthetic/PYZ.pyz",
            "source_sha256": _sha256_bytes(pyz_content),
            "source_size": len(pyz_content),
            "stored_sha256": _sha256_bytes(pyz_content),
            "stored_size": len(pyz_content),
            "typecode": "z",
            "uncompressed_size": len(pyz_content),
        }
    )
    embedded_paths.append("pkv-loopback-provider.exe!/PYZ.pyz#/synthetic_module")
    source_paths.extend(
        [
            "python-prefix/synthetic/PYZ.pyz",
            "python-prefix/synthetic/synthetic_module.py",
        ]
    )
    assert len(entries) == 66
    source_records = [
        {
            "component_ids": [component_id],
            "conda_component_ids": [],
            "distribution_names": list(entry["distribution_names"]),
            "occurrences": [
                {
                    "destination": str(entry["name"]),
                    "slot": "embedded:pkv-loopback-provider.exe",
                    "type": str(entry["kind"]),
                }
            ],
            "path": str(entry["source_ref"]),
            "sha256": str(entry["source_sha256"]),
            "size": int(entry["source_size"]),
        }
        for entry in entries
        if entry["kind"] != "OPTION"
    ]
    source_records.extend(
        [
            {
                "component_ids": [component_id],
                "conda_component_ids": [],
                "distribution_names": [],
                "occurrences": [
                    {
                        "destination": "pkv-loopback-provider.exe",
                        "slot": "bootloader-prefix:pkv-loopback-provider.exe",
                        "type": "EXECUTABLE",
                    }
                ],
                "path": "python-prefix/Lib/site-packages/PyInstaller/bootloader/run.exe",
                "sha256": _sha256_bytes(bootloader_prefix),
                "size": len(bootloader_prefix),
            },
            {
                "component_ids": [component_id],
                "conda_component_ids": [],
                "distribution_names": [],
                "occurrences": [
                    {
                        "destination": "synthetic_module",
                        "slot": "pure-modules",
                        "type": "PYMODULE",
                    }
                ],
                "path": "python-prefix/synthetic/synthetic_module.py",
                "sha256": _sha256_bytes(b""),
                "size": 0,
            },
        ]
    )
    source_records.sort(key=lambda item: str(item["path"]).encode("utf-8"))
    source_paths = [str(item["path"]) for item in source_records]
    unowned_source_paths = [
        str(item["path"])
        for item in source_records
        if not item["conda_component_ids"] and not item["distribution_names"]
    ]
    embedded_paths.insert(0, "pkv-loopback-provider.exe!/<bootloader-prefix>")
    component = {
        "classification_ids": [],
        "contains_native_payload": True,
        "embedded_paths": sorted(set(embedded_paths)),
        "id": component_id,
        "identity_status": "complete",
        "name": "PyInstaller bootloader",
        "payload_paths": ["pkv-loopback-provider.exe"],
        "source_paths": source_paths,
        "type": "runtime",
        "version": "6.21.0",
    }
    registry_hash = "7" * 64
    authority = {
        "artifact_kind": "e2e_test_harness",
        "artifact_status": artifact_status,
        "build_fingerprint": build_fingerprint,
        "conda_native_registry_path": "packaging/locks/conda-native-registry.v1.json",
        "conda_native_registry_sha256": registry_hash,
        "environment_lock_path": "packaging/locks/release-environment.v2.json",
        "environment_lock_sha256": toolchain_hash,
        "release_blocker_authority": harness_authority,
        "release_blocker_authority_sha256": harness_authority_sha,
        "release_blockers": harness_blockers,
        "release_eligible": False,
        "source_revision": source_revision,
    }
    runtime_sha = _sha256_bytes(runtime.read_bytes())
    archive_material = {
        "bootloader_input": {
            "source_ref": "python-prefix/Lib/site-packages/PyInstaller/bootloader/run.exe",
            "source_sha256": _sha256_bytes(bootloader_prefix),
            "source_size": len(bootloader_prefix),
        },
        "bootloader_prefix_sha256": _sha256_bytes(bootloader_prefix),
        "bootloader_prefix_size": len(bootloader_prefix),
        "component_ids": [component_id],
        "entries": entries,
        "entry_count": len(entries),
        "executable_artifact_path": runtime.name,
        "executable_sha256": runtime_sha,
        "executable_size": runtime.stat().st_size,
        "pkg_sha256": _sha256_bytes(pkg_suffix),
        "pkg_size": len(pkg_suffix),
        "python_library": "python311.dll",
        "python_version": 311,
    }
    archive = {
        **archive_material,
        "portable_graph_sha256": _sha256_bytes(_canonical_json_bytes(archive_material)),
    }
    payload_tree_sha = _sha256_bytes(
        f"{runtime.name}\0{runtime.stat().st_size}\0{runtime_sha}\n".encode("utf-8")
    )
    payload_row = {
        "artifact_path": runtime.name,
        "component_ids": [component_id],
        "embedded_archive_graph_sha256": archive["portable_graph_sha256"],
        "embedded_component_ids": [component_id],
        "embedded_entry_count": 66,
        "embedded_pkg_sha256": archive["pkg_sha256"],
        "embedded_pkg_size": archive["pkg_size"],
        "kind": "PYINSTALLER_BOOTLOADER_EXECUTABLE",
        "path": runtime.name,
        "sha256": runtime_sha,
        "size": runtime.stat().st_size,
    }
    analysis_sha = "8" * 64
    portable_binding = {
        "analysis_graph_sha256": analysis_sha,
        "artifact_path_base": ".",
        "conda_native_registry_sha256": registry_hash,
        "embedded_archives_sha256": _sha256_bytes(_canonical_json_bytes([archive])),
        "payload_tree_sha256": payload_tree_sha,
    }
    closure_sha = _sha256_bytes(_canonical_json_bytes(portable_binding))
    artifact_closure_sha = _sha256_bytes(
        _canonical_json_bytes(
            {"authority": authority, "inventory_closure_sha256": closure_sha}
        )
    )
    inventory = {
        "analysis": {
            "entry_count": 66,
            "portable_graph_sha256": analysis_sha,
            "source_count": len(source_records),
            "sources": source_records,
            "virtual_entries": [],
        },
        "authority": authority,
        "bindings": {
            **portable_binding,
            "artifact_closure_sha256": artifact_closure_sha,
            "closure_sha256": closure_sha,
        },
        "components": [component],
        "coverage": {
            "conda_native_registry_sha256": registry_hash,
            "embedded_archive_count": 1,
            "embedded_entry_count": 66,
            "payload_file_count": 1,
            "unattributed_native_file_count": 0,
            "unattributed_native_paths": [],
            "unowned_source_path_count": len(unowned_source_paths),
            "unowned_source_paths": unowned_source_paths,
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
            "file_count": 1,
            "files": [payload_row],
            "path_base": ".",
            "tree_sha256": payload_tree_sha,
        },
        "schema_version": "pkv.release-inventory.v1",
    }
    inventory_path = stage / "release-inventory.json"
    inventory_path.write_text(
        json.dumps(inventory, sort_keys=True) + "\n", encoding="utf-8"
    )
    inventory_sha = _sha256_bytes(inventory_path.read_bytes())

    harness_license_entries = [
        {
            "license_expression": "Python-2.0",
            "license_files": [
                {
                    "path": f"licenses/{cpython_license.name}",
                    "sha256": hashlib.sha256(cpython_license.read_bytes()).hexdigest(),
                    "source_kind": "compliance_asset",
                }
            ],
            "name": "cpython",
            "purl": "pkg:generic/cpython@3.11.15",
            "version": "3.11.15",
        },
        {
            "license_expression": "GPL-2.0-or-later WITH Bootloader-exception",
            "license_files": [
                {
                    "path": f"licenses/{pyinstaller_license.name}",
                    "sha256": hashlib.sha256(
                        pyinstaller_license.read_bytes()
                    ).hexdigest(),
                    "source_kind": "compliance_asset",
                }
            ],
            "name": "pyinstaller",
            "purl": "pkg:generic/pyinstaller@6.21.0",
            "version": "6.21.0",
        },
    ]
    harness_license_index = license_root / "index.json"
    component_sha = _sha256_bytes(_canonical_json_bytes(component))
    pyinstaller_license_row = harness_license_entries[1]["license_files"]
    actual_runtime_license = {
        "classifications": [],
        "component_id": component_id,
        "component_sha256": component_sha,
        "embedded_paths": component["embedded_paths"],
        "license": {"expression": "GPL-2.0-or-later WITH Bootloader-exception"},
        "license_files": pyinstaller_license_row,
        "license_material_status": "top-level-only-compliance-hold",
        "name": component["name"],
        "payload_paths": component["payload_paths"],
        "purl": "pkg:generic/build-runtime-pyinstaller-bootloader@6.21.0",
        "source_paths": component["source_paths"],
        "version": component["version"],
    }
    harness_license_index.write_text(
        json.dumps(
            {
                "schema_version": "pkv.license-index.v2",
                "actual_runtime_inventory": {
                    "components": [actual_runtime_license],
                    "release_inventory_closure_sha256": closure_sha,
                    "release_inventory_path": "release-inventory.json",
                },
                "entries": harness_license_entries,
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    sbom = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "version": 1,
        "metadata": {
            "timestamp": "2026-01-01T00:00:00Z",
            "component": {
                "bom-ref": "pkg:generic/pkv-w4-loopback-harness@1.0.0",
                "name": "PKV W4 Loopback Harness",
                "type": "application",
                "version": "1.0.0",
            },
            "properties": [
                {"name": "pkv:artifact-kind", "value": "e2e_test_harness"},
                {"name": "pkv:artifact-status", "value": artifact_status},
                {"name": "pkv:release-blocker", "value": harness_blockers[0]},
                {
                    "name": "pkv:release-blocker-authority-sha256",
                    "value": harness_authority_sha,
                },
                {"name": "pkv:release-eligible", "value": "false"},
                {
                    "name": "pkv:release-inventory-closure-sha256",
                    "value": closure_sha,
                },
                {
                    "name": "pkv:release-inventory-path",
                    "value": "release-inventory.json",
                },
                {"name": "pkv:release-inventory-sha256", "value": inventory_sha},
                {
                    "name": "pkv:release-payload-membership",
                    "value": "forbidden",
                },
            ],
        },
        "components": [
            {
                "bom-ref": f"urn:pkv:release-inventory-component:{component_sha}",
                "licenses": [actual_runtime_license["license"]],
                "name": component["name"],
                "properties": [
                    {"name": "pkv:inventory-component-id", "value": component_id},
                    {"name": "pkv:inventory-component-sha256", "value": component_sha},
                    {"name": "pkv:inventory-identity-status", "value": "complete"},
                    {"name": "pkv:contains-native-payload", "value": "true"},
                    {
                        "name": "pkv:license-material-status",
                        "value": "top-level-only-compliance-hold",
                    },
                    *[
                        {"name": "pkv:payload-path", "value": path}
                        for path in component["payload_paths"]
                    ],
                    *[
                        {"name": "pkv:embedded-path", "value": path}
                        for path in component["embedded_paths"]
                    ],
                ],
                "purl": actual_runtime_license["purl"],
                "type": "library",
                "version": component["version"],
            }
        ],
        "dependencies": [
            {
                "ref": "pkg:generic/pkv-w4-loopback-harness@1.0.0",
                "dependsOn": [f"urn:pkv:release-inventory-component:{component_sha}"],
            }
        ],
    }
    sbom_path = stage / "sbom.cdx.json"
    sbom_path.write_text(json.dumps(sbom, sort_keys=True) + "\n", encoding="utf-8")
    (stage / "COMPLIANCE-HOLD.txt").write_text(
        "INTERNAL VERIFICATION ONLY - NATIVE COMPLIANCE HOLD\n", encoding="utf-8"
    )
    legal_paths = [
        "COMPLIANCE-HOLD.txt",
        "LICENSE",
        "THIRD-PARTY-NOTICES.txt",
        f"licenses/{cpython_license.name}",
        "licenses/index.json",
        f"licenses/{pyinstaller_license.name}",
        "release-inventory.json",
        "sbom.cdx.json",
    ]
    legal_manifest = {
        "schema_version": "pkv.harness-legal-manifest.v1",
        "artifact_kind": "e2e_test_harness",
        "artifact_status": artifact_status,
        "build_fingerprint": build_fingerprint,
        "release_blocker_authority": harness_authority,
        "release_blocker_authority_sha256": harness_authority_sha,
        "release_blockers": harness_blockers,
        "release_eligible": False,
        "release_inventory_closure_sha256": closure_sha,
        "release_inventory_sha256": inventory_sha,
        "entries": [
            {
                "path": relative,
                "sha256": hashlib.sha256((stage / relative).read_bytes()).hexdigest(),
                "size": (stage / relative).stat().st_size,
            }
            for relative in legal_paths
        ],
    }
    legal_manifest_path = stage / "legal-manifest.json"
    legal_manifest_path.write_text(
        json.dumps(legal_manifest, sort_keys=True) + "\n", encoding="utf-8"
    )
    manifest = {
        "schema_version": "pkv.w3.loopback.manifest.v1",
        "contract_id": "w3.openai_compatible_loopback.v1",
        "harness_version": "1.0.0",
        "distribution": "e2e-only",
        "release_payload_membership": "forbidden",
        "runtime": {
            "kind": "frozen",
            "path": runtime.name,
            "size": runtime.stat().st_size,
            "sha256": hashlib.sha256(runtime.read_bytes()).hexdigest(),
        },
        "contract": {
            "path": contract.name,
            "sha256": hashlib.sha256(contract.read_bytes()).hexdigest(),
        },
        "scripts": script_rows,
        "build": {
            "source_revision": source_revision,
            "build_fingerprint_sha256": build_fingerprint,
            "toolchain_lock_sha256": toolchain_hash,
        },
    }
    manifest_path = stage / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8"
    )
    package_root.mkdir(parents=True, exist_ok=True)
    harness_zip = package_root / f"{harness_id}.zip"
    with zipfile.ZipFile(harness_zip, "w", compression=zipfile.ZIP_STORED) as archive:
        for path in sorted(item for item in stage.rglob("*") if item.is_file()):
            archive.write(path, f"{harness_id}/{path.relative_to(stage).as_posix()}")
    zip_hash = hashlib.sha256(harness_zip.read_bytes()).hexdigest()
    (package_root / f"{harness_zip.name}.sha256").write_bytes(
        f"{zip_hash}  {harness_zip.name}\n".encode("ascii")
    )
    provenance = {
        "schema_version": "pkv.w3-harness-provenance.v1",
        "artifact_file": harness_zip.name,
        "artifact_sha256": zip_hash,
        "artifact_size": harness_zip.stat().st_size,
        "artifact_status": artifact_status,
        "build_fingerprint": build_fingerprint,
        "contract_sha256": hashlib.sha256(contract.read_bytes()).hexdigest(),
        "harness_version": "1.0.0",
        "artifact_kind": "e2e_test_harness",
        "legal_manifest_path": f"{harness_id}/legal-manifest.json",
        "legal_manifest_sha256": hashlib.sha256(
            legal_manifest_path.read_bytes()
        ).hexdigest(),
        "manifest_path": f"{harness_id}/manifest.json",
        "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "release_blocker_authority": harness_authority,
        "release_blocker_authority_sha256": harness_authority_sha,
        "release_blockers": harness_blockers,
        "release_eligible": False,
        "release_inventory_closure_sha256": closure_sha,
        "release_inventory_path": f"{harness_id}/release-inventory.json",
        "release_inventory_sha256": inventory_sha,
        "release_payload_membership": "forbidden",
        "runtime_path": f"{harness_id}/{runtime.name}",
        "runtime_sha256": hashlib.sha256(runtime.read_bytes()).hexdigest(),
        "sbom_path": f"{harness_id}/sbom.cdx.json",
        "sbom_sha256": hashlib.sha256(sbom_path.read_bytes()).hexdigest(),
        "source_revision": source_revision,
        "toolchain_lock_sha256": toolchain_hash,
    }
    (package_root / f"{harness_id}.provenance.json").write_text(
        json.dumps(provenance, sort_keys=True) + "\n", encoding="utf-8"
    )


def _write_compliance_bundle(
    root: Path,
    *,
    build_fingerprint: str,
    source_revision: str,
    compliance_manifest_sha256: str,
    blockers: list[str],
) -> tuple[Path, Path]:
    root.mkdir(parents=True)
    source_name = "html2text-2020.1.16.tar.gz"
    source = root / source_name
    shutil.copy2(
        REPOSITORY_ROOT / "packaging" / "compliance-sources" / source_name,
        source,
    )
    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    assert (
        source_sha == "e296318e16b059ddb97f7a8a1d6a5c1d7af4544049a01e261731d2d5cc277bbb"
    )
    assert source.stat().st_size == 49464
    (root / f"{source_name}.sha256").write_bytes(
        f"{source_sha}  {source_name}\n".encode("ascii")
    )
    authority = []
    for blocker in blockers:
        row: dict[str, object] = {
            "condition": f"synthetic unresolved condition for {blocker}",
            "id": blocker,
            "resolution": f"synthetic required resolution for {blocker}",
        }
        if blocker == "html2text-gpl-compliance":
            row["resolution_requirements"] = [
                "combined-work-licensing-decision",
                "corresponding-source-scope-and-persistent-location",
                "spdx-license-expression",
                "whole-work-license-and-notices",
            ]
        if blocker == "conda-native-license-materials-and-spdx":
            row["affected_component_selectors"] = [
                "component:*[native-payload]",
                "conda-package:*",
            ]
        authority.append(row)
    authority_sha = hashlib.sha256(
        (json.dumps(authority, sort_keys=True, separators=(",", ":")) + "\n").encode(
            "utf-8"
        )
    ).hexdigest()
    manifest = {
        "schema_version": "pkv.compliance-source-bundle.v1",
        "artifact_kind": "corresponding_source_bundle",
        "build_fingerprint": build_fingerprint,
        "compliance_manifest_sha256": compliance_manifest_sha256,
        "files": [
            {
                "component": "html2text",
                "license_expression_assessment": "GPL-3.0-only",
                "license_expression_status": "requires_legal_confirmation",
                "path": source_name,
                "sha256": source_sha,
                "size": source.stat().st_size,
                "version": "2020.1.16",
            }
        ],
        "release_blockers": blockers,
        "release_blocker_authority": authority,
        "release_blocker_authority_sha256": authority_sha,
        "release_eligible": False,
        "source_revision": source_revision,
    }
    manifest_path = root / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8"
    )
    provenance = {
        "schema_version": "pkv.compliance-source-provenance.v1",
        "artifact_kind": "corresponding_source_bundle",
        "build_fingerprint": build_fingerprint,
        "compliance_manifest_sha256": compliance_manifest_sha256,
        "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "release_blockers": blockers,
        "release_blocker_authority": authority,
        "release_blocker_authority_sha256": authority_sha,
        "release_eligible": False,
        "source_file": source_name,
        "source_sha256": source_sha,
        "source_revision": source_revision,
    }
    provenance_path = root / "provenance.json"
    provenance_path.write_text(
        json.dumps(provenance, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest_path, provenance_path


def _new_launcher_fixture(root: Path) -> dict[str, Path]:
    assert not _is_within(root, REPOSITORY_ROOT)
    controller_root = root / "controller"
    fixture = controller_root / "fixtures"
    harness = root / "harness"
    for directory in (controller_root, fixture, harness):
        directory.mkdir(parents=True)

    sentinel = root / "controller-was-executed.txt"
    controller = controller_root / "Invoke-W4ArtifactE2E.ps1"
    parameter_names = (
        "CandidateRoot",
        "DistributionZip",
        "DistributionSha256",
        "ProvenancePath",
        "ComplianceSourcesRoot",
        "ComplianceManifestPath",
        "ComplianceProvenancePath",
        "FixtureRoot",
        "EvidenceRoot",
        "WorkspaceRoot",
        "ScenarioContract",
        "HarnessRoot",
        "ExecutionId",
    )
    parameters = ",\n".join(f"    [string]${name}" for name in parameter_names)
    controller.write_text(
        "param(\n"
        + parameters
        + "\n)\n"
        + "[System.IO.File]::WriteAllText('"
        + _ps_single_quoted(sentinel)
        + "', 'executed')\nexit 91\n",
        encoding="utf-8",
    )
    for module_name in ("W4.Driver.psm1", "W4.Scenarios.psm1"):
        (controller_root / module_name).write_text(
            "# synthetic launcher boundary module\n", encoding="utf-8"
        )
    scenario_contract = controller_root / "scenarios.v1.json"
    shutil.copy2(SCENARIO_CONTRACT, scenario_contract)
    (fixture / "fixture-manifest.v1.json").write_text(
        json.dumps(
            {
                "schema_version": "pkv.m13.w4-fixtures.v1",
                "synthetic_only": True,
                "contains_credentials": False,
                "contains_real_vault_data": False,
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    candidate_root = root / "candidate"
    candidate_root.mkdir()
    distribution = candidate_root / f"{ARTIFACT_ID}.zip"
    with zipfile.ZipFile(distribution, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr(f"{ARTIFACT_ID}/placeholder.txt", "synthetic")
    digest = hashlib.sha256(distribution.read_bytes()).hexdigest()
    sidecar = candidate_root / f"{distribution.name}.sha256"
    sidecar.write_bytes(f"{digest}  {distribution.name}\n".encode("ascii"))
    provenance = candidate_root / f"{ARTIFACT_ID}.provenance.json"
    blockers = [
        "conda-native-license-materials-and-spdx",
        "html2text-gpl-compliance",
        "native-msvc-license-and-provenance",
        "qt-corresponding-source-location",
        "qt-linkage-and-replacement-not-proven",
        "qt-module-license-audit",
        "qt-notice-placeholders",
    ]
    compliance_authority_sha = "7" * 64
    compliance_root = root / "compliance-sources"
    compliance_manifest, compliance_provenance = _write_compliance_bundle(
        compliance_root,
        build_fingerprint="2" * 64,
        source_revision="5" * 40,
        compliance_manifest_sha256=compliance_authority_sha,
        blockers=blockers,
    )
    source_path = compliance_root / "html2text-2020.1.16.tar.gz"
    compliance_manifest_payload = json.loads(
        compliance_manifest.read_text(encoding="utf-8")
    )
    provenance_payload = {
        "schema_version": "pkv.artifact-provenance.v1",
        "artifact_file": distribution.name,
        "artifact_kind": "test_candidate",
        "artifact_status": "test-candidate-on-compliance-hold",
        "artifact_sha256": digest,
        "artifact_size": distribution.stat().st_size,
        "build_info_path": f"{ARTIFACT_ID}/build-info.json",
        "build_info_sha256": "1" * 64,
        "build_fingerprint": "2" * 64,
        "compliance_manifest_sha256": compliance_authority_sha,
        "conda_hardlink_threat_evidence": {
            "schema_version": "pkv.conda-hardlink-threat-evidence.v1",
            "anchors": [
                {
                    "hardlink_count": 2,
                    "label": label,
                    "path": path,
                    "sha256": character * 64,
                    "size": index + 1,
                }
                for index, (label, path, character) in enumerate(
                    (
                        (
                            "numpy-package-anchor",
                            "Lib/site-packages/numpy/__init__.py",
                            "b",
                        ),
                        ("python-dll", "python311.dll", "c"),
                        ("python-executable", "python.exe", "d"),
                    )
                )
            ],
            "observed_hardlink_anchor_count": 3,
            "release_eligible_environment_requirement": "copy-only-no-hardlinks",
            "threat_model": "accepted_for_test_candidate",
            "validation_scope": [
                "before-build-a",
                "after-build-a",
                "before-build-b",
                "after-build-b",
                "before-publication",
            ],
        },
        "compliance_sources": {
            "manifest_path": "../compliance-sources/manifest.json",
            "manifest_sha256": hashlib.sha256(
                compliance_manifest.read_bytes()
            ).hexdigest(),
            "provenance_path": "../compliance-sources/provenance.json",
            "provenance_sha256": hashlib.sha256(
                compliance_provenance.read_bytes()
            ).hexdigest(),
            "root": "../compliance-sources",
            "source_file": source_path.name,
            "source_sha256": hashlib.sha256(source_path.read_bytes()).hexdigest(),
            "source_size": source_path.stat().st_size,
        },
        "payload_manifest_path": f"{ARTIFACT_ID}/payload-manifest.json",
        "payload_manifest_sha256": "3" * 64,
        "sbom_path": f"{ARTIFACT_ID}/sbom.cdx.json",
        "sbom_sha256": "4" * 64,
        "source_revision": "5" * 40,
        "release_blockers": blockers,
        "release_blocker_authority": compliance_manifest_payload[
            "release_blocker_authority"
        ],
        "release_blocker_authority_sha256": compliance_manifest_payload[
            "release_blocker_authority_sha256"
        ],
        "release_eligible": False,
        "release_inventory_artifact_closure_sha256": "8" * 64,
        "release_inventory_closure_sha256": "9" * 64,
        "release_inventory_path": f"{ARTIFACT_ID}/release-inventory.json",
        "release_inventory_sha256": "a" * 64,
        "version": "0.8.1",
    }
    provenance.write_text(
        json.dumps(provenance_payload, sort_keys=True) + "\n", encoding="utf-8"
    )
    _write_harness_package(
        harness,
        build_fingerprint=provenance_payload["build_fingerprint"],
        source_revision=provenance_payload["source_revision"],
    )
    _write_driver_manifest(controller_root)

    return {
        "candidate_root": candidate_root,
        "zip": distribution,
        "sidecar": sidecar,
        "provenance": provenance,
        "controller": controller,
        "driver_root": controller_root,
        "controller_module": controller_root / "W4.Driver.psm1",
        "scenario_contract": scenario_contract,
        "fixture": fixture,
        "harness": harness,
        "compliance_root": compliance_root,
        "compliance_manifest": compliance_manifest,
        "compliance_provenance": compliance_provenance,
        "evidence": root / "evidence",
        "workspace": root / "workspace",
        "harness_workspace": root / "harness-workspace",
        "sentinel": sentinel,
        "run_id": Path(f"negative-{uuid.uuid4().hex}"),
    }


def _new_real_controller_fixture(root: Path) -> dict[str, Path]:
    bundle = _new_launcher_fixture(root)
    controller_root = bundle["controller"].parent
    for source in (CONTROLLER, DRIVER_MODULE, SCENARIO_MODULE, SCENARIO_CONTRACT):
        shutil.copy2(source, controller_root / source.name)
    bundle["controller"] = controller_root / CONTROLLER.name
    bundle["scenario_contract"] = controller_root / SCENARIO_CONTRACT.name

    _write_driver_manifest(controller_root)
    return bundle


@pytest.fixture
def external_scratch() -> Path:
    """Provide a disposable root that satisfies the runner's source isolation."""

    with tempfile.TemporaryDirectory(
        prefix=".pkv-w4-negative-", dir=REPOSITORY_ROOT.parent
    ) as temporary:
        root = Path(temporary).resolve()
        assert not _is_within(root, REPOSITORY_ROOT)
        yield root


def test_w4_scripts_parse_and_modules_import_in_windows_powershell_5() -> None:
    paths = (RUNNER, CONTROLLER, DRIVER_MODULE, SCENARIO_MODULE)
    path_literals = ",".join(f"'{_ps_single_quoted(path)}'" for path in paths)
    command = (
        "$ErrorActionPreference='Stop';"
        f"$paths=@({path_literals});"
        "$commands=@();"
        "foreach($path in $paths){"
        "$tokens=$null;$parseErrors=$null;"
        "$ast=[System.Management.Automation.Language.Parser]::ParseFile("
        "$path,[ref]$tokens,[ref]$parseErrors);"
        "if($parseErrors.Count -ne 0){throw (($parseErrors|% Message)-join '; ')};"
        "$commands+=@($ast.FindAll({param($node)"
        "$node -is [System.Management.Automation.Language.CommandAst]},$true)|"
        "% GetCommandName)};"
        "Import-Module '"
        + _ps_single_quoted(DRIVER_MODULE)
        + "' -Force -ErrorAction Stop;"
        "Import-Module '"
        + _ps_single_quoted(SCENARIO_MODULE)
        + "' -Force -ErrorAction Stop;"
        "[ordered]@{major=$PSVersionTable.PSVersion.Major;commands=@($commands)}|"
        "ConvertTo-Json -Compress"
    )

    result = _run_powershell(["-Command", command], cwd=REPOSITORY_ROOT)

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["major"] == 5
    invoked = {str(value).lower() for value in payload["commands"] if value}
    assert invoked.isdisjoint({"python", "python.exe", "py", "py.exe"})


def test_scenario_contract_freezes_ten_scenarios_and_eleven_unique_rows() -> None:
    contract = json.loads(_read(SCENARIO_CONTRACT))
    scenarios = contract["ordered_scenarios"]
    scenario_ids = [item["scenario_id"] for item in scenarios]
    rows = [row for item in scenarios for row in item["matrix_rows"]]

    assert len(scenarios) == 10
    assert len(scenario_ids) == len(set(scenario_ids)) == 10
    assert len(rows) == len(set(rows)) == 11
    assert set(rows) == EXPECTED_MATRIX_ROWS
    assert set(contract["required_matrix_rows"]) == EXPECTED_MATRIX_ROWS
    harness_scenarios = [
        item["scenario_id"] for item in scenarios if item["requires_harness"]
    ]
    assert harness_scenarios == ["w4.chat_loopback.v1"]


def test_controller_consumes_the_complete_uia_registry() -> None:
    contract = json.loads(_read(SCENARIO_CONTRACT))
    uia = contract["uia"]
    assert uia["navigation_names"] == ["浏览", "搜索", "归档", "对话", "统计", "设置"]
    assert len(uia["required_automation_ids"]) == len(
        set(uia["required_automation_ids"])
    )

    controller_source = _read(CONTROLLER)
    scenario_source = _read(SCENARIO_MODULE)
    assert "required_automation_ids" in controller_source
    assert "uia-contract-coverage.json" in controller_source
    assert "navigation_names" in scenario_source
    assert "uia-navigation-contract.json" in scenario_source


def test_outer_gate_requires_canonical_utc_evidence_timestamp() -> None:
    source = _read(RUNNER)

    assert "$record.executed_at -is [string]" in source
    assert "[DateTime]::TryParseExact(" in source
    assert "DateTimeStyles]::RoundtripKind" in source
    assert r"\.\d{7}Z$" in source
    assert "-not $executedAtIsUtc" in source


def test_outer_gate_applies_exact_special_distribution_owner_predicate() -> None:
    source = _read(RUNNER)

    assert source.count("Assert-DistributionOwnerSet") == 4
    assert "build-runtime:pyinstaller-bootloader" in source
    assert "build-runtime:pyinstaller-hooks'" in source
    assert "build-runtime:pyinstaller-hooks-contrib" in source
    assert "python-distribution:$canonicalName" in source
    assert "generic distribution component owners are not exact" in source
    assert "-AllowPyInstallerBootloader:$sourceAllowsPyInstallerBootloader" in source


def test_w4_sbom_status_is_bound_to_final_license_index_status() -> None:
    outer_source = _read(RUNNER)
    product_source = _read(SCENARIO_MODULE)

    assert "requires-license-index-binding" not in outer_source
    assert "requires-license-index-binding" not in product_source
    assert "Get-ExpectedLicenseMaterialStatus" in outer_source
    assert "return 'bound'" in outer_source
    assert "[string]$runtimeLicenseRow.license_material_status" in outer_source
    assert "Assert-W4LicenseMaterialStatusBinding" in product_source
    assert "SBOM/license-index license material status is invalid" in product_source


def test_build_environment_contract_requires_a_per_build_home(tmp_path: Path) -> None:
    accepted = _run_build_environment_contract_validator(
        tmp_path, dict(BUILD_ENVIRONMENT_CONTRACT)
    )
    assert accepted.returncode == 0, accepted.stderr

    missing_home = dict(BUILD_ENVIRONMENT_CONTRACT)
    missing_home.pop("home_directory")
    missing = _run_build_environment_contract_validator(tmp_path, missing_home)
    assert missing.returncode != 0
    assert "missing required field: home_directory" in missing.stderr

    ambient_home = dict(BUILD_ENVIRONMENT_CONTRACT)
    ambient_home["home_directory"] = "ambient-user-profile"
    rejected = _run_build_environment_contract_validator(tmp_path, ambient_home)
    assert rejected.returncode != 0
    assert "not the frozen clean build contract" in rejected.stderr


def test_controller_bundle_has_no_source_import_or_test_bypass() -> None:
    source = "\n".join(
        _read(path) for path in (CONTROLLER, DRIVER_MODULE, SCENARIO_MODULE)
    )
    assert re.search(r"(?im)^\s*(?:from|import)\s+src(?:\.|\s|$)", source) is None
    assert (
        re.search(
            r"(?im)^\s*Import-Module\b[^\r\n]*(?:[/\\]src[/\\.]|-m\s+src\b)",
            source,
        )
        is None
    )
    assert (
        re.search(r"(?im)^\s*(?:&\s*)?(?:python(?:\.exe)?|py(?:\.exe)?)\b", source)
        is None
    )
    assert (
        re.search(
            r"(?im)^\s*(?:Start-Process|Invoke-W4Process)\b[^\r\n]*"
            r"-FileName\s+['\"]?(?:python(?:\.exe)?|py(?:\.exe)?)\b",
            source,
        )
        is None
    )
    for bypass in ("s" + "kip", "x" + "fail"):
        assert re.search(rf"(?i)\b{bypass}\b", source) is None


def test_controller_statically_rejects_zip_traversal_and_ambiguous_entries() -> None:
    source = _read(CONTROLLER)

    assert "ZIP entry violates the single-root/no-traversal contract" in source
    assert "ZIP entry escaped extraction root" in source
    assert "ZIP contains duplicate case-insensitive entry" in source
    assert "@($segments | Where-Object { $_ -eq '.' -or $_ -eq '..' })" in source
    assert "Test-W4PathContainedBy -Candidate $fullTarget -Root $Destination" in source
    assert "[System.IO.FileMode]::CreateNew" in source


def test_controller_statically_enforces_sidecar_and_exact_provenance_schema() -> None:
    source = _read(CONTROLLER)
    match = re.search(
        r"\$provenanceFields\s*=\s*@\((.*?)\)\s*Assert-W4ExactFields",
        source,
        flags=re.DOTALL,
    )

    assert match is not None
    assert _extract_single_quoted_values(match.group(1)) == PROVENANCE_FIELDS
    assert "ZIP .sha256 sidecar does not exactly bind" in source
    assert "$Matches[1] -ne $artifactSha" in source
    assert "$Matches[2] -ne $artifactFileName" in source
    assert "Assert-W4ExactFields -Object $provenance" in source
    assert "Artifact provenance path/hash cross-check failed after extraction" in source


def test_controller_statically_enforces_evidence_identity_and_one_decision() -> None:
    source = _read(CONTROLLER)
    evidence_block = source[
        source.index("function Test-W4EvidenceRecord") : source.index("\ntry {", 250)
    ]
    fields_match = re.search(
        r"\$fields\s*=\s*@\((.*?)\)\s*Assert-W4ExactFields", evidence_block, re.DOTALL
    )
    decision_match = re.search(
        r"\$decision\s*=\s*if\s*\((.*?)\n\s*\$summary\s*=",
        source,
        re.DOTALL,
    )

    assert fields_match is not None
    assert _extract_single_quoted_values(fields_match.group(1)) == EVIDENCE_FIELDS
    assert "@('artifact_verified', 'artifact_failed')" in evidence_block
    assert "^[0-9a-f]{64}$" in evidence_block
    assert "Evidence path is not safe relative path" in evidence_block
    assert "Evidence path is missing or escaped run root" in evidence_block
    assert len(re.findall(r"\$decision\s*=", source)) == 1
    assert decision_match is not None
    assert _extract_single_quoted_values(decision_match.group(1)) == {"release", "hold"}
    assert "decision = $decision" in source
    assert "if (-not $functionalVerified)" in source
    assert "[bool]$provenance.release_eligible" in decision_match.group(1)
    assert "@($provenance.release_blockers).Count -eq 0" in decision_match.group(1)


@pytest.mark.parametrize(
    ("missing_key", "expected_error"),
    [
        ("controller_module", "W4 controller bundle is incomplete"),
        ("harness", "Required path does not exist"),
        ("fixture", "Required path does not exist"),
        ("sidecar", "Required path does not exist"),
        ("provenance", "Required path does not exist"),
    ],
)
def test_full_matrix_fails_before_controller_when_required_input_is_missing(
    external_scratch: Path, missing_key: str, expected_error: str
) -> None:
    bundle = _new_launcher_fixture(external_scratch)
    missing = bundle[missing_key]
    if missing.is_dir():
        shutil.rmtree(missing)
    else:
        missing.unlink()

    result = _run_powershell(
        _full_matrix_command(bundle), cwd=external_scratch, timeout=30
    )

    assert result.returncode != 0
    assert expected_error in result.stderr
    assert not bundle[
        "sentinel"
    ].exists(), f"controller executed before {missing_key} was rejected"


def test_real_controller_rejects_zip_slip_before_product_execution(
    external_scratch: Path,
) -> None:
    bundle = _new_real_controller_fixture(external_scratch)
    distribution = bundle["zip"]
    distribution.unlink()
    with zipfile.ZipFile(distribution, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr(f"{ARTIFACT_ID}/../escape.txt", "must-not-extract")
    artifact_sha = hashlib.sha256(distribution.read_bytes()).hexdigest()
    bundle["sidecar"].write_bytes(
        f"{artifact_sha}  {distribution.name}\n".encode("ascii")
    )
    provenance = json.loads(bundle["provenance"].read_text(encoding="utf-8"))
    provenance["artifact_sha256"] = artifact_sha
    provenance["artifact_size"] = distribution.stat().st_size
    bundle["provenance"].write_text(
        json.dumps(provenance, sort_keys=True) + "\n", encoding="utf-8"
    )

    result = _run_powershell(
        _full_matrix_command(bundle), cwd=external_scratch, timeout=30
    )

    assert result.returncode != 0
    assert "single-root/no-traversal contract" in result.stderr
    assert not list(external_scratch.rglob("escape.txt"))


def test_full_matrix_rejects_zero_exit_controller_without_bound_evidence(
    external_scratch: Path,
) -> None:
    bundle = _new_launcher_fixture(external_scratch)
    controller = bundle["controller"]
    controller.write_text(
        _read(controller).replace("exit 91", "Write-Output '{}'\nexit 0"),
        encoding="utf-8",
    )
    _write_driver_manifest(bundle["driver_root"])

    result = _run_powershell(
        _full_matrix_command(bundle), cwd=external_scratch, timeout=30
    )

    assert result.returncode != 0
    assert bundle["sentinel"].is_file(), result.stderr
    assert "without a run evidence root" in result.stderr


def test_full_matrix_rejects_nested_fixture_hardlink_before_controller(
    external_scratch: Path,
) -> None:
    bundle = _new_launcher_fixture(external_scratch)
    manifest = bundle["fixture"] / "fixture-manifest.v1.json"
    source = external_scratch / "hardlink-source.json"
    source.write_bytes(manifest.read_bytes())
    manifest.unlink()
    os.link(source, manifest)
    _write_driver_manifest(bundle["driver_root"])

    result = _run_powershell(
        _full_matrix_command(bundle), cwd=external_scratch, timeout=30
    )

    assert result.returncode != 0
    assert "Unsafe HardLink rejected" in result.stderr
    assert not bundle["sentinel"].exists()


def test_full_matrix_rejects_nested_fixture_junction_before_controller(
    external_scratch: Path,
) -> None:
    bundle = _new_launcher_fixture(external_scratch)
    junction_target = external_scratch / "junction-target"
    junction_target.mkdir()
    (junction_target / "canary.txt").write_text("outside fixture\n", encoding="utf-8")
    junction = bundle["fixture"] / "nested-junction"
    setup = _run_powershell(
        [
            "-Command",
            "New-Item -ItemType Junction -Path '"
            + _ps_single_quoted(junction)
            + "' -Target '"
            + _ps_single_quoted(junction_target)
            + "' -ErrorAction Stop | Out-Null",
        ],
        cwd=external_scratch,
    )
    assert setup.returncode == 0, setup.stderr

    result = _run_powershell(
        _full_matrix_command(bundle), cwd=external_scratch, timeout=30
    )

    assert result.returncode != 0
    assert "Unsafe ReparsePoint rejected" in result.stderr
    assert not bundle["sentinel"].exists()


@pytest.mark.parametrize(
    "inputs",
    [
        {},
        {"packaging/locks/release-environment.v2.json": "b" * 64},
        {"PACKAGING/LOCKS/RELEASE-ENVIRONMENT.V2.JSON": "a" * 64},
        {"packaging/locks/release-environment.v2.json": "A" * 64},
    ],
)
def test_release_lock_binding_rejects_missing_different_or_noncanonical_case(
    inputs: dict[str, str],
) -> None:
    inputs_json = json.dumps(inputs, separators=(",", ":")).replace("'", "''")
    command = (
        "$ErrorActionPreference='Stop';"
        "Import-Module '" + _ps_single_quoted(DRIVER_MODULE) + "' -Force;"
        "$module=Import-Module '"
        + _ps_single_quoted(SCENARIO_MODULE)
        + "' -Force -PassThru;"
        "$inputs='" + inputs_json + "'|ConvertFrom-Json;"
        "& $module {param($value,$expected) "
        "Assert-W4ReleaseLockBinding -Inputs $value -ExpectedSha256 $expected} "
        "$inputs ('a'*64)"
    )

    result = _run_powershell(["-Command", command], cwd=REPOSITORY_ROOT)

    assert result.returncode != 0
    assert "environment lock hash is not bound" in result.stderr


@pytest.mark.parametrize("json_value", ["null", '"false"', "0"])
def test_json_boolean_contract_rejects_null_string_and_number(json_value: str) -> None:
    command = (
        "$ErrorActionPreference='Stop';"
        "Import-Module '" + _ps_single_quoted(DRIVER_MODULE) + "' -Force;"
        "$module=Import-Module '"
        + _ps_single_quoted(SCENARIO_MODULE)
        + "' -Force -PassThru;"
        "$value='" + json_value + "'|ConvertFrom-Json;"
        "& $module {param($item) "
        "Assert-W4ExactBoolean -Value $item -Expected $false -Label eligibility} "
        "$value"
    )

    result = _run_powershell(["-Command", command], cwd=REPOSITORY_ROOT)

    assert result.returncode != 0
    assert "must be the JSON boolean false" in result.stderr


def test_exact_json_field_contract_is_case_sensitive_in_powershell_5() -> None:
    command = (
        "$ErrorActionPreference='Stop';"
        "Import-Module '" + _ps_single_quoted(DRIVER_MODULE) + "' -Force;"
        "$module=Import-Module '"
        + _ps_single_quoted(SCENARIO_MODULE)
        + "' -Force -PassThru;"
        '$value=\'{"Schema_Version":"x"}\'|ConvertFrom-Json;'
        "& $module {param($item) "
        "Assert-W4ExactObjectFields -Object $item -Fields @('schema_version') "
        "-Label schema} $value"
    )

    result = _run_powershell(["-Command", command], cwd=REPOSITORY_ROOT)

    assert result.returncode != 0
    assert "missing required field: schema_version" in result.stderr
    assert "-cne" in _read(CONTROLLER)
    assert "-cne" in _read(RUNNER)


@pytest.mark.parametrize(
    ("actual_expression", "expected_error"),
    [
        ("@('Tools/List')", "mismatch"),
        ("@('tools/list','TOOLS/LIST')", "case-colliding"),
    ],
)
def test_mcp_surface_set_rejects_case_drift_and_case_collisions(
    actual_expression: str, expected_error: str
) -> None:
    command = (
        "$ErrorActionPreference='Stop';"
        "Import-Module '" + _ps_single_quoted(DRIVER_MODULE) + "' -Force;"
        "$module=Import-Module '"
        + _ps_single_quoted(SCENARIO_MODULE)
        + "' -Force -PassThru;"
        f"$actual={actual_expression};"
        "& $module {param($value) Assert-W4SetEqual -Actual $value "
        "-Expected @('tools/list') -Label 'MCP surface'} $actual"
    )

    result = _run_powershell(["-Command", command], cwd=REPOSITORY_ROOT)

    assert result.returncode != 0
    assert expected_error in result.stderr
