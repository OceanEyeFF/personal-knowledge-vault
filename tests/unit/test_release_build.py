"""W3 packaging-contract tests for deterministic release construction."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import shutil
import subprocess
import time
import zipfile
from pathlib import Path

import pytest

from scripts import build_release


PROJECT_ROOT = Path(__file__).parents[2]
POLICY = json.loads(
    (PROJECT_ROOT / "packaging" / "payload-policy.v1.json").read_text(encoding="utf-8")
)
pytestmark = pytest.mark.packaging_contract


def _write_payload(root: Path, *, reverse: bool = False) -> None:
    files = {
        "app/pkv.exe": b"cli",
        "app/pkv-mcp.exe": b"mcp",
        "app/_internal/config/config.yaml": (
            b'ai:\n  llm:\n    api_key: ""\nprocessors:\n  zhihu:\n    cookie: ""\n'
        ),
        "Install.ps1": b"# install\n",
        "Uninstall.ps1": b"# uninstall\n",
        "LICENSE": b"license\n",
        "THIRD-PARTY-NOTICES.txt": b"notices\n",
        "USER-GUIDE.md": b"guide\n",
        "licenses/index.json": b"{}\n",
        "licenses/demo-pkg/LICENSE.txt": b"demo license\n",
        "build-info.json": b"{}\n",
        "dependency-manifest.json": b"{}\n",
        "release-inventory.json": b"{}\n",
        "sbom.cdx.json": b"{}\n",
    }
    items = list(files.items())
    if reverse:
        items.reverse()
    for relative, content in items:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)


def _write_bound_inventory_metadata(root: Path, *, fingerprint: str) -> None:
    payload_rows: list[dict[str, object]] = []
    payload_tree = hashlib.sha256()
    for path in sorted((root / "app").rglob("*"), key=lambda item: item.as_posix()):
        if not path.is_file():
            continue
        relative = path.relative_to(root / "app").as_posix()
        digest = build_release.sha256_file(path)
        size = path.stat().st_size
        payload_rows.append(
            {
                "artifact_path": f"app/{relative}",
                "path": relative,
                "sha256": digest,
                "size": size,
            }
        )
        payload_tree.update(relative.encode("utf-8"))
        payload_tree.update(b"\0")
        payload_tree.update(str(size).encode("ascii"))
        payload_tree.update(b"\0")
        payload_tree.update(digest.encode("ascii"))
        payload_tree.update(b"\n")
    payload_tree_sha256 = payload_tree.hexdigest()
    blocker_authority = [
        {
            "condition": "synthetic contract hold",
            "id": "test-hold",
            "resolution": "resolve the synthetic hold",
        }
    ]
    blocker_authority_sha256 = build_release.sha256_bytes(
        build_release.canonical_json_bytes(blocker_authority)
    )
    hardlink_evidence = {
        "schema_version": "pkv.conda-hardlink-threat-evidence.v1",
        "anchors": [],
        "observed_hardlink_anchor_count": 0,
        "release_eligible_environment_requirement": "copy-only-no-hardlinks",
        "threat_model": "accepted_for_test_candidate",
        "validation_scope": list(
            build_release.BUILD_ENVIRONMENT_CONTRACT[
                "live_environment_byte_revalidation"
            ]
        ),
    }
    authority = {
        "artifact_kind": "test_candidate",
        "artifact_status": "test-candidate-on-compliance-hold",
        "build_fingerprint": fingerprint,
        "release_blocker_authority": blocker_authority,
        "release_blocker_authority_sha256": blocker_authority_sha256,
        "release_blockers": ["test-hold"],
        "release_eligible": False,
    }
    closure_sha256 = "8" * 64
    artifact_closure_sha256 = build_release.sha256_bytes(
        build_release.canonical_json_bytes(
            {
                "authority": authority,
                "inventory_closure_sha256": closure_sha256,
            }
        )
    )
    component = {
        "classification_ids": [],
        "contains_native_payload": False,
        "embedded_paths": ["app/pkv.exe!/PYZ.pyz#/demo_pkg"],
        "id": "python-distribution:demo-pkg",
        "identity_status": "complete",
        "license": "MIT",
        "name": "demo-pkg",
        "payload_paths": [],
        "purl": "pkg:pypi/demo-pkg@1.0.0",
        "source_paths": ["python-prefix/Lib/site-packages/demo_pkg.py"],
        "type": "python-distribution",
        "version": "1.0.0",
    }
    component_sha256 = build_release.sha256_bytes(
        build_release.canonical_json_bytes(component)
    )
    inventory = {
        "schema_version": "pkv.release-inventory.v1",
        "authority": authority,
        "bindings": {
            "artifact_closure_sha256": artifact_closure_sha256,
            "closure_sha256": closure_sha256,
            "payload_tree_sha256": payload_tree_sha256,
        },
        "components": [component],
        "payload": {
            "file_count": len(payload_rows),
            "files": payload_rows,
            "path_base": "app",
            "tree_sha256": payload_tree_sha256,
        },
    }
    build_release.write_canonical_json(root / "release-inventory.json", inventory)
    inventory_sha256 = build_release.sha256_file(root / "release-inventory.json")
    build_release.write_canonical_json(
        root / "build-info.json",
        {
            "schema_version": build_release.SCHEMA_BUILD_INFO,
            "artifact_kind": authority["artifact_kind"],
            "artifact_status": authority["artifact_status"],
            "build_fingerprint": fingerprint,
            "conda_hardlink_threat_evidence": hardlink_evidence,
            "release_blocker_authority": blocker_authority,
            "release_blocker_authority_sha256": blocker_authority_sha256,
            "release_blockers": authority["release_blockers"],
            "release_eligible": authority["release_eligible"],
            "release_inventory_artifact_closure_sha256": artifact_closure_sha256,
            "release_inventory_closure_sha256": closure_sha256,
            "release_inventory_path": "release-inventory.json",
            "release_inventory_sha256": inventory_sha256,
            "toolchain": {"conda_hardlink_threat_evidence": hardlink_evidence},
        },
    )
    build_release.write_canonical_json(
        root / "licenses" / "index.json",
        {
            "actual_runtime_inventory": {
                "components": [
                    {
                        "classifications": [],
                        "component_id": component["id"],
                        "component_sha256": component_sha256,
                        "embedded_paths": component["embedded_paths"],
                        "license": {"expression": "MIT"},
                        "license_files": [
                            {
                                "path": "licenses/demo-pkg/LICENSE.txt",
                                "sha256": build_release.sha256_file(
                                    root / "licenses" / "demo-pkg" / "LICENSE.txt"
                                ),
                                "source_kind": "distribution_metadata",
                            }
                        ],
                        "license_material_status": "bound",
                        "name": component["name"],
                        "payload_paths": component["payload_paths"],
                        "purl": component["purl"],
                        "source_paths": component["source_paths"],
                        "version": component["version"],
                    }
                ],
                "release_inventory_closure_sha256": closure_sha256,
                "release_inventory_path": "release-inventory.json",
            },
            "entries": [],
            "schema_version": "pkv.license-index.v2",
        },
    )
    build_release.write_canonical_json(
        root / "sbom.cdx.json",
        {
            "components": [
                {
                    "bom-ref": (
                        "urn:pkv:release-inventory-component:" + component_sha256
                    ),
                    "licenses": [{"expression": "MIT"}],
                    "name": component["name"],
                    "properties": [
                        {
                            "name": "pkv:inventory-component-id",
                            "value": component["id"],
                        },
                        {
                            "name": "pkv:inventory-component-sha256",
                            "value": component_sha256,
                        },
                        {
                            "name": "pkv:inventory-identity-status",
                            "value": "complete",
                        },
                        {
                            "name": "pkv:contains-native-payload",
                            "value": "false",
                        },
                        {
                            "name": "pkv:license-material-status",
                            "value": "bound",
                        },
                        {
                            "name": "pkv:embedded-path",
                            "value": component["embedded_paths"][0],
                        },
                    ],
                    "purl": component["purl"],
                    "type": "library",
                    "version": component["version"],
                }
            ],
            "metadata": {
                "properties": [
                    {
                        "name": "pkv:release-blocker-authority-sha256",
                        "value": blocker_authority_sha256,
                    },
                    {
                        "name": "pkv:release-inventory-closure-sha256",
                        "value": closure_sha256,
                    },
                    {
                        "name": "pkv:release-inventory-path",
                        "value": "release-inventory.json",
                    },
                    {
                        "name": "pkv:release-inventory-sha256",
                        "value": inventory_sha256,
                    },
                ]
            },
        },
    )


def test_canonical_json_is_stable_utf8_with_lf() -> None:
    left = build_release.canonical_json_bytes({"z": 1, "中": [2, 1]})
    right = build_release.canonical_json_bytes({"中": [2, 1], "z": 1})

    assert left == right
    assert left.endswith(b"\n")
    assert b"\\u4e2d" not in left


def test_inventory_sbom_uses_conda_declared_license_as_name_and_deduplicates_tags() -> (
    None
):
    inventory = {
        "payload": {"files": []},
        "embedded_archives": [
            {
                "entries": [
                    {
                        "component_ids": ["conda-package:openssl", "native:openssl"],
                        "kind": "BINARY",
                        "name": "libssl-3-x64.dll",
                    },
                    {
                        "component_ids": ["python-distribution:cffi"],
                        "kind": "EXTENSION",
                        "name": "_cffi_backend.cp311-win_amd64.pyd",
                    },
                    {
                        "component_ids": ["python-distribution:cryptography"],
                        "kind": "EXTENSION",
                        "name": "cryptography/hazmat/bindings/_rust.pyd",
                    },
                    {
                        "component_ids": ["python-distribution:hnswlib"],
                        "kind": "EXTENSION",
                        "name": "hnswlib.cp311-win_amd64.pyd",
                    },
                    {
                        "component_ids": ["python-distribution:pyyaml"],
                        "kind": "EXTENSION",
                        "name": "yaml/_yaml.cp311-win_amd64.pyd",
                    },
                ]
            }
        ],
        "components": [
            {
                "id": "conda-package:openssl",
                "identity_status": "complete",
                "contains_native_payload": True,
                "name": "openssl",
                "type": "library",
                "version": "3.6.1",
                "build": "h725018a_1",
                "subdir": "win-64",
                "declared_license": "Apache 2.0 metadata spelling",
                "classification_ids": ["native:openssl"],
                "embedded_paths": ["harness.exe!/libssl-3-x64.dll"],
                "payload_paths": [],
                "source_paths": ["python-prefix/Library/bin/libssl-3-x64.dll"],
            },
            {
                "id": "native:openssl",
                "identity_status": "classification-only",
                "contains_native_payload": True,
                "classification_ids": [],
                "name": "OpenSSL native runtime",
                "type": "library",
                "embedded_paths": ["harness.exe!/libssl-3-x64.dll"],
                "payload_paths": [],
                "source_paths": ["python-prefix/Library/bin/libssl-3-x64.dll"],
            },
            {
                "id": "python-distribution:cryptography",
                "identity_status": "complete",
                "contains_native_payload": True,
                "classification_ids": [],
                "name": "cryptography",
                "type": "python-distribution",
                "version": "49.0.0",
                "license": "Apache-2.0 OR BSD-3-Clause",
                "purl": "pkg:pypi/cryptography@49.0.0",
                "embedded_paths": ["app/pkv.exe!/PYZ.pyz"],
                "payload_paths": [],
                "source_paths": [],
            },
            {
                "id": "python-distribution:cffi",
                "identity_status": "complete",
                "contains_native_payload": True,
                "classification_ids": [],
                "name": "cffi",
                "type": "python-distribution",
                "version": "2.1.0",
                "license": "MIT-0",
                "purl": "pkg:pypi/cffi@2.1.0",
                "embedded_paths": ["app/pkv.exe!/_cffi_backend.cp311-win_amd64.pyd"],
                "payload_paths": [],
                "source_paths": [],
            },
            {
                "id": "python-distribution:hnswlib",
                "identity_status": "complete",
                "contains_native_payload": True,
                "classification_ids": [],
                "name": "hnswlib",
                "type": "python-distribution",
                "version": "0.8.0",
                "license": "Apache-2.0",
                "purl": "pkg:conda/hnswlib@0.8.0",
                "embedded_paths": ["app/pkv.exe!/hnswlib.cp311-win_amd64.pyd"],
                "payload_paths": [],
                "source_paths": [],
            },
            {
                "id": "python-distribution:pyyaml",
                "identity_status": "complete",
                "contains_native_payload": True,
                "classification_ids": [],
                "name": "PyYAML",
                "type": "python-distribution",
                "version": "6.0.1",
                "license": "MIT",
                "purl": "pkg:pypi/pyyaml@6.0.1",
                "embedded_paths": ["app/pkv.exe!/yaml/_yaml.cp311-win_amd64.pyd"],
                "payload_paths": [],
                "source_paths": [],
            },
        ],
    }

    components = build_release._inventory_sbom_components(inventory, [])

    # The fixture has one Conda component plus four complete Python
    # distributions.  ``native:openssl`` is classification-only and therefore
    # deliberately omitted from the SBOM result.
    assert len(components) == 5
    assert components[0]["licenses"] == [
        {"license": {"name": "Apache 2.0 metadata spelling"}}
    ]
    assert {
        item["value"]
        for item in components[0]["properties"]
        if item["name"] == "pkv:payload-classification"
    } == {"native:openssl"}
    assert {
        item["value"]
        for item in components[0]["properties"]
        if item["name"] == "pkv:embedded-path"
    } == {"harness.exe!/libssl-3-x64.dll"}
    assert {
        item["value"]
        for item in components[0]["properties"]
        if item["name"] == "pkv:license-material-status"
    } == {"metadata-only-compliance-hold"}
    cryptography = next(item for item in components if item["name"] == "cryptography")
    assert not any(
        item["name"] == "pkv:payload-classification"
        for item in cryptography["properties"]
    )
    assert {
        item["value"]
        for item in cryptography["properties"]
        if item["name"] == "pkv:license-material-status"
    } == {"top-level-only-compliance-hold"}
    for held_name in ("cffi", "hnswlib", "PyYAML"):
        held_component = next(item for item in components if item["name"] == held_name)
        assert {
            item["value"]
            for item in held_component["properties"]
            if item["name"] == "pkv:license-material-status"
        } == {"top-level-only-compliance-hold"}

    cffi_inventory = next(
        item
        for item in inventory["components"]
        if item["id"] == "python-distribution:cffi"
    )
    cffi_inventory["contains_native_payload"] = False
    with pytest.raises(build_release.ReleaseBuildError, match="native-payload binding"):
        build_release._inventory_sbom_components(inventory, [])


def test_make_sbom_uses_final_license_index_material_status() -> None:
    component = {
        "classification_ids": [],
        "contains_native_payload": False,
        "embedded_paths": ["app/pkv.exe!/PYZ.pyz#/demo_pkg"],
        "id": "python-distribution:demo-pkg",
        "identity_status": "complete",
        "license": "MIT",
        "name": "demo-pkg",
        "payload_paths": ["app/demo.txt"],
        "purl": "pkg:pypi/demo-pkg@1.0.0",
        "source_paths": ["python-prefix/Lib/site-packages/demo_pkg.py"],
        "type": "python-distribution",
        "version": "1.0.0",
    }
    component_sha256 = build_release.sha256_bytes(
        build_release.canonical_json_bytes(component)
    )
    closure_sha256 = "8" * 64
    inventory = {
        "bindings": {"closure_sha256": closure_sha256},
        "components": [component],
        "embedded_archives": [
            {
                "entries": [
                    {
                        "component_ids": [component["id"]],
                        "kind": "PYMODULE",
                        "name": "demo_pkg",
                    }
                ]
            }
        ],
        "payload": {
            "files": [
                {
                    "artifact_path": "app/demo.txt",
                    "component_ids": [component["id"]],
                    "kind": "DATA",
                    "path": "demo.txt",
                }
            ]
        },
    }
    license_index = {
        "actual_runtime_inventory": {
            "components": [
                {
                    "classifications": [],
                    "component_id": component["id"],
                    "component_sha256": component_sha256,
                    "embedded_paths": component["embedded_paths"],
                    "license": {"expression": "MIT"},
                    "license_files": [
                        {
                            "path": "licenses/demo-pkg/LICENSE.txt",
                            "sha256": "1" * 64,
                            "source_kind": "distribution_metadata",
                        }
                    ],
                    "license_material_status": "bound",
                    "name": component["name"],
                    "payload_paths": component["payload_paths"],
                    "purl": component["purl"],
                    "source_paths": component["source_paths"],
                    "version": component["version"],
                }
            ],
            "release_inventory_closure_sha256": closure_sha256,
            "release_inventory_path": "release-inventory.json",
        },
        "entries": [],
        "schema_version": "pkv.license-index.v2",
    }

    sbom = build_release.make_sbom(
        [],
        version="0.8.1",
        source_date_epoch=1_777_777_777,
        compliance={
            "artifact_status": "test-candidate-on-compliance-hold",
            "compliance_manifest_sha256": "2" * 64,
            "release_blocker_authority_sha256": "3" * 64,
            "release_blockers": ["test-hold"],
            "release_eligible": False,
        },
        inventory=inventory,
        inventory_sha256="4" * 64,
        license_index=license_index,
    )

    status = [
        property_["value"]
        for property_ in sbom["components"][0]["properties"]
        if property_["name"] == "pkv:license-material-status"
    ]
    assert status == ["bound"]
    assert "requires-license-index-binding" not in json.dumps(sbom)


def test_pyinstaller_embedded_runtime_components_use_file_scoped_licenses() -> None:
    inventory = {
        "payload": {
            "files": [
                {
                    "component_ids": [
                        "build-runtime:pyinstaller-bootloader",
                        "build-runtime:pyinstaller-hooks",
                        "build-runtime:pyinstaller-hooks-contrib",
                    ],
                    "kind": "PYINSTALLER_BOOTLOADER_EXECUTABLE",
                    "path": "pkv.exe",
                }
            ]
        },
        "embedded_archives": [
            {
                "entries": [
                    {
                        "component_ids": ["build-runtime:pyinstaller-bootloader"],
                        "kind": "OPTION",
                        "name": "pyi-runtime-tmpdir NULL",
                    },
                    {
                        "component_ids": ["build-runtime:pyinstaller-hooks"],
                        "kind": "PYSOURCE",
                        "name": "pyi_rth_pkgutil",
                    },
                    {
                        "component_ids": ["build-runtime:pyinstaller-hooks-contrib"],
                        "kind": "PYSOURCE",
                        "name": "pyi_rth_cryptography_openssl",
                    },
                ]
            }
        ],
        "components": [
            {
                "id": identifier,
                "identity_status": "complete",
                "contains_native_payload": identifier.endswith("bootloader"),
                "classification_ids": [],
                "name": name,
                "type": "runtime",
                "version": version,
                "embedded_paths": [f"app/pkv.exe!/{identifier.split(':')[-1]}"],
                "payload_paths": [],
                "source_paths": [],
            }
            for identifier, name, version in (
                (
                    "build-runtime:pyinstaller-bootloader",
                    "PyInstaller bootloader",
                    "6.21.0",
                ),
                (
                    "build-runtime:pyinstaller-hooks",
                    "PyInstaller runtime hooks",
                    "6.21.0",
                ),
                (
                    "build-runtime:pyinstaller-hooks-contrib",
                    "PyInstaller hooks-contrib runtime hooks",
                    "2026.6",
                ),
            )
        ],
    }
    locked = [
        {
            "name": "pyinstaller",
            "version": "6.21.0",
            "license": "GPL-2.0-or-later WITH Bootloader-exception",
            "purl": "pkg:pypi/pyinstaller@6.21.0",
        },
        {
            "name": "pyinstaller-hooks-contrib",
            "version": "2026.6",
            "license": "Apache-2.0 OR GPL-2.0-or-later",
            "purl": "pkg:pypi/pyinstaller-hooks-contrib@2026.6",
        },
    ]

    components = build_release._inventory_sbom_components(inventory, locked)
    by_id = {
        next(
            item["value"]
            for item in component["properties"]
            if item["name"] == "pkv:inventory-component-id"
        ): component
        for component in components
    }

    assert by_id["build-runtime:pyinstaller-bootloader"]["licenses"] == [
        {"expression": "GPL-2.0-or-later WITH Bootloader-exception"}
    ]
    assert by_id["build-runtime:pyinstaller-hooks"]["licenses"] == [
        {"expression": "Apache-2.0"}
    ]
    assert by_id["build-runtime:pyinstaller-hooks-contrib"]["licenses"] == [
        {"expression": "Apache-2.0"}
    ]
    assert len({component["purl"] for component in components}) == 3


def test_harness_runtime_copy_remains_exactly_bound_to_inventory(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "pkv-loopback-provider.exe"
    runtime.write_bytes(b"frozen-runtime")
    record = {
        "path": runtime.name,
        "sha256": build_release.sha256_file(runtime),
        "size": runtime.stat().st_size,
    }
    inventory = {"payload": {"files": [record]}}

    bound = build_release._harness_runtime_inventory_record(inventory, runtime.name)
    build_release._assert_file_matches_inventory_record(
        runtime, bound, label="test harness runtime"
    )
    archive = tmp_path / "harness.zip"
    member = f"harness/{runtime.name}"
    with zipfile.ZipFile(archive, "w") as output:
        output.write(runtime, member)
    build_release._assert_zip_member_matches_inventory_record(
        archive, member, bound, label="test harness ZIP runtime"
    )

    runtime.write_bytes(b"mutated-runtime")
    with pytest.raises(build_release.ReleaseBuildError, match="release inventory"):
        build_release._assert_file_matches_inventory_record(
            runtime, bound, label="test harness runtime"
        )
    with zipfile.ZipFile(archive, "w") as output:
        output.write(runtime, member)
    with pytest.raises(build_release.ReleaseBuildError, match="release inventory"):
        build_release._assert_zip_member_matches_inventory_record(
            archive, member, bound, label="test harness ZIP runtime"
        )
    with pytest.raises(build_release.ReleaseBuildError, match="exactly one"):
        build_release._harness_runtime_inventory_record(
            {"payload": {"files": [record, dict(record)]}}, runtime.name
        )


def test_source_date_epoch_is_utc_and_dos_normalized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    epoch = 1_777_777_777
    monkeypatch.setenv("TZ", "Pacific/Honolulu")
    if hasattr(time, "tzset"):
        time.tzset()
    first = build_release.zip_timestamp(epoch)
    monkeypatch.setenv("TZ", "Asia/Shanghai")
    if hasattr(time, "tzset"):
        time.tzset()
    second = build_release.zip_timestamp(epoch)

    assert first == second
    assert first[-1] % 2 == 0


def test_payload_manifest_is_sorted_and_self_excluding(tmp_path: Path) -> None:
    root = tmp_path / "payload"
    _write_payload(root, reverse=True)
    manifest = build_release.generate_payload_manifest(root, "a" * 64)

    paths = [entry["path"] for entry in manifest["entries"]]
    assert paths == sorted(paths, key=lambda value: value.encode("utf-8"))
    assert "payload-manifest.json" not in paths
    assert manifest["self_excluded_paths"] == ["payload-manifest.json"]
    assert len(manifest["tree_sha256"]) == 64


def test_payload_policy_accepts_release_shell_and_rejects_sensitive_material(
    tmp_path: Path,
) -> None:
    root = tmp_path / "payload"
    _write_payload(root)
    manifest = build_release.generate_payload_manifest(root, "b" * 64)
    build_release.write_canonical_json(root / "payload-manifest.json", manifest)

    accepted = build_release.scan_payload(root, POLICY)
    assert "app/pkv.exe" in accepted

    local_config = root / "app" / "_internal" / "config" / "local.yaml"
    local_config.write_text("api_key: secret\n", encoding="utf-8")
    with pytest.raises(build_release.ReleaseBuildError, match="forbidden filename"):
        build_release.scan_payload(root, POLICY)


def test_payload_policy_rejects_nonempty_bundled_secret(tmp_path: Path) -> None:
    root = tmp_path / "payload"
    _write_payload(root)
    (root / "app" / "_internal" / "config" / "config.yaml").write_text(
        "ai:\n  llm:\n    api_key: not-a-release-secret\n", encoding="utf-8"
    )
    manifest = build_release.generate_payload_manifest(root, "c" * 64)
    build_release.write_canonical_json(root / "payload-manifest.json", manifest)

    with pytest.raises(build_release.ReleaseBuildError, match="non-empty sensitive"):
        build_release.scan_payload(root, POLICY)


@pytest.mark.parametrize(
    ("relative", "content"),
    [
        ("app/_internal/vendor/settings.json", '{"api_key":"secret"}\n'),
        ("app/_internal/vendor/settings.toml", 'client_secret = "secret"\n'),
    ],
)
def test_payload_policy_rejects_secrets_outside_config_directory(
    tmp_path: Path, relative: str, content: str
) -> None:
    root = tmp_path / "payload"
    _write_payload(root)
    candidate = root / relative
    candidate.parent.mkdir(parents=True, exist_ok=True)
    candidate.write_text(content, encoding="utf-8")

    with pytest.raises(build_release.ReleaseBuildError, match="non-empty sensitive"):
        build_release.scan_payload(
            root, POLICY, allow_missing={"payload-manifest.json"}
        )


def test_payload_policy_allows_only_exact_frozen_public_ca_bundle(
    tmp_path: Path,
) -> None:
    root = tmp_path / "payload"
    _write_payload(root)
    certificate = b"-----BEGIN CERTIFICATE-----\npublic-ca\n-----END CERTIFICATE-----\n"
    certificate_path = root / "app" / "_internal" / "certifi" / "cacert.pem"
    certificate_path.parent.mkdir(parents=True)
    certificate_path.write_bytes(certificate)
    policy = json.loads(json.dumps(POLICY))
    policy["allowed_public_certificate_files"] = {
        "app/_internal/certifi/cacert.pem": build_release.sha256_bytes(certificate)
    }

    build_release.scan_payload(root, policy, allow_missing={"payload-manifest.json"})

    unapproved = root / "app" / "_internal" / "other.pem"
    unapproved.write_bytes(certificate)
    with pytest.raises(build_release.ReleaseBuildError, match="unapproved PEM"):
        build_release.scan_payload(
            root, policy, allow_missing={"payload-manifest.json"}
        )


def test_payload_policy_rejects_private_key_even_at_public_ca_path(
    tmp_path: Path,
) -> None:
    root = tmp_path / "payload"
    _write_payload(root)
    private_key = b"-----BEGIN PRIVATE KEY-----\nnot-a-key\n-----END PRIVATE KEY-----\n"
    certificate_path = root / "app" / "_internal" / "certifi" / "cacert.pem"
    certificate_path.parent.mkdir(parents=True)
    certificate_path.write_bytes(private_key)
    policy = json.loads(json.dumps(POLICY))
    policy["allowed_public_certificate_files"] = {
        "app/_internal/certifi/cacert.pem": build_release.sha256_bytes(private_key)
    }

    with pytest.raises(build_release.ReleaseBuildError, match="private-key material"):
        build_release.scan_payload(
            root, policy, allow_missing={"payload-manifest.json"}
        )


def test_payload_policy_rejects_external_harness_marker(tmp_path: Path) -> None:
    root = tmp_path / "payload"
    _write_payload(root)
    (root / "USER-GUIDE.md").write_text(
        "must not bundle pkv.loopback-provider\n", encoding="utf-8"
    )
    manifest = build_release.generate_payload_manifest(root, "d" * 64)
    build_release.write_canonical_json(root / "payload-manifest.json", manifest)

    with pytest.raises(build_release.ReleaseBuildError, match="harness/test marker"):
        build_release.scan_payload(root, POLICY)


def test_deterministic_zip_ignores_creation_order_and_filesystem_mtime(
    tmp_path: Path,
) -> None:
    first_root = tmp_path / "one"
    second_root = tmp_path / "two"
    _write_payload(first_root)
    _write_payload(second_root, reverse=True)
    for index, path in enumerate(second_root.rglob("*")):
        if path.is_file():
            os.utime(path, (1_600_000_000 + index, 1_600_000_000 + index))
    first_manifest = build_release.generate_payload_manifest(first_root, "e" * 64)
    second_manifest = build_release.generate_payload_manifest(second_root, "e" * 64)
    build_release.write_canonical_json(
        first_root / "payload-manifest.json", first_manifest
    )
    build_release.write_canonical_json(
        second_root / "payload-manifest.json", second_manifest
    )
    first_zip = tmp_path / "one.zip"
    second_zip = tmp_path / "two.zip"
    kwargs = {
        "archive_root": "PersonalKnowledgeVault-0.8.1-windows-x86_64",
        "source_date_epoch": 1_777_777_777,
    }

    build_release.create_deterministic_zip(first_root, first_zip, **kwargs)
    build_release.create_deterministic_zip(second_root, second_zip, **kwargs)

    assert first_zip.read_bytes() == second_zip.read_bytes()
    build_release.validate_deterministic_zip(first_zip, **kwargs)
    with zipfile.ZipFile(first_zip) as archive:
        assert all(not item.extra and not item.comment for item in archive.infolist())


def test_payload_hardlink_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "payload"
    _write_payload(root)
    source = root / "app" / "pkv.exe"
    linked = root / "app" / "linked.exe"
    try:
        os.link(source, linked)
    except OSError as exc:
        pytest.skip(f"hardlink unavailable: {exc}")

    with pytest.raises(build_release.ReleaseBuildError, match="hardlinks"):
        build_release.generate_payload_manifest(root, "f" * 64)


def test_conda_hardlink_threat_is_candidate_only_and_byte_revalidation_detects_drift(
    tmp_path: Path,
) -> None:
    prefix = tmp_path / "release-prefix"
    prefix.mkdir()
    source = prefix / "python.exe"
    linked = prefix / "python311.dll"
    source.write_bytes(b"locked-runtime")
    try:
        os.link(source, linked)
    except OSError as exc:
        pytest.skip(f"hardlink unavailable: {exc}")

    baseline = build_release.sha256_file(source)
    linked.write_bytes(b"drifted-runtime")
    assert build_release.sha256_file(source) != baseline
    linked.write_bytes(b"locked-runtime")
    assert build_release.sha256_file(source) == baseline
    assert source.stat().st_nlink > 1
    assert (
        build_release.BUILD_ENVIRONMENT_CONTRACT["conda_hardlink_threat_model"]
        == "accepted_for_test_candidate"
    )
    assert build_release.BUILD_ENVIRONMENT_CONTRACT[
        "live_environment_byte_revalidation"
    ] == [
        "before-build-a",
        "after-build-a",
        "before-build-b",
        "after-build-b",
        "before-publication",
    ]
    with pytest.raises(
        build_release.ReleaseBuildError, match="release-eligible environment.*hardlink"
    ):
        build_release._assert_copy_only_release_environment(prefix)


def test_build_fingerprint_changes_with_any_contract_input() -> None:
    base = build_release._build_fingerprint(
        version="0.8.1",
        revision="1" * 40,
        source_date_epoch=1_777_777_777,
        inputs={"spec": "a" * 64},
        toolchain={"pyinstaller": "6.21.0"},
    )
    changed = build_release._build_fingerprint(
        version="0.8.1",
        revision="1" * 40,
        source_date_epoch=1_777_777_777,
        inputs={"spec": "b" * 64},
        toolchain={"pyinstaller": "6.21.0"},
    )

    assert len(base) == 64
    assert base != changed


def test_release_contract_rejects_traversal_before_build() -> None:
    contract = json.loads(
        (PROJECT_ROOT / "packaging" / "release-contract.v1.json").read_text(
            encoding="utf-8"
        )
    )
    contract["pyinstaller_spec"] = "../outside.spec"

    with pytest.raises(build_release.ReleaseBuildError, match="spec"):
        build_release.validate_release_contract(contract)


def test_payload_policy_contract_rejects_security_weakening() -> None:
    weakened = json.loads(json.dumps(POLICY))
    weakened["forbidden_suffixes"].remove(".py")

    with pytest.raises(build_release.ReleaseBuildError, match="frozen security"):
        build_release.validate_payload_policy(weakened)


def test_build_once_rejects_unbound_lock_and_compliance_inputs(
    tmp_path: Path,
) -> None:
    contract = json.loads(
        (PROJECT_ROOT / "packaging" / "release-contract.v1.json").read_text(
            encoding="utf-8"
        )
    )
    compliance = {
        "artifact_status": "test-candidate-on-compliance-hold",
        "compliance_manifest_sha256": "3" * 64,
        "release_blockers": ["hold"],
        "release_eligible": False,
    }

    with pytest.raises(build_release.ReleaseBuildError, match="authority hashes"):
        build_release._build_once(
            project_root=tmp_path,
            canonical_root=tmp_path / "build",
            contract=contract,
            policy=POLICY,
            revision="1" * 40,
            source_date_epoch=1_777_777_777,
            components=[],
            compliance=compliance,
            lock_sha256="2" * 64,
            input_hashes={
                "packaging/locks/release-environment.v2.json": "1" * 64,
                "packaging/compliance-sources.v1.json": "3" * 64,
            },
        )


def test_compliance_authority_preserves_exact_candidate_hold(tmp_path: Path) -> None:
    packaging = tmp_path / "packaging"
    shutil.copytree(PROJECT_ROOT / "packaging" / "licenses", packaging / "licenses")
    shutil.copytree(
        PROJECT_ROOT / "packaging" / "compliance-sources",
        packaging / "compliance-sources",
    )
    shutil.copyfile(
        PROJECT_ROOT / "packaging" / "compliance-sources.v1.json",
        packaging / "compliance-sources.v1.json",
    )

    contract_path = packaging / "compliance-sources.v1.json"
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    expected_authority = sorted(
        contract["fail_closed_release_blockers"],
        key=lambda item: item["id"].encode("utf-8"),
    )
    state = build_release.validate_compliance_sources(tmp_path)
    assert state == {
        "artifact_status": "test-candidate-on-compliance-hold",
        "compliance_manifest_sha256": (
            "95bbb2eb9de112a36aaf1f01321c827df76b4f013fcbf9903765428ee7381370"
        ),
        "release_blocker_authority": expected_authority,
        "release_blocker_authority_sha256": build_release.sha256_bytes(
            build_release.canonical_json_bytes(expected_authority)
        ),
        "release_blockers": [
            "conda-native-license-materials-and-spdx",
            "html2text-gpl-compliance",
            "native-msvc-license-and-provenance",
        ],
        "release_eligible": False,
    }

    original_contract = json.loads(json.dumps(contract))
    html2text_blocker = next(
        item
        for item in contract["fail_closed_release_blockers"]
        if item["id"] == "html2text-gpl-compliance"
    )
    html2text_blocker["resolution_requirements"] = html2text_blocker[
        "resolution_requirements"
    ][:-1]
    build_release.write_canonical_json(contract_path, contract)
    with pytest.raises(
        build_release.ReleaseBuildError, match="GPL compliance requirements"
    ):
        build_release.validate_compliance_sources(tmp_path)

    contract = original_contract
    native_blocker = next(
        item
        for item in contract["fail_closed_release_blockers"]
        if item["id"] == "conda-native-license-materials-and-spdx"
    )
    native_blocker["affected_component_selectors"] = native_blocker[
        "affected_component_selectors"
    ][:-1]
    build_release.write_canonical_json(contract_path, contract)
    with pytest.raises(
        build_release.ReleaseBuildError, match="native license hold component selectors"
    ):
        build_release.validate_compliance_sources(tmp_path)

    contract = original_contract
    contract["fail_closed_release_blockers"] = []
    build_release.write_canonical_json(contract_path, contract)
    with pytest.raises(build_release.ReleaseBuildError, match="frozen hold"):
        build_release.validate_compliance_sources(tmp_path)


def test_final_zip_is_exactly_bound_to_manifest_and_build_info(
    tmp_path: Path,
) -> None:
    fingerprint = "9" * 64
    archive_root = "PersonalKnowledgeVault-0.8.1-windows-x86_64"
    payload = tmp_path / "payload"
    _write_payload(payload)
    _write_bound_inventory_metadata(payload, fingerprint=fingerprint)
    manifest = build_release.generate_payload_manifest(payload, fingerprint)
    build_release.write_canonical_json(payload / "payload-manifest.json", manifest)
    archive = tmp_path / "release.zip"
    build_release.create_deterministic_zip(
        payload,
        archive,
        archive_root=archive_root,
        source_date_epoch=1_777_777_777,
    )

    build_release.validate_artifact_payload(
        archive,
        archive_root=archive_root,
        expected_build_fingerprint=fingerprint,
    )

    (payload / "USER-GUIDE.md").write_text("tampered\n", encoding="utf-8")
    tampered = tmp_path / "tampered.zip"
    build_release.create_deterministic_zip(
        payload,
        tampered,
        archive_root=archive_root,
        source_date_epoch=1_777_777_777,
    )
    with pytest.raises(
        build_release.ReleaseBuildError, match="metadata mismatch|hash mismatch"
    ):
        build_release.validate_artifact_payload(
            tampered,
            archive_root=archive_root,
            expected_build_fingerprint=fingerprint,
        )


def test_final_zip_rejects_inventory_that_disagrees_with_app_bytes(
    tmp_path: Path,
) -> None:
    fingerprint = "7" * 64
    archive_root = "PersonalKnowledgeVault-0.8.1-windows-x86_64"
    payload = tmp_path / "payload"
    _write_payload(payload)
    _write_bound_inventory_metadata(payload, fingerprint=fingerprint)

    inventory_path = payload / "release-inventory.json"
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    inventory["payload"]["files"][0]["sha256"] = "0" * 64
    build_release.write_canonical_json(inventory_path, inventory)
    inventory_sha256 = build_release.sha256_file(inventory_path)

    build_info_path = payload / "build-info.json"
    build_info = json.loads(build_info_path.read_text(encoding="utf-8"))
    build_info["release_inventory_sha256"] = inventory_sha256
    build_release.write_canonical_json(build_info_path, build_info)
    sbom_path = payload / "sbom.cdx.json"
    sbom = json.loads(sbom_path.read_text(encoding="utf-8"))
    for property_ in sbom["metadata"]["properties"]:
        if property_["name"] == "pkv:release-inventory-sha256":
            property_["value"] = inventory_sha256
    build_release.write_canonical_json(sbom_path, sbom)

    manifest = build_release.generate_payload_manifest(payload, fingerprint)
    build_release.write_canonical_json(payload / "payload-manifest.json", manifest)
    archive = tmp_path / "inventory-tampered.zip"
    build_release.create_deterministic_zip(
        payload,
        archive,
        archive_root=archive_root,
        source_date_epoch=1_777_777_777,
    )
    with pytest.raises(
        build_release.ReleaseBuildError,
        match="inventory payload files differ",
    ):
        build_release.validate_artifact_payload(
            archive,
            archive_root=archive_root,
            expected_build_fingerprint=fingerprint,
        )


@pytest.mark.parametrize("mutated_document", ["sbom", "license-index", "both"])
def test_final_zip_rejects_license_status_mismatch_after_manifest_rebind(
    tmp_path: Path,
    mutated_document: str,
) -> None:
    fingerprint = "6" * 64
    archive_root = "PersonalKnowledgeVault-0.8.1-windows-x86_64"
    payload = tmp_path / "payload"
    _write_payload(payload)
    _write_bound_inventory_metadata(payload, fingerprint=fingerprint)

    if mutated_document in {"sbom", "both"}:
        document_path = payload / "sbom.cdx.json"
        document = json.loads(document_path.read_text(encoding="utf-8"))
        status_properties = [
            property_
            for property_ in document["components"][0]["properties"]
            if property_["name"] == "pkv:license-material-status"
        ]
        assert len(status_properties) == 1
        status_properties[0]["value"] = "metadata-only-compliance-hold"
        build_release.write_canonical_json(document_path, document)
    if mutated_document in {"license-index", "both"}:
        document_path = payload / "licenses" / "index.json"
        document = json.loads(document_path.read_text(encoding="utf-8"))
        document["actual_runtime_inventory"]["components"][0][
            "license_material_status"
        ] = "metadata-only-compliance-hold"
        build_release.write_canonical_json(document_path, document)

    manifest = build_release.generate_payload_manifest(payload, fingerprint)
    build_release.write_canonical_json(payload / "payload-manifest.json", manifest)
    archive = tmp_path / f"{mutated_document}-status-tampered.zip"
    build_release.create_deterministic_zip(
        payload,
        archive,
        archive_root=archive_root,
        source_date_epoch=1_777_777_777,
    )
    with pytest.raises(
        build_release.ReleaseBuildError,
        match="license index material status|SBOM/license-index material status",
    ):
        build_release.validate_artifact_payload(
            archive,
            archive_root=archive_root,
            expected_build_fingerprint=fingerprint,
        )


def test_dist_root_link_is_rejected_without_touching_target(tmp_path: Path) -> None:
    project = tmp_path / "project"
    outside = tmp_path / "outside"
    project.mkdir()
    outside.mkdir()
    sentinel = outside / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")
    try:
        (project / "dist").symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlink unavailable: {exc}")

    with pytest.raises(
        build_release.ReleaseBuildError, match="link/reparse point|normal directory"
    ):
        build_release._prepare_dist_root(project)
    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_release_publish_fails_closed_when_independent_outputs_differ(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    (project / "packaging" / "locks").mkdir(parents=True)
    contract = json.loads(
        (PROJECT_ROOT / "packaging" / "release-contract.v1.json").read_text(
            encoding="utf-8"
        )
    )
    build_release.write_canonical_json(
        project / "packaging" / "release-contract.v1.json",
        contract,
    )
    build_release.write_canonical_json(
        project / "packaging" / "payload-policy.v1.json",
        POLICY,
    )
    build_release.write_canonical_json(
        project / "packaging" / "locks" / "release-environment.v2.json",
        {"schema_version": "pkv.release-environment-lock.v2"},
    )
    monkeypatch.setattr(
        build_release, "git_release_identity", lambda _root: ("1" * 40, 1_777_777_777)
    )
    monkeypatch.setattr(build_release, "_validate_windows_release_host", lambda: None)
    monkeypatch.setattr(build_release, "validate_environment_lock", lambda _lock: [])
    monkeypatch.setattr(
        build_release,
        "validate_compliance_sources",
        lambda _root: {
            "artifact_status": "test-candidate-on-compliance-hold",
            "compliance_manifest_sha256": "2" * 64,
            "release_blockers": ["blocked"],
            "release_eligible": False,
        },
    )
    monkeypatch.setattr(build_release, "_input_hashes", lambda _root, _contract: {})

    def fake_materialize(
        _project_root: Path,
        destination: Path,
        *,
        revision: str,
        source_date_epoch: int,
    ) -> None:
        assert revision == "1" * 40
        assert source_date_epoch == 1_777_777_777
        packaging = destination / "packaging"
        (packaging / "locks").mkdir(parents=True)
        for relative in (
            "release-contract.v1.json",
            "payload-policy.v1.json",
        ):
            shutil.copyfile(project / "packaging" / relative, packaging / relative)
        shutil.copyfile(
            project / "packaging" / "locks" / "release-environment.v2.json",
            packaging / "locks" / "release-environment.v2.json",
        )

    monkeypatch.setattr(build_release, "_materialize_git_head", fake_materialize)
    calls = 0

    def fake_build_once(**kwargs: object) -> Path:
        nonlocal calls
        calls += 1
        canonical = Path(kwargs["canonical_root"])
        output = canonical / "output"
        output.mkdir(parents=True, exist_ok=True)
        (output / "artifact.zip").write_bytes(f"run-{calls}".encode("ascii"))
        return output

    monkeypatch.setattr(build_release, "_build_once", fake_build_once)

    with pytest.raises(build_release.ReleaseBuildError, match="not byte-identical"):
        build_release.build_release(project)

    assert not (project / "dist" / "release").exists()
    assert (project / "dist" / "reproducibility-failure.json").is_file()


@pytest.mark.skipif(os.name != "nt", reason="Windows build environment contract")
def test_clean_build_environment_ignores_ambient_pollution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    python_prefix = tmp_path / "python-prefix"
    for relative in ("Scripts", "Library/bin", "DLLs"):
        (python_prefix / relative).mkdir(parents=True, exist_ok=True)
    locked_git = tmp_path / "locked-git" / "git.exe"
    locked_git.parent.mkdir()
    temporary_root = tmp_path / "physical-build" / "temp"

    monkeypatch.setattr(build_release.sys, "prefix", str(python_prefix))
    monkeypatch.setattr(
        build_release, "_assert_safe_directory_chain", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        build_release, "_locked_regular_file", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(build_release, "_locked_git_executable", lambda: locked_git)

    polluted = {
        "HOME": "C:/ambient/home",
        "PYTHONHOME": "C:/ambient/python-home",
        "PYTHONPATH": "C:/ambient/python-path",
        "PYTHONUSERBASE": "C:/ambient/user-site",
        "PYTHONWARNINGS": "error",
        "CONDA_PREFIX": "C:/ambient/conda",
        "CONDA_DEFAULT_ENV": "ambient-conda",
        "CONDA_DLL_SEARCH_MODIFICATION_ENABLE": "1",
        "QT_PLUGIN_PATH": "C:/ambient/qt/plugins",
        "QT_QPA_PLATFORM_PLUGIN_PATH": "C:/ambient/qt/platforms",
        "PYSIDE_DESIGNER_PLUGINS": "C:/ambient/pyside/plugins",
        "PYINSTALLER_CONFIG_DIR": "C:/ambient/pyinstaller",
        "HTTP_PROXY": "http://ambient.invalid:8000",
        "HTTPS_PROXY": "http://ambient.invalid:8443",
        "ALL_PROXY": "socks5://ambient.invalid:1080",
        "NO_PROXY": "*",
        "OPENAI_API_KEY": "ambient-openai-key",
        "DEEPSEEK_API_KEY": "ambient-deepseek-key",
        "PKV_LLM_API_KEY": "ambient-pkv-key",
        "UNRELATED_SECRET_KEY": "ambient-secret",
        "PATH": "C:/ambient/path-shim",
        "TEMP": "C:/ambient/temp",
        "TMP": "C:/ambient/tmp",
        "USERPROFILE": "C:/ambient/profile",
    }
    for name, value in polluted.items():
        monkeypatch.setenv(name, value)

    first = build_release._clean_build_environment(
        1_777_777_777, temporary_root=temporary_root
    )
    for name in polluted:
        monkeypatch.setenv(name, f"second-{name.lower()}")
    second = build_release._clean_build_environment(
        1_777_777_777, temporary_root=temporary_root
    )

    assert first == second
    assert set(first) == {
        "COMSPEC",
        "HOME",
        "LANG",
        "LC_ALL",
        "PATH",
        "PATHEXT",
        "PROCESSOR_ARCHITECTURE",
        "PYINSTALLER_CONFIG_DIR",
        "PYTHONDONTWRITEBYTECODE",
        "PYTHONHASHSEED",
        "PYTHONIOENCODING",
        "PYTHONNOUSERSITE",
        "PYTHONSAFEPATH",
        "PYTHONUTF8",
        "SOURCE_DATE_EPOCH",
        "SystemRoot",
        "TEMP",
        "TMP",
        "TZ",
        "USERPROFILE",
        "WINDIR",
    }
    expected_path = [
        python_prefix.resolve(),
        python_prefix.resolve() / "Scripts",
        python_prefix.resolve() / "Library" / "bin",
        python_prefix.resolve() / "DLLs",
        Path("C:/Windows/System32"),
        locked_git.parent,
    ]
    assert first["PATH"].split(os.pathsep) == [str(path) for path in expected_path]
    assert first["PYTHONNOUSERSITE"] == "1"
    assert first["PYTHONSAFEPATH"] == "1"
    assert first["PYINSTALLER_CONFIG_DIR"] == str(temporary_root / "pyinstaller-config")
    assert first["HOME"] == first["USERPROFILE"] == str(temporary_root / "home")
    assert (temporary_root / "home").is_dir()
    assert first["TEMP"] == first["TMP"] == str(temporary_root)
    assert first["SOURCE_DATE_EPOCH"] == "1777777777"
    assert first["TZ"] == "UTC"
    assert not {
        "PYTHONHOME",
        "PYTHONPATH",
        "PYTHONUSERBASE",
        "PYTHONWARNINGS",
        "CONDA_PREFIX",
        "CONDA_DEFAULT_ENV",
        "CONDA_DLL_SEARCH_MODIFICATION_ENABLE",
        "QT_PLUGIN_PATH",
        "QT_QPA_PLATFORM_PLUGIN_PATH",
        "PYSIDE_DESIGNER_PLUGINS",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "OPENAI_API_KEY",
        "DEEPSEEK_API_KEY",
        "PKV_LLM_API_KEY",
        "UNRELATED_SECRET_KEY",
    }.intersection(first)


def test_clean_git_environment_blocks_replace_and_config_injection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PATH", "C:/ambient/git-shim")
    for name in (
        "GIT_REPLACE_REF_BASE",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_KEY_0",
        "GIT_CONFIG_VALUE_0",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_DIR",
        "GIT_WORK_TREE",
    ):
        monkeypatch.setenv(name, "ambient-injection")

    environment = build_release._clean_git_environment()

    expected_path = os.pathsep.join(
        (
            str(Path(str(build_release.GIT_TOOL_CONTRACT["path"])).parent),
            str(Path("C:/Windows/System32")),
        )
    )
    assert environment["PATH"] == expected_path
    assert "ambient/git-shim" not in environment["PATH"].replace("\\", "/")
    assert {
        name.upper() for name in environment if name.upper().startswith("GIT_")
    } == {
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_KEY_0",
        "GIT_CONFIG_NOSYSTEM",
        "GIT_CONFIG_VALUE_0",
        "GIT_NO_REPLACE_OBJECTS",
        "GIT_OPTIONAL_LOCKS",
        "GIT_TERMINAL_PROMPT",
    }
    assert environment["GIT_CONFIG_COUNT"] == "1"
    assert environment["GIT_CONFIG_GLOBAL"] == "NUL"
    assert environment["GIT_CONFIG_KEY_0"] == "core.attributesFile"
    assert environment["GIT_CONFIG_VALUE_0"] == "NUL"
    assert environment["GIT_CONFIG_NOSYSTEM"] == "1"
    assert environment["GIT_NO_REPLACE_OBJECTS"] == "1"
    assert environment["GIT_OPTIONAL_LOCKS"] == "0"
    assert environment["GIT_TERMINAL_PROMPT"] == "0"


def test_git_release_identity_uses_locked_absolute_git_despite_path_shim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    (project / ".git").mkdir(parents=True)
    locked_git = (tmp_path / "trusted" / "git.exe").resolve()
    monkeypatch.setenv("PATH", str(tmp_path / "malicious-git-shim"))
    monkeypatch.setenv("GIT_REPLACE_REF_BASE", "refs/replace/")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.fsmonitor")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "malicious-helper")
    monkeypatch.delenv("SOURCE_DATE_EPOCH", raising=False)
    monkeypatch.setattr(build_release, "_locked_git_executable", lambda: locked_git)
    calls: list[list[str]] = []

    def fake_run(command: list[str], *, cwd: Path, environment: dict[str, str]) -> str:
        calls.append(command)
        assert cwd == project
        assert command[0] == str(locked_git)
        assert environment["PATH"] == os.pathsep.join(
            (
                str(Path(str(build_release.GIT_TOOL_CONTRACT["path"])).parent),
                str(Path("C:/Windows/System32")),
            )
        )
        assert "malicious-git-shim" not in environment["PATH"]
        assert environment["GIT_NO_REPLACE_OBJECTS"] == "1"
        assert "GIT_REPLACE_REF_BASE" not in environment
        assert environment["GIT_CONFIG_COUNT"] == "1"
        assert environment["GIT_CONFIG_KEY_0"] == "core.attributesFile"
        assert environment["GIT_CONFIG_VALUE_0"] == "NUL"
        arguments = command[1:]
        if arguments == ["rev-parse", "--show-toplevel"]:
            return str(project)
        if arguments == ["rev-parse", "--absolute-git-dir"]:
            return str(project / ".git")
        if arguments == ["status", "--porcelain=v1", "--untracked-files=all"]:
            return ""
        if arguments == ["rev-parse", "HEAD"]:
            return "a" * 40
        if arguments == ["show", "-s", "--format=%ct", "a" * 40]:
            return "1777777777"
        raise AssertionError(f"unexpected Git command: {command}")

    monkeypatch.setattr(build_release, "_run", fake_run)

    assert build_release.git_release_identity(project) == ("a" * 40, 1_777_777_777)
    assert len(calls) == 7


@pytest.mark.windows_release_env
@pytest.mark.skipif(os.name != "nt", reason="Windows Git materializer contract")
def test_git_materializer_ignores_export_attributes_and_preserves_blob_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    git = Path(str(build_release.GIT_TOOL_CONTRACT["path"]))
    if not git.is_file():
        pytest.skip("locked Git executable unavailable")
    project = tmp_path / "project"
    project.mkdir()
    repository_environment = build_release._clean_git_environment()
    repository_environment.update(
        {
            "GIT_AUTHOR_EMAIL": "w3-test@example.invalid",
            "GIT_AUTHOR_NAME": "W3 Test",
            "GIT_COMMITTER_EMAIL": "w3-test@example.invalid",
            "GIT_COMMITTER_NAME": "W3 Test",
        }
    )

    def run_git(
        arguments: list[str], *, environment: dict[str, str] | None = None
    ) -> str:
        completed = subprocess.run(
            [str(git), *arguments],
            cwd=project,
            env=repository_environment if environment is None else environment,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        assert completed.returncode == 0, completed.stdout
        return completed.stdout.strip()

    run_git(["init", "."])
    global_attributes = tmp_path / "global-attributes"
    global_attributes.write_text(
        "global-ignore.txt export-ignore\n" "global-subst.txt export-subst\n",
        encoding="utf-8",
    )
    (project / ".git" / "info" / "attributes").write_text(
        "info-ignore.txt export-ignore\n" "info-subst.txt export-subst\n",
        encoding="utf-8",
    )
    tracked = {
        ".gitattributes": (
            b"tree-ignore.txt export-ignore\n" b"tree-subst.txt export-subst\n"
        ),
        "global-ignore.txt": b"global ignore must remain\n",
        "global-subst.txt": b"global literal $Format:%H$ must remain\n",
        "info-ignore.txt": b"info ignore must remain\n",
        "info-subst.txt": b"info literal $Format:%H$ must remain\n",
        "tree-ignore.txt": b"tree ignore must remain\n",
        "tree-subst.txt": b"tree literal $Format:%H$ must remain\n",
    }
    for relative, content in tracked.items():
        (project / relative).write_bytes(content)
    run_git(["add", "--", "."])
    run_git(["commit", "-m", "materializer attribute regression"])
    revision = run_git(["rev-parse", "HEAD"])
    assert len(revision) == 40

    attributes_environment = dict(repository_environment)
    attributes_environment["GIT_CONFIG_VALUE_0"] = str(global_attributes)
    active_attributes = run_git(
        [
            "check-attr",
            "export-ignore",
            "export-subst",
            "--",
            "global-ignore.txt",
            "global-subst.txt",
            "info-ignore.txt",
            "info-subst.txt",
            "tree-ignore.txt",
            "tree-subst.txt",
        ],
        environment=attributes_environment,
    )
    assert "global-ignore.txt: export-ignore: set" in active_attributes
    assert "global-subst.txt: export-subst: set" in active_attributes
    assert "info-ignore.txt: export-ignore: set" in active_attributes
    assert "info-subst.txt: export-subst: set" in active_attributes
    assert "tree-ignore.txt: export-ignore: set" in active_attributes
    assert "tree-subst.txt: export-subst: set" in active_attributes

    monkeypatch.setattr(build_release, "_locked_git_executable", lambda: git)
    monkeypatch.setattr(
        build_release,
        "_clean_git_environment",
        lambda: dict(attributes_environment),
    )
    destination = tmp_path / "physical-source" / "snapshot"
    build_release._materialize_git_head(
        project,
        destination,
        revision=revision,
        source_date_epoch=1_777_777_777,
    )

    assert not (destination / ".git").exists()
    assert {
        path.relative_to(destination).as_posix(): path.read_bytes()
        for path in destination.rglob("*")
        if path.is_file()
    } == tracked


@pytest.mark.parametrize(
    ("raw_tree", "message"),
    [
        (
            b"120000 blob " + b"a" * 40 + b"\tsymlink\0",
            "unsafe entry",
        ),
        (
            b"160000 commit " + b"a" * 40 + b"\tsubmodule\0",
            "unsafe entry",
        ),
        (
            b"100644 blob " + b"a" * 40 + b"\tdirectory/../escape.txt\0",
            "unsafe entry",
        ),
        (
            b"100644 blob " + b"a" * 40 + b"\tCONIN$.txt\0",
            "unsafe entry",
        ),
        (
            b"100644 blob " + b"a" * 40 + b"\tCONOUT$.log\0",
            "unsafe entry",
        ),
        (
            b"100644 blob " + b"a" * 40 + b"\tCLOCK$\0",
            "unsafe entry",
        ),
        (
            b"100644 blob "
            + b"a" * 40
            + b"\tREADME.md\0"
            + b"100644 blob "
            + b"b" * 40
            + b"\treadme.md\0",
            "case-colliding path",
        ),
    ],
    ids=(
        "symlink",
        "submodule",
        "unsafe-path",
        "conin-device",
        "conout-device",
        "clock-device",
        "case-collision",
    ),
)
def test_git_materializer_rejects_non_regular_or_unsafe_tree_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    raw_tree: bytes,
    message: str,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    calls = 0

    def fake_run_bytes(
        command: list[str],
        *,
        cwd: Path,
        environment: dict[str, str],
        input_bytes: bytes | None = None,
    ) -> bytes:
        nonlocal calls
        calls += 1
        assert cwd == project
        assert input_bytes is None
        assert command[1:4] == ["ls-tree", "-rz", "--full-tree"]
        return raw_tree

    monkeypatch.setattr(
        build_release, "_locked_git_executable", lambda: tmp_path / "locked-git.exe"
    )
    monkeypatch.setattr(build_release, "_clean_git_environment", lambda: {})
    monkeypatch.setattr(build_release, "_run_bytes", fake_run_bytes)

    with pytest.raises(build_release.ReleaseBuildError, match=message):
        build_release._materialize_git_head(
            project,
            tmp_path / "physical-source" / "snapshot",
            revision="a" * 40,
            source_date_epoch=1_777_777_777,
        )
    assert calls == 1


def test_invoke_pyinstaller_uses_clean_environment_and_per_root_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    spec = project / "packaging" / "pkv.spec"
    dist = tmp_path / "dist"
    clean_calls: list[tuple[int, Path]] = []
    invocations: list[tuple[list[str], Path, dict[str, str]]] = []

    def fake_clean_environment(
        source_date_epoch: int, *, temporary_root: Path
    ) -> dict[str, str]:
        clean_calls.append((source_date_epoch, temporary_root))
        return {"TEMP": str(temporary_root), "BUILD_CALL": str(len(clean_calls))}

    def fake_run(command: list[str], *, cwd: Path, environment: dict[str, str]) -> str:
        invocations.append((command, cwd, environment))
        return ""

    monkeypatch.setattr(
        build_release, "_clean_build_environment", fake_clean_environment
    )
    monkeypatch.setattr(build_release, "_run", fake_run)
    work_roots = [tmp_path / "physical-a" / "work", tmp_path / "physical-b" / "work"]

    for work_root in work_roots:
        build_release._invoke_pyinstaller(
            project_root=project,
            spec_path=spec,
            work_root=work_root,
            dist_root=dist,
            source_date_epoch=1_777_777_777,
        )

    expected_temps = [
        work_root.parent / f"{work_root.name}-process-temp" for work_root in work_roots
    ]
    assert clean_calls == [
        (1_777_777_777, expected_temps[0]),
        (1_777_777_777, expected_temps[1]),
    ]
    assert expected_temps[0] != expected_temps[1]
    assert len(invocations) == 2
    for index, (command, cwd, environment) in enumerate(invocations):
        assert cwd == project
        assert command == [
            build_release.sys.executable,
            "-m",
            "PyInstaller",
            "--clean",
            "--noconfirm",
            "--workpath",
            str(work_roots[index]),
            "--distpath",
            str(dist),
            str(spec),
        ]
        assert environment == {
            "TEMP": str(expected_temps[index]),
            "BUILD_CALL": str(index + 1),
        }


def test_build_release_powershell_wrapper_is_fail_closed() -> None:
    source = (PROJECT_ROOT / "scripts" / "build-release.ps1").read_text(
        encoding="utf-8"
    )

    assert '$ErrorActionPreference = "Stop"' in source
    assert source.index("$exitCode = 1") < source.index("try {")
    assert "$null -eq $LASTEXITCODE" in source
    assert source.index("$global:LASTEXITCODE = $null") < source.index(
        '& (Join-Path $PSScriptRoot "run-windows.ps1")'
    )
    assert source.rstrip().endswith("exit $exitCode")


@pytest.mark.skipif(os.name != "nt", reason="PowerShell wrapper contract")
@pytest.mark.parametrize(
    ("runner_body", "expected_exit_code"),
    [("exit 23\n", 23), ("throw 'runner failed'\n", 1), ("return\n", 1)],
)
def test_build_release_powershell_wrapper_propagates_failures(
    tmp_path: Path, runner_body: str, expected_exit_code: int
) -> None:
    powershell = shutil.which("pwsh.exe") or shutil.which("powershell.exe")
    if powershell is None:
        pytest.skip("PowerShell executable unavailable")
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    wrapper = scripts / "build-release.ps1"
    shutil.copyfile(PROJECT_ROOT / "scripts" / "build-release.ps1", wrapper)
    (scripts / "run-windows.ps1").write_text(runner_body, encoding="utf-8")

    completed = subprocess.run(
        [
            powershell,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(wrapper),
        ],
        cwd=tmp_path,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    assert completed.returncode == expected_exit_code, completed.stdout


def test_windows_release_host_accepts_native_api_when_ambient_architecture_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FastCtx child environments need not provide Windows arch variables."""

    monkeypatch.delenv("PROCESSOR_ARCHITECTURE", raising=False)
    monkeypatch.delenv("PROCESSOR_ARCHITEW6432", raising=False)
    monkeypatch.setattr(build_release, "_is_windows_host", lambda: True)
    monkeypatch.setattr(build_release, "_python_is_64_bit", lambda: True)
    monkeypatch.setattr(
        build_release,
        "_query_windows_native_architecture",
        lambda: ("amd64", False),
    )

    build_release._validate_windows_release_host()


def test_windows_native_architecture_rejects_unavailable_iswow64process2(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legacy APIs cannot prove native x64 instead of ARM64 emulation."""

    class _Function:
        def __init__(self, result: object) -> None:
            self.result = result

        def __call__(self, *_arguments: object) -> object:
            return self.result

    class _Kernel32WithoutIsWow64Process2:
        GetCurrentProcess = _Function(1)

    monkeypatch.setattr(build_release, "_is_windows_host", lambda: True)
    monkeypatch.setattr(
        build_release.ctypes,
        "WinDLL",
        lambda *_arguments, **_kwargs: _Kernel32WithoutIsWow64Process2(),
        raising=False,
    )

    assert build_release._query_windows_native_architecture() is None


@pytest.mark.parametrize(
    "host_architecture",
    [None, ("amd64", True), ("arm64", False), ("x86", False), ("unknown", False)],
)
def test_windows_release_host_rejects_non_native_or_unverified_architecture(
    monkeypatch: pytest.MonkeyPatch,
    host_architecture: tuple[str, bool] | None,
) -> None:
    monkeypatch.setattr(build_release, "_is_windows_host", lambda: True)
    monkeypatch.setattr(build_release, "_python_is_64_bit", lambda: True)
    monkeypatch.setattr(
        build_release,
        "_query_windows_native_architecture",
        lambda: host_architecture,
    )

    with pytest.raises(
        build_release.ReleaseBuildError,
        match="native Windows x86-64 Python",
    ):
        build_release._validate_windows_release_host()


@pytest.mark.parametrize(
    "windows_host,python_is_64_bit",
    [(False, True), (True, False)],
)
def test_windows_release_host_rejects_non_windows_and_32_bit_before_native_query(
    monkeypatch: pytest.MonkeyPatch,
    windows_host: bool,
    python_is_64_bit: bool,
) -> None:
    monkeypatch.setattr(build_release, "_is_windows_host", lambda: windows_host)
    monkeypatch.setattr(
        build_release,
        "_python_is_64_bit",
        lambda: python_is_64_bit,
    )

    def unexpected_native_query() -> tuple[str, bool]:
        raise AssertionError("native architecture API must not run")

    monkeypatch.setattr(
        build_release,
        "_query_windows_native_architecture",
        unexpected_native_query,
    )

    with pytest.raises(
        build_release.ReleaseBuildError,
        match="native Windows x86-64 Python",
    ):
        build_release._validate_windows_release_host()


@pytest.mark.windows_release_env
@pytest.mark.skipif(os.name != "nt", reason="exact Windows release environment")
def test_windows_release_host_ignores_missing_ambient_architecture_variables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PROCESSOR_ARCHITECTURE", raising=False)
    monkeypatch.delenv("PROCESSOR_ARCHITEW6432", raising=False)

    build_release._validate_windows_release_host()


@pytest.mark.windows_release_env
@pytest.mark.skipif(os.name != "nt", reason="exact Windows release environment")
def test_release_toolchain_lock_matches_current_builder() -> None:
    lock = json.loads(
        (
            PROJECT_ROOT / "packaging" / "locks" / "release-environment.v2.json"
        ).read_text(encoding="utf-8")
    )
    resolved = build_release.validate_environment_lock(lock)
    versions = {item["name"]: item["version"] for item in resolved}

    assert versions["pyinstaller"] == "6.21.0"
    assert versions["pyinstaller-hooks-contrib"] == "2026.6"
    assert versions["pefile"] == "2024.8.26"
    assert versions["pywin32-ctypes"] == "0.2.3"
    assert versions["altgraph"] == "0.17.5"


@pytest.mark.windows_release_env
@pytest.mark.skipif(os.name != "nt", reason="exact Windows release environment")
def test_release_toolchain_lock_rejects_metadata_hash_drift() -> None:
    lock = json.loads(
        (
            PROJECT_ROOT / "packaging" / "locks" / "release-environment.v2.json"
        ).read_text(encoding="utf-8")
    )
    build_component = next(
        component for component in lock["distributions"] if component["role"] == "build"
    )
    build_component["metadata_files"]["METADATA"]["sha256"] = "0" * 64

    with pytest.raises(build_release.ReleaseBuildError, match="metadata hash mismatch"):
        build_release.validate_environment_lock(lock)


@pytest.mark.windows_release_env
@pytest.mark.skipif(os.name != "nt", reason="exact Windows release environment")
def test_distribution_license_materials_are_indexed_and_hashed(tmp_path: Path) -> None:
    lock = json.loads(
        (
            PROJECT_ROOT / "packaging" / "locks" / "release-environment.v2.json"
        ).read_text(encoding="utf-8")
    )
    components = build_release.validate_environment_lock(lock)

    index = build_release.collect_license_materials(
        components, tmp_path / "licenses", project_root=PROJECT_ROOT
    )

    assert (tmp_path / "licenses" / "index.json").is_file()
    assert index["schema_version"] == "pkv.license-index.v1"
    assert len(index["entries"]) == len(components)
    assert all(item["license_files"] for item in index["entries"])
    assert all(
        len(license_file["sha256"]) == 64
        for item in index["entries"]
        for license_file in item["license_files"]
    )
    for item in index["entries"]:
        if item["name"] == "cpython":
            assert item["metadata_declared_license_files"] == []
            continue
        distribution = importlib.metadata.distribution(item["name"])
        declared = distribution.metadata.get_all("License-File") or []
        assert item["metadata_declared_license_files"] == sorted(
            declared, key=lambda value: value.encode("utf-8")
        )
        assert sum(
            material.get("declared_by_metadata") is True
            for material in item["license_files"]
        ) == len(declared)
