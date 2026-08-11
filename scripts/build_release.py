"""Build the reproducible Windows release artifact.

This module intentionally uses only the Python standard library.  The build
machine may use Conda to provide the locked toolchain, but the resulting
artifact is a PyInstaller onedir bundle and has no Python/Conda dependency at
runtime.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import importlib.metadata
import json
import os
import re
import shutil
import stat
import struct
import subprocess
import sys
import sysconfig
import tarfile
import zipfile
import zlib
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence

if __package__:
    from .release_inventory import (
        InventoryError,
        build_release_inventory,
        discover_executable_pkg_tocs,
    )
else:  # pragma: no cover - exercised by the canonical script entry point
    from release_inventory import (
        InventoryError,
        build_release_inventory,
        discover_executable_pkg_tocs,
    )


SCHEMA_BUILD_INFO = "pkv.build-info.v1"
SCHEMA_DEPENDENCIES = "pkv.dependency-manifest.v1"
SCHEMA_PAYLOAD = "pkv.payload-manifest.v1"
SCHEMA_PROVENANCE = "pkv.artifact-provenance.v1"
SCHEMA_SBOM = "CycloneDX"
RELEASE_COMPONENT_ROLE_MAP_SHA256 = (
    "7f1e2eb33bfc100421278dea9e6e1f14b640867279fff234ba80ec94558cc978"
)
COMPLIANCE_ARTIFACT_CONTRACT = {
    "cpython-3.11.15-license": (
        "packaging/licenses/cpython-3.11.15-LICENSE.txt",
        13936,
        "3b2f81fe21d181c499c59a256c8e1968455d6689d269aa85373bfb6af41da3bf",
    ),
    "pyinstaller-6.21.0-copying-and-bootloader-exception": (
        "packaging/licenses/pyinstaller-6.21.0-COPYING.txt",
        32138,
        "dcf75fdb959db1e3b41c0f8505069d2ece781b5ec6b3d0a4d30975cfc6580245",
    ),
    "pyside6-6.11.1-license-selection": (
        "packaging/licenses/pyside6-6.11.1-license-selection.txt",
        144,
        "dd6a83da77d0b6a113a574154c12cb009751c2b0dfe5c22e288cd86ebc2a1f13",
    ),
    "qt-pyside6-6.11.1-lgpl-3.0-only": (
        "packaging/licenses/qt-pyside6-6.11.1-LGPL-3.0-only.txt",
        7651,
        "da7eabb7bafdf7d3ae5e9f223aa5bdc1eece45ac569dc21b3b037520b4464768",
    ),
    "qt-pyside6-6.11.1-gpl-2.0-only": (
        "packaging/licenses/qt-pyside6-6.11.1-GPL-2.0-only.txt",
        18092,
        "8177f97513213526df2cf6184d8ff986c675afb514d4e68a404010521b880643",
    ),
    "qt-pyside6-6.11.1-gpl-3.0-only": (
        "packaging/licenses/qt-pyside6-6.11.1-GPL-3.0-only.txt",
        35147,
        "8ceb4b9ee5adedde47b31e975c1d90c73ad27b6b165a1dcd80c7c545eb65b903",
    ),
    "qt-pyside6-6.11.1-lgpl-user-notice-template": (
        "packaging/licenses/qt-pyside6-6.11.1-LGPL-user-notice-template.txt",
        3554,
        "9112fd72a849a189e42cd8f3c742a93dfe8ea452d124d452e1e9d5954babf03a",
    ),
    "html2text-2020.1.16-sdist": (
        "packaging/compliance-sources/html2text-2020.1.16.tar.gz",
        49464,
        "e296318e16b059ddb97f7a8a1d6a5c1d7af4544049a01e261731d2d5cc277bbb",
    ),
}
COMPLIANCE_BLOCKER_IDS = frozenset(
    {
        "conda-native-license-materials-and-spdx",
        "html2text-gpl-compliance",
        "native-msvc-license-and-provenance",
        "qt-corresponding-source-location",
        "qt-linkage-and-replacement-not-proven",
        "qt-module-license-audit",
        "qt-notice-placeholders",
    }
)
HTML2TEXT_GPL_COMPLIANCE_REQUIREMENTS = (
    "combined-work-licensing-decision",
    "corresponding-source-scope-and-persistent-location",
    "spdx-license-expression",
    "whole-work-license-and-notices",
)
NATIVE_LICENSE_HOLD_COMPONENT_SELECTORS = (
    "component:*[native-payload]",
    "conda-package:*",
)
HARNESS_ARTIFACT_STATUS = "internal-verification-only-on-native-compliance-hold"
HARNESS_BLOCKER_IDS = ("harness-native-license-and-provenance",)
HARNESS_BLOCKER_AUTHORITY = (
    {
        "condition": (
            "The frozen internal E2E harness has exact EXE/PKG/CArchive/PYZ entry and "
            "source-byte bindings, but its embedded native/runtime subcomponents do not "
            "yet have complete approved license materials, redistribution provenance, "
            "or legal authorization for distribution outside controlled W4."
        ),
        "id": HARNESS_BLOCKER_IDS[0],
        "resolution": (
            "Cross-bind every actual embedded native/runtime subcomponent to approved "
            "license, notice, redistribution-provenance, and legal-review evidence before "
            "any distribution outside controlled W4."
        ),
    },
)
GIT_TOOL_CONTRACT = {
    "path": "C:/Program Files/Git/mingw64/bin/git.exe",
    "version": "git version 2.54.0.windows.1",
    "sha256": "cab4c4eea1d869cf9f7be73868dc9a90ad2df1b1b673e5f8c8714a576c25ea96",
    "size": 4422544,
    "runtime_files": {
        "libiconv-2.dll": {
            "sha256": "7a282a854e01be726c6cccfe46f548c716aa45b3014818468253aaa4efbcd067",
            "size": 1143148,
        },
        "libintl-8.dll": {
            "sha256": "0537c3dd2378218508ebe3cc416d72a99ee2d24ae1c5525e23458f32544ef861",
            "size": 298731,
        },
        "libpcre2-8-0.dll": {
            "sha256": "c135a87ed0f11eae8ffc4cb469671ff0b3f5d71fab5fb024e9b1e7241ca25b52",
            "size": 717955,
        },
        "libwinpthread-1.dll": {
            "sha256": "851f61482ad5b6aac7c6abc54bbe31d24f89e0ca683a75fcec2d47f86b2d2242",
            "size": 65442,
        },
        "zlib1.dll": {
            "sha256": "93e9243a44c29200eeacaf9658efe2558581770e4b11ca4b500e18e424a6e3b5",
            "size": 128488,
        },
    },
    "system_dll_policy": "Windows system DLLs are target-platform inputs",
}
BUILD_ENVIRONMENT_CONTRACT = {
    "conda_hardlink_threat_model": "accepted_for_test_candidate",
    "home_directory": "per-physical-build-root",
    "inherit_ambient": False,
    "hardlink_sensitive_roots": [
        "python-prefix",
        "python-prefix/DLLs",
        "python-prefix/Lib",
        "python-prefix/Lib/site-packages",
        "python-prefix/Library/bin",
    ],
    "path_roles": [
        "python-prefix",
        "python-scripts",
        "python-library-bin",
        "python-dlls",
        "windows-system32",
        "locked-git-directory",
    ],
    "temporary_directory": "per-physical-build-root",
    "python_hash_seed": "0",
    "python_no_user_site": True,
    "source_date_epoch": "git-commit-timestamp",
    "timezone": "UTC",
    "live_environment_byte_revalidation": [
        "before-build-a",
        "after-build-a",
        "before-build-b",
        "after-build-b",
        "before-publication",
    ],
    "release_eligible_environment_requirement": "copy-only-no-hardlinks",
}
ZIP_MIN_EPOCH = 315532800  # 1980-01-01T00:00:00Z
WINDOWS_REPARSE_POINT = 0x400
SENSITIVE_ENV_NAMES = frozenset(
    {
        "DEEPSEEK_API_KEY",
        "OPENAI_API_KEY",
        "PKV_LLM_API_KEY",
        "PKV_EMBD_API_KEY",
        "PYTHONHOME",
        "PYTHONPATH",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
    }
)


class ReleaseBuildError(RuntimeError):
    """A deterministic, fail-closed release contract violation."""


def canonical_json_bytes(value: Any) -> bytes:
    """Return canonical UTF-8 JSON with one trailing LF."""

    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def write_canonical_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json_bytes(value))


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_distribution_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseBuildError(f"cannot read JSON contract: {path}") from exc
    if not isinstance(payload, dict):
        raise ReleaseBuildError(f"JSON contract root must be an object: {path}")
    return payload


def _contract_relative_path(value: Any, *, label: str) -> str:
    text = str(value)
    path = PurePosixPath(text)
    if (
        not text
        or "\\" in text
        or path.is_absolute()
        or ".." in path.parts
        or ":" in text
        or path.as_posix() != text
    ):
        raise ReleaseBuildError(f"unsafe {label} in release contract: {text!r}")
    return text


def validate_release_contract(contract: Mapping[str, Any]) -> None:
    if contract.get("schema_version") != "pkv.release-contract.v1":
        raise ReleaseBuildError("unsupported release contract schema")
    version = str(contract.get("version", ""))
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise ReleaseBuildError(
            "release version must be a canonical three-part version"
        )
    if contract.get("product_id") != "personal-knowledge-vault":
        raise ReleaseBuildError("unexpected release product id")
    if contract.get("target") != "windows-x86_64":
        raise ReleaseBuildError("unsupported release target")
    expected_archive_root = f"PersonalKnowledgeVault-{version}-windows-x86_64"
    if contract.get("archive_root") != expected_archive_root:
        raise ReleaseBuildError("release archive root does not match version/target")
    if contract.get("pyinstaller_spec") != "packaging/pkv.spec":
        raise ReleaseBuildError("unexpected PyInstaller spec path")
    if contract.get("pyinstaller_collect_dir") != "pkv":
        raise ReleaseBuildError("unexpected PyInstaller collect directory")
    _contract_relative_path(contract["pyinstaller_spec"], label="PyInstaller spec path")
    _contract_relative_path(
        contract["pyinstaller_collect_dir"], label="PyInstaller collect directory"
    )
    expected_entrypoints = [
        {"path": "app/pkv.exe", "role": "cli", "console": True},
        {"path": "app/pkv-gui.exe", "role": "gui", "console": False},
        {"path": "app/pkv-mcp.exe", "role": "mcp_stdio", "console": True},
    ]
    if contract.get("entrypoints") != expected_entrypoints:
        raise ReleaseBuildError(
            "release entrypoint contract differs from the frozen layout"
        )
    if contract.get("source_date_epoch") != {
        "authority": "git_commit_timestamp",
        "zip_min_epoch": ZIP_MIN_EPOCH,
        "dos_resolution_seconds": 2,
    }:
        raise ReleaseBuildError(
            "release timestamp contract differs from the frozen policy"
        )
    if contract.get("install") != {
        "scope": "per-user",
        "program_root": "%LOCALAPPDATA%/Programs/PersonalKnowledgeVault",
        "data_root": "%LOCALAPPDATA%/PersonalKnowledgeVault",
        "upgrade_policy": "reject-cross-version",
        "uninstall_data_policy": "retain-unless-explicit",
    }:
        raise ReleaseBuildError(
            "release install contract differs from the frozen policy"
        )
    if contract.get("reproducibility") != {
        "unsigned_runs": 2,
        "unsigned_requirement": "byte-identical",
        "zip_order": "utf8-byte-lexical",
        "zip_compression": "deflate-9",
    }:
        raise ReleaseBuildError("release reproducibility contract is invalid")
    if contract.get("artifact_routing") != {
        "eligibility_authority": "packaging/compliance-sources.v1.json",
        "candidate_directory": "dist/candidate",
        "candidate_artifact_kind": "test_candidate",
        "release_directory": "dist/release",
        "release_artifact_kind": "release",
    }:
        raise ReleaseBuildError("release Artifact routing contract is invalid")
    additional_inputs = contract.get("additional_fingerprint_inputs")
    if not isinstance(additional_inputs, list) or not additional_inputs:
        raise ReleaseBuildError(
            "additional fingerprint inputs must be a non-empty list"
        )
    normalized_inputs = [
        _contract_relative_path(value, label="fingerprint input")
        for value in additional_inputs
    ]
    if len(set(normalized_inputs)) != len(normalized_inputs):
        raise ReleaseBuildError("duplicate additional fingerprint input")


def validate_payload_policy(policy: Mapping[str, Any]) -> None:
    """Freeze the security-critical v1 payload policy against weakening."""

    expected = {
        "schema_version": "pkv.payload-policy.v1",
        "allowed_top_level": [
            "app",
            "Install.ps1",
            "Uninstall.ps1",
            "LICENSE",
            "THIRD-PARTY-NOTICES.txt",
            "USER-GUIDE.md",
            "COMPLIANCE-HOLD.txt",
            "licenses",
            "build-info.json",
            "dependency-manifest.json",
            "release-inventory.json",
            "sbom.cdx.json",
            "payload-manifest.json",
        ],
        "required_paths": [
            "app/pkv.exe",
            "app/pkv-gui.exe",
            "app/pkv-mcp.exe",
            "Install.ps1",
            "Uninstall.ps1",
            "LICENSE",
            "THIRD-PARTY-NOTICES.txt",
            "USER-GUIDE.md",
            "licenses/index.json",
            "build-info.json",
            "dependency-manifest.json",
            "release-inventory.json",
            "sbom.cdx.json",
            "payload-manifest.json",
        ],
        "forbidden_path_components": [
            ".git",
            ".github",
            ".pytest_cache",
            ".data",
            ".data-test",
            ".data_test",
            "__pycache__",
            "tests",
            "test",
            "fixtures",
            "harness",
            "vault",
            "logs",
            "tmp",
            "backups",
        ],
        "forbidden_basenames": [
            "local.yaml",
            "local.yml",
            "conftest.py",
            ".coverage",
            "coverage.xml",
            "junit.xml",
            "id_rsa",
            "credentials.json",
            "secrets.json",
            "auth.json",
            "token.json",
        ],
        "forbidden_suffixes": [
            ".py",
            ".pyc",
            ".pyo",
            ".db",
            ".sqlite",
            ".sqlite3",
            ".idx",
            ".log",
            ".key",
            ".pfx",
            ".p12",
        ],
        "allowed_public_certificate_files": {
            "app/_internal/certifi/cacert.pem": (
                "bbc7e9c01d7551bb8a159b5dedd989b8ee3ce105aff522b68eb1b01bf854cab0"
            )
        },
        "sensitive_config_keys": [
            "access_token",
            "api_key",
            "apikey",
            "auth_token",
            "authorization",
            "bearer_token",
            "client_secret",
            "cookie",
            "credential",
            "credentials",
            "id_token",
            "oauth_token",
            "passcode",
            "passphrase",
            "passwd",
            "password",
            "private_key",
            "refresh_token",
            "secret",
            "session_token",
            "token",
        ],
        "text_suffixes": [
            ".txt",
            ".json",
            ".yaml",
            ".yml",
            ".ini",
            ".cfg",
            ".toml",
            ".ps1",
            ".md",
            ".sql",
            ".qss",
        ],
        "forbidden_text_markers": [
            "pkv.loopback-provider",
            "pkv.loopback_provider",
            "fake_provider",
            "test_mode_provider",
            "m13_w4_harness",
        ],
    }
    if dict(policy) != expected:
        raise ReleaseBuildError(
            "payload policy differs from the frozen security-critical v1 contract"
        )


def _run(
    command: Sequence[str],
    *,
    cwd: Path,
    environment: Mapping[str, str] | None = None,
) -> str:
    completed = subprocess.run(
        list(command),
        cwd=cwd,
        env=None if environment is None else dict(environment),
        check=False,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if completed.returncode != 0:
        raise ReleaseBuildError(
            f"command failed ({completed.returncode}): {' '.join(command)}\n"
            f"{completed.stdout}"
        )
    return completed.stdout.strip()


def _run_bytes(
    command: Sequence[str],
    *,
    cwd: Path,
    environment: Mapping[str, str],
    input_bytes: bytes | None = None,
) -> bytes:
    completed = subprocess.run(
        list(command),
        cwd=cwd,
        env=dict(environment),
        check=False,
        input=input_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if completed.returncode != 0:
        output = (completed.stdout + completed.stderr).decode("utf-8", errors="replace")
        raise ReleaseBuildError(
            f"command failed ({completed.returncode}): {' '.join(command)}\n{output}"
        )
    return completed.stdout


def _clean_git_environment() -> dict[str, str]:
    system_root = Path("C:/Windows")
    system32 = system_root / "System32"
    git_directory = Path(str(GIT_TOOL_CONTRACT["path"])).parent
    return {
        "COMSPEC": str(system32 / "cmd.exe"),
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_GLOBAL": "NUL",
        "GIT_CONFIG_KEY_0": "core.attributesFile",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_VALUE_0": "NUL",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": os.pathsep.join((str(git_directory), str(system32))),
        "PATHEXT": ".COM;.EXE;.BAT;.CMD",
        "SystemRoot": str(system_root),
        "TZ": "UTC",
        "WINDIR": str(system_root),
    }


def _locked_git_executable() -> Path:
    path = Path(str(GIT_TOOL_CONTRACT["path"]))
    if (
        Path(os.path.abspath(path)) != path
        or path.as_posix() != GIT_TOOL_CONTRACT["path"]
    ):
        raise ReleaseBuildError("Git tool contract path is not canonical")
    _locked_regular_file(path, prefix=Path(path.anchor), label="locked Git executable")
    if (
        path.stat().st_size != GIT_TOOL_CONTRACT["size"]
        or sha256_file(path) != GIT_TOOL_CONTRACT["sha256"]
    ):
        raise ReleaseBuildError("Git executable differs from the exact tool contract")
    runtime_files = GIT_TOOL_CONTRACT["runtime_files"]
    if not isinstance(runtime_files, Mapping) or not runtime_files:
        raise ReleaseBuildError("Git runtime file contract is invalid")
    for filename, raw_expected in sorted(runtime_files.items()):
        if (
            not isinstance(filename, str)
            or PurePosixPath(filename).name != filename
            or not isinstance(raw_expected, Mapping)
            or set(raw_expected) != {"sha256", "size"}
        ):
            raise ReleaseBuildError("Git runtime file contract is invalid")
        runtime = path.parent / filename
        _locked_regular_file(
            runtime, prefix=Path(path.anchor), label="locked Git runtime"
        )
        if runtime.stat().st_size != int(raw_expected["size"]) or sha256_file(
            runtime
        ) != str(raw_expected["sha256"]):
            raise ReleaseBuildError(f"Git runtime differs from lock: {filename}")
    version = _run(
        [str(path), "--version"],
        cwd=path.parent,
        environment=_clean_git_environment(),
    )
    if version != GIT_TOOL_CONTRACT["version"]:
        raise ReleaseBuildError("Git version differs from the exact tool contract")
    return path


def git_release_identity(project_root: Path) -> tuple[str, int]:
    """Require a clean checkout and return (HEAD, commit epoch)."""

    git_environment = _clean_git_environment()
    git = str(_locked_git_executable())
    top_level = Path(
        _run(
            [git, "rev-parse", "--show-toplevel"],
            cwd=project_root,
            environment=git_environment,
        )
    ).resolve(strict=True)
    if top_level != project_root.resolve(strict=True):
        raise ReleaseBuildError("git repository top-level differs from project root")
    git_directory = Path(
        _run(
            [git, "rev-parse", "--absolute-git-dir"],
            cwd=project_root,
            environment=git_environment,
        )
    )
    if git_directory.resolve(strict=True) != (project_root / ".git").resolve(
        strict=True
    ):
        raise ReleaseBuildError("release build requires the project .git directory")
    _assert_safe_directory_chain(git_directory, authority=project_root)
    status = _run(
        [git, "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=project_root,
        environment=git_environment,
    )
    if status:
        raise ReleaseBuildError(
            "release build requires a clean checkout; commit or remove changes first"
        )
    revision = _run(
        [git, "rev-parse", "HEAD"],
        cwd=project_root,
        environment=git_environment,
    )
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ReleaseBuildError("git HEAD is not a full SHA-1 revision")
    raw_epoch = _run(
        [git, "show", "-s", "--format=%ct", revision],
        cwd=project_root,
        environment=git_environment,
    )
    try:
        epoch = int(raw_epoch)
    except ValueError as exc:
        raise ReleaseBuildError("git commit timestamp is not an integer") from exc
    ambient = os.environ.get("SOURCE_DATE_EPOCH")
    if ambient is not None and ambient != str(epoch):
        raise ReleaseBuildError(
            "ambient SOURCE_DATE_EPOCH differs from the authoritative git commit epoch"
        )
    if _run(
        [git, "rev-parse", "HEAD"], cwd=project_root, environment=git_environment
    ) != revision or _run(
        [git, "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=project_root,
        environment=git_environment,
    ):
        raise ReleaseBuildError("Git checkout changed while freezing release identity")
    return revision, epoch


def zip_timestamp(epoch: int) -> tuple[int, int, int, int, int, int]:
    """Return the fixed UTC DOS timestamp (two-second resolution)."""

    normalized = max(int(epoch), ZIP_MIN_EPOCH)
    normalized -= normalized % 2
    value = datetime.fromtimestamp(normalized, tz=timezone.utc)
    return value.year, value.month, value.day, value.hour, value.minute, value.second


def _path_is_reparse(path: Path, info: os.stat_result | None = None) -> bool:
    details = info if info is not None else path.lstat()
    attributes = getattr(details, "st_file_attributes", 0)
    return bool(attributes & WINDOWS_REPARSE_POINT)


def _validate_regular_file(path: Path) -> os.stat_result:
    try:
        details = path.lstat()
    except OSError as exc:
        raise ReleaseBuildError(f"cannot inspect payload path: {path}") from exc
    if stat.S_ISLNK(details.st_mode) or _path_is_reparse(path, details):
        raise ReleaseBuildError(f"payload links/reparse points are forbidden: {path}")
    if not stat.S_ISREG(details.st_mode):
        raise ReleaseBuildError(f"payload entry is not a regular file: {path}")
    if details.st_nlink > 1:
        raise ReleaseBuildError(f"payload hardlinks are forbidden: {path}")
    return details


def _iter_payload_files(root: Path) -> list[tuple[str, Path]]:
    if not root.is_dir():
        raise ReleaseBuildError(f"payload root is not a directory: {root}")
    items: list[tuple[str, Path]] = []
    for path in root.rglob("*"):
        details = path.lstat()
        if stat.S_ISLNK(details.st_mode) or _path_is_reparse(path, details):
            raise ReleaseBuildError(
                f"payload links/reparse points are forbidden: {path}"
            )
        if path.is_dir():
            continue
        _validate_regular_file(path)
        relative = path.relative_to(root).as_posix()
        normalized = PurePosixPath(relative)
        if normalized.is_absolute() or ".." in normalized.parts or ":" in relative:
            raise ReleaseBuildError(f"unsafe payload relative path: {relative}")
        items.append((relative, path))
    return sorted(items, key=lambda item: item[0].encode("utf-8"))


def _value_is_nonempty_secret(value: Any) -> bool:
    if value is None or value is False:
        return False
    if isinstance(value, str):
        return value.strip() != ""
    if isinstance(value, (list, tuple, dict, set)):
        return bool(value)
    return True


def _canonical_sensitive_key(value: str) -> str:
    value = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", value)
    return re.sub(r"[-.]+", "_", value).strip("_ ").casefold()


def _assert_no_structured_secrets(
    relative: str,
    text: str,
    sensitive_keys: set[str],
) -> None:
    """Reject non-empty credentials in bundled JSON/YAML/INI-style data."""

    canonical_keys = {_canonical_sensitive_key(value) for value in sensitive_keys}
    compact_keys = {value.replace("_", "") for value in canonical_keys}

    def key_is_sensitive(value: str) -> bool:
        canonical = _canonical_sensitive_key(value)
        return canonical in canonical_keys or canonical.replace("_", "") in compact_keys

    if relative.casefold().endswith(".json"):
        try:
            document = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ReleaseBuildError(f"bundled JSON is invalid: {relative}") from exc
        pending = [document]
        while pending:
            current = pending.pop()
            if isinstance(current, dict):
                for key, value in current.items():
                    if key_is_sensitive(str(key)) and _value_is_nonempty_secret(value):
                        raise ReleaseBuildError(
                            f"payload contains a non-empty sensitive field {key!r}: {relative}"
                        )
                    pending.append(value)
            elif isinstance(current, list):
                pending.extend(current)
        return

    assignment = re.compile(
        r"(?im)^\s*[\"']?(?P<key>[A-Za-z][A-Za-z0-9_.-]*)[\"']?"
        r"\s*[:=]\s*(?P<value>[^\r\n]*)$"
    )
    empty_values = {"", "''", '""', "~", "null", "none", "false", "[]", "{}"}
    for match in assignment.finditer(text):
        key = match.group("key")
        if not key_is_sensitive(key):
            continue
        value = match.group("value").strip().rstrip(",").strip()
        if value.startswith("#") or value.casefold() in empty_values:
            continue
        raise ReleaseBuildError(
            f"payload contains a non-empty sensitive field {key!r}: {relative}"
        )


def scan_payload(
    root: Path,
    policy: Mapping[str, Any],
    *,
    allow_missing: Iterable[str] = (),
) -> list[str]:
    """Apply the release allowlist/denylist to an unpacked payload."""

    files = _iter_payload_files(root)
    relative_paths = [relative for relative, _ in files]
    folded: dict[str, str] = {}
    for relative in relative_paths:
        collision = folded.setdefault(relative.casefold(), relative)
        if collision != relative:
            raise ReleaseBuildError(
                f"case-insensitive payload path collision: {collision!r} / {relative!r}"
            )

    allowed_top = {str(value).casefold() for value in policy["allowed_top_level"]}
    forbidden_components = {
        str(value).casefold() for value in policy["forbidden_path_components"]
    }
    forbidden_suffixes = {
        str(value).casefold() for value in policy["forbidden_suffixes"]
    }
    forbidden_basenames = {
        str(value).casefold() for value in policy["forbidden_basenames"]
    }
    text_suffixes = {str(value).casefold() for value in policy["text_suffixes"]}
    markers = [str(value).casefold() for value in policy["forbidden_text_markers"]]
    public_certificates = {
        str(relative): str(digest).lower()
        for relative, digest in policy["allowed_public_certificate_files"].items()
    }
    sensitive_keys = {str(value) for value in policy["sensitive_config_keys"]}

    for relative, path in files:
        parts = PurePosixPath(relative).parts
        top = parts[0].casefold()
        if top not in allowed_top:
            raise ReleaseBuildError(
                f"payload top-level entry is not allowlisted: {relative}"
            )
        if any(part.casefold() in forbidden_components for part in parts):
            raise ReleaseBuildError(
                f"payload contains forbidden path component: {relative}"
            )
        basename = parts[-1].casefold()
        if (
            basename in forbidden_basenames
            or basename == ".env"
            or basename.startswith(".env.")
        ):
            raise ReleaseBuildError(f"payload contains forbidden filename: {relative}")
        if path.suffix.casefold() in forbidden_suffixes:
            raise ReleaseBuildError(f"payload contains forbidden suffix: {relative}")
        if path.suffix.casefold() == ".pem":
            expected_certificate_hash = public_certificates.get(relative)
            if (
                expected_certificate_hash is None
                or sha256_file(path) != expected_certificate_hash
            ):
                raise ReleaseBuildError(
                    f"payload contains an unapproved PEM file: {relative}"
                )
        if (
            path.suffix.casefold() in text_suffixes
            or path.suffix.casefold() == ".pem"
            or basename
            in {
                "license",
                "third-party-notices.txt",
            }
        ):
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeError as exc:
                raise ReleaseBuildError(
                    f"release text file is not UTF-8: {relative}"
                ) from exc
            folded_text = text.casefold()
            if re.search(r"-----begin(?: [a-z0-9]+)* private key-----", folded_text):
                raise ReleaseBuildError(
                    f"payload contains private-key material: {relative}"
                )
            for marker in markers:
                if marker in folded_text:
                    raise ReleaseBuildError(
                        f"payload contains forbidden harness/test marker {marker!r}: {relative}"
                    )

            suffix = path.suffix.casefold()
            should_scan_structured = relative.startswith(
                "app/_internal/config/"
            ) or suffix in {".json", ".yaml", ".yml", ".ini", ".cfg", ".toml"}
            if should_scan_structured:
                _assert_no_structured_secrets(relative, text, sensitive_keys)

    required = {str(value) for value in policy["required_paths"]}
    missing_allowed = set(allow_missing)
    available = set(relative_paths)
    missing = sorted(required - available - missing_allowed)
    if missing:
        raise ReleaseBuildError(f"payload required paths are missing: {missing}")

    return relative_paths


def payload_role(relative: str) -> str:
    if relative.startswith("app/"):
        if relative in {
            "app/pkv.exe",
            "app/pkv-gui.exe",
            "app/pkv-mcp.exe",
        }:
            return "entrypoint"
        return "runtime"
    if relative.startswith("licenses/"):
        return "third_party_license"
    mapping = {
        "Install.ps1": "installer",
        "Uninstall.ps1": "uninstaller",
        "LICENSE": "license",
        "THIRD-PARTY-NOTICES.txt": "third_party_notices",
        "USER-GUIDE.md": "user_guide",
        "COMPLIANCE-HOLD.txt": "compliance_hold",
        "build-info.json": "build_metadata",
        "dependency-manifest.json": "dependency_manifest",
        "release-inventory.json": "release_inventory",
        "sbom.cdx.json": "sbom",
    }
    return mapping.get(relative, "release_metadata")


def generate_payload_manifest(root: Path, build_fingerprint: str) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    tree = hashlib.sha256()
    for relative, path in _iter_payload_files(root):
        if relative == "payload-manifest.json":
            continue
        size = path.stat().st_size
        digest = sha256_file(path)
        entries.append(
            {
                "path": relative,
                "role": payload_role(relative),
                "sha256": digest,
                "size": size,
            }
        )
        tree.update(relative.encode("utf-8"))
        tree.update(b"\0")
        tree.update(str(size).encode("ascii"))
        tree.update(b"\0")
        tree.update(digest.encode("ascii"))
        tree.update(b"\n")
    return {
        "schema_version": SCHEMA_PAYLOAD,
        "build_fingerprint": build_fingerprint,
        "self_excluded_paths": ["payload-manifest.json"],
        "entries": entries,
        "tree_sha256": tree.hexdigest(),
    }


def create_deterministic_zip(
    payload_root: Path,
    archive_path: Path,
    *,
    archive_root: str,
    source_date_epoch: int,
) -> None:
    """Create a deterministic ZIP without filesystem timestamps or ACLs."""

    if not re.fullmatch(r"[A-Za-z0-9._-]+", archive_root):
        raise ReleaseBuildError(f"unsafe ZIP archive root: {archive_root!r}")
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    timestamp = zip_timestamp(source_date_epoch)
    with zipfile.ZipFile(
        archive_path,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
        strict_timestamps=True,
    ) as archive:
        archive.comment = b""
        for relative, path in _iter_payload_files(payload_root):
            name = f"{archive_root}/{relative}"
            info = zipfile.ZipInfo(name, date_time=timestamp)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            mode = 0o100755 if relative.lower().endswith(".exe") else 0o100644
            info.external_attr = mode << 16
            info.internal_attr = 0
            info.extra = b""
            info.comment = b""
            archive.writestr(
                info,
                path.read_bytes(),
                compress_type=zipfile.ZIP_DEFLATED,
                compresslevel=9,
            )


def validate_deterministic_zip(
    archive_path: Path,
    *,
    archive_root: str,
    source_date_epoch: int,
) -> None:
    expected_timestamp = zip_timestamp(source_date_epoch)
    with zipfile.ZipFile(archive_path, "r") as archive:
        if archive.comment:
            raise ReleaseBuildError("release ZIP comment must be empty")
        names = [entry.filename for entry in archive.infolist()]
        if names != sorted(names, key=lambda value: value.encode("utf-8")):
            raise ReleaseBuildError("release ZIP entries are not byte-lexically sorted")
        folded: set[str] = set()
        for entry in archive.infolist():
            path = PurePosixPath(entry.filename)
            if (
                path.is_absolute()
                or ".." in path.parts
                or not entry.filename.startswith(f"{archive_root}/")
                or entry.is_dir()
            ):
                raise ReleaseBuildError(f"unsafe release ZIP entry: {entry.filename}")
            key = entry.filename.casefold()
            if key in folded:
                raise ReleaseBuildError(
                    f"duplicate release ZIP entry: {entry.filename}"
                )
            folded.add(key)
            if entry.date_time != expected_timestamp:
                raise ReleaseBuildError(
                    f"non-deterministic ZIP timestamp: {entry.filename}"
                )
            if entry.extra or entry.comment:
                raise ReleaseBuildError(
                    f"ZIP extra/comment is forbidden: {entry.filename}"
                )
            if entry.compress_type != zipfile.ZIP_DEFLATED:
                raise ReleaseBuildError(f"unexpected ZIP compression: {entry.filename}")


def validate_artifact_payload(
    archive_path: Path,
    *,
    archive_root: str,
    expected_build_fingerprint: str,
) -> None:
    """Re-open the final ZIP and bind every byte to its embedded manifest."""

    manifest_relative = "payload-manifest.json"
    manifest_name = f"{archive_root}/{manifest_relative}"
    build_info_name = f"{archive_root}/build-info.json"
    with zipfile.ZipFile(archive_path, "r") as archive:
        infos = {entry.filename: entry for entry in archive.infolist()}
        manifest_info = infos.get(manifest_name)
        if manifest_info is None or manifest_info.file_size > 4 * 1024 * 1024:
            raise ReleaseBuildError(
                "release ZIP payload manifest is missing or oversized"
            )
        manifest_bytes = archive.read(manifest_info)
        try:
            manifest = json.loads(manifest_bytes.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ReleaseBuildError("release ZIP payload manifest is invalid") from exc
        if (
            not isinstance(manifest, dict)
            or canonical_json_bytes(manifest) != manifest_bytes
            or manifest.get("schema_version") != SCHEMA_PAYLOAD
            or manifest.get("build_fingerprint") != expected_build_fingerprint
            or manifest.get("self_excluded_paths") != [manifest_relative]
        ):
            raise ReleaseBuildError("release ZIP payload manifest contract is invalid")
        raw_entries = manifest.get("entries")
        if not isinstance(raw_entries, list):
            raise ReleaseBuildError("release ZIP payload manifest entries are invalid")

        expected_names = {manifest_name}
        tree = hashlib.sha256()
        prior_path: bytes | None = None
        seen_paths: set[str] = set()
        verified_manifest_entries: dict[str, tuple[int, str]] = {}
        for raw_entry in raw_entries:
            if not isinstance(raw_entry, dict) or set(raw_entry) != {
                "path",
                "role",
                "sha256",
                "size",
            }:
                raise ReleaseBuildError("release ZIP payload manifest entry is invalid")
            relative = _contract_relative_path(
                raw_entry["path"], label="payload manifest path"
            )
            if relative == manifest_relative or relative.casefold() in seen_paths:
                raise ReleaseBuildError(
                    "release ZIP payload manifest path is duplicated"
                )
            seen_paths.add(relative.casefold())
            encoded_path = relative.encode("utf-8")
            if prior_path is not None and encoded_path <= prior_path:
                raise ReleaseBuildError(
                    "release ZIP payload manifest entries are not byte-lexically sorted"
                )
            prior_path = encoded_path
            archive_name = f"{archive_root}/{relative}"
            info = infos.get(archive_name)
            expected_size = raw_entry["size"]
            expected_hash = str(raw_entry["sha256"]).lower()
            if (
                info is None
                or not isinstance(expected_size, int)
                or isinstance(expected_size, bool)
                or expected_size < 0
                or info.file_size != expected_size
                or not re.fullmatch(r"[0-9a-f]{64}", expected_hash)
            ):
                raise ReleaseBuildError(
                    f"release ZIP payload manifest metadata mismatch: {relative}"
                )
            digest = hashlib.sha256()
            with archive.open(info, "r") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            if digest.hexdigest() != expected_hash:
                raise ReleaseBuildError(
                    f"release ZIP payload hash mismatch: {relative}"
                )
            if raw_entry["role"] != payload_role(relative):
                raise ReleaseBuildError(
                    f"release ZIP payload role mismatch: {relative}"
                )
            expected_names.add(archive_name)
            verified_manifest_entries[relative] = (expected_size, expected_hash)
            tree.update(encoded_path)
            tree.update(b"\0")
            tree.update(str(expected_size).encode("ascii"))
            tree.update(b"\0")
            tree.update(expected_hash.encode("ascii"))
            tree.update(b"\n")
        if set(infos) != expected_names:
            raise ReleaseBuildError(
                "release ZIP entry set differs from payload manifest"
            )
        if tree.hexdigest() != str(manifest.get("tree_sha256", "")).lower():
            raise ReleaseBuildError("release ZIP payload tree hash mismatch")

        build_info = infos.get(build_info_name)
        if build_info is None or build_info.file_size > 4 * 1024 * 1024:
            raise ReleaseBuildError("release ZIP build-info is missing or oversized")
        build_bytes = archive.read(build_info)
        try:
            build_document = json.loads(build_bytes.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ReleaseBuildError("release ZIP build-info is invalid") from exc
        if (
            not isinstance(build_document, dict)
            or canonical_json_bytes(build_document) != build_bytes
            or build_document.get("schema_version") != SCHEMA_BUILD_INFO
            or build_document.get("build_fingerprint") != expected_build_fingerprint
        ):
            raise ReleaseBuildError(
                "release ZIP build-info does not match the payload manifest"
            )
        blocker_authority = build_document.get("release_blocker_authority")
        blocker_authority_sha256 = str(
            build_document.get("release_blocker_authority_sha256", "")
        )
        if (
            not isinstance(blocker_authority, list)
            or any(not isinstance(item, Mapping) for item in blocker_authority)
            or not re.fullmatch(r"[0-9a-f]{64}", blocker_authority_sha256)
            or sha256_bytes(canonical_json_bytes(blocker_authority))
            != blocker_authority_sha256
            or [
                str(item.get("id", ""))
                for item in blocker_authority
                if isinstance(item, Mapping)
            ]
            != build_document.get("release_blockers")
        ):
            raise ReleaseBuildError("release ZIP blocker authority binding is invalid")
        hardlink_evidence = build_document.get("conda_hardlink_threat_evidence")
        release_eligible = build_document.get("release_eligible")
        if (
            not isinstance(release_eligible, bool)
            or not isinstance(hardlink_evidence, dict)
            or hardlink_evidence
            != build_document.get("toolchain", {}).get("conda_hardlink_threat_evidence")
            or hardlink_evidence.get("schema_version")
            != "pkv.conda-hardlink-threat-evidence.v1"
            or hardlink_evidence.get("release_eligible_environment_requirement")
            != "copy-only-no-hardlinks"
            or hardlink_evidence.get("threat_model")
            != (
                "copy-only-release-environment"
                if release_eligible
                else "accepted_for_test_candidate"
            )
            or (
                release_eligible
                and hardlink_evidence.get("observed_hardlink_anchor_count") != 0
            )
        ):
            raise ReleaseBuildError("release ZIP hardlink threat evidence is invalid")

        inventory_name = f"{archive_root}/release-inventory.json"
        inventory_info = infos.get(inventory_name)
        if inventory_info is None or inventory_info.file_size > 128 * 1024 * 1024:
            raise ReleaseBuildError("release ZIP inventory is missing or oversized")
        inventory_bytes = archive.read(inventory_info)
        inventory_sha256 = sha256_bytes(inventory_bytes)
        try:
            inventory = json.loads(inventory_bytes.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ReleaseBuildError("release ZIP inventory is invalid") from exc
        if (
            not isinstance(inventory, dict)
            or canonical_json_bytes(inventory) != inventory_bytes
            or inventory.get("schema_version") != "pkv.release-inventory.v1"
            or build_document.get("release_inventory_path") != "release-inventory.json"
            or build_document.get("release_inventory_sha256") != inventory_sha256
            or build_document.get("release_inventory_closure_sha256")
            != inventory.get("bindings", {}).get("closure_sha256")
            or build_document.get("release_inventory_artifact_closure_sha256")
            != inventory.get("bindings", {}).get("artifact_closure_sha256")
        ):
            raise ReleaseBuildError(
                "release ZIP build-info/inventory binding is inconsistent"
            )
        inventory_payload = inventory.get("payload")
        if (
            not isinstance(inventory_payload, dict)
            or inventory_payload.get("path_base") != "app"
            or not isinstance(inventory_payload.get("files"), list)
            or inventory.get("bindings", {}).get("payload_tree_sha256")
            != inventory_payload.get("tree_sha256")
        ):
            raise ReleaseBuildError("release ZIP inventory payload binding is invalid")
        inventory_payload_files: dict[str, tuple[int, str]] = {}
        inventory_tree = hashlib.sha256()
        inventory_tree_rows: list[tuple[str, int, str]] = []
        for raw_file in inventory_payload["files"]:
            if not isinstance(raw_file, dict):
                raise ReleaseBuildError(
                    "release ZIP inventory payload entry is invalid"
                )
            artifact_path = _contract_relative_path(
                raw_file.get("artifact_path"), label="release inventory artifact path"
            )
            if not artifact_path.startswith("app/"):
                raise ReleaseBuildError(
                    "release ZIP inventory payload entry escapes the app root"
                )
            relative_path = _contract_relative_path(
                raw_file.get("path"), label="release inventory payload path"
            )
            if artifact_path != f"app/{relative_path}":
                raise ReleaseBuildError(
                    "release ZIP inventory payload path/base binding is invalid"
                )
            size = raw_file.get("size")
            digest = str(raw_file.get("sha256", ""))
            if (
                artifact_path in inventory_payload_files
                or not isinstance(size, int)
                or isinstance(size, bool)
                or size < 0
                or not re.fullmatch(r"[0-9a-f]{64}", digest)
            ):
                raise ReleaseBuildError(
                    "release ZIP inventory payload entry is invalid"
                )
            inventory_payload_files[artifact_path] = (size, digest)
            inventory_tree_rows.append((relative_path, size, digest))
        for relative_path, size, digest in sorted(
            inventory_tree_rows, key=lambda item: item[0].encode("utf-8")
        ):
            inventory_tree.update(relative_path.encode("utf-8"))
            inventory_tree.update(b"\0")
            inventory_tree.update(str(size).encode("ascii"))
            inventory_tree.update(b"\0")
            inventory_tree.update(digest.encode("ascii"))
            inventory_tree.update(b"\n")
        expected_app_files = {
            relative: details
            for relative, details in verified_manifest_entries.items()
            if relative.startswith("app/")
        }
        if (
            inventory_payload_files != expected_app_files
            or inventory_payload.get("file_count") != len(inventory_payload_files)
            or inventory_tree.hexdigest() != inventory_payload.get("tree_sha256")
        ):
            raise ReleaseBuildError(
                "release ZIP inventory payload files differ from packaged app bytes"
            )
        authority = inventory.get("authority")
        if (
            not isinstance(authority, dict)
            or authority.get("build_fingerprint") != expected_build_fingerprint
            or authority.get("artifact_kind") != build_document.get("artifact_kind")
            or authority.get("artifact_status") != build_document.get("artifact_status")
            or authority.get("release_eligible")
            is not build_document.get("release_eligible")
            or authority.get("release_blockers")
            != build_document.get("release_blockers")
            or authority.get("release_blocker_authority") != blocker_authority
            or authority.get("release_blocker_authority_sha256")
            != blocker_authority_sha256
        ):
            raise ReleaseBuildError("release ZIP inventory authority is inconsistent")
        expected_artifact_closure = sha256_bytes(
            canonical_json_bytes(
                {
                    "authority": authority,
                    "inventory_closure_sha256": inventory["bindings"]["closure_sha256"],
                }
            )
        )
        if (
            inventory["bindings"].get("artifact_closure_sha256")
            != expected_artifact_closure
        ):
            raise ReleaseBuildError("release ZIP inventory artifact closure is invalid")

        sbom_info = infos.get(f"{archive_root}/sbom.cdx.json")
        if sbom_info is None or sbom_info.file_size > 128 * 1024 * 1024:
            raise ReleaseBuildError("release ZIP SBOM is missing or oversized")
        sbom_bytes = archive.read(sbom_info)
        try:
            sbom = json.loads(sbom_bytes.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ReleaseBuildError("release ZIP SBOM is invalid") from exc
        if not isinstance(sbom, dict) or canonical_json_bytes(sbom) != sbom_bytes:
            raise ReleaseBuildError("release ZIP SBOM contract is invalid")

        license_index_info = infos.get(f"{archive_root}/licenses/index.json")
        if (
            license_index_info is None
            or license_index_info.file_size > 128 * 1024 * 1024
        ):
            raise ReleaseBuildError("release ZIP license index is missing or oversized")
        license_index_bytes = archive.read(license_index_info)
        try:
            license_index = json.loads(license_index_bytes.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ReleaseBuildError("release ZIP license index is invalid") from exc
        if (
            not isinstance(license_index, dict)
            or canonical_json_bytes(license_index) != license_index_bytes
        ):
            raise ReleaseBuildError("release ZIP license index contract is invalid")
        raw_sbom_components = sbom.get("components")
        if not isinstance(raw_sbom_components, list):
            raise ReleaseBuildError("release ZIP SBOM component closure is invalid")
        _cross_bind_sbom_components_to_license_index(
            raw_sbom_components,
            inventory=inventory,
            license_index=license_index,
            bind_unresolved_status=False,
        )
        raw_properties = sbom.get("metadata", {}).get("properties", [])
        if not isinstance(raw_properties, list):
            raise ReleaseBuildError("release ZIP SBOM properties are invalid")
        sbom_properties = {
            str(item.get("name")): str(item.get("value"))
            for item in raw_properties
            if isinstance(item, Mapping)
            and item.get("name")
            in {
                "pkv:release-inventory-closure-sha256",
                "pkv:release-inventory-path",
                "pkv:release-inventory-sha256",
                "pkv:release-blocker-authority-sha256",
            }
        }
        if sbom_properties != {
            "pkv:release-inventory-closure-sha256": str(
                inventory["bindings"]["closure_sha256"]
            ),
            "pkv:release-inventory-path": "release-inventory.json",
            "pkv:release-inventory-sha256": inventory_sha256,
            "pkv:release-blocker-authority-sha256": blocker_authority_sha256,
        }:
            raise ReleaseBuildError(
                "release ZIP SBOM/inventory binding is inconsistent"
            )


def _locked_regular_file(path: Path, *, prefix: Path, label: str) -> tuple[str, int]:
    prefix = Path(os.path.abspath(prefix))
    located = Path(os.path.abspath(path))
    try:
        relative = located.relative_to(prefix)
    except ValueError as exc:
        raise ReleaseBuildError(f"{label} escapes the release environment") from exc
    if not os.path.lexists(located):
        raise ReleaseBuildError(f"{label} is missing: {located}")
    cursor = prefix
    for part in relative.parts:
        cursor /= part
        details = cursor.lstat()
        if stat.S_ISLNK(details.st_mode) or _path_is_reparse(cursor, details):
            raise ReleaseBuildError(f"{label} contains a link/reparse point: {cursor}")
    details = located.lstat()
    if not stat.S_ISREG(details.st_mode):
        raise ReleaseBuildError(f"{label} is not a regular file: {located}")
    resolved = located.resolve(strict=True)
    try:
        resolved.relative_to(prefix.resolve(strict=True))
    except ValueError as exc:
        raise ReleaseBuildError(
            f"{label} resolves outside the release environment"
        ) from exc
    return relative.as_posix(), details.st_size


def _file_tree_sha256(rows: Iterable[tuple[str, Path, int]]) -> str:
    digest = hashlib.sha256()
    seen: set[str] = set()
    for relative, path, size in sorted(rows, key=lambda item: item[0].encode("utf-8")):
        if relative.casefold() in seen:
            raise ReleaseBuildError(f"duplicate path in environment tree: {relative}")
        seen.add(relative.casefold())
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(size).encode("ascii"))
        digest.update(b"\0")
        digest.update(sha256_file(path).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _distribution_lock_fingerprint(
    distribution: importlib.metadata.Distribution,
    expected: Mapping[str, Any],
    *,
    prefix: Path,
    strict: bool,
) -> str:
    expected_metadata = expected["metadata_files"]
    metadata_root = Path(os.path.abspath(getattr(distribution, "_path")))
    metadata_sha256 = ""
    for basename in ("METADATA", "RECORD", "direct_url.json", "INSTALLER"):
        locked_file = expected_metadata.get(basename)
        candidate = metadata_root / basename
        exists = os.path.lexists(candidate)
        if locked_file is None:
            if exists:
                raise ReleaseBuildError(
                    f"unexpected distribution metadata file for {distribution.metadata['Name']}: {basename}"
                )
            continue
        if not isinstance(locked_file, Mapping):
            raise ReleaseBuildError(
                "distribution metadata lock entry must be object/null"
            )
        relative, _ = _locked_regular_file(
            candidate,
            prefix=prefix,
            label=f"distribution metadata {distribution.metadata['Name']} {basename}",
        )
        if relative != str(locked_file.get("path")):
            raise ReleaseBuildError(
                f"distribution metadata path differs from lock: {distribution.metadata['Name']} {basename}"
            )
        actual_hash = sha256_file(candidate)
        if actual_hash != str(locked_file.get("sha256", "")).lower():
            raise ReleaseBuildError(
                f"distribution metadata hash mismatch: {distribution.metadata['Name']} {basename}"
            )
        if basename == "METADATA":
            metadata_sha256 = actual_hash
    if strict and not metadata_sha256:
        raise ReleaseBuildError("every distribution must lock its METADATA file")

    raw_files = list(distribution.files or ())
    omitted: list[str] = []
    rows: list[tuple[str, Path, int]] = []
    prefix = Path(os.path.abspath(prefix))
    for package_path in raw_files:
        raw = str(package_path)
        located = Path(os.path.abspath(distribution.locate_file(package_path)))
        try:
            located.relative_to(prefix)
            contained = True
        except ValueError:
            contained = False
        if not contained or not os.path.lexists(located):
            if strict:
                raise ReleaseBuildError(
                    f"locked release distribution file is missing/outside: {distribution.metadata['Name']} {raw}"
                )
            omitted.append(raw)
            continue
        relative, size = _locked_regular_file(
            located,
            prefix=prefix,
            label=f"distribution file {distribution.metadata['Name']} {raw}",
        )
        rows.append((relative, located, size))

    expected_tree = expected["installed_files_tree"]
    omitted_digest = hashlib.sha256()
    for raw in sorted(omitted, key=lambda value: value.encode("utf-8")):
        omitted_digest.update(raw.encode("utf-8"))
        omitted_digest.update(b"\n")
    actual_values = {
        "declared_file_count": len(raw_files),
        "hashed_file_count": len(rows),
        "sha256": _file_tree_sha256(rows),
        "missing_or_outside_path_count": len(omitted),
        "missing_or_outside_paths_sha256": omitted_digest.hexdigest(),
    }
    if actual_values != expected_tree:
        raise ReleaseBuildError(
            f"installed distribution content tree differs from lock: {distribution.metadata['Name']}"
        )
    return metadata_sha256


def _validate_python_runtime(lock: Mapping[str, Any], *, prefix: Path) -> None:
    runtime = lock["python_runtime"]
    executable = runtime["python_executable"]
    if executable.get("path") != "python.exe" or not re.fullmatch(
        r"[0-9a-f]{64}", str(executable.get("sha256", ""))
    ):
        raise ReleaseBuildError("Python executable lock entry is invalid")
    executable_path = prefix / str(executable["path"])
    if executable_path.resolve(strict=True) != Path(sys.executable).resolve(
        strict=True
    ):
        raise ReleaseBuildError("locked Python executable path differs from runtime")
    _locked_regular_file(executable_path, prefix=prefix, label="Python executable")
    if sha256_file(executable_path) != str(executable["sha256"]).lower():
        raise ReleaseBuildError("Python executable hash differs from runtime lock")
    dll = runtime["python311.dll"]
    if dll.get("path") != "python311.dll" or not re.fullmatch(
        r"[0-9a-f]{64}", str(dll.get("sha256", ""))
    ):
        raise ReleaseBuildError("python311.dll lock entry is invalid")
    dll_path = prefix / str(dll["path"])
    _locked_regular_file(dll_path, prefix=prefix, label="python311.dll")
    if sha256_file(dll_path) != str(dll["sha256"]).lower():
        raise ReleaseBuildError("python311.dll hash differs from runtime lock")

    stdlib_lock = runtime["stdlib_tree"]
    stdlib_root = Path(os.path.abspath(sysconfig.get_path("stdlib")))
    purelib_root = Path(os.path.abspath(sysconfig.get_path("purelib")))
    if stdlib_root != prefix / str(stdlib_lock["root"]):
        raise ReleaseBuildError("stdlib root differs from runtime lock")
    expected_exclusions = [(purelib_root.relative_to(prefix)).as_posix()]
    if stdlib_lock["excluded_subtrees"] != expected_exclusions:
        raise ReleaseBuildError("stdlib exclusion set differs from runtime lock")
    rows: list[tuple[str, Path, int]] = []
    for raw_root, directory_names, file_names in os.walk(
        stdlib_root, topdown=True, followlinks=False
    ):
        current = Path(raw_root)
        retained_directories: list[str] = []
        for name in directory_names:
            candidate = current / name
            if candidate == purelib_root:
                continue
            if candidate.is_symlink() or _path_is_reparse(candidate):
                raise ReleaseBuildError(
                    f"stdlib contains a link/reparse point: {candidate}"
                )
            retained_directories.append(name)
        directory_names[:] = retained_directories
        for name in file_names:
            candidate = current / name
            relative, size = _locked_regular_file(
                candidate, prefix=prefix, label="stdlib file"
            )
            rows.append((relative, candidate, size))
    if len(rows) != int(stdlib_lock["file_count"]):
        raise ReleaseBuildError("stdlib file count differs from runtime lock")
    if _file_tree_sha256(rows) != str(stdlib_lock["sha256"]).lower():
        raise ReleaseBuildError("stdlib content tree differs from runtime lock")

    site_packages_lock = runtime["site_packages_tree"]
    site_packages_root = Path(os.path.abspath(sysconfig.get_path("purelib")))
    if (
        site_packages_lock.get("root") != "Lib/site-packages"
        or site_packages_root != prefix / "Lib" / "site-packages"
        or site_packages_lock.get("recursive") is not True
        or site_packages_lock.get("file_selection")
        != (
            "all physical regular files recursively under root, including __pycache__ "
            "directories and .pyc files; links/reparse points, duplicate paths, and "
            "Windows case-insensitive path collisions are rejected"
        )
    ):
        raise ReleaseBuildError("site-packages tree selection policy is invalid")
    site_packages_rows: list[tuple[str, Path, int]] = []
    for raw_root, directory_names, file_names in os.walk(
        site_packages_root, topdown=True, followlinks=False
    ):
        current = Path(raw_root)
        for name in directory_names:
            candidate = current / name
            if candidate.is_symlink() or _path_is_reparse(candidate):
                raise ReleaseBuildError(
                    f"site-packages contains a link/reparse point: {candidate}"
                )
        for name in file_names:
            candidate = current / name
            relative, size = _locked_regular_file(
                candidate, prefix=prefix, label="site-packages file"
            )
            site_packages_rows.append((relative, candidate, size))
    if (
        len(site_packages_rows) != int(site_packages_lock["file_count"])
        or _file_tree_sha256(site_packages_rows)
        != str(site_packages_lock["sha256"]).lower()
    ):
        raise ReleaseBuildError("site-packages content tree differs from runtime lock")

    native = runtime["native_runtime_trees"]
    if native.get("semantics") != (
        "superset build-input lock for native runtime closure discovery; inclusion "
        "does not assert that every locked file is shipped in the installer payload"
    ) or native.get("selection_policy") != (
        "all selected paths are relative to sys.prefix and use "
        "canonical_tree_sha256_spec; missing roots/files, non-regular files, "
        "links/reparse points, duplicate paths, and Windows case-insensitive path "
        "collisions are rejected"
    ):
        raise ReleaseBuildError("native runtime selection policy is invalid")
    for key, expected_root in (("dlls", "DLLs"), ("library_bin", "Library/bin")):
        tree_lock = native[key]
        if (
            tree_lock.get("root") != expected_root
            or tree_lock.get("recursive") is not True
            or tree_lock.get("file_selection")
            != "all recursively installed regular files"
        ):
            raise ReleaseBuildError(
                f"native runtime tree selection policy is invalid: {key}"
            )
        root = prefix / str(tree_lock["root"])
        _assert_safe_directory_chain(root, authority=prefix)
        if not root.is_dir() or tree_lock.get("recursive") is not True:
            raise ReleaseBuildError(f"native runtime tree root is invalid: {key}")
        native_rows: list[tuple[str, Path, int]] = []
        for raw_root, directory_names, file_names in os.walk(
            root, topdown=True, followlinks=False
        ):
            current = Path(raw_root)
            for name in directory_names:
                candidate = current / name
                if candidate.is_symlink() or _path_is_reparse(candidate):
                    raise ReleaseBuildError(
                        f"native runtime tree contains a link/reparse point: {candidate}"
                    )
            for name in file_names:
                candidate = current / name
                relative, size = _locked_regular_file(
                    candidate, prefix=prefix, label=f"native runtime file {key}"
                )
                native_rows.append((relative, candidate, size))
        if (
            len(native_rows) != int(tree_lock["file_count"])
            or _file_tree_sha256(native_rows) != str(tree_lock["sha256"]).lower()
        ):
            raise ReleaseBuildError(f"native runtime tree differs from lock: {key}")

    root_lock = native["prefix_root_dll_pyd"]
    if (
        root_lock.get("root") != "."
        or root_lock.get("recursive") is not False
        or root_lock.get("include_suffixes_case_insensitive") != [".dll", ".pyd"]
        or root_lock.get("excluded_paths") != ["python311.dll"]
    ):
        raise ReleaseBuildError("prefix native runtime selection policy is invalid")
    root_rows: list[tuple[str, Path, int]] = []
    for candidate in prefix.iterdir():
        if candidate.name.casefold() == "python311.dll":
            continue
        if candidate.suffix.casefold() not in {".dll", ".pyd"}:
            continue
        relative, size = _locked_regular_file(
            candidate, prefix=prefix, label="prefix native runtime file"
        )
        root_rows.append((relative, candidate, size))
    if (
        len(root_rows) != int(root_lock["file_count"])
        or _file_tree_sha256(root_rows) != str(root_lock["sha256"]).lower()
    ):
        raise ReleaseBuildError("prefix native runtime tree differs from lock")


def validate_environment_lock(lock: Mapping[str, Any]) -> list[dict[str, Any]]:
    if lock.get("schema_version") != "pkv.release-environment-lock.v2":
        raise ReleaseBuildError("unsupported release environment lock schema")
    if lock.get("target") != "windows-x86_64":
        raise ReleaseBuildError("release environment lock target is unsupported")
    if lock.get("git_tool") != GIT_TOOL_CONTRACT:
        raise ReleaseBuildError("release Git tool lock differs from contract")
    _locked_git_executable()
    if lock.get("canonical_tree_sha256_spec") != {
        "algorithm": "sha256",
        "path_base": "sys.prefix",
        "path_representation": ("forward-slash relative path encoded as UTF-8"),
        "ordering": "ascending lexicographic order of UTF-8 path bytes",
        "entry_framing": (
            "UTF8(path) || NUL || ASCII(decimal byte size) || NUL || "
            "ASCII(lowercase file sha256) || LF"
        ),
        "collision_policy": (
            "duplicate canonical paths and Windows case-insensitive path collisions "
            "are rejected"
        ),
        "release_distribution_selection": (
            "every path declared by importlib.metadata Distribution.files; every "
            "declared path must exist, be prefix-contained, and be a regular "
            "non-link/non-reparse file"
        ),
        "environment_only_selection": (
            "existing prefix-contained regular files declared by Distribution.files; "
            "missing or outside raw Distribution.files paths are separately frozen"
        ),
        "missing_or_outside_ordering": (
            "ascending lexicographic order of raw Distribution.files path UTF-8 bytes"
        ),
        "missing_or_outside_framing": ("UTF8(raw Distribution.files path) || LF"),
        "link_policy": (
            "any existing symlink or Windows reparse point selected or encountered "
            "below sys.prefix is rejected"
        ),
    }:
        raise ReleaseBuildError("release environment tree-hash contract is invalid")
    expected_python = str(lock["python_version"])
    actual_python = ".".join(str(value) for value in sys.version_info[:3])
    if actual_python != expected_python:
        raise ReleaseBuildError(
            f"release Python mismatch: expected {expected_python}, got {actual_python}"
        )
    prefix = Path(os.path.abspath(sys.prefix))
    _validate_python_runtime(lock, prefix=prefix)
    if (
        sha256_file(Path(sys.executable))
        != str(lock["python_executable_sha256"]).lower()
    ):
        raise ReleaseBuildError(
            "release Python executable hash differs from the exact toolchain lock"
        )

    actual_distributions: dict[str, importlib.metadata.Distribution] = {}
    for distribution in importlib.metadata.distributions():
        raw_name = distribution.metadata.get("Name")
        if not raw_name:
            raise ReleaseBuildError("installed distribution has no canonical name")
        normalized = _canonical_distribution_name(str(raw_name))
        if normalized in actual_distributions:
            raise ReleaseBuildError(f"duplicate installed distribution: {normalized}")
        actual_distributions[normalized] = distribution

    release_entries = list(lock["distributions"])
    environment_entries = list(lock["environment_only_distributions"])
    if len(release_entries) != 65 or len(environment_entries) != 26:
        raise ReleaseBuildError("release environment component partition is invalid")
    role_rows = sorted(
        (
            {
                "name": _canonical_distribution_name(str(component["name"])),
                "role": str(component["role"]),
            }
            for component in release_entries
        ),
        key=lambda item: item["name"].encode("utf-8"),
    )
    if (
        any(
            item["role"] not in {"build", "runtime", "operations"} for item in role_rows
        )
        or sha256_bytes(canonical_json_bytes(role_rows))
        != RELEASE_COMPONENT_ROLE_MAP_SHA256
    ):
        raise ReleaseBuildError(
            "release component/role authority differs from contract"
        )
    expected_names: set[str] = set()
    resolved: list[dict[str, Any]] = []
    for component in release_entries:
        name = _canonical_distribution_name(str(component["name"]))
        if name in expected_names:
            raise ReleaseBuildError(f"duplicate distribution in release lock: {name}")
        expected_names.add(name)
        distribution = actual_distributions.get(name)
        if distribution is None:
            raise ReleaseBuildError(f"locked distribution is missing: {name}")
        expected_version = str(component["version"])
        if distribution.version != expected_version:
            raise ReleaseBuildError(
                f"locked distribution mismatch for {name}: expected {expected_version}, got {distribution.version}"
            )
        metadata_sha256 = _distribution_lock_fingerprint(
            distribution, component, prefix=prefix, strict=True
        )
        resolved.append(
            {
                "component_kind": (
                    "build_tool_and_bootloader_runtime"
                    if name == "pyinstaller"
                    else "python_distribution"
                ),
                "installed_files_sha256": str(
                    component["installed_files_tree"]["sha256"]
                ),
                "license": str(component["license"]),
                "metadata_sha256": metadata_sha256,
                "name": name,
                "purl": str(component["purl"]),
                "role": str(component["role"]),
                "version": distribution.version,
            }
        )
    for component in environment_entries:
        name = _canonical_distribution_name(str(component["name"]))
        if component.get("normalized_name") != name or name in expected_names:
            raise ReleaseBuildError(
                f"invalid environment-only distribution lock: {name}"
            )
        expected_names.add(name)
        distribution = actual_distributions.get(name)
        if distribution is None or distribution.version != str(component["version"]):
            raise ReleaseBuildError(
                f"environment-only distribution differs from lock: {name}"
            )
        _distribution_lock_fingerprint(
            distribution, component, prefix=prefix, strict=False
        )

    inventory = lock["distribution_inventory"]
    if int(inventory["release_distribution_count"]) != len(release_entries) or int(
        inventory["environment_only_distribution_count"]
    ) != len(environment_entries):
        raise ReleaseBuildError("release distribution inventory counts are invalid")
    if set(actual_distributions) != expected_names:
        missing = sorted(expected_names - set(actual_distributions))
        extra = sorted(set(actual_distributions) - expected_names)
        raise ReleaseBuildError(
            f"release environment distribution set differs from lock; missing={missing}, extra={extra}"
        )

    expected_bootloader_names = {"run.exe", "runw.exe"}
    bootloader_lock = lock["pyinstaller_bootloaders"]
    if set(bootloader_lock) != expected_bootloader_names or any(
        not re.fullmatch(r"[0-9a-f]{64}", str(value))
        for value in bootloader_lock.values()
    ):
        raise ReleaseBuildError(
            "PyInstaller bootloader lock must contain exact run/runw hashes"
        )
    pyinstaller_distribution = actual_distributions["pyinstaller"]
    bootloader_root = Path(
        pyinstaller_distribution.locate_file(
            "PyInstaller/bootloader/Windows-64bit-intel"
        )
    )
    for filename, expected_hash in sorted(bootloader_lock.items()):
        bootloader = bootloader_root / filename
        _locked_regular_file(bootloader, prefix=prefix, label="PyInstaller bootloader")
        validate_x86_64_pe(bootloader)
        if sha256_file(bootloader) != str(expected_hash).lower():
            raise ReleaseBuildError(
                f"PyInstaller bootloader hash differs from lock: {filename}"
            )
    runtime_fingerprint = sha256_bytes(canonical_json_bytes(lock["python_runtime"]))
    resolved.append(
        {
            "component_kind": "platform_runtime",
            "installed_files_sha256": runtime_fingerprint,
            "license": "Python-2.0",
            "metadata_sha256": sha256_file(Path(sys.executable)),
            "name": "cpython",
            "purl": f"pkg:generic/cpython@{actual_python}",
            "role": "runtime",
            "version": actual_python,
        }
    )
    return sorted(resolved, key=lambda item: (item["name"], item["version"]))


def make_dependency_manifest(
    components: Sequence[Mapping[str, Any]],
    lock_sha256: str,
    *,
    compliance: Mapping[str, Any],
    inventory: Mapping[str, Any],
    inventory_sha256: str,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_DEPENDENCIES,
        "artifact_status": compliance["artifact_status"],
        "release_eligible": compliance["release_eligible"],
        "release_blocker_authority": list(compliance["release_blocker_authority"]),
        "release_blocker_authority_sha256": compliance[
            "release_blocker_authority_sha256"
        ],
        "release_blockers": list(compliance["release_blockers"]),
        "environment_lock_sha256": lock_sha256,
        "license_index_path": "licenses/index.json",
        "release_inventory_closure_sha256": inventory["bindings"]["closure_sha256"],
        "release_inventory_path": "release-inventory.json",
        "release_inventory_sha256": inventory_sha256,
        "components": [dict(component) for component in components],
    }


def collect_license_materials(
    components: Sequence[Mapping[str, Any]], destination: Path, *, project_root: Path
) -> dict[str, Any]:
    """Copy distribution-provided license/notice files into the payload."""

    destination.mkdir(parents=True, exist_ok=True)
    override_contract = _load_json(
        project_root / "packaging" / "license-overrides.v1.json"
    )
    if override_contract.get("schema_version") != "pkv.license-overrides.v1":
        raise ReleaseBuildError("unsupported license override contract schema")
    overrides: dict[tuple[str, str], Mapping[str, Any]] = {}
    validate_compliance_sources(project_root)
    compliance_contract = _load_json(
        project_root / "packaging" / "compliance-sources.v1.json"
    )
    compliance_artifacts = {
        str(item["id"]): item for item in compliance_contract["artifacts"]
    }
    for override in override_contract.get("entries", []):
        key = (
            _canonical_distribution_name(str(override["name"])),
            str(override["version"]),
        )
        if key in overrides:
            raise ReleaseBuildError(f"duplicate license override: {key}")
        overrides[key] = override
    used_overrides: set[tuple[str, str]] = set()
    entries: list[dict[str, Any]] = []
    for component in components:
        name = str(component["name"])
        distribution = (
            None if name == "cpython" else importlib.metadata.distribution(name)
        )
        distribution_files = list(distribution.files or ()) if distribution else []
        file_index = {
            PurePosixPath(str(raw_relative).replace("\\", "/")).as_posix(): raw_relative
            for raw_relative in distribution_files
        }
        candidates: dict[str, tuple[Path, bool]] = {}
        metadata_root_name = (
            Path(getattr(distribution, "_path")).name if distribution else ""
        )
        declared_license_files = (
            distribution.metadata.get_all("License-File") or [] if distribution else []
        )
        for raw_header in declared_license_files:
            header = _contract_relative_path(
                str(raw_header).replace("\\", "/"),
                label=f"License-File header for {name}",
            )
            possible_paths = (
                f"{metadata_root_name}/licenses/{header}",
                f"{metadata_root_name}/{header}",
            )
            matches = [value for value in possible_paths if value in file_index]
            if len(matches) != 1:
                raise ReleaseBuildError(
                    f"declared License-File is missing or ambiguous for {name}: {header}"
                )
            relative = matches[0]
            assert distribution is not None
            source = Path(distribution.locate_file(file_index[relative]))
            if not source.is_file():
                raise ReleaseBuildError(
                    f"declared License-File is not a regular file for {name}: {header}"
                )
            candidates[relative] = (source, True)

        for raw_relative in distribution_files:
            relative = PurePosixPath(str(raw_relative).replace("\\", "/"))
            if relative.is_absolute() or ".." in relative.parts:
                continue
            basename = relative.name.casefold()
            if not basename.startswith(
                ("license", "licence", "copying", "notice", "authors")
            ):
                continue
            assert distribution is not None
            source = Path(distribution.locate_file(raw_relative))
            if source.is_file():
                candidates.setdefault(relative.as_posix(), (source, False))
        used: set[str] = set()
        license_files: list[dict[str, Any]] = []
        for relative, (source, declared_by_metadata) in sorted(
            candidates.items(), key=lambda item: item[0].encode("utf-8")
        ):
            basename = PurePosixPath(relative).name
            key = basename.casefold()
            if key in used:
                stem = Path(basename).stem
                suffix = Path(basename).suffix
                short_hash = sha256_bytes(relative.encode("utf-8"))[:12]
                basename = f"{stem}-{short_hash}{suffix}"
                key = basename.casefold()
            used.add(key)
            target = destination / name / basename
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            license_files.append(
                {
                    "path": target.relative_to(destination.parent).as_posix(),
                    "sha256": sha256_file(target),
                    "source_kind": "distribution",
                    "declared_by_metadata": declared_by_metadata,
                    "source_distribution_path": relative,
                    "source_url": None,
                }
            )
        extra_asset_ids: tuple[str, ...] = ()
        if name == "cpython":
            extra_asset_ids = ("cpython-3.11.15-license",)
        elif name == "pyinstaller":
            extra_asset_ids = ("pyinstaller-6.21.0-copying-and-bootloader-exception",)
        elif name in {
            "pyside6",
            "pyside6-addons",
            "pyside6-essentials",
            "shiboken6",
        }:
            extra_asset_ids = (
                "pyside6-6.11.1-license-selection",
                "qt-pyside6-6.11.1-lgpl-3.0-only",
                "qt-pyside6-6.11.1-gpl-3.0-only",
            )
        for asset_id in extra_asset_ids:
            artifact = compliance_artifacts[asset_id]
            source_relative = _contract_relative_path(
                artifact["path"], label="compliance license asset path"
            )
            source = project_root / source_relative
            basename = source.name
            key = basename.casefold()
            if key in used:
                raise ReleaseBuildError(
                    f"duplicate compliance license basename for {name}: {basename}"
                )
            used.add(key)
            target = destination / name / basename
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            license_files.append(
                {
                    "declared_by_metadata": False,
                    "path": target.relative_to(destination.parent).as_posix(),
                    "sha256": sha256_file(target),
                    "source_distribution_path": None,
                    "source_kind": "compliance_asset",
                    "source_revision": artifact.get("source_revision"),
                    "source_sha256": artifact.get("source_sha256"),
                    "source_url": artifact.get("source_url"),
                }
            )
        if not license_files:
            override_key = (name, str(component["version"]))
            override = overrides.get(override_key)
            if override is None:
                raise ReleaseBuildError(
                    f"locked dependency provides no distributable license material: {name}"
                )
            if str(override["license_expression"]) != str(component["license"]):
                raise ReleaseBuildError(f"license override expression mismatch: {name}")
            relative_override = _contract_relative_path(
                override["path"], label="license override path"
            )
            if not relative_override.startswith("packaging/licenses/"):
                raise ReleaseBuildError(
                    "license override must be under packaging/licenses"
                )
            override_source = project_root / relative_override
            expected_hash = str(override["sha256"]).lower()
            if (
                not override_source.is_file()
                or sha256_file(override_source) != expected_hash
            ):
                raise ReleaseBuildError(f"license override hash mismatch: {name}")
            source_url = str(override.get("source_url", ""))
            source_revision = str(override.get("source_revision", ""))
            source_sha256 = str(override.get("source_sha256", "")).lower()
            normalization = str(override.get("vendored_normalization", ""))
            audit_reason = str(override.get("audit_reason", ""))
            if (
                not re.fullmatch(r"[0-9a-f]{40}", source_revision)
                or source_revision not in source_url
                or not re.fullmatch(r"[0-9a-f]{64}", source_sha256)
                or normalization
                != "one trailing LF appended; all upstream content bytes otherwise unchanged"
                or not audit_reason
            ):
                raise ReleaseBuildError(
                    f"license override provenance is incomplete: {name}"
                )
            target = destination / name / Path(relative_override).name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(override_source, target)
            license_files.append(
                {
                    "audit_reason": audit_reason,
                    "path": target.relative_to(destination.parent).as_posix(),
                    "sha256": sha256_file(target),
                    "source_distribution_path": None,
                    "source_kind": "audited_override",
                    "source_revision": source_revision,
                    "source_sha256": source_sha256,
                    "source_url": source_url,
                    "vendored_normalization": normalization,
                }
            )
            used_overrides.add(override_key)
        entry: dict[str, Any] = {
            "license_expression": str(component["license"]),
            "license_files": license_files,
            "metadata_declared_license_files": sorted(
                (str(value) for value in declared_license_files),
                key=lambda value: value.encode("utf-8"),
            ),
            "name": name,
            "purl": str(component["purl"]),
            "version": str(component["version"]),
        }
        if name == "html2text":
            source_artifact = compliance_artifacts["html2text-2020.1.16-sdist"]
            entry["corresponding_source"] = {
                "distribution_path": (
                    "dist/compliance-sources/html2text-2020.1.16.tar.gz"
                ),
                "sha256": source_artifact["sha256"],
                "size": source_artifact["size"],
                "source_url": source_artifact["source_url"],
            }
        entries.append(entry)
    if used_overrides != set(overrides):
        raise ReleaseBuildError("license override contract contains an unused entry")
    entries.sort(key=lambda item: (item["name"], item["version"]))
    index = {
        "schema_version": "pkv.license-index.v1",
        "entries": entries,
    }
    write_canonical_json(destination / "index.json", index)
    return index


def _inventory_native_component_ids(inventory: Mapping[str, Any]) -> set[str]:
    native_kinds = {"BINARY", "EXECUTABLE", "EXTENSION"}
    native_suffixes = (".dll", ".dylib", ".exe", ".pyd", ".so")
    native_components: set[str] = set()

    def component_ids(record: Mapping[str, Any], *, label: str) -> list[str]:
        raw_ids = record.get("component_ids")
        if (
            not isinstance(raw_ids, list)
            or any(type(value) is not str or not value for value in raw_ids)
            or raw_ids != sorted(set(raw_ids), key=lambda value: value.encode("utf-8"))
        ):
            raise ReleaseBuildError(f"{label} component binding is invalid")
        return list(raw_ids)

    payload = inventory.get("payload")
    raw_payload_files = payload.get("files") if isinstance(payload, Mapping) else None
    if not isinstance(raw_payload_files, list):
        raise ReleaseBuildError("release inventory payload file closure is invalid")
    for raw_file in raw_payload_files:
        if not isinstance(raw_file, Mapping):
            raise ReleaseBuildError("release inventory payload file entry is invalid")
        identifiers = component_ids(raw_file, label="payload file")
        kind = str(raw_file.get("kind", ""))
        logical_path = str(
            raw_file.get("toc_destination")
            or raw_file.get("artifact_path")
            or raw_file.get("path")
            or ""
        ).casefold()
        if kind == "PYINSTALLER_BOOTLOADER_EXECUTABLE":
            native_components.add("build-runtime:pyinstaller-bootloader")
        elif kind in native_kinds or logical_path.endswith(native_suffixes):
            native_components.update(identifiers)

    raw_archives = inventory.get("embedded_archives")
    if not isinstance(raw_archives, list) or not raw_archives:
        raise ReleaseBuildError("release inventory embedded archive closure is invalid")
    for raw_archive in raw_archives:
        raw_entries = (
            raw_archive.get("entries") if isinstance(raw_archive, Mapping) else None
        )
        if not isinstance(raw_entries, list) or not raw_entries:
            raise ReleaseBuildError(
                "release inventory embedded archive entry set is invalid"
            )
        for raw_entry in raw_entries:
            if not isinstance(raw_entry, Mapping):
                raise ReleaseBuildError(
                    "release inventory embedded archive entry is invalid"
                )
            identifiers = component_ids(raw_entry, label="embedded archive entry")
            kind = str(raw_entry.get("kind", ""))
            logical_path = str(raw_entry.get("name", "")).casefold()
            if kind in native_kinds or logical_path.endswith(native_suffixes):
                native_components.update(identifiers)
    return native_components


def _inventory_sbom_components(
    inventory: Mapping[str, Any],
    locked_components: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    locked_by_name = {
        _canonical_distribution_name(str(item["name"])): item
        for item in locked_components
    }
    static_licenses = {
        "application:project": "MIT",
        "build-runtime:pyinstaller-bootloader": (
            "GPL-2.0-or-later WITH Bootloader-exception"
        ),
        "build-runtime:pyinstaller-hooks": "Apache-2.0",
        "build-runtime:pyinstaller-hooks-contrib": "Apache-2.0",
        "framework:qt-pyside": "LGPL-3.0-only",
        "native:msvc-runtime": "LicenseRef-MicrosoftVisualCpp2015-2022Runtime",
        "native:openssl": "Apache-2.0",
        "native:sqlite": "LicenseRef-SQLite-Public-Domain",
        "native:zlib": "Zlib",
        "runtime:cpython": "Python-2.0",
    }
    raw_components = inventory.get("components")
    if not isinstance(raw_components, list) or not raw_components:
        raise ReleaseBuildError("release inventory component closure is empty")
    native_component_ids = _inventory_native_component_ids(inventory)
    classification_component_ids: set[str] = set()
    for raw_component in raw_components:
        if not isinstance(raw_component, Mapping):
            raise ReleaseBuildError("release inventory component entry is invalid")
        if raw_component.get("identity_status") != "classification-only":
            continue
        classification_id = str(raw_component.get("id", ""))
        if not classification_id:
            raise ReleaseBuildError("release inventory classification id is invalid")
        classification_component_ids.add(classification_id)
    result: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for raw_component in raw_components:
        if not isinstance(raw_component, Mapping):
            raise ReleaseBuildError("release inventory component entry is invalid")
        component_id = str(raw_component.get("id", ""))
        if not component_id or component_id in seen_ids:
            raise ReleaseBuildError("release inventory component id is invalid")
        seen_ids.add(component_id)
        identity_status = str(raw_component.get("identity_status", ""))
        if identity_status == "classification-only":
            continue
        if identity_status != "complete":
            raise ReleaseBuildError(
                f"release inventory component identity is unresolved: {component_id}"
            )
        contains_native_payload = raw_component.get("contains_native_payload")
        if type(contains_native_payload) is not bool or contains_native_payload != (
            component_id in native_component_ids
        ):
            raise ReleaseBuildError(
                f"release inventory native-payload binding is invalid: {component_id}"
            )
        if component_id == "application:project":
            continue

        locked: Mapping[str, Any] | None = None
        if component_id.startswith("python-distribution:"):
            locked = locked_by_name.get(
                _canonical_distribution_name(component_id.split(":", 1)[1])
            )
        elif component_id == "runtime:cpython":
            locked = locked_by_name.get("cpython")
        elif component_id in {
            "build-runtime:pyinstaller-bootloader",
            "build-runtime:pyinstaller-hooks",
        }:
            locked = locked_by_name.get("pyinstaller")
        elif component_id == "build-runtime:pyinstaller-hooks-contrib":
            locked = locked_by_name.get("pyinstaller-hooks-contrib")

        name = str(raw_component.get("name", "")).strip()
        version_value = raw_component.get("version")
        version = str(version_value).strip() if version_value is not None else ""
        license_expression = str(
            raw_component.get("license") or raw_component.get("declared_license") or ""
        ).strip()
        purl = str(raw_component.get("purl", "")).strip()
        if locked is not None:
            if component_id.startswith("build-runtime:pyinstaller-"):
                locked_version = str(locked["version"])
                if version and version != locked_version:
                    raise ReleaseBuildError(
                        f"PyInstaller runtime component version drift: {component_id}"
                    )
                version = locked_version
                license_expression = static_licenses[component_id]
                purl = "pkg:generic/" + component_id.replace(":", "-") + f"@{version}"
            else:
                name = str(locked["name"])
                version = str(locked["version"])
                license_expression = str(locked["license"])
                purl = str(locked["purl"])
        elif component_id.startswith("conda-package:") and version:
            build = str(raw_component.get("build", ""))
            subdir = str(raw_component.get("subdir", ""))
            if not build or subdir != "win-64":
                raise ReleaseBuildError(
                    f"Conda inventory component lacks build/subdir: {component_id}"
                )
            purl = f"pkg:conda/{name}@{version}?build={build}&subdir={subdir}"
        if not license_expression:
            license_expression = static_licenses.get(component_id, "")
        if (
            not name
            or not license_expression
            or (identity_status == "complete" and not version)
        ):
            raise ReleaseBuildError(
                f"release inventory component lacks SBOM identity/license: {component_id}"
            )
        if not purl and version:
            generic_name = re.sub(r"[^a-z0-9._-]+", "-", name.casefold()).strip("-")
            if not generic_name:
                raise ReleaseBuildError(
                    f"release inventory component lacks a stable purl: {component_id}"
                )
            purl = f"pkg:generic/{generic_name}@{version}"
        component_hash = sha256_bytes(canonical_json_bytes(dict(raw_component)))
        bom_ref = f"urn:pkv:release-inventory-component:{component_hash}"
        raw_classifications = raw_component.get("classification_ids")
        if (
            not isinstance(raw_classifications, list)
            or raw_classifications
            != sorted(
                set(raw_classifications), key=lambda value: str(value).encode("utf-8")
            )
            or any(
                type(value) is not str or value not in classification_component_ids
                for value in raw_classifications
            )
        ):
            raise ReleaseBuildError(
                f"release inventory classification binding is invalid: {component_id}"
            )
        classifications = list(raw_classifications)
        if component_id.startswith("conda-package:"):
            license_material_status = "metadata-only-compliance-hold"
        elif contains_native_payload or set(classifications) & {
            "framework:qt-pyside",
            "native:msvc-runtime",
        }:
            license_material_status = "top-level-only-compliance-hold"
        else:
            license_material_status = "requires-license-index-binding"
        properties = [
            {"name": "pkv:inventory-component-id", "value": component_id},
            {"name": "pkv:inventory-component-sha256", "value": component_hash},
            {
                "name": "pkv:inventory-identity-status",
                "value": str(raw_component["identity_status"]),
            },
            {
                "name": "pkv:contains-native-payload",
                "value": "true" if contains_native_payload else "false",
            },
            {
                "name": "pkv:license-material-status",
                "value": license_material_status,
            },
            *[
                {"name": "pkv:payload-path", "value": str(path)}
                for path in raw_component.get("payload_paths", [])
            ],
            *[
                {"name": "pkv:embedded-path", "value": str(path)}
                for path in raw_component.get("embedded_paths", [])
            ],
            *[
                {"name": "pkv:payload-classification", "value": classification}
                for classification in classifications
            ],
            *(
                [
                    {
                        "name": "pkv:license-expression-status",
                        "value": "requires-legal-confirmation",
                    }
                ]
                if component_id == "python-distribution:html2text"
                else []
            ),
            *[
                {"name": f"pkv:conda-{key.replace('_', '-')}", "value": str(value)}
                for key in (
                    "build",
                    "build_number",
                    "channel",
                    "package_sha256",
                    "record_file",
                    "record_sha256",
                    "subdir",
                )
                if (value := raw_component.get(key)) is not None
            ],
        ]
        sbom_component = {
            "bom-ref": bom_ref,
            "licenses": [
                (
                    {"license": {"name": license_expression}}
                    if component_id.startswith("conda-package:")
                    else {"expression": license_expression}
                )
            ],
            "name": name,
            "properties": properties,
            "type": (
                "framework" if raw_component.get("type") == "framework" else "library"
            ),
        }
        if purl:
            sbom_component["purl"] = purl
        if version:
            sbom_component["version"] = version
        result.append(sbom_component)
    result.sort(
        key=lambda item: next(
            property_["value"]
            for property_ in item["properties"]
            if property_["name"] == "pkv:inventory-component-id"
        ).encode("utf-8")
    )
    return result


def _cross_bind_sbom_components_to_license_index(
    sbom_components: Sequence[Mapping[str, Any]],
    *,
    inventory: Mapping[str, Any],
    license_index: Mapping[str, Any],
    bind_unresolved_status: bool,
) -> list[dict[str, Any]]:
    """Cross-bind SBOM identities to the inventory-backed license index.

    ``_inventory_sbom_components`` must run before the license collector has
    decided whether ordinary (non-held) components have usable material.  The
    v2 index is the authority for that final status.  This helper is shared by
    product and harness generation, while final ZIP validation calls it with
    ``bind_unresolved_status=False`` so a stale placeholder cannot pass.
    """

    actual_runtime = license_index.get("actual_runtime_inventory")
    raw_index_components = (
        actual_runtime.get("components")
        if isinstance(actual_runtime, Mapping)
        else None
    )
    inventory_bindings = inventory.get("bindings")
    if (
        license_index.get("schema_version") != "pkv.license-index.v2"
        or not isinstance(actual_runtime, Mapping)
        or not isinstance(raw_index_components, list)
        or not isinstance(inventory_bindings, Mapping)
        or actual_runtime.get("release_inventory_path") != "release-inventory.json"
        or actual_runtime.get("release_inventory_closure_sha256")
        != inventory_bindings.get("closure_sha256")
    ):
        raise ReleaseBuildError("license index runtime inventory binding is invalid")

    raw_inventory_components = inventory.get("components")
    if not isinstance(raw_inventory_components, list):
        raise ReleaseBuildError("release inventory component closure is invalid")
    inventory_components: dict[str, Mapping[str, Any]] = {}
    for raw_component in raw_inventory_components:
        if not isinstance(raw_component, Mapping):
            raise ReleaseBuildError("release inventory component entry is invalid")
        component_id = str(raw_component.get("id", ""))
        if raw_component.get("identity_status") != "complete" or component_id == (
            "application:project"
        ):
            continue
        if not component_id or component_id in inventory_components:
            raise ReleaseBuildError("release inventory component id is invalid")
        inventory_components[component_id] = raw_component

    allowed_statuses = {
        "bound",
        "metadata-only-compliance-hold",
        "top-level-only-compliance-hold",
    }
    indexed_components: dict[str, Mapping[str, Any]] = {}
    index_order: list[str] = []
    for raw_component in raw_index_components:
        if not isinstance(raw_component, Mapping):
            raise ReleaseBuildError("license index runtime component is invalid")
        component_id = str(raw_component.get("component_id", ""))
        if not component_id or component_id in indexed_components:
            raise ReleaseBuildError("license index runtime component id is invalid")
        indexed_components[component_id] = raw_component
        index_order.append(component_id)
    if index_order != sorted(index_order, key=lambda value: value.encode("utf-8")):
        raise ReleaseBuildError("license index runtime components are not sorted")
    if set(indexed_components) != set(inventory_components):
        raise ReleaseBuildError(
            "license index runtime component set differs from release inventory"
        )

    result: list[dict[str, Any]] = []
    seen_sbom_ids: set[str] = set()
    sbom_order: list[str] = []
    for raw_sbom_component in sbom_components:
        if not isinstance(raw_sbom_component, Mapping):
            raise ReleaseBuildError("SBOM runtime component is invalid")
        raw_properties = raw_sbom_component.get("properties")
        if not isinstance(raw_properties, list):
            raise ReleaseBuildError("SBOM runtime component properties are invalid")
        property_values: dict[str, list[str]] = {}
        copied_properties: list[dict[str, Any]] = []
        for raw_property in raw_properties:
            if (
                not isinstance(raw_property, Mapping)
                or set(raw_property) != {"name", "value"}
                or type(raw_property.get("name")) is not str
                or type(raw_property.get("value")) is not str
            ):
                raise ReleaseBuildError("SBOM runtime component property is invalid")
            name = str(raw_property["name"])
            value = str(raw_property["value"])
            property_values.setdefault(name, []).append(value)
            copied_properties.append({"name": name, "value": value})

        def single_property(name: str) -> str:
            values = property_values.get(name, [])
            if len(values) != 1:
                raise ReleaseBuildError(
                    f"SBOM runtime component property is not singular: {name}"
                )
            return values[0]

        component_id = single_property("pkv:inventory-component-id")
        if component_id in seen_sbom_ids:
            raise ReleaseBuildError("SBOM runtime component id is duplicated")
        seen_sbom_ids.add(component_id)
        sbom_order.append(component_id)
        indexed = indexed_components.get(component_id)
        inventory_component = inventory_components.get(component_id)
        if indexed is None or inventory_component is None:
            raise ReleaseBuildError(
                "SBOM runtime component set differs from license index"
            )

        component_sha256 = sha256_bytes(canonical_json_bytes(dict(inventory_component)))
        expected_paths = {
            "classifications": list(inventory_component.get("classification_ids", [])),
            "embedded_paths": list(inventory_component.get("embedded_paths", [])),
            "payload_paths": list(inventory_component.get("payload_paths", [])),
            "source_paths": list(inventory_component.get("source_paths", [])),
        }
        expected_type = (
            "framework" if inventory_component.get("type") == "framework" else "library"
        )
        contains_native_payload = inventory_component.get("contains_native_payload")
        if type(contains_native_payload) is not bool:
            raise ReleaseBuildError(
                f"release inventory native-payload flag is invalid: {component_id}"
            )
        if (
            indexed.get("component_sha256") != component_sha256
            or single_property("pkv:inventory-component-sha256") != component_sha256
            or single_property("pkv:inventory-identity-status") != "complete"
            or single_property("pkv:contains-native-payload")
            != ("true" if contains_native_payload else "false")
            or raw_sbom_component.get("bom-ref")
            != f"urn:pkv:release-inventory-component:{component_sha256}"
            or raw_sbom_component.get("type") != expected_type
            or indexed.get("classifications") != expected_paths["classifications"]
            or indexed.get("embedded_paths") != expected_paths["embedded_paths"]
            or indexed.get("payload_paths") != expected_paths["payload_paths"]
            or indexed.get("source_paths") != expected_paths["source_paths"]
            or property_values.get("pkv:payload-classification", [])
            != expected_paths["classifications"]
            or property_values.get("pkv:embedded-path", [])
            != expected_paths["embedded_paths"]
            or property_values.get("pkv:payload-path", [])
            != expected_paths["payload_paths"]
        ):
            raise ReleaseBuildError(
                f"SBOM/license-index inventory binding is invalid: {component_id}"
            )

        raw_licenses = raw_sbom_component.get("licenses")
        if (
            not isinstance(raw_licenses, list)
            or len(raw_licenses) != 1
            or not isinstance(raw_licenses[0], Mapping)
            or indexed.get("license") != dict(raw_licenses[0])
            or indexed.get("name") != raw_sbom_component.get("name")
        ):
            raise ReleaseBuildError(
                f"SBOM/license-index identity is invalid: {component_id}"
            )
        for key in ("purl", "version"):
            if (key in indexed) != (key in raw_sbom_component) or (
                key in indexed and indexed[key] != raw_sbom_component[key]
            ):
                raise ReleaseBuildError(
                    f"SBOM/license-index identity is invalid: {component_id}"
                )
        license_files = indexed.get("license_files")
        final_status = indexed.get("license_material_status")
        classifications = expected_paths["classifications"]
        if component_id.startswith("conda-package:"):
            provisional_status = "metadata-only-compliance-hold"
        elif contains_native_payload or set(classifications) & {
            "framework:qt-pyside",
            "native:msvc-runtime",
        }:
            provisional_status = "top-level-only-compliance-hold"
        else:
            provisional_status = "requires-license-index-binding"
        expected_final_status = (
            provisional_status
            if provisional_status.endswith("compliance-hold")
            else ("bound" if license_files else "metadata-only-compliance-hold")
        )
        if (
            not isinstance(license_files, list)
            or final_status not in allowed_statuses
            or final_status != expected_final_status
        ):
            raise ReleaseBuildError(
                f"license index material status is invalid: {component_id}"
            )
        current_status = single_property("pkv:license-material-status")
        if bind_unresolved_status:
            valid_status = current_status == provisional_status
        else:
            valid_status = current_status == expected_final_status
        if not valid_status:
            raise ReleaseBuildError(
                f"SBOM/license-index material status is inconsistent: {component_id}"
            )
        for property_ in copied_properties:
            if property_["name"] == "pkv:license-material-status":
                property_["value"] = str(final_status)
        copied_component = dict(raw_sbom_component)
        copied_component["properties"] = copied_properties
        result.append(copied_component)

    if sbom_order != sorted(
        sbom_order, key=lambda value: value.encode("utf-8")
    ) or seen_sbom_ids != set(indexed_components):
        raise ReleaseBuildError("SBOM runtime component set/order is invalid")
    return result


def make_sbom(
    components: Sequence[Mapping[str, Any]],
    *,
    version: str,
    source_date_epoch: int,
    compliance: Mapping[str, Any],
    inventory: Mapping[str, Any],
    inventory_sha256: str,
    license_index: Mapping[str, Any],
) -> dict[str, Any]:
    if not re.fullmatch(r"[0-9a-f]{64}", inventory_sha256):
        raise ReleaseBuildError("release inventory hash is invalid")
    inventory_components = _cross_bind_sbom_components_to_license_index(
        _inventory_sbom_components(inventory, components),
        inventory=inventory,
        license_index=license_index,
        bind_unresolved_status=True,
    )
    timestamp = (
        datetime.fromtimestamp(max(source_date_epoch, 0), tz=timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )

    application_ref = f"pkg:generic/personal-knowledge-vault@{version}"
    return {
        "bomFormat": SCHEMA_SBOM,
        "specVersion": "1.5",
        "version": 1,
        "metadata": {
            "timestamp": timestamp,
            "component": {
                "bom-ref": application_ref,
                "name": "Personal Knowledge Vault",
                "type": "application",
                "version": version,
            },
            "properties": [
                {
                    "name": "pkv:artifact-status",
                    "value": str(compliance["artifact_status"]),
                },
                {
                    "name": "pkv:compliance-manifest-sha256",
                    "value": str(compliance["compliance_manifest_sha256"]),
                },
                {
                    "name": "pkv:release-eligible",
                    "value": str(bool(compliance["release_eligible"])).lower(),
                },
                {
                    "name": "pkv:release-blocker-authority-sha256",
                    "value": str(compliance["release_blocker_authority_sha256"]),
                },
                {
                    "name": "pkv:release-inventory-closure-sha256",
                    "value": str(inventory["bindings"]["closure_sha256"]),
                },
                {
                    "name": "pkv:release-inventory-path",
                    "value": "release-inventory.json",
                },
                {
                    "name": "pkv:release-inventory-sha256",
                    "value": inventory_sha256,
                },
                *[
                    {"name": "pkv:release-blocker", "value": str(identifier)}
                    for identifier in compliance["release_blockers"]
                ],
            ],
        },
        "components": inventory_components,
        "dependencies": [
            {
                "ref": application_ref,
                "dependsOn": [item["bom-ref"] for item in inventory_components],
            }
        ],
    }


def _bind_license_index_to_inventory(
    index_path: Path,
    *,
    inventory: Mapping[str, Any],
    locked_components: Sequence[Mapping[str, Any]],
    release_eligible: bool,
) -> dict[str, Any]:
    index = _load_json(index_path)
    if index.get("schema_version") != "pkv.license-index.v1" or not isinstance(
        index.get("entries"), list
    ):
        raise ReleaseBuildError("dependency license index is invalid")
    dependency_entries = list(index["entries"])
    materials_by_name = {
        _canonical_distribution_name(str(item["name"])): item
        for item in dependency_entries
        if isinstance(item, Mapping) and item.get("name")
    }
    inventory_records = {
        str(item["id"]): item
        for item in inventory["components"]
        if isinstance(item, Mapping) and item.get("id")
    }
    actual_components: list[dict[str, Any]] = []
    for sbom_component in _inventory_sbom_components(inventory, locked_components):
        properties = {
            str(item["name"]): str(item["value"])
            for item in sbom_component["properties"]
            if isinstance(item, Mapping)
            and set(item) == {"name", "value"}
            and not str(item["name"]).startswith("pkv:payload-")
        }
        component_id = properties["pkv:inventory-component-id"]
        record = inventory_records[component_id]
        material_name = _canonical_distribution_name(str(sbom_component["name"]))
        if component_id in {
            "build-runtime:pyinstaller-bootloader",
            "build-runtime:pyinstaller-hooks",
        }:
            material_name = "pyinstaller"
        elif component_id == "build-runtime:pyinstaller-hooks-contrib":
            material_name = "pyinstaller-hooks-contrib"
        material_entry = materials_by_name.get(material_name)
        license_files = (
            list(material_entry.get("license_files", []))
            if material_entry is not None
            else []
        )
        license_choice = dict(sbom_component["licenses"][0])
        declared_material_status = properties["pkv:license-material-status"]
        if declared_material_status.endswith("compliance-hold"):
            license_material_status = declared_material_status
        elif license_files:
            license_material_status = "bound"
        else:
            license_material_status = "metadata-only-compliance-hold"
        actual_entry: dict[str, Any] = {
            "classifications": sorted(
                {
                    str(item["value"])
                    for item in sbom_component["properties"]
                    if item.get("name") == "pkv:payload-classification"
                },
                key=lambda value: value.encode("utf-8"),
            ),
            "component_id": component_id,
            "component_sha256": properties["pkv:inventory-component-sha256"],
            "license": license_choice,
            "license_files": license_files,
            "license_material_status": license_material_status,
            "name": sbom_component["name"],
            "embedded_paths": list(record.get("embedded_paths", [])),
            "payload_paths": list(record.get("payload_paths", [])),
            "source_paths": list(record.get("source_paths", [])),
        }
        if component_id == "python-distribution:html2text":
            actual_entry["license_expression_status"] = "requires_legal_confirmation"
        for key in ("purl", "version"):
            if key in sbom_component:
                actual_entry[key] = sbom_component[key]
        actual_components.append(actual_entry)
    actual_components.sort(key=lambda item: item["component_id"].encode("utf-8"))
    if release_eligible and any(
        item["license_material_status"] != "bound" for item in actual_components
    ):
        raise ReleaseBuildError(
            "release-eligible Artifact has unresolved runtime license materials"
        )
    bound = {
        "schema_version": "pkv.license-index.v2",
        "actual_runtime_inventory": {
            "components": actual_components,
            "release_inventory_closure_sha256": inventory["bindings"]["closure_sha256"],
            "release_inventory_path": "release-inventory.json",
        },
        "entries": dependency_entries,
    }
    write_canonical_json(index_path, bound)
    return bound


def _assert_safe_directory_chain(path: Path, *, authority: Path) -> None:
    root = Path(os.path.abspath(authority))
    candidate = Path(os.path.abspath(path))
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise ReleaseBuildError(f"path escapes build authority: {path}") from exc
    cursor = root
    for part in (Path(), *relative.parts):
        if part != Path():
            cursor /= part
        if not cursor.exists() and not cursor.is_symlink():
            continue
        details = cursor.lstat()
        if stat.S_ISLNK(details.st_mode) or _path_is_reparse(cursor, details):
            raise ReleaseBuildError(
                f"build path contains a link/reparse point: {cursor}"
            )
        if not stat.S_ISDIR(details.st_mode):
            raise ReleaseBuildError(f"build directory path contains a file: {cursor}")


def _safe_rmtree(path: Path, *, authority: Path) -> None:
    _assert_safe_directory_chain(path, authority=authority)
    candidate = path.resolve(strict=False)
    root = authority.resolve(strict=True)
    if candidate == root or root not in candidate.parents:
        raise ReleaseBuildError(
            f"refusing to remove path outside build authority: {path}"
        )
    if path.exists():
        shutil.rmtree(path)


def _prepare_dist_root(project_root: Path) -> Path:
    dist_root = project_root / "dist"
    _assert_safe_directory_chain(dist_root, authority=project_root)
    if dist_root.exists() or dist_root.is_symlink():
        details = dist_root.lstat()
        if (
            stat.S_ISLNK(details.st_mode)
            or _path_is_reparse(dist_root, details)
            or not stat.S_ISDIR(details.st_mode)
        ):
            raise ReleaseBuildError("release dist root must be a normal directory")
    else:
        dist_root.mkdir()
    resolved_project = project_root.resolve(strict=True)
    resolved_dist = dist_root.resolve(strict=True)
    if resolved_project not in resolved_dist.parents:
        raise ReleaseBuildError("release dist root escapes the project root")
    return dist_root


def _materialize_git_head(
    project_root: Path,
    destination: Path,
    *,
    revision: str,
    source_date_epoch: int,
) -> None:
    """Materialize exact tracked blobs without consulting Git attributes."""

    physical_root = destination.parent
    _safe_rmtree(physical_root, authority=physical_root.parent)
    physical_root.mkdir(parents=True)
    git = str(_locked_git_executable())
    git_environment = _clean_git_environment()
    raw_tree = _run_bytes(
        [git, "ls-tree", "-rz", "--full-tree", revision],
        cwd=project_root,
        environment=git_environment,
    )
    tree_entries: list[tuple[str, str, str]] = []
    seen_paths: set[str] = set()
    seen_folded_paths: dict[str, str] = {}
    for raw_entry in raw_tree.split(b"\0"):
        if not raw_entry:
            continue
        try:
            raw_header, raw_path = raw_entry.split(b"\t", 1)
            raw_mode, raw_type, raw_object = raw_header.split(b" ")
            mode = raw_mode.decode("ascii")
            object_type = raw_type.decode("ascii")
            object_id = raw_object.decode("ascii")
            relative_text = raw_path.decode("utf-8")
        except (UnicodeDecodeError, ValueError) as exc:
            raise ReleaseBuildError("Git tree contains an invalid entry") from exc
        relative = PurePosixPath(relative_text)
        forbidden_windows = '<>:"\\|?*'
        reserved_windows = {
            "con",
            "conin$",
            "conout$",
            "clock$",
            "prn",
            "aux",
            "nul",
            *{f"com{number}" for number in range(1, 10)},
            *{f"lpt{number}" for number in range(1, 10)},
        }
        if (
            mode not in {"100644", "100755"}
            or object_type != "blob"
            or not re.fullmatch(r"[0-9a-f]{40}", object_id)
            or not relative_text
            or relative.is_absolute()
            or relative.as_posix() != relative_text
            or any(
                not part
                or part in {".", ".."}
                or part.endswith((" ", "."))
                or any(character in forbidden_windows for character in part)
                or any(ord(character) < 32 for character in part)
                or part.split(".", 1)[0].casefold() in reserved_windows
                for part in relative.parts
            )
        ):
            raise ReleaseBuildError(
                f"Git tree contains an unsafe entry: {relative_text!r}"
            )
        folded = relative_text.casefold()
        if relative_text in seen_paths or folded in seen_folded_paths:
            raise ReleaseBuildError(
                "Git tree contains a duplicate/case-colliding path: "
                f"{seen_folded_paths.get(folded, relative_text)!r} / {relative_text!r}"
            )
        seen_paths.add(relative_text)
        seen_folded_paths[folded] = relative_text
        tree_entries.append((relative_text, mode, object_id))
    if not tree_entries:
        raise ReleaseBuildError("Git revision has no materializable tracked files")
    tree_entries.sort(key=lambda item: item[0].encode("utf-8"))

    batch_input = b"".join(
        object_id.encode("ascii") + b"\n" for _, _, object_id in tree_entries
    )
    batch_output = _run_bytes(
        [git, "cat-file", "--batch"],
        cwd=project_root,
        environment=git_environment,
        input_bytes=batch_input,
    )
    destination.mkdir()
    destination_root = destination.resolve(strict=True)
    cursor = 0
    for relative_text, mode, expected_object in tree_entries:
        header_end = batch_output.find(b"\n", cursor)
        if header_end < 0:
            raise ReleaseBuildError("Git cat-file batch output is truncated")
        try:
            raw_object, raw_type, raw_size = batch_output[cursor:header_end].split(b" ")
            actual_object = raw_object.decode("ascii")
            actual_type = raw_type.decode("ascii")
            size = int(raw_size.decode("ascii"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ReleaseBuildError("Git cat-file batch header is invalid") from exc
        content_start = header_end + 1
        content_end = content_start + size
        if (
            actual_object != expected_object
            or actual_type != "blob"
            or size < 0
            or content_end >= len(batch_output)
            or batch_output[content_end : content_end + 1] != b"\n"
        ):
            raise ReleaseBuildError("Git cat-file batch output differs from ls-tree")
        content = batch_output[content_start:content_end]
        calculated_object = hashlib.sha1(
            b"blob " + str(size).encode("ascii") + b"\0" + content
        ).hexdigest()
        if calculated_object != expected_object:
            raise ReleaseBuildError("Git blob bytes do not match their object identity")
        relative = PurePosixPath(relative_text)
        target = destination.joinpath(*relative.parts)
        target_parent = target.parent.resolve(strict=False)
        if (
            target_parent != destination_root
            and destination_root not in target_parent.parents
        ):
            raise ReleaseBuildError(
                f"Git tree path escapes source root: {relative_text}"
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as output:
            output.write(content)
        target.chmod(0o755 if mode == "100755" else 0o644)
        os.utime(target, (source_date_epoch, source_date_epoch))
        cursor = content_end + 1
    if cursor != len(batch_output):
        raise ReleaseBuildError("Git cat-file batch output contains trailing data")
    directories = [
        destination,
        *[path for path in destination.rglob("*") if path.is_dir()],
    ]
    for directory in sorted(
        directories, key=lambda path: len(path.parts), reverse=True
    ):
        os.utime(directory, (source_date_epoch, source_date_epoch))


def _clean_build_environment(
    source_date_epoch: int, *, temporary_root: Path
) -> dict[str, str]:
    temporary_root = Path(os.path.abspath(temporary_root))
    _assert_safe_directory_chain(temporary_root, authority=temporary_root.parent)
    temporary_root.mkdir(parents=True, exist_ok=True)
    system_root = Path("C:/Windows")
    system32 = system_root / "System32"
    comspec = system32 / "cmd.exe"
    python_prefix = Path(sys.prefix).resolve(strict=True)
    path_entries = [
        python_prefix,
        python_prefix / "Scripts",
        python_prefix / "Library" / "bin",
        python_prefix / "DLLs",
        system32,
        _locked_git_executable().parent,
    ]
    for path in path_entries:
        _assert_safe_directory_chain(path, authority=Path(path.anchor))
        if not path.is_dir():
            raise ReleaseBuildError(f"locked build PATH directory is missing: {path}")
    _locked_regular_file(comspec, prefix=Path(comspec.anchor), label="cmd.exe")
    pyinstaller_config = temporary_root / "pyinstaller-config"
    pyinstaller_config.mkdir(exist_ok=True)
    _assert_safe_directory_chain(pyinstaller_config, authority=temporary_root.parent)
    build_home = temporary_root / "home"
    build_home.mkdir(exist_ok=True)
    _assert_safe_directory_chain(build_home, authority=temporary_root.parent)
    return {
        "COMSPEC": str(comspec),
        "HOME": str(build_home),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": os.pathsep.join(str(path) for path in path_entries),
        "PATHEXT": ".COM;.EXE;.BAT;.CMD",
        "PROCESSOR_ARCHITECTURE": "AMD64",
        "PYINSTALLER_CONFIG_DIR": str(pyinstaller_config),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONHASHSEED": "0",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONNOUSERSITE": "1",
        "PYTHONSAFEPATH": "1",
        "PYTHONUTF8": "1",
        "SOURCE_DATE_EPOCH": str(source_date_epoch),
        "SystemRoot": str(system_root),
        "TEMP": str(temporary_root),
        "TMP": str(temporary_root),
        "TZ": "UTC",
        "USERPROFILE": str(build_home),
        "WINDIR": str(system_root),
    }


def _source_tree_fingerprint(root: Path) -> dict[str, Any]:
    rows: list[tuple[str, Path, int]] = []
    for path in root.rglob("*"):
        details = path.lstat()
        if stat.S_ISLNK(details.st_mode) or _path_is_reparse(path, details):
            raise ReleaseBuildError(
                f"source snapshot contains a link/reparse point: {path}"
            )
        if stat.S_ISDIR(details.st_mode):
            continue
        details = _validate_regular_file(path)
        relative = path.relative_to(root).as_posix()
        rows.append((relative, path, details.st_size))
    return {
        "file_count": len(rows),
        "sha256": _file_tree_sha256(rows),
    }


def _conda_hardlink_threat_evidence(*, release_eligible: bool) -> dict[str, Any]:
    prefix = Path(os.path.abspath(sys.prefix))
    numpy_anchor = Path(
        importlib.metadata.distribution("numpy").locate_file("numpy/__init__.py")
    )
    raw_anchors = {
        "numpy-package-anchor": numpy_anchor,
        "python-dll": prefix / "python311.dll",
        "python-executable": Path(sys.executable),
    }
    anchors: list[dict[str, Any]] = []
    for label, path in sorted(raw_anchors.items()):
        relative, size = _locked_regular_file(
            path, prefix=prefix, label=f"hardlink observation {label}"
        )
        details = path.lstat()
        anchors.append(
            {
                "hardlink_count": details.st_nlink,
                "label": label,
                "path": relative,
                "sha256": sha256_file(path),
                "size": size,
            }
        )
    observed = [item for item in anchors if int(item["hardlink_count"]) > 1]
    return {
        "schema_version": "pkv.conda-hardlink-threat-evidence.v1",
        "anchors": anchors,
        "observed_hardlink_anchor_count": len(observed),
        "release_eligible_environment_requirement": "copy-only-no-hardlinks",
        "threat_model": (
            "copy-only-release-environment"
            if release_eligible
            else "accepted_for_test_candidate"
        ),
        "validation_scope": list(
            BUILD_ENVIRONMENT_CONTRACT["live_environment_byte_revalidation"]
        ),
    }


def _assert_copy_only_release_environment(prefix: Path) -> None:
    root = Path(os.path.abspath(prefix))
    _assert_safe_directory_chain(root, authority=root.parent)
    for path in root.rglob("*"):
        details = path.lstat()
        if stat.S_ISLNK(details.st_mode) or _path_is_reparse(path, details):
            raise ReleaseBuildError(
                f"release-eligible environment contains a link/reparse point: {path}"
            )
        if stat.S_ISREG(details.st_mode) and details.st_nlink > 1:
            raise ReleaseBuildError(
                f"release-eligible environment contains a hardlink: {path}"
            )


def _toolchain_info(
    components: Sequence[Mapping[str, Any]],
    *,
    hardlink_evidence: Mapping[str, Any],
) -> dict[str, Any]:
    versions = {
        item["name"]: item["version"] for item in components if item["role"] == "build"
    }
    pyinstaller_distribution = importlib.metadata.distribution("pyinstaller")
    bootloader_root = Path(
        pyinstaller_distribution.locate_file(
            "PyInstaller/bootloader/Windows-64bit-intel"
        )
    )
    return {
        "build_environment_contract": dict(BUILD_ENVIRONMENT_CONTRACT),
        "conda_hardlink_threat_evidence": dict(hardlink_evidence),
        "git": dict(GIT_TOOL_CONTRACT),
        "pyinstaller_bootloaders": {
            filename: sha256_file(bootloader_root / filename)
            for filename in ("run.exe", "runw.exe")
        },
        "python_executable_sha256": sha256_file(Path(sys.executable)),
        "python_implementation": sys.implementation.name,
        "python_version": ".".join(str(value) for value in sys.version_info[:3]),
        "release_build_distributions": versions,
        "zlib_runtime_version": zlib.ZLIB_RUNTIME_VERSION,
        "zlib_version": zlib.ZLIB_VERSION,
    }


def validate_compliance_sources(project_root: Path) -> dict[str, Any]:
    manifest_path = project_root / "packaging" / "compliance-sources.v1.json"
    contract = _load_json(manifest_path)
    if contract.get("schema_version") != "pkv.compliance-sources.v1" or contract.get(
        "policy"
    ) != {
        "network_required_at_build_time": False,
        "upstream_text_assets_are_byte_for_byte_upstream": True,
        "release_validation": "fail_closed",
    }:
        raise ReleaseBuildError("compliance source contract is invalid")
    raw_artifacts = contract.get("artifacts")
    if not isinstance(raw_artifacts, list):
        raise ReleaseBuildError("compliance artifact inventory is invalid")
    artifacts: dict[str, Mapping[str, Any]] = {}
    for raw_artifact in raw_artifacts:
        if not isinstance(raw_artifact, Mapping):
            raise ReleaseBuildError("compliance artifact entry is invalid")
        identifier = str(raw_artifact.get("id", ""))
        if identifier in artifacts:
            raise ReleaseBuildError(f"duplicate compliance artifact: {identifier}")
        artifacts[identifier] = raw_artifact
    if set(artifacts) != set(COMPLIANCE_ARTIFACT_CONTRACT):
        raise ReleaseBuildError("compliance artifact set differs from contract")
    for identifier, (relative, expected_size, expected_hash) in sorted(
        COMPLIANCE_ARTIFACT_CONTRACT.items()
    ):
        artifact = artifacts[identifier]
        if (
            artifact.get("path") != relative
            or artifact.get("size") != expected_size
            or str(artifact.get("sha256", "")).lower() != expected_hash
        ):
            raise ReleaseBuildError(
                f"compliance artifact metadata differs from contract: {identifier}"
            )
        path = project_root / _contract_relative_path(
            relative, label="compliance artifact path"
        )
        details = _validate_regular_file(path)
        if details.st_size != expected_size or sha256_file(path) != expected_hash:
            raise ReleaseBuildError(
                f"compliance artifact bytes differ from contract: {identifier}"
            )
        if artifact.get("vendored_normalization") == "none" and (
            artifact.get("source_size") != expected_size
            or str(artifact.get("source_sha256", "")).lower() != expected_hash
        ):
            raise ReleaseBuildError(
                f"upstream compliance provenance differs from bytes: {identifier}"
            )
        source_url = artifact.get("source_url")
        if source_url is not None and not str(source_url).startswith("https://"):
            raise ReleaseBuildError(f"compliance source URL is not HTTPS: {identifier}")

    source_archive = (
        project_root / COMPLIANCE_ARTIFACT_CONTRACT["html2text-2020.1.16-sdist"][0]
    )
    with tarfile.open(source_archive, mode="r:gz") as archive:
        members = archive.getmembers()
        if not members:
            raise ReleaseBuildError("html2text corresponding source archive is empty")
        for member in members:
            relative = PurePosixPath(member.name)
            if (
                relative.is_absolute()
                or ".." in relative.parts
                or ":" in member.name
                or member.issym()
                or member.islnk()
                or not (member.isdir() or member.isfile())
            ):
                raise ReleaseBuildError(
                    f"unsafe html2text corresponding source member: {member.name}"
                )

    raw_blockers = contract.get("fail_closed_release_blockers")
    if not isinstance(raw_blockers, list):
        raise ReleaseBuildError("compliance blocker inventory is invalid")
    blocker_ids: list[str] = []
    blocker_authority: list[dict[str, Any]] = []
    for blocker in raw_blockers:
        identifier = str(blocker.get("id", "")) if isinstance(blocker, Mapping) else ""
        expected_keys = {"id", "condition", "resolution"}
        if identifier == "html2text-gpl-compliance":
            expected_keys.add("resolution_requirements")
        if identifier == "conda-native-license-materials-and-spdx":
            expected_keys.add("affected_component_selectors")
        if (
            not isinstance(blocker, Mapping)
            or set(blocker) != expected_keys
            or not str(blocker["condition"]).strip()
            or not str(blocker["resolution"]).strip()
        ):
            raise ReleaseBuildError("compliance blocker entry is invalid")
        if identifier == "html2text-gpl-compliance" and blocker.get(
            "resolution_requirements"
        ) != list(HTML2TEXT_GPL_COMPLIANCE_REQUIREMENTS):
            raise ReleaseBuildError(
                "html2text GPL compliance requirements differ from the frozen hold"
            )
        if identifier == "conda-native-license-materials-and-spdx" and blocker.get(
            "affected_component_selectors"
        ) != list(NATIVE_LICENSE_HOLD_COMPONENT_SELECTORS):
            raise ReleaseBuildError(
                "native license hold component selectors differ from the frozen hold"
            )
        blocker_ids.append(identifier)
        authority_entry = {
            "condition": str(blocker["condition"]),
            "id": identifier,
            "resolution": str(blocker["resolution"]),
        }
        if identifier == "html2text-gpl-compliance":
            authority_entry["resolution_requirements"] = list(
                HTML2TEXT_GPL_COMPLIANCE_REQUIREMENTS
            )
        if identifier == "conda-native-license-materials-and-spdx":
            authority_entry["affected_component_selectors"] = list(
                NATIVE_LICENSE_HOLD_COMPONENT_SELECTORS
            )
        blocker_authority.append(authority_entry)
    if len(blocker_ids) != len(set(blocker_ids)) or set(blocker_ids) != set(
        COMPLIANCE_BLOCKER_IDS
    ):
        raise ReleaseBuildError("compliance blocker set differs from the frozen hold")
    canonical_blockers = sorted(blocker_ids, key=lambda value: value.encode("utf-8"))
    blocker_authority.sort(key=lambda item: item["id"].encode("utf-8"))
    blocker_authority_sha256 = sha256_bytes(canonical_json_bytes(blocker_authority))
    template_path = (
        project_root
        / COMPLIANCE_ARTIFACT_CONTRACT["qt-pyside6-6.11.1-lgpl-user-notice-template"][0]
    )
    if not re.search(r"<[A-Z0-9_]+>", template_path.read_text(encoding="utf-8")):
        raise ReleaseBuildError(
            "Qt notice changed without resolving the frozen compliance hold"
        )
    return {
        "artifact_status": "test-candidate-on-compliance-hold",
        "compliance_manifest_sha256": sha256_file(manifest_path),
        "release_blocker_authority": blocker_authority,
        "release_blocker_authority_sha256": blocker_authority_sha256,
        "release_blockers": canonical_blockers,
        "release_eligible": False,
    }


def _input_hashes(project_root: Path, contract: Mapping[str, Any]) -> dict[str, str]:
    paths = [
        "packaging/pkv.spec",
        "packaging/runtime-resources.json",
        "packaging/release-contract.v1.json",
        "packaging/payload-policy.v1.json",
        "packaging/locks/conda-native-registry.v1.json",
        "packaging/locks/release-environment.v2.json",
        "packaging/license-overrides.v1.json",
        "packaging/compliance-sources.v1.json",
        "packaging/licenses/jieba-0.42.1-MIT.txt",
        "scripts/release_inventory.py",
        "scripts/build_release.py",
        "scripts/install/Install.ps1",
        "scripts/install/Uninstall.ps1",
        "LICENSE",
        "THIRD-PARTY-NOTICES.txt",
        "docs/operations/release/USER-GUIDE.md",
    ]
    paths.extend(
        str(value) for value in contract.get("additional_fingerprint_inputs", [])
    )
    for directory_name in ("packaging/licenses", "packaging/compliance-sources"):
        directory = project_root / directory_name
        if not directory.exists():
            continue
        for candidate in directory.rglob("*"):
            if candidate.is_file():
                paths.append(candidate.relative_to(project_root).as_posix())
    results: dict[str, str] = {}
    for relative in sorted(set(paths), key=lambda value: value.encode("utf-8")):
        path = project_root / relative
        if not path.is_file():
            raise ReleaseBuildError(f"release fingerprint input is missing: {relative}")
        results[relative] = sha256_file(path)
    return results


def _build_fingerprint(
    *,
    version: str,
    revision: str,
    source_date_epoch: int,
    inputs: Mapping[str, str],
    toolchain: Mapping[str, Any],
) -> str:
    return sha256_bytes(
        canonical_json_bytes(
            {
                "inputs": dict(inputs),
                "revision": revision,
                "source_date_epoch": source_date_epoch,
                "target": "windows-x86_64",
                "toolchain": dict(toolchain),
                "version": version,
            }
        )
    )


def _copy_release_shell(project_root: Path, release_root: Path) -> None:
    sources = {
        project_root
        / "scripts"
        / "install"
        / "Install.ps1": release_root
        / "Install.ps1",
        project_root
        / "scripts"
        / "install"
        / "Uninstall.ps1": release_root
        / "Uninstall.ps1",
        project_root / "LICENSE": release_root / "LICENSE",
        project_root
        / "THIRD-PARTY-NOTICES.txt": release_root
        / "THIRD-PARTY-NOTICES.txt",
        project_root
        / "docs"
        / "operations"
        / "release"
        / "USER-GUIDE.md": release_root
        / "USER-GUIDE.md",
    }
    for source, destination in sources.items():
        if not source.is_file():
            raise ReleaseBuildError(f"release shell input is missing: {source}")
        shutil.copyfile(source, destination)


def _invoke_pyinstaller(
    *,
    project_root: Path,
    spec_path: Path,
    work_root: Path,
    dist_root: Path,
    source_date_epoch: int,
) -> None:
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--clean",
        "--noconfirm",
        "--workpath",
        str(work_root),
        "--distpath",
        str(dist_root),
        str(spec_path),
    ]
    _run(
        command,
        cwd=project_root,
        environment=_clean_build_environment(
            source_date_epoch,
            temporary_root=work_root.parent / f"{work_root.name}-process-temp",
        ),
    )


def _find_analysis_toc(work_root: Path) -> Path:
    candidates = sorted(
        work_root.rglob("Analysis-00.toc"),
        key=lambda path: path.as_posix().encode("utf-8"),
    )
    if len(candidates) != 1:
        raise ReleaseBuildError(
            "PyInstaller work tree must contain exactly one Analysis-00.toc; "
            f"found {len(candidates)}"
        )
    _validate_regular_file(candidates[0])
    return candidates[0]


def _build_bound_release_inventory(
    *,
    project_root: Path,
    work_root: Path,
    payload_root: Path,
    artifact_path_base: str,
    bootloader_executables: Sequence[str],
    components: Sequence[Mapping[str, Any]],
    revision: str,
    build_fingerprint: str,
    lock_sha256: str,
    input_hashes: Mapping[str, str],
    artifact_kind: str,
    artifact_status: str,
    release_eligible: bool,
    release_blocker_authority: Sequence[Mapping[str, Any]],
    release_blocker_authority_sha256: str,
    release_blockers: Sequence[str],
) -> dict[str, Any]:
    canonical_blocker_authority = [dict(item) for item in release_blocker_authority]
    if (
        [str(item.get("id", "")) for item in canonical_blocker_authority]
        != list(release_blockers)
        or not re.fullmatch(r"[0-9a-f]{64}", release_blocker_authority_sha256)
        or sha256_bytes(canonical_json_bytes(canonical_blocker_authority))
        != release_blocker_authority_sha256
    ):
        raise ReleaseBuildError("release blocker authority/hash binding is invalid")
    registry_relative = "packaging/locks/conda-native-registry.v1.json"
    registry_path = project_root / registry_relative
    registry = _load_json(registry_path)
    registry_sha256 = sha256_file(registry_path)
    if input_hashes.get(registry_relative) != registry_sha256:
        raise ReleaseBuildError(
            "build fingerprint does not bind the Conda native registry"
        )
    try:
        inventory = build_release_inventory(
            _find_analysis_toc(work_root),
            payload_root,
            source_roots={
                "build-work": work_root,
                "python-prefix": Path(sys.prefix),
                "source": project_root,
            },
            bootloader_executables=bootloader_executables,
            executable_pkg_tocs=discover_executable_pkg_tocs(
                work_root, bootloader_executables
            ),
            conda_native_registry=registry,
            conda_meta_root=Path(sys.prefix) / "conda-meta",
            artifact_path_base=artifact_path_base,
            python_version=".".join(str(value) for value in sys.version_info[:3]),
            target="windows-x86_64",
        )
    except InventoryError as exc:
        raise ReleaseBuildError(
            f"actual PyInstaller closure is invalid: {exc}"
        ) from exc

    if inventory.get("schema_version") != "pkv.release-inventory.v1":
        raise ReleaseBuildError("actual PyInstaller inventory schema is invalid")
    bindings = inventory.get("bindings")
    coverage = inventory.get("coverage")
    payload = inventory.get("payload")
    embedded_archives = inventory.get("embedded_archives")
    if (
        not isinstance(bindings, Mapping)
        or not isinstance(coverage, Mapping)
        or not isinstance(payload, Mapping)
        or not isinstance(embedded_archives, list)
        or len(embedded_archives) != len(bootloader_executables)
        or bindings.get("conda_native_registry_sha256") != registry_sha256
        or payload.get("path_base") != (artifact_path_base or ".")
        or coverage.get("unattributed_native_file_count") != 0
        or coverage.get("unattributed_native_paths") != []
        or coverage.get("unresolved_component_ids") != []
    ):
        raise ReleaseBuildError("actual PyInstaller inventory coverage is incomplete")
    expected_embedded_paths = {
        f"{artifact_path_base}/{name}" if artifact_path_base else name
        for name in bootloader_executables
    }
    if (
        {
            str(item.get("executable_artifact_path", ""))
            for item in embedded_archives
            if isinstance(item, Mapping)
        }
        != expected_embedded_paths
        or any(
            not isinstance(item, Mapping)
            or int(item.get("entry_count", 0)) <= 0
            or not re.fullmatch(
                r"[0-9a-f]{64}", str(item.get("portable_graph_sha256", ""))
            )
            or not re.fullmatch(r"[0-9a-f]{64}", str(item.get("pkg_sha256", "")))
            for item in embedded_archives
        )
        or bindings.get("embedded_archives_sha256")
        != sha256_bytes(canonical_json_bytes(embedded_archives))
    ):
        raise ReleaseBuildError(
            "actual PyInstaller embedded archive binding is invalid"
        )
    for name in (
        "analysis_graph_sha256",
        "closure_sha256",
        "conda_native_registry_sha256",
        "embedded_archives_sha256",
        "payload_tree_sha256",
    ):
        if not re.fullmatch(r"[0-9a-f]{64}", str(bindings.get(name, ""))):
            raise ReleaseBuildError(f"release inventory binding is invalid: {name}")

    locked_by_name = {
        _canonical_distribution_name(str(item["name"])): item for item in components
    }
    seen_distributions: set[str] = set()
    for raw_distribution in inventory.get("included_distributions", []):
        if not isinstance(raw_distribution, Mapping):
            raise ReleaseBuildError("release inventory distribution entry is invalid")
        name = _canonical_distribution_name(str(raw_distribution.get("name", "")))
        locked = locked_by_name.get(name)
        if (
            not name
            or name in seen_distributions
            or locked is None
            or str(raw_distribution.get("version", "")) != str(locked["version"])
            or (
                locked["role"] == "build"
                and name not in {"pyinstaller", "pyinstaller-hooks-contrib"}
            )
            or locked["role"] not in {"build", "runtime"}
        ):
            raise ReleaseBuildError(
                f"actual PyInstaller distribution closure violates roles: {name}"
            )
        seen_distributions.add(name)

    raw_component_rows = inventory.get("components")
    if not isinstance(raw_component_rows, list):
        raise ReleaseBuildError("release inventory components are invalid")
    component_ids = {
        str(item.get("id", ""))
        for item in raw_component_rows
        if isinstance(item, Mapping)
    }
    required_component_ids = {
        "application:project",
        "build-runtime:pyinstaller-bootloader",
        "runtime:cpython",
    }
    if (
        not required_component_ids <= component_ids
        or "native:msvc-runtime" not in component_ids
        or any(
            item.get("identity_status") not in {"classification-only", "complete"}
            for item in raw_component_rows
            if isinstance(item, Mapping)
        )
        or any(
            not list(item.get("payload_paths", []))
            and not list(item.get("embedded_paths", []))
            for item in raw_component_rows
            if isinstance(item, Mapping)
        )
    ):
        raise ReleaseBuildError("release inventory lacks required runtime identities")

    expected_files: dict[str, tuple[int, str]] = {}
    for path in payload_root.rglob("*"):
        if path.is_dir():
            continue
        details = _validate_regular_file(path)
        relative = path.relative_to(payload_root).as_posix()
        artifact_path = (
            f"{artifact_path_base}/{relative}" if artifact_path_base else relative
        )
        expected_files[artifact_path] = (details.st_size, sha256_file(path))
    recorded_files: dict[str, tuple[int, str]] = {}
    for raw_file in payload.get("files", []):
        if not isinstance(raw_file, Mapping):
            raise ReleaseBuildError("release inventory payload entry is invalid")
        artifact_path = str(raw_file.get("artifact_path", ""))
        if artifact_path in recorded_files:
            raise ReleaseBuildError("release inventory payload path is duplicated")
        recorded_files[artifact_path] = (
            int(raw_file.get("size", -1)),
            str(raw_file.get("sha256", "")),
        )
    if recorded_files != expected_files:
        raise ReleaseBuildError(
            "release inventory payload file hashes differ from packaged bytes"
        )

    authority = {
        "artifact_kind": artifact_kind,
        "artifact_status": artifact_status,
        "build_fingerprint": build_fingerprint,
        "conda_native_registry_path": registry_relative,
        "conda_native_registry_sha256": registry_sha256,
        "environment_lock_path": "packaging/locks/release-environment.v2.json",
        "environment_lock_sha256": lock_sha256,
        "release_blocker_authority": canonical_blocker_authority,
        "release_blocker_authority_sha256": release_blocker_authority_sha256,
        "release_blockers": list(release_blockers),
        "release_eligible": release_eligible,
        "source_revision": revision,
    }
    inventory["authority"] = authority
    inventory["bindings"]["artifact_closure_sha256"] = sha256_bytes(
        canonical_json_bytes(
            {
                "authority": authority,
                "inventory_closure_sha256": bindings["closure_sha256"],
            }
        )
    )
    return inventory


def _write_harness_legal_materials(
    *,
    project_root: Path,
    package_root: Path,
    source_date_epoch: int,
    build_fingerprint: str,
    inventory: Mapping[str, Any],
    inventory_sha256: str,
    components: Sequence[Mapping[str, Any]],
) -> tuple[Path, Path]:
    harness_blocker_authority = [dict(item) for item in HARNESS_BLOCKER_AUTHORITY]
    harness_blocker_authority_sha256 = sha256_bytes(
        canonical_json_bytes(harness_blocker_authority)
    )
    shutil.copyfile(project_root / "LICENSE", package_root / "LICENSE")
    materials = (
        (
            "cpython",
            ".".join(str(value) for value in sys.version_info[:3]),
            "Python-2.0",
            "cpython-3.11.15-LICENSE.txt",
        ),
        (
            "pyinstaller",
            importlib.metadata.version("pyinstaller"),
            "GPL-2.0-or-later WITH Bootloader-exception",
            "pyinstaller-6.21.0-COPYING.txt",
        ),
    )
    license_entries: list[dict[str, Any]] = []
    for name, version, expression, filename in materials:
        source = project_root / "packaging" / "licenses" / filename
        target = package_root / "licenses" / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        license_entries.append(
            {
                "license_expression": expression,
                "license_files": [
                    {
                        "path": f"licenses/{filename}",
                        "sha256": sha256_file(target),
                        "source_kind": "compliance_asset",
                    }
                ],
                "name": name,
                "purl": f"pkg:generic/{name}@{version}",
                "version": version,
            }
        )
    license_index = {
        "schema_version": "pkv.license-index.v1",
        "artifact_kind": "e2e_test_harness",
        "entries": license_entries,
    }
    license_index_path = package_root / "licenses" / "index.json"
    write_canonical_json(license_index_path, license_index)
    bound_license_index = _bind_license_index_to_inventory(
        license_index_path,
        inventory=inventory,
        locked_components=components,
        release_eligible=False,
    )
    hold_text = (
        "INTERNAL E2E TEST HARNESS - NATIVE COMPLIANCE HOLD - NOT FOR DISTRIBUTION\n"
        "===========================================================================\n\n"
        "This frozen executable is only for isolated W4 functional verification.\n"
        "It is not release-eligible and must not be included in the PKV product payload.\n\n"
        "Release blocker:\n"
        "- harness-native-license-and-provenance\n\n"
        "The actual PyInstaller closure is recorded in release-inventory.json; native\n"
        "MSVC/UCRT and Conda package license/provenance materials remain on hold.\n"
    )
    (package_root / "COMPLIANCE-HOLD.txt").write_text(
        hold_text, encoding="utf-8", newline="\n"
    )
    (package_root / "THIRD-PARTY-NOTICES.txt").write_text(
        "PKV W4 Loopback Harness - Third-Party Notices\n"
        "================================================\n\n"
        "INTERNAL VERIFICATION ONLY; NOT FOR DISTRIBUTION.\n\n"
        "This e2e-only frozen test harness embeds the CPython runtime, a "
        "PyInstaller bootloader, and the native closure listed file-by-file in "
        "release-inventory.json.\n"
        "The complete CPython license and the PyInstaller COPYING file, including "
        "the Bootloader Exception, are in licenses/.\n"
        "Conda/native package license strings are recorded in licenses/index.json, "
        "but their complete license/provenance materials remain unresolved and are "
        "an explicit release blocker.\n"
        "This harness is physically separate from the Personal Knowledge Vault "
        "product candidate and is forbidden from product payload membership.\n",
        encoding="utf-8",
        newline="\n",
    )
    timestamp = (
        datetime.fromtimestamp(max(source_date_epoch, 0), tz=timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )
    application_ref = "pkg:generic/pkv-w4-loopback-harness@1.0.0"
    inventory_components = _cross_bind_sbom_components_to_license_index(
        _inventory_sbom_components(inventory, components),
        inventory=inventory,
        license_index=bound_license_index,
        bind_unresolved_status=True,
    )
    sbom = {
        "bomFormat": SCHEMA_SBOM,
        "specVersion": "1.5",
        "version": 1,
        "metadata": {
            "timestamp": timestamp,
            "component": {
                "bom-ref": application_ref,
                "name": "PKV W4 Loopback Harness",
                "type": "application",
                "version": "1.0.0",
            },
            "properties": [
                {"name": "pkv:artifact-kind", "value": "e2e_test_harness"},
                {"name": "pkv:artifact-status", "value": HARNESS_ARTIFACT_STATUS},
                {
                    "name": "pkv:release-blocker",
                    "value": HARNESS_BLOCKER_IDS[0],
                },
                {
                    "name": "pkv:release-blocker-authority-sha256",
                    "value": harness_blocker_authority_sha256,
                },
                {"name": "pkv:release-eligible", "value": "false"},
                {
                    "name": "pkv:release-inventory-closure-sha256",
                    "value": str(inventory["bindings"]["closure_sha256"]),
                },
                {
                    "name": "pkv:release-inventory-path",
                    "value": "release-inventory.json",
                },
                {
                    "name": "pkv:release-inventory-sha256",
                    "value": inventory_sha256,
                },
                {"name": "pkv:release-payload-membership", "value": "forbidden"},
            ],
        },
        "components": inventory_components,
        "dependencies": [
            {
                "ref": application_ref,
                "dependsOn": [item["bom-ref"] for item in inventory_components],
            }
        ],
    }
    sbom_path = package_root / "sbom.cdx.json"
    write_canonical_json(sbom_path, sbom)
    legal_paths = (
        "COMPLIANCE-HOLD.txt",
        "LICENSE",
        "THIRD-PARTY-NOTICES.txt",
        "licenses/cpython-3.11.15-LICENSE.txt",
        "licenses/index.json",
        "licenses/pyinstaller-6.21.0-COPYING.txt",
        "release-inventory.json",
        "sbom.cdx.json",
    )
    legal_manifest = {
        "schema_version": "pkv.harness-legal-manifest.v1",
        "artifact_kind": "e2e_test_harness",
        "artifact_status": HARNESS_ARTIFACT_STATUS,
        "build_fingerprint": build_fingerprint,
        "release_blocker_authority": harness_blocker_authority,
        "release_blocker_authority_sha256": harness_blocker_authority_sha256,
        "release_blockers": list(HARNESS_BLOCKER_IDS),
        "release_eligible": False,
        "release_inventory_closure_sha256": inventory["bindings"]["closure_sha256"],
        "release_inventory_sha256": inventory_sha256,
        "entries": [
            {
                "path": relative,
                "sha256": sha256_file(package_root / relative),
                "size": (package_root / relative).stat().st_size,
            }
            for relative in legal_paths
        ],
    }
    legal_manifest_path = package_root / "legal-manifest.json"
    write_canonical_json(legal_manifest_path, legal_manifest)
    return legal_manifest_path, sbom_path


def _harness_runtime_inventory_record(
    inventory: Mapping[str, Any], runtime_name: str
) -> Mapping[str, Any]:
    payload = inventory.get("payload")
    raw_files = payload.get("files") if isinstance(payload, Mapping) else None
    if not isinstance(raw_files, list):
        raise ReleaseBuildError("harness inventory payload closure is invalid")
    matches = [
        item
        for item in raw_files
        if isinstance(item, Mapping) and item.get("path") == runtime_name
    ]
    if len(matches) != 1:
        raise ReleaseBuildError(
            "harness inventory does not bind exactly one frozen runtime"
        )
    record = matches[0]
    if (
        not re.fullmatch(r"[0-9a-f]{64}", str(record.get("sha256", "")))
        or type(record.get("size")) is not int
        or int(record["size"]) < 0
    ):
        raise ReleaseBuildError("harness inventory runtime digest is invalid")
    return record


def _assert_file_matches_inventory_record(
    path: Path, record: Mapping[str, Any], *, label: str
) -> None:
    if record["sha256"] != sha256_file(path) or record["size"] != path.stat().st_size:
        raise ReleaseBuildError(f"{label} differs from the release inventory")


def _assert_zip_member_matches_inventory_record(
    archive_path: Path,
    member_name: str,
    record: Mapping[str, Any],
    *,
    label: str,
) -> None:
    digest = hashlib.sha256()
    total = 0
    with zipfile.ZipFile(archive_path, "r") as archive:
        matches = [item for item in archive.infolist() if item.filename == member_name]
        if len(matches) != 1 or matches[0].file_size != record["size"]:
            raise ReleaseBuildError(f"{label} differs from the release inventory")
        with archive.open(matches[0], "r") as stream:
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > record["size"]:
                    raise ReleaseBuildError(
                        f"{label} differs from the release inventory"
                    )
                digest.update(chunk)
    if total != record["size"] or digest.hexdigest() != record["sha256"]:
        raise ReleaseBuildError(f"{label} differs from the release inventory")


def _build_frozen_harness(
    *,
    project_root: Path,
    canonical_root: Path,
    output_root: Path,
    revision: str,
    source_date_epoch: int,
    build_fingerprint: str,
    lock_sha256: str,
    input_hashes: Mapping[str, str],
    components: Sequence[Mapping[str, Any]],
) -> None:
    harness_blocker_authority = [dict(item) for item in HARNESS_BLOCKER_AUTHORITY]
    harness_blocker_authority_sha256 = sha256_bytes(
        canonical_json_bytes(harness_blocker_authority)
    )
    harness_source = project_root / "packaging" / "harness"
    harness_name = "PKV-W4-LoopbackHarness-1.0.0-windows-x86_64"
    work_root = canonical_root / "harness-pyinstaller-work"
    dist_root = canonical_root / "harness-pyinstaller-dist"
    _invoke_pyinstaller(
        project_root=project_root,
        spec_path=harness_source / "pkv-loopback-provider.spec",
        work_root=work_root,
        dist_root=dist_root,
        source_date_epoch=source_date_epoch,
    )
    runtime = dist_root / "pkv-loopback-provider.exe"
    if not runtime.is_file():
        raise ReleaseBuildError("frozen loopback harness executable is missing")
    validate_x86_64_pe(runtime)

    package_root = canonical_root / "harness-payload" / harness_name
    package_root.mkdir(parents=True)
    packaged_runtime = package_root / runtime.name
    contract_source = harness_source / "contract.v1.json"
    packaged_contract = package_root / contract_source.name
    shutil.copyfile(contract_source, packaged_contract)
    packaged_scripts: list[Path] = []
    script_names = (
        "provider-error.v1.json",
        "stop.v1.json",
        "success.v1.json",
        "w4-chat-lifecycle.v1.json",
    )
    for script_name in script_names:
        source = harness_source / "scripts" / script_name
        destination = package_root / "scripts" / script_name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        packaged_scripts.append(destination)

    inventory = _build_bound_release_inventory(
        project_root=project_root,
        work_root=work_root,
        payload_root=dist_root,
        artifact_path_base="",
        bootloader_executables=[runtime.name],
        components=components,
        revision=revision,
        build_fingerprint=build_fingerprint,
        lock_sha256=lock_sha256,
        input_hashes=input_hashes,
        artifact_kind="e2e_test_harness",
        artifact_status=HARNESS_ARTIFACT_STATUS,
        release_eligible=False,
        release_blocker_authority=harness_blocker_authority,
        release_blocker_authority_sha256=harness_blocker_authority_sha256,
        release_blockers=HARNESS_BLOCKER_IDS,
    )
    runtime_record = _harness_runtime_inventory_record(inventory, runtime.name)
    _assert_file_matches_inventory_record(
        runtime,
        runtime_record,
        label="frozen harness after inventory generation",
    )
    shutil.copyfile(runtime, packaged_runtime)
    _assert_file_matches_inventory_record(
        packaged_runtime, runtime_record, label="packaged harness runtime"
    )
    inventory_path = package_root / "release-inventory.json"
    write_canonical_json(inventory_path, inventory)
    inventory_sha256 = sha256_file(inventory_path)
    legal_manifest_path, sbom_path = _write_harness_legal_materials(
        project_root=project_root,
        package_root=package_root,
        source_date_epoch=source_date_epoch,
        build_fingerprint=build_fingerprint,
        inventory=inventory,
        inventory_sha256=inventory_sha256,
        components=components,
    )

    manifest_path = package_root / "manifest.json"
    manifest_command = [
        sys.executable,
        str(harness_source / "build_manifest.py"),
        "--output",
        str(manifest_path),
        "--runtime",
        str(packaged_runtime),
        "--runtime-kind",
        "frozen",
        "--contract",
        str(packaged_contract),
    ]
    for script_path in packaged_scripts:
        manifest_command.extend(("--script", str(script_path)))
    manifest_command.extend(
        (
            "--source-revision",
            revision,
            "--build-fingerprint-sha256",
            build_fingerprint,
            "--toolchain-lock-sha256",
            lock_sha256,
        )
    )
    _run(
        manifest_command,
        cwd=harness_source,
        environment=_clean_build_environment(
            source_date_epoch,
            temporary_root=canonical_root / "harness-manifest-process-temp",
        ),
    )

    artifact_name = f"{harness_name}.zip"
    artifact_path = output_root / artifact_name
    _assert_file_matches_inventory_record(
        packaged_runtime,
        runtime_record,
        label="packaged harness runtime after manifest generation",
    )
    create_deterministic_zip(
        package_root,
        artifact_path,
        archive_root=harness_name,
        source_date_epoch=source_date_epoch,
    )
    validate_deterministic_zip(
        artifact_path,
        archive_root=harness_name,
        source_date_epoch=source_date_epoch,
    )
    _assert_file_matches_inventory_record(
        packaged_runtime,
        runtime_record,
        label="packaged harness runtime after ZIP creation",
    )
    _assert_zip_member_matches_inventory_record(
        artifact_path,
        f"{harness_name}/{runtime.name}",
        runtime_record,
        label="harness ZIP runtime",
    )
    artifact_hash = sha256_file(artifact_path)
    (output_root / f"{artifact_name}.sha256").write_text(
        f"{artifact_hash}  {artifact_name}\n", encoding="ascii", newline="\n"
    )
    write_canonical_json(
        output_root / f"{harness_name}.provenance.json",
        {
            "schema_version": "pkv.w3-harness-provenance.v1",
            "artifact_file": artifact_name,
            "artifact_sha256": artifact_hash,
            "artifact_size": artifact_path.stat().st_size,
            "artifact_status": HARNESS_ARTIFACT_STATUS,
            "build_fingerprint": build_fingerprint,
            "contract_sha256": sha256_file(packaged_contract),
            "harness_version": "1.0.0",
            "artifact_kind": "e2e_test_harness",
            "legal_manifest_path": f"{harness_name}/legal-manifest.json",
            "legal_manifest_sha256": sha256_file(legal_manifest_path),
            "manifest_path": f"{harness_name}/manifest.json",
            "manifest_sha256": sha256_file(manifest_path),
            "release_blocker_authority": harness_blocker_authority,
            "release_blocker_authority_sha256": harness_blocker_authority_sha256,
            "release_blockers": list(HARNESS_BLOCKER_IDS),
            "release_eligible": False,
            "release_inventory_closure_sha256": inventory["bindings"]["closure_sha256"],
            "release_inventory_path": f"{harness_name}/release-inventory.json",
            "release_inventory_sha256": inventory_sha256,
            "release_payload_membership": "forbidden",
            "runtime_path": f"{harness_name}/pkv-loopback-provider.exe",
            "runtime_sha256": sha256_file(packaged_runtime),
            "sbom_path": f"{harness_name}/sbom.cdx.json",
            "sbom_sha256": sha256_file(sbom_path),
            "source_revision": revision,
            "toolchain_lock_sha256": lock_sha256,
        },
    )


def _build_compliance_sources(
    *,
    project_root: Path,
    output_root: Path,
    revision: str,
    build_fingerprint: str,
    compliance: Mapping[str, Any],
) -> None:
    output_root.mkdir(parents=True)
    source_name = "html2text-2020.1.16.tar.gz"
    source = project_root / "packaging" / "compliance-sources" / source_name
    destination = output_root / source_name
    shutil.copyfile(source, destination)
    source_hash = sha256_file(destination)
    expected = COMPLIANCE_ARTIFACT_CONTRACT["html2text-2020.1.16-sdist"]
    if destination.stat().st_size != expected[1] or source_hash != expected[2]:
        raise ReleaseBuildError("published html2text corresponding source differs")
    (output_root / f"{source_name}.sha256").write_text(
        f"{source_hash}  {source_name}\n", encoding="ascii", newline="\n"
    )
    manifest = {
        "schema_version": "pkv.compliance-source-bundle.v1",
        "artifact_kind": "corresponding_source_bundle",
        "build_fingerprint": build_fingerprint,
        "compliance_manifest_sha256": compliance["compliance_manifest_sha256"],
        "files": [
            {
                "component": "html2text",
                "license_expression_assessment": "GPL-3.0-only",
                "license_expression_status": "requires_legal_confirmation",
                "path": source_name,
                "sha256": source_hash,
                "size": destination.stat().st_size,
                "version": "2020.1.16",
            }
        ],
        "release_blockers": list(compliance["release_blockers"]),
        "release_blocker_authority": list(compliance["release_blocker_authority"]),
        "release_blocker_authority_sha256": compliance[
            "release_blocker_authority_sha256"
        ],
        "release_eligible": bool(compliance["release_eligible"]),
        "source_revision": revision,
    }
    manifest_path = output_root / "manifest.json"
    write_canonical_json(manifest_path, manifest)
    write_canonical_json(
        output_root / "provenance.json",
        {
            "schema_version": "pkv.compliance-source-provenance.v1",
            "artifact_kind": "corresponding_source_bundle",
            "build_fingerprint": build_fingerprint,
            "compliance_manifest_sha256": compliance["compliance_manifest_sha256"],
            "manifest_sha256": sha256_file(manifest_path),
            "release_blockers": list(compliance["release_blockers"]),
            "release_blocker_authority": list(compliance["release_blocker_authority"]),
            "release_blocker_authority_sha256": compliance[
                "release_blocker_authority_sha256"
            ],
            "release_eligible": bool(compliance["release_eligible"]),
            "source_file": source_name,
            "source_sha256": source_hash,
            "source_revision": revision,
        },
    )


def _build_once(
    *,
    project_root: Path,
    canonical_root: Path,
    contract: Mapping[str, Any],
    policy: Mapping[str, Any],
    revision: str,
    source_date_epoch: int,
    components: Sequence[Mapping[str, Any]],
    compliance: Mapping[str, Any],
    lock_sha256: str,
    input_hashes: Mapping[str, str],
) -> Path:
    lock_input = "packaging/locks/release-environment.v2.json"
    compliance_input = "packaging/compliance-sources.v1.json"
    if (
        not re.fullmatch(r"[0-9a-f]{64}", lock_sha256)
        or input_hashes.get(lock_input) != lock_sha256
        or input_hashes.get(compliance_input)
        != compliance.get("compliance_manifest_sha256")
    ):
        raise ReleaseBuildError(
            "build inputs do not bind the environment/compliance authority hashes"
        )
    _safe_rmtree(canonical_root, authority=canonical_root.parent)
    canonical_root.mkdir(parents=True)
    pyi_work = canonical_root / "pyinstaller-work"
    pyi_dist = canonical_root / "pyinstaller-dist"
    release_root = canonical_root / "payload" / str(contract["archive_root"])
    output_root = canonical_root / "output"
    release_eligible = bool(compliance["release_eligible"])
    release_blockers = list(compliance["release_blockers"])
    if release_eligible == bool(release_blockers):
        raise ReleaseBuildError("compliance eligibility/blocker state is inconsistent")
    artifact_channel = "release" if release_eligible else "candidate"
    routing = contract["artifact_routing"]
    artifact_kind = (
        routing["release_artifact_kind"]
        if release_eligible
        else routing["candidate_artifact_kind"]
    )
    artifact_output_root = output_root / artifact_channel
    harness_output_root = output_root / "e2e-harness"
    compliance_output_root = output_root / "compliance-sources"
    release_root.mkdir(parents=True)
    artifact_output_root.mkdir(parents=True)
    harness_output_root.mkdir(parents=True)

    hardlink_evidence = _conda_hardlink_threat_evidence(
        release_eligible=release_eligible
    )
    if release_eligible:
        _assert_copy_only_release_environment(Path(sys.prefix))
    toolchain = _toolchain_info(components, hardlink_evidence=hardlink_evidence)
    fingerprint = _build_fingerprint(
        version=str(contract["version"]),
        revision=revision,
        source_date_epoch=source_date_epoch,
        inputs=input_hashes,
        toolchain=toolchain,
    )

    spec_path = project_root / str(contract["pyinstaller_spec"])
    _invoke_pyinstaller(
        project_root=project_root,
        spec_path=spec_path,
        work_root=pyi_work,
        dist_root=pyi_dist,
        source_date_epoch=source_date_epoch,
    )
    collect_root = pyi_dist / str(contract["pyinstaller_collect_dir"])
    if not collect_root.is_dir():
        raise ReleaseBuildError(
            f"PyInstaller collect directory is missing: {collect_root}"
        )
    shutil.copytree(collect_root, release_root / "app")
    for entrypoint in contract["entrypoints"]:
        validate_x86_64_pe(release_root / str(entrypoint["path"]))
    inventory = _build_bound_release_inventory(
        project_root=project_root,
        work_root=pyi_work,
        payload_root=release_root / "app",
        artifact_path_base="app",
        bootloader_executables=[
            PurePosixPath(str(entrypoint["path"])).name
            for entrypoint in contract["entrypoints"]
        ],
        components=components,
        revision=revision,
        build_fingerprint=fingerprint,
        lock_sha256=lock_sha256,
        input_hashes=input_hashes,
        artifact_kind=artifact_kind,
        artifact_status=str(compliance["artifact_status"]),
        release_eligible=release_eligible,
        release_blocker_authority=compliance["release_blocker_authority"],
        release_blocker_authority_sha256=str(
            compliance["release_blocker_authority_sha256"]
        ),
        release_blockers=release_blockers,
    )
    inventory_path = release_root / "release-inventory.json"
    write_canonical_json(inventory_path, inventory)
    inventory_sha256 = sha256_file(inventory_path)
    _copy_release_shell(project_root, release_root)
    if not release_eligible:
        hold_lines = [
            "TEST CANDIDATE - COMPLIANCE HOLD - NOT FOR DISTRIBUTION",
            "=======================================================",
            "",
            "This unsigned Artifact exists only for deterministic build validation and W4 functional E2E.",
            "It is not eligible for release or end-user distribution.",
            "",
            "Release blockers:",
            *[f"- {identifier}" for identifier in release_blockers],
            "",
            "Authoritative machine-readable status: build-info.json and the external provenance sidecar.",
            "Corresponding-source candidate bundle: ../compliance-sources/ (outside this ZIP).",
            "",
        ]
        hold_text = "\n".join(hold_lines)
        (release_root / "COMPLIANCE-HOLD.txt").write_text(
            hold_text, encoding="utf-8", newline="\n"
        )
        notices_path = release_root / "THIRD-PARTY-NOTICES.txt"
        notices = notices_path.read_text(encoding="utf-8")
        notices_path.write_text(
            hold_text + "\n" + notices,
            encoding="utf-8",
            newline="\n",
        )

    build_info = {
        "schema_version": SCHEMA_BUILD_INFO,
        "version": str(contract["version"]),
        "target": str(contract["target"]),
        "source_revision": revision,
        "source_tree_clean": True,
        "source_date_epoch": source_date_epoch,
        "zip_timestamp_epoch": max(source_date_epoch, ZIP_MIN_EPOCH)
        - max(source_date_epoch, ZIP_MIN_EPOCH) % 2,
        "build_fingerprint": fingerprint,
        "artifact_kind": artifact_kind,
        "artifact_status": compliance["artifact_status"],
        "compliance_manifest_sha256": compliance["compliance_manifest_sha256"],
        "conda_hardlink_threat_evidence": hardlink_evidence,
        "release_blocker_authority": list(compliance["release_blocker_authority"]),
        "release_blocker_authority_sha256": compliance[
            "release_blocker_authority_sha256"
        ],
        "release_blockers": release_blockers,
        "release_eligible": release_eligible,
        "release_inventory_artifact_closure_sha256": inventory["bindings"][
            "artifact_closure_sha256"
        ],
        "release_inventory_closure_sha256": inventory["bindings"]["closure_sha256"],
        "release_inventory_path": "release-inventory.json",
        "release_inventory_sha256": inventory_sha256,
        "inputs": dict(input_hashes),
        "toolchain": toolchain,
    }
    write_canonical_json(release_root / "build-info.json", build_info)
    dependency_manifest = make_dependency_manifest(
        components,
        lock_sha256,
        compliance=compliance,
        inventory=inventory,
        inventory_sha256=inventory_sha256,
    )
    write_canonical_json(release_root / "dependency-manifest.json", dependency_manifest)
    collect_license_materials(
        components, release_root / "licenses", project_root=project_root
    )
    bound_license_index = _bind_license_index_to_inventory(
        release_root / "licenses" / "index.json",
        inventory=inventory,
        locked_components=components,
        release_eligible=release_eligible,
    )
    write_canonical_json(
        release_root / "sbom.cdx.json",
        make_sbom(
            components,
            version=str(contract["version"]),
            source_date_epoch=source_date_epoch,
            compliance=compliance,
            inventory=inventory,
            inventory_sha256=inventory_sha256,
            license_index=bound_license_index,
        ),
    )

    scan_payload(release_root, policy, allow_missing={"payload-manifest.json"})
    payload_manifest = generate_payload_manifest(release_root, fingerprint)
    write_canonical_json(release_root / "payload-manifest.json", payload_manifest)
    scan_payload(release_root, policy)

    artifact_name = f"{contract['archive_root']}.zip"
    artifact_path = artifact_output_root / artifact_name
    create_deterministic_zip(
        release_root,
        artifact_path,
        archive_root=str(contract["archive_root"]),
        source_date_epoch=source_date_epoch,
    )
    validate_deterministic_zip(
        artifact_path,
        archive_root=str(contract["archive_root"]),
        source_date_epoch=source_date_epoch,
    )
    validate_artifact_payload(
        artifact_path,
        archive_root=str(contract["archive_root"]),
        expected_build_fingerprint=fingerprint,
    )
    artifact_sha = sha256_file(artifact_path)
    (artifact_output_root / f"{artifact_name}.sha256").write_text(
        f"{artifact_sha}  {artifact_name}\n", encoding="ascii", newline="\n"
    )
    _build_compliance_sources(
        project_root=project_root,
        output_root=compliance_output_root,
        revision=revision,
        build_fingerprint=fingerprint,
        compliance=compliance,
    )
    compliance_manifest_path = compliance_output_root / "manifest.json"
    compliance_provenance_path = compliance_output_root / "provenance.json"
    compliance_source_path = compliance_output_root / "html2text-2020.1.16.tar.gz"
    provenance = {
        "schema_version": SCHEMA_PROVENANCE,
        "artifact_file": artifact_name,
        "artifact_kind": artifact_kind,
        "artifact_status": compliance["artifact_status"],
        "artifact_sha256": artifact_sha,
        "artifact_size": artifact_path.stat().st_size,
        "build_info_path": f"{contract['archive_root']}/build-info.json",
        "build_info_sha256": sha256_file(release_root / "build-info.json"),
        "build_fingerprint": fingerprint,
        "compliance_manifest_sha256": compliance["compliance_manifest_sha256"],
        "conda_hardlink_threat_evidence": hardlink_evidence,
        "compliance_sources": {
            "manifest_path": "../compliance-sources/manifest.json",
            "manifest_sha256": sha256_file(compliance_manifest_path),
            "provenance_path": "../compliance-sources/provenance.json",
            "provenance_sha256": sha256_file(compliance_provenance_path),
            "root": "../compliance-sources",
            "source_file": "html2text-2020.1.16.tar.gz",
            "source_sha256": sha256_file(compliance_source_path),
            "source_size": compliance_source_path.stat().st_size,
        },
        "payload_manifest_path": f"{contract['archive_root']}/payload-manifest.json",
        "payload_manifest_sha256": sha256_file(release_root / "payload-manifest.json"),
        "sbom_path": f"{contract['archive_root']}/sbom.cdx.json",
        "sbom_sha256": sha256_file(release_root / "sbom.cdx.json"),
        "source_revision": revision,
        "release_blockers": release_blockers,
        "release_blocker_authority": list(compliance["release_blocker_authority"]),
        "release_blocker_authority_sha256": compliance[
            "release_blocker_authority_sha256"
        ],
        "release_eligible": release_eligible,
        "release_inventory_artifact_closure_sha256": inventory["bindings"][
            "artifact_closure_sha256"
        ],
        "release_inventory_closure_sha256": inventory["bindings"]["closure_sha256"],
        "release_inventory_path": f"{contract['archive_root']}/release-inventory.json",
        "release_inventory_sha256": inventory_sha256,
        "version": str(contract["version"]),
    }
    write_canonical_json(
        artifact_output_root / f"{contract['archive_root']}.provenance.json", provenance
    )
    _build_frozen_harness(
        project_root=project_root,
        canonical_root=canonical_root,
        output_root=harness_output_root,
        revision=revision,
        source_date_epoch=source_date_epoch,
        build_fingerprint=fingerprint,
        lock_sha256=lock_sha256,
        input_hashes=input_hashes,
        components=components,
    )
    expected_artifact_files = {
        artifact_name,
        f"{artifact_name}.sha256",
        f"{contract['archive_root']}.provenance.json",
    }
    actual_artifact_files = {
        path.name for path in artifact_output_root.iterdir() if path.is_file()
    }
    if actual_artifact_files != expected_artifact_files or any(
        path.is_dir() for path in artifact_output_root.iterdir()
    ):
        raise ReleaseBuildError("candidate/release output contains unexpected files")
    harness_name = "PKV-W4-LoopbackHarness-1.0.0-windows-x86_64"
    expected_harness_files = {
        f"{harness_name}.zip",
        f"{harness_name}.zip.sha256",
        f"{harness_name}.provenance.json",
    }
    actual_harness_files = {
        path.name for path in harness_output_root.iterdir() if path.is_file()
    }
    if actual_harness_files != expected_harness_files or any(
        path.is_dir() for path in harness_output_root.iterdir()
    ):
        raise ReleaseBuildError("E2E harness output contains unexpected files")
    expected_compliance_files = {
        "html2text-2020.1.16.tar.gz",
        "html2text-2020.1.16.tar.gz.sha256",
        "manifest.json",
        "provenance.json",
    }
    actual_compliance_files = {
        path.name for path in compliance_output_root.iterdir() if path.is_file()
    }
    if actual_compliance_files != expected_compliance_files or any(
        path.is_dir() for path in compliance_output_root.iterdir()
    ):
        raise ReleaseBuildError("compliance-source output contains unexpected files")
    return output_root


def _directory_hashes(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): sha256_file(path)
        for path in sorted(root.rglob("*"), key=lambda item: item.as_posix())
        if path.is_file()
    }


def _is_windows_host() -> bool:
    """Return whether this interpreter is running on Windows.

    This narrow wrapper keeps the host gate independently testable without
    mutating ``os.name`` (which would change pathlib behaviour process-wide).
    """

    return os.name == "nt"


def _python_is_64_bit() -> bool:
    """Return whether the running Python process has 64-bit pointers."""

    return sys.maxsize > 2**32


def _query_windows_native_architecture() -> tuple[str, bool] | None:
    """Return ``(native_architecture, is_wow64_or_emulated)`` from Win32.

    Environment variables such as PROCESSOR_ARCHITECTURE are process-launch
    metadata, not an authority for a reproducible release gate.  Prefer
    IsWow64Process2 because it identifies both the process and native machine.
    IsWow64Process plus GetNativeSystemInfo is not a valid release fallback:
    x64 emulation on ARM64 can report an x64 process/native pair there.  An
    unavailable or failed query is intentionally represented as ``None`` so
    callers fail closed.
    """

    if not _is_windows_host():
        return None

    try:
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        get_current_process = kernel32.GetCurrentProcess
        get_current_process.argtypes = []
        get_current_process.restype = wintypes.HANDLE
        current_process = get_current_process()

        is_wow64_process2 = getattr(kernel32, "IsWow64Process2", None)
        if is_wow64_process2 is None:
            return None

        process_machine = wintypes.USHORT()
        native_machine = wintypes.USHORT()
        is_wow64_process2.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.USHORT),
            ctypes.POINTER(wintypes.USHORT),
        ]
        is_wow64_process2.restype = wintypes.BOOL
        if not is_wow64_process2(
            current_process,
            ctypes.byref(process_machine),
            ctypes.byref(native_machine),
        ):
            return None

        native_architecture = {
            0x8664: "amd64",  # IMAGE_FILE_MACHINE_AMD64
            0xAA64: "arm64",  # IMAGE_FILE_MACHINE_ARM64
            0x014C: "x86",  # IMAGE_FILE_MACHINE_I386
        }.get(native_machine.value, "unknown")
        # IMAGE_FILE_MACHINE_UNKNOWN means the process is native.  Any
        # non-zero process machine is WOW64 or another emulation layer.
        return native_architecture, process_machine.value != 0
    except (AttributeError, OSError):
        return None


def _validate_windows_release_host() -> None:
    if not _is_windows_host() or not _python_is_64_bit():
        raise ReleaseBuildError(
            "release Artifact must be built by native Windows x86-64 Python"
        )

    host_architecture = _query_windows_native_architecture()
    if host_architecture != ("amd64", False):
        raise ReleaseBuildError(
            "release Artifact must be built by native Windows x86-64 Python"
        )


def validate_x86_64_pe(path: Path) -> None:
    try:
        with path.open("rb") as stream:
            if stream.read(2) != b"MZ":
                raise ReleaseBuildError(f"release executable lacks DOS header: {path}")
            stream.seek(0x3C)
            raw_offset = stream.read(4)
            if len(raw_offset) != 4:
                raise ReleaseBuildError(
                    f"release executable has a short DOS header: {path}"
                )
            pe_offset = struct.unpack("<I", raw_offset)[0]
            if pe_offset < 0x40 or pe_offset > 16 * 1024 * 1024:
                raise ReleaseBuildError(
                    f"release executable PE offset is unsafe: {path}"
                )
            stream.seek(pe_offset)
            header = stream.read(6)
    except OSError as exc:
        raise ReleaseBuildError(f"cannot inspect release executable: {path}") from exc
    if len(header) != 6 or header[:4] != b"PE\0\0":
        raise ReleaseBuildError(f"release executable lacks PE signature: {path}")
    machine = struct.unpack("<H", header[4:])[0]
    if machine != 0x8664:
        raise ReleaseBuildError(f"release executable is not PE x86-64: {path}")


def _load_snapshot_release_inputs(
    source_root: Path,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    contract = _load_json(source_root / "packaging" / "release-contract.v1.json")
    policy = _load_json(source_root / "packaging" / "payload-policy.v1.json")
    lock = _load_json(
        source_root / "packaging" / "locks" / "release-environment.v2.json"
    )
    validate_release_contract(contract)
    validate_payload_policy(policy)
    if lock.get("schema_version") != "pkv.release-environment-lock.v2":
        raise ReleaseBuildError("unsupported release environment lock schema")
    return contract, policy, lock


def build_release(project_root: Path) -> Path:
    lexical_project_root = Path(os.path.abspath(project_root))
    _assert_safe_directory_chain(
        lexical_project_root, authority=lexical_project_root.parent
    )
    project_root = lexical_project_root.resolve(strict=True)
    _validate_windows_release_host()

    revision, source_date_epoch = git_release_identity(project_root)
    dist_root = _prepare_dist_root(project_root)
    compare_root = dist_root / ".w3-repro"
    release_root = dist_root / "release"
    candidate_root = dist_root / "candidate"
    harness_root = dist_root / "e2e-harness"
    compliance_root = dist_root / "compliance-sources"
    _safe_rmtree(compare_root, authority=project_root)
    _safe_rmtree(release_root, authority=project_root)
    _safe_rmtree(candidate_root, authority=project_root)
    _safe_rmtree(harness_root, authority=project_root)
    _safe_rmtree(compliance_root, authority=project_root)
    compare_root.mkdir(parents=True)

    first_physical_root = compare_root / "physical-a"
    second_physical_root = compare_root / "physical-b"
    first_source = first_physical_root / "source"
    second_source = second_physical_root / "source"
    _materialize_git_head(
        project_root,
        first_source,
        revision=revision,
        source_date_epoch=source_date_epoch,
    )
    _materialize_git_head(
        project_root,
        second_source,
        revision=revision,
        source_date_epoch=source_date_epoch,
    )
    first_source_tree = _source_tree_fingerprint(first_source)
    second_source_tree = _source_tree_fingerprint(second_source)
    if first_source_tree != second_source_tree:
        raise ReleaseBuildError(
            "independent git-archived source trees are not byte-identical"
        )
    first_contract, first_policy, first_lock = _load_snapshot_release_inputs(
        first_source
    )
    second_contract, second_policy, second_lock = _load_snapshot_release_inputs(
        second_source
    )
    if (
        canonical_json_bytes(first_contract) != canonical_json_bytes(second_contract)
        or canonical_json_bytes(first_policy) != canonical_json_bytes(second_policy)
        or canonical_json_bytes(first_lock) != canonical_json_bytes(second_lock)
    ):
        raise ReleaseBuildError(
            "independent git-archived release contracts are not byte-identical"
        )
    first_compliance = validate_compliance_sources(first_source)
    second_compliance = validate_compliance_sources(second_source)
    if first_compliance != second_compliance:
        raise ReleaseBuildError(
            "independent git-archived compliance authorities differ"
        )
    first_inputs = _input_hashes(first_source, first_contract)
    second_inputs = _input_hashes(second_source, second_contract)
    first_inputs["__git_head_tree_sha256"] = str(first_source_tree["sha256"])
    second_inputs["__git_head_tree_sha256"] = str(second_source_tree["sha256"])
    if first_inputs != second_inputs:
        raise ReleaseBuildError(
            "independent git-archived source inputs are not byte-identical"
        )
    first_lock_sha = sha256_file(
        first_source / "packaging" / "locks" / "release-environment.v2.json"
    )
    second_lock_sha = sha256_file(
        second_source / "packaging" / "locks" / "release-environment.v2.json"
    )
    if first_lock_sha != second_lock_sha:
        raise ReleaseBuildError("independent git-archived environment locks differ")
    lock_sha = first_lock_sha

    first_components = validate_environment_lock(first_lock)
    first_output = _build_once(
        project_root=first_source,
        canonical_root=first_physical_root / "build",
        contract=first_contract,
        policy=first_policy,
        revision=revision,
        source_date_epoch=source_date_epoch,
        components=first_components,
        compliance=first_compliance,
        lock_sha256=lock_sha,
        input_hashes=first_inputs,
    )
    if (
        validate_environment_lock(first_lock) != first_components
        or _source_tree_fingerprint(first_source) != first_source_tree
        or validate_compliance_sources(first_source) != first_compliance
        or {
            **_input_hashes(first_source, first_contract),
            "__git_head_tree_sha256": str(first_source_tree["sha256"]),
        }
        != first_inputs
    ):
        raise ReleaseBuildError("release environment changed during first build")

    second_components = validate_environment_lock(second_lock)
    if second_components != first_components:
        raise ReleaseBuildError(
            "release environment changed between independent builds"
        )
    second_output = _build_once(
        project_root=second_source,
        canonical_root=second_physical_root / "build",
        contract=second_contract,
        policy=second_policy,
        revision=revision,
        source_date_epoch=source_date_epoch,
        components=second_components,
        compliance=second_compliance,
        lock_sha256=lock_sha,
        input_hashes=second_inputs,
    )
    if (
        validate_environment_lock(second_lock) != second_components
        or _source_tree_fingerprint(second_source) != second_source_tree
        or validate_compliance_sources(second_source) != second_compliance
        or {
            **_input_hashes(second_source, second_contract),
            "__git_head_tree_sha256": str(second_source_tree["sha256"]),
        }
        != second_inputs
    ):
        raise ReleaseBuildError("release environment changed during second build")
    first_hashes = _directory_hashes(first_output)
    second_hashes = _directory_hashes(second_output)
    if first_hashes != second_hashes:
        diagnostic = {
            "schema_version": "pkv.reproducibility-diagnostic.v1",
            "first": first_hashes,
            "second": second_hashes,
        }
        write_canonical_json(dist_root / "reproducibility-failure.json", diagnostic)
        raise ReleaseBuildError(
            "independent unsigned builds are not byte-identical; Artifact was not published"
        )
    if (
        _source_tree_fingerprint(first_source) != first_source_tree
        or _source_tree_fingerprint(second_source) != second_source_tree
        or validate_environment_lock(second_lock) != second_components
    ):
        raise ReleaseBuildError("release authority changed before publication")

    release_eligible = bool(second_compliance["release_eligible"])
    artifact_channel = "release" if release_eligible else "candidate"
    artifact_root = release_root if release_eligible else candidate_root
    artifact_stage = dist_root / ".artifact-publish"
    harness_stage = dist_root / ".harness-publish"
    compliance_stage = dist_root / ".compliance-publish"
    _safe_rmtree(artifact_stage, authority=project_root)
    _safe_rmtree(harness_stage, authority=project_root)
    _safe_rmtree(compliance_stage, authority=project_root)
    if _directory_hashes(second_output) != second_hashes:
        raise ReleaseBuildError("verified output changed before publication staging")
    shutil.copytree(second_output / artifact_channel, artifact_stage)
    shutil.copytree(second_output / "e2e-harness", harness_stage)
    shutil.copytree(second_output / "compliance-sources", compliance_stage)
    expected_artifact_hashes = {
        relative.removeprefix(f"{artifact_channel}/"): digest
        for relative, digest in second_hashes.items()
        if relative.startswith(f"{artifact_channel}/")
    }
    expected_harness_hashes = {
        relative.removeprefix("e2e-harness/"): digest
        for relative, digest in second_hashes.items()
        if relative.startswith("e2e-harness/")
    }
    expected_compliance_hashes = {
        relative.removeprefix("compliance-sources/"): digest
        for relative, digest in second_hashes.items()
        if relative.startswith("compliance-sources/")
    }
    if (
        _directory_hashes(artifact_stage) != expected_artifact_hashes
        or _directory_hashes(harness_stage) != expected_harness_hashes
        or _directory_hashes(compliance_stage) != expected_compliance_hashes
        or _directory_hashes(second_output) != second_hashes
    ):
        raise ReleaseBuildError(
            "publication staging bytes differ from verified outputs"
        )
    compliance_stage.replace(compliance_root)
    harness_stage.replace(harness_root)
    artifact_stage.replace(artifact_root)
    if not release_eligible and release_root.exists():
        raise ReleaseBuildError(
            "compliance-held candidate must not publish dist/release"
        )
    _safe_rmtree(compare_root, authority=project_root)
    return artifact_root


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--project-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    arguments = parser.parse_args(argv)
    try:
        output = build_release(arguments.project_root)
    except ReleaseBuildError as exc:
        print(f"W3 release build failed: {exc}", file=sys.stderr)
        return 1
    kind = "release" if output.name == "release" else "test candidate"
    print(f"W3 reproducible {kind} build complete: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
