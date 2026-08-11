"""Synthetic fail-closed contracts for the W4 Artifact E2E controller.

These tests inspect or execute only the external PowerShell driver boundary.
They do not import ``src`` and do not require a built product Artifact.
"""

from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
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
SCENARIO_CONTRACT = DRIVER_ROOT / "scenarios.v2.json"
DRIVER_EXPORTER = DRIVER_ROOT / "Export-W4DriverBundle.ps1"
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


_PROCESS_QUERY_INFORMATION = 0x0400
_SYNCHRONIZE = 0x00100000
_ERROR_INVALID_PARAMETER = 87
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_FILETIME_TO_DATETIME_TICKS = 504_911_232_000_000_000
_STILL_ACTIVE = 259


class _W4IoCounters(ctypes.Structure):
    _fields_ = [
        ("read_operation_count", ctypes.c_uint64),
        ("write_operation_count", ctypes.c_uint64),
        ("other_operation_count", ctypes.c_uint64),
        ("read_transfer_count", ctypes.c_uint64),
        ("write_transfer_count", ctypes.c_uint64),
        ("other_transfer_count", ctypes.c_uint64),
    ]


class _W4JobBasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("per_process_user_time_limit", ctypes.c_int64),
        ("per_job_user_time_limit", ctypes.c_int64),
        ("limit_flags", wintypes.DWORD),
        ("minimum_working_set_size", ctypes.c_size_t),
        ("maximum_working_set_size", ctypes.c_size_t),
        ("active_process_limit", wintypes.DWORD),
        ("affinity", ctypes.c_size_t),
        ("priority_class", wintypes.DWORD),
        ("scheduling_class", wintypes.DWORD),
    ]


class _W4JobExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("basic_limit_information", _W4JobBasicLimitInformation),
        ("io_info", _W4IoCounters),
        ("process_memory_limit", ctypes.c_size_t),
        ("job_memory_limit", ctypes.c_size_t),
        ("peak_process_memory_used", ctypes.c_size_t),
        ("peak_job_memory_used", ctypes.c_size_t),
    ]


if os.name == "nt":
    _W4_KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _W4_KERNEL32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
    _W4_KERNEL32.CreateJobObjectW.restype = wintypes.HANDLE
    _W4_KERNEL32.SetInformationJobObject.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    )
    _W4_KERNEL32.SetInformationJobObject.restype = wintypes.BOOL
    _W4_KERNEL32.OpenProcess.argtypes = (
        wintypes.DWORD,
        wintypes.BOOL,
        wintypes.DWORD,
    )
    _W4_KERNEL32.OpenProcess.restype = wintypes.HANDLE
    _W4_KERNEL32.GetProcessId.argtypes = (wintypes.HANDLE,)
    _W4_KERNEL32.GetProcessId.restype = wintypes.DWORD
    _W4_KERNEL32.AssignProcessToJobObject.argtypes = (
        wintypes.HANDLE,
        wintypes.HANDLE,
    )
    _W4_KERNEL32.AssignProcessToJobObject.restype = wintypes.BOOL
    _W4_KERNEL32.IsProcessInJob.argtypes = (
        wintypes.HANDLE,
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.BOOL),
    )
    _W4_KERNEL32.IsProcessInJob.restype = wintypes.BOOL
    _W4_KERNEL32.GetProcessTimes.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
    )
    _W4_KERNEL32.GetProcessTimes.restype = wintypes.BOOL
    _W4_KERNEL32.GetExitCodeProcess.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    )
    _W4_KERNEL32.GetExitCodeProcess.restype = wintypes.BOOL
    _W4_KERNEL32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
    _W4_KERNEL32.TerminateJobObject.restype = wintypes.BOOL
    _W4_KERNEL32.CloseHandle.argtypes = (wintypes.HANDLE,)
    _W4_KERNEL32.CloseHandle.restype = wintypes.BOOL
else:
    _W4_KERNEL32 = None


def _w4_raise_last_error(operation: str) -> None:
    error = ctypes.get_last_error()
    raise OSError(error, f"{operation} failed: {ctypes.FormatError(error).strip()}")


def _w4_filetime_to_datetime_ticks(value: wintypes.FILETIME) -> int:
    raw = (int(value.dwHighDateTime) << 32) | int(value.dwLowDateTime)
    return raw + _FILETIME_TO_DATETIME_TICKS


def _w4_close_handle(handle: wintypes.HANDLE) -> None:
    assert _W4_KERNEL32 is not None
    if not _W4_KERNEL32.CloseHandle(handle):
        _w4_raise_last_error("CloseHandle")


def _w4_open_process_for_identity(process_id: int) -> wintypes.HANDLE | None:
    assert _W4_KERNEL32 is not None
    access = _PROCESS_QUERY_INFORMATION | _SYNCHRONIZE
    handle = _W4_KERNEL32.OpenProcess(access, False, process_id)
    if handle:
        return handle
    if ctypes.get_last_error() == _ERROR_INVALID_PARAMETER:
        return None
    _w4_raise_last_error(f"OpenProcess(pid={process_id})")


def _w4_process_start_ticks(handle: wintypes.HANDLE) -> int:
    assert _W4_KERNEL32 is not None
    created = wintypes.FILETIME()
    exited = wintypes.FILETIME()
    kernel = wintypes.FILETIME()
    user = wintypes.FILETIME()
    if not _W4_KERNEL32.GetProcessTimes(
        handle,
        ctypes.byref(created),
        ctypes.byref(exited),
        ctypes.byref(kernel),
        ctypes.byref(user),
    ):
        _w4_raise_last_error("GetProcessTimes")
    return _w4_filetime_to_datetime_ticks(created)


class _W4KillOnCloseJob:
    """Test-only ownership of a job that kills its entire tree when closed."""

    def __init__(self) -> None:
        if _W4_KERNEL32 is None:
            raise RuntimeError("W4 UIA probe requires Windows Job Object APIs")
        if ctypes.sizeof(_W4JobBasicLimitInformation) not in (48, 64):
            raise RuntimeError("unexpected JOBOBJECT_BASIC_LIMIT_INFORMATION layout")
        if ctypes.sizeof(_W4JobExtendedLimitInformation) not in (112, 144):
            raise RuntimeError("unexpected JOBOBJECT_EXTENDED_LIMIT_INFORMATION layout")
        handle = _W4_KERNEL32.CreateJobObjectW(None, None)
        if not handle:
            _w4_raise_last_error("CreateJobObjectW")
        self._handle: wintypes.HANDLE | None = handle
        try:
            information = _W4JobExtendedLimitInformation()
            information.basic_limit_information.limit_flags = (
                _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            )
            if not _W4_KERNEL32.SetInformationJobObject(
                handle,
                _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                ctypes.cast(ctypes.byref(information), ctypes.c_void_p),
                ctypes.sizeof(information),
            ):
                _w4_raise_last_error("SetInformationJobObject")
        except BaseException:
            _w4_close_handle(handle)
            self._handle = None
            raise

    def assign_controller_handle(self, controller: subprocess.Popen[str]) -> int:
        assert _W4_KERNEL32 is not None
        if self._handle is None:
            raise RuntimeError("Job Object handle is already closed")
        raw_handle = getattr(controller, "_handle", None)
        if raw_handle is None:
            raise RuntimeError("controller did not expose its retained native process handle")
        try:
            controller_handle = wintypes.HANDLE(int(raw_handle))
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                "controller retained native process handle was not representable"
            ) from exc
        expected_process_id = int(controller.pid)
        observed_process_id = int(_W4_KERNEL32.GetProcessId(controller_handle))
        if observed_process_id <= 0:
            _w4_raise_last_error("GetProcessId(controller handle)")
        if observed_process_id != expected_process_id:
            raise RuntimeError("controller retained handle did not bind its Popen PID")
        if controller.poll() is not None:
            raise RuntimeError("controller exited before Job Object assignment")
        start_ticks = _w4_process_start_ticks(controller_handle)
        if not _W4_KERNEL32.AssignProcessToJobObject(self._handle, controller_handle):
            _w4_raise_last_error(
                f"AssignProcessToJobObject(controller pid={expected_process_id})"
            )
        in_job = wintypes.BOOL()
        if not _W4_KERNEL32.IsProcessInJob(
            controller_handle, self._handle, ctypes.byref(in_job)
        ):
            _w4_raise_last_error("IsProcessInJob(controller handle)")
        if not bool(in_job.value):
            raise RuntimeError("UIA selection probe controller was not contained by its Job Object")
        if controller.poll() is not None:
            raise RuntimeError("controller exited immediately after Job Object assignment")
        return start_ticks

    def assert_process_is_member(
        self,
        process_id: int,
        *,
        expected_start_ticks: int | None = None,
        label: str,
    ) -> int:
        assert _W4_KERNEL32 is not None
        if self._handle is None:
            raise RuntimeError("Job Object handle is already closed")
        process_handle = _w4_open_process_for_identity(process_id)
        if process_handle is None:
            raise RuntimeError(f"UIA selection probe {label} exited before Job Object validation")
        try:
            actual_start_ticks = _w4_process_start_ticks(process_handle)
            if (
                expected_start_ticks is not None
                and actual_start_ticks != expected_start_ticks
            ):
                raise RuntimeError(
                    f"UIA selection probe {label} identity did not match its ready record"
                )
            in_job = wintypes.BOOL()
            if not _W4_KERNEL32.IsProcessInJob(
                process_handle, self._handle, ctypes.byref(in_job)
            ):
                _w4_raise_last_error("IsProcessInJob")
            if not bool(in_job.value):
                raise RuntimeError(
                    f"UIA selection probe {label} was not contained by the controller Job Object"
                )
            return actual_start_ticks
        finally:
            _w4_close_handle(process_handle)

    def terminate(self) -> None:
        assert _W4_KERNEL32 is not None
        if self._handle is None:
            return
        if not _W4_KERNEL32.TerminateJobObject(self._handle, 1):
            _w4_raise_last_error("TerminateJobObject")

    def close(self) -> None:
        if self._handle is None:
            return
        handle = self._handle
        self._handle = None
        _w4_close_handle(handle)


def _w4_same_process_identity_is_live(process_id: int, start_ticks: int) -> bool:
    handle = _w4_open_process_for_identity(process_id)
    if handle is None:
        return False
    try:
        if _w4_process_start_ticks(handle) != start_ticks:
            return False
        exit_code = wintypes.DWORD()
        if not _W4_KERNEL32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            _w4_raise_last_error("GetExitCodeProcess")
        return int(exit_code.value) == _STILL_ACTIVE
    finally:
        _w4_close_handle(handle)


def _w4_wait_for_process_identity_absent(process_id: int, start_ticks: int) -> None:
    deadline = time.monotonic() + 5
    while _w4_same_process_identity_is_live(process_id, start_ticks):
        if time.monotonic() >= deadline:
            raise AssertionError("UIA selection probe identity survived Job Object cleanup")
        time.sleep(0.05)


def _w4_reap_controller_after_job_failure(
    controller: subprocess.Popen[str], job: _W4KillOnCloseJob | None
) -> None:
    """Fail closed after Job setup/termination failures without trusting a PID."""

    failures: list[BaseException] = []
    if job is not None:
        try:
            job.terminate()
        except BaseException as exc:
            failures.append(exc)
        finally:
            try:
                job.close()
            except BaseException as exc:
                failures.append(exc)

    # `Popen` owns a handle to the exact controller process object.  Unlike a
    # PID reopen this remains safe if the numeric PID has already been reused.
    kill_error: BaseException | None = None
    try:
        if controller.poll() is None:
            controller.kill()
    except BaseException as exc:
        kill_error = exc
    try:
        controller.wait(timeout=5)
    except BaseException as exc:
        failures.append(exc)
    if controller.poll() is None:
        if kill_error is not None:
            failures.append(kill_error)
        failures.append(
            AssertionError("UIA selection probe controller survived bounded cleanup")
        )
    if failures:
        raise RuntimeError("UIA selection probe cleanup failed closed") from failures[0]


def _w4_read_uia_probe_identity(
    ready_path: Path,
    *,
    nonce: str,
    title: str,
) -> tuple[int, int]:
    payload = json.loads(ready_path.read_text(encoding="utf-8"))
    assert set(payload) == {
        "nonce",
        "process_id",
        "start_time_utc_ticks",
        "title",
        "automation_id",
    }
    assert payload["nonce"] == nonce
    assert type(payload["process_id"]) is int and payload["process_id"] > 0
    assert (
        type(payload["start_time_utc_ticks"]) is int
        and payload["start_time_utc_ticks"] > 0
    )
    assert payload["title"] == title
    assert payload["automation_id"] == "w4_selection_probe_window"
    return payload["process_id"], payload["start_time_utc_ticks"]


def _w4_wait_for_uia_probe_identity(
    controller: subprocess.Popen[str],
    ready_path: Path,
    *,
    nonce: str,
    title: str,
) -> tuple[int, int]:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if ready_path.is_file():
            try:
                return _w4_read_uia_probe_identity(ready_path, nonce=nonce, title=title)
            except (OSError, json.JSONDecodeError):
                # The child publishes through a same-directory atomic move.  A
                # short-lived read race must remain bounded and must never turn
                # a partial sidecar into accepted readiness.
                pass
        if controller.poll() is not None:
            raise AssertionError("UIA selection probe controller exited before readiness")
        time.sleep(0.05)
    raise AssertionError("UIA selection probe did not publish bounded readiness")


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


def _run_scenario_contract_validator(
    tmp_path: Path, contract: dict[str, object]
) -> subprocess.CompletedProcess[str]:
    contract_path = tmp_path / f"scenario-contract-{uuid.uuid4().hex}.json"
    contract_path.write_text(json.dumps(contract), encoding="utf-8")
    command = (
        "$ErrorActionPreference='Stop';"
        f"Import-Module '{_ps_single_quoted(DRIVER_MODULE)}' -Force;"
        "$tokens=$null;$parseErrors=$null;"
        f"$ast=[System.Management.Automation.Language.Parser]::ParseFile("
        f"'{_ps_single_quoted(CONTROLLER)}',[ref]$tokens,[ref]$parseErrors);"
        "if($parseErrors.Count -ne 0){throw (($parseErrors|% Message)-join '; ')};"
        "$definitions=@($ast.FindAll({param($node)"
        "$node -is [System.Management.Automation.Language.FunctionDefinitionAst]},"
        "$true));"
        "foreach($definition in $definitions){"
        ". ([scriptblock]::Create($definition.Extent.Text))};"
        f"$contract=[IO.File]::ReadAllText('{_ps_single_quoted(contract_path)}')|"
        "ConvertFrom-Json;"
        "Test-W4ScenarioContract -Contract $contract"
    )
    return _run_powershell(["-Command", command], cwd=REPOSITORY_ROOT)


def _run_outer_tree_manifest_cross_hash_probe(
    tree_root: Path,
) -> subprocess.CompletedProcess[str]:
    """Compare the outer runner's in-memory tree hash with the W4 driver."""
    command = (
        "$ErrorActionPreference='Stop';"
        "$tokens=$null;$parseErrors=$null;"
        f"$ast=[System.Management.Automation.Language.Parser]::ParseFile("
        f"'{_ps_single_quoted(RUNNER)}',[ref]$tokens,[ref]$parseErrors);"
        "if($parseErrors.Count -ne 0){throw (($parseErrors|% Message)-join '; ')};"
        "$definitions=@($ast.FindAll({param($node)"
        "$node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and "
        "$node.Parent -is [System.Management.Automation.Language.NamedBlockAst]},"
        "$true));"
        "$needed=@('Get-CanonicalExistingPath','Get-LocalFileHash',"
        "'Get-StringSha256','Get-Utf8SortedStrings','Get-TreeManifestRows',"
        "'Get-TreeManifestSha256');"
        "foreach($name in $needed){"
        "$definition=@($definitions|Where-Object {$_.Name -ceq $name});"
        "if($definition.Count -ne 1){throw ('runner function was not unique: '+$name)};"
        ". ([scriptblock]::Create($definition[0].Extent.Text))};"
        f"$module=Import-Module '{_ps_single_quoted(DRIVER_MODULE)}' "
        "-Force -PassThru;"
        f"$root='{_ps_single_quoted(tree_root)}';"
        "$outerRows=@(Get-TreeManifestRows -Root $root);"
        "$outerHash=Get-TreeManifestSha256 -Root $root;"
        "$driverRows=@(& $module {param($value) "
        "Get-W4TreeManifest -Root $value} $root);"
        "$driverHash=& $module {param($value) Get-W4TreeSha256 -Root $value} $root;"
        "$outerPaths=@($outerRows|ForEach-Object {[string]$_.path});"
        "$driverPaths=@($driverRows|ForEach-Object {[string]$_.path});"
        "$legacyUtf8Paths=@(Get-Utf8SortedStrings -Values $driverPaths);"
        "[ordered]@{outer_paths=@($outerPaths);driver_paths=@($driverPaths);"
        "legacy_utf8_paths=@($legacyUtf8Paths);outer_manifest_json="
        "($outerRows|ConvertTo-Json -Depth 5 -Compress);driver_manifest_json="
        "($driverRows|ConvertTo-Json -Depth 5 -Compress);outer_hash=$outerHash;"
        "driver_hash=$driverHash}|ConvertTo-Json -Depth 8 -Compress"
    )
    return _run_powershell(["-Command", command], cwd=REPOSITORY_ROOT)


def _run_mcp_durable_seed_validator(
    tmp_path: Path, payload: dict[str, object]
) -> subprocess.CompletedProcess[str]:
    payload_path = tmp_path / f"mcp-durable-seed-{uuid.uuid4().hex}.json"
    payload_path.write_text(json.dumps(payload), encoding="utf-8")
    command = (
        "$ErrorActionPreference='Stop';"
        f"Import-Module '{_ps_single_quoted(DRIVER_MODULE)}' -Force;"
        f"$module=Import-Module '{_ps_single_quoted(SCENARIO_MODULE)}' "
        "-Force -PassThru;"
        f"$payload=[IO.File]::ReadAllText('{_ps_single_quoted(payload_path)}')|"
        "ConvertFrom-Json;"
        "& $module {param($value) "
        "Assert-W4McpDurableSeedPayload -Payload $value} $payload"
    )
    return _run_powershell(["-Command", command], cwd=REPOSITORY_ROOT)


def _run_upgrade_rejection_result_validator(
    tmp_path: Path,
    *,
    stdout: str,
    stderr: str,
    expected_code: str = "database_upgrade_required",
) -> subprocess.CompletedProcess[str]:
    result_path = tmp_path / f"upgrade-result-{uuid.uuid4().hex}.json"
    result_path.write_text(
        json.dumps({"StandardOutput": stdout, "StandardError": stderr}),
        encoding="utf-8",
    )
    escaped_code = expected_code.replace("'", "''")
    command = (
        "$ErrorActionPreference='Stop';"
        f"Import-Module '{_ps_single_quoted(DRIVER_MODULE)}' -Force;"
        f"$module=Import-Module '{_ps_single_quoted(SCENARIO_MODULE)}' "
        "-Force -PassThru;"
        f"$result=[IO.File]::ReadAllText('{_ps_single_quoted(result_path)}')|"
        "ConvertFrom-Json;"
        "& $module {param($value,$code) "
        "ConvertFrom-W4UpgradeRejectionResult -Result $value "
        "-ExpectedCode $code -Label 'contract-test'} $result "
        f"'{escaped_code}' | Out-Null"
    )
    return _run_powershell(["-Command", command], cwd=REPOSITORY_ROOT)


def _run_w4_mutation_helper(
    tmp_path: Path, *, helper: str, mode: str
) -> subprocess.CompletedProcess[str]:
    assert helper in {
        "Invoke-W4WithTemporarilyMissingFile",
        "Invoke-W4WithFilePathBlockedByDirectory",
    }
    assert mode in {"success", "action_throw", "replacement"}
    source = tmp_path / f"{helper}-{mode}.source"
    backup = tmp_path / f"{helper}-{mode}.backup"
    source.write_text("original-synthetic-bytes", encoding="utf-8")
    command = (
        "$ErrorActionPreference='Stop';"
        f"Import-Module '{_ps_single_quoted(DRIVER_MODULE)}' -Force;"
        f"$module=Import-Module '{_ps_single_quoted(SCENARIO_MODULE)}' "
        "-Force -PassThru;"
        f"$source='{_ps_single_quoted(source)}';"
        f"$backup='{_ps_single_quoted(backup)}';"
        f"$helper='{helper}';$mode='{mode}';"
        "$result=& $module {param($source,$backup,$helper,$mode)"
        "$caught=$null;$actionResult=$null;"
        "try {"
        "if($helper -ceq 'Invoke-W4WithTemporarilyMissingFile'){"
        "$actionResult=Invoke-W4WithTemporarilyMissingFile "
        "-FilePath $source -BackupPath $backup -Label 'contract missing' -Action {"
        "if($mode -ceq 'action_throw'){throw 'action-sentinel'};"
        "if($mode -ceq 'replacement'){"
        "[IO.File]::WriteAllText($source,'replacement',[Text.Encoding]::UTF8);"
        "return 'replacement-created'};"
        "if(Test-Path -LiteralPath $source){throw 'source-remained-present'};"
        "if(-not(Test-Path -LiteralPath $backup -PathType Leaf)){"
        "throw 'backup-was-not-created'};return 'success'}"
        "}else{"
        "$actionResult=Invoke-W4WithFilePathBlockedByDirectory "
        "-FilePath $source -BackupPath $backup -Label 'contract blocked' -Action {"
        "if($mode -ceq 'action_throw'){throw 'action-sentinel'};"
        "if(-not(Test-Path -LiteralPath $source -PathType Container)){"
        "throw 'source-was-not-blocked-by-directory'};"
        "if(-not(Test-Path -LiteralPath $backup -PathType Leaf)){"
        "throw 'backup-was-not-created'};"
        "if($mode -ceq 'replacement'){"
        "[IO.File]::WriteAllText((Join-Path $source 'replacement.txt'),"
        "'replacement',[Text.Encoding]::UTF8);return 'replacement-created'};"
        "return 'success'}"
        "}"
        "}catch{$caught=$_.Exception.Message};"
        "$unexpected=$backup+'.unexpected';"
        "$unexpectedItem=Get-Item -LiteralPath $unexpected -Force "
        "-ErrorAction SilentlyContinue;"
        "[ordered]@{action_result=$actionResult;caught=$caught;"
        "source_text=[IO.File]::ReadAllText($source,[Text.Encoding]::UTF8);"
        "backup_exists=[bool](Test-Path -LiteralPath $backup);"
        "unexpected_exists=[bool](Test-Path -LiteralPath $unexpected);"
        "unexpected_is_directory=[bool]($null -ne $unexpectedItem -and "
        "$unexpectedItem.PSIsContainer)}|ConvertTo-Json -Compress"
        "} $source $backup $helper $mode;"
        "$result"
    )
    return _run_powershell(["-Command", command], cwd=REPOSITORY_ROOT)


def _run_tcp_owner_contract_probe() -> subprocess.CompletedProcess[str]:
    command = (
        "$ErrorActionPreference='Stop';"
        f"Import-Module '{_ps_single_quoted(DRIVER_MODULE)}' -Force;"
        "$listener=$null;$client=$null;$accepted=$null;$other=$null;"
        "try {"
        "$listener=[Net.Sockets.TcpListener]::new("
        "[Net.IPAddress]::Parse('127.0.0.1'),0);$listener.Start(1);"
        "$endpoint=[Net.IPEndPoint]$listener.LocalEndpoint;"
        "$client=[Net.Sockets.TcpClient]::new();"
        "$client.Connect($endpoint.Address,$endpoint.Port);"
        "$accepted=$listener.AcceptTcpClient();"
        "$self=[Diagnostics.Process]::GetCurrentProcess();"
        "$positive=Assert-W4TcpClientOwnedByProcess -Client $accepted "
        "-ExpectedServerEndpoint $endpoint -Process $self -TimeoutMilliseconds 2000;"
        "$psi=[Diagnostics.ProcessStartInfo]::new();"
        "$psi.FileName=(Join-Path $PSHOME 'powershell.exe');"
        "$psi.Arguments='-NoLogo -NoProfile -NonInteractive "
        "-Command Start-Sleep -Seconds 20';"
        "$psi.UseShellExecute=$false;$psi.CreateNoWindow=$true;"
        "$other=[Diagnostics.Process]::Start($psi);"
        "if($null -eq $other){throw 'different-live-root-did-not-start'};"
        "$negativeRejected=$false;$negativeMessage='';"
        "try {Assert-W4TcpClientOwnedByProcess -Client $accepted "
        "-ExpectedServerEndpoint $endpoint -Process $other "
        "-TimeoutMilliseconds 2000|Out-Null}"
        "catch {$negativeRejected=$true;$negativeMessage=$_.Exception.Message};"
        "if(-not $negativeRejected){throw 'different-live-root-was-accepted'};"
        "$client.Dispose();$client=$null;"
        "$zeroRejected=$false;$zeroMessage='';"
        "try {Assert-W4TcpClientOwnedByProcess -Client $accepted "
        "-ExpectedServerEndpoint $endpoint -Process $self "
        "-TimeoutMilliseconds 500|Out-Null}"
        "catch {$zeroRejected=$true;$zeroMessage=$_.Exception.Message};"
        "if(-not $zeroRejected){throw 'zero-owner-row-was-accepted'};"
        "[ordered]@{owner_verified=[bool]$positive.OwnerVerified;"
        "owner_pid=[int]$positive.OwnerProcessId;self_pid=[int]$self.Id;"
        "owner_start_ticks=[int64]$positive.OwnerStartTimeUtcTicks;"
        "different_live_root_rejected=$negativeRejected;"
        "different_live_root_error=$negativeMessage;"
        "zero_owner_rejected=$zeroRejected;zero_owner_error=$zeroMessage}"
        "|ConvertTo-Json -Compress"
        "}finally{"
        "if($null -ne $accepted){$accepted.Dispose()};"
        "if($null -ne $client){$client.Dispose()};"
        "if($null -ne $listener){$listener.Stop()};"
        "if($null -ne $other){"
        "try{$other.Refresh();if(-not $other.HasExited){$other.Kill();"
        "[void]$other.WaitForExit(5000)}}catch{};$other.Dispose()}"
        "}"
    )
    return _run_powershell(
        ["-Command", command], cwd=REPOSITORY_ROOT, timeout=30
    )


def _run_uia_selection_pattern_probe_bounded(
    tmp_path: Path,
    *,
    force_controller_failure: bool = False,
    force_outer_timeout: bool = False,
) -> tuple[subprocess.CompletedProcess[str] | None, Path, dict[str, object] | None]:
    title = f"W4SelectionProbe-{uuid.uuid4().hex}"
    nonce = uuid.uuid4().hex
    launch_gate = tmp_path / f"uia-selection-launch-{uuid.uuid4().hex}.gate"
    enable_gate = tmp_path / f"uia-selection-enable-{uuid.uuid4().hex}.gate"
    close_request = tmp_path / f"uia-selection-close-{uuid.uuid4().hex}.request"
    close_ack = tmp_path / f"uia-selection-close-{uuid.uuid4().hex}.ack"
    ready_path = tmp_path / f"uia-selection-ready-{uuid.uuid4().hex}.json"
    cleanup_path = tmp_path / f"uia-selection-cleanup-{uuid.uuid4().hex}.json"
    result_path = tmp_path / f"uia-selection-result-{uuid.uuid4().hex}.json"
    error_path = tmp_path / f"uia-selection-error-{uuid.uuid4().hex}.txt"
    expected_title = title
    discovery_seconds = 15
    outer_timeout_seconds = 3 if force_outer_timeout else 45
    force_controller_failure_literal = (
        "$true" if force_controller_failure else "$false"
    )
    force_outer_timeout_literal = "$true" if force_outer_timeout else "$false"

    child_script = f"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName PresentationFramework -ErrorAction Stop
$launchGate = '{_ps_single_quoted(launch_gate)}'
$readyPath = '{_ps_single_quoted(ready_path)}'
$closeRequest = '{_ps_single_quoted(close_request)}'
$closeAck = '{_ps_single_quoted(close_ack)}'
$nonce = '{nonce}'
$title = '{title}'
$automationId = 'w4_selection_probe_window'
$gateDeadline = [DateTime]::UtcNow.AddSeconds(10)
while (-not (Test-Path -LiteralPath $launchGate -PathType Leaf)) {{
    if ([DateTime]::UtcNow -ge $gateDeadline) {{
        throw 'UIA selection probe launch gate was not released'
    }}
    Start-Sleep -Milliseconds 25
}}
$window = [System.Windows.Window]::new()
$window.Title = $title
$window.Width = 360
$window.Height = 240
$window.WindowStartupLocation = [System.Windows.WindowStartupLocation]::Manual
$window.Left = 120
$window.Top = 120
$window.Topmost = $true
$window.ShowActivated = $true
[System.Windows.Automation.AutomationProperties]::SetAutomationId($window, $automationId)
$list = [System.Windows.Controls.ListBox]::new()
$list.SelectionMode = [System.Windows.Controls.SelectionMode]::Single
[System.Windows.Automation.AutomationProperties]::SetAutomationId(
    $list,
    'w4_selection_probe_list'
)
[void]$list.Items.Add('alpha')
[void]$list.Items.Add('beta')
$list.UnselectAll()
$window.Content = $list
$window.Add_ContentRendered({{
    $list.UnselectAll()
    [void]$window.Activate()
    $ready = [ordered]@{{
        nonce = $nonce
        process_id = [int]$PID
        start_time_utc_ticks = [int64]([Diagnostics.Process]::GetCurrentProcess().StartTime.ToUniversalTime().Ticks)
        title = $title
        automation_id = $automationId
    }}
    $readyTempPath = "$readyPath.$PID.$([Guid]::NewGuid().ToString('N')).tmp"
    try {{
        [System.IO.File]::WriteAllText(
            $readyTempPath,
            ($ready | ConvertTo-Json -Compress),
            [System.Text.UTF8Encoding]::new($false)
        )
        [System.IO.File]::Move($readyTempPath, $readyPath)
    }} finally {{
        if (Test-Path -LiteralPath $readyTempPath -PathType Leaf) {{
            Remove-Item -LiteralPath $readyTempPath -Force -ErrorAction SilentlyContinue
        }}
    }}
}})
$selfExpiry = [DateTime]::UtcNow.AddSeconds(30)
$closeTimer = [System.Windows.Threading.DispatcherTimer]::new()
$closeTimer.Interval = [TimeSpan]::FromMilliseconds(100)
$closeTimer.Add_Tick({{
    $reason = $null
    if (Test-Path -LiteralPath $closeRequest -PathType Leaf) {{
        $reason = 'close_request'
    }} elseif ([DateTime]::UtcNow -ge $selfExpiry) {{
        $reason = 'self_expiry'
    }}
    if ($null -ne $reason) {{
        try {{
            [System.IO.File]::WriteAllText(
                $closeAck,
                $reason,
                [System.Text.UTF8Encoding]::new($false)
            )
        }} catch {{}}
        $closeTimer.Stop()
        $window.Close()
    }}
}})
$closeTimer.Start()
try {{
    [void]$window.ShowDialog()
}} finally {{
    $closeTimer.Stop()
}}
"""
    encoded_child = base64.b64encode(child_script.encode("utf-16le")).decode("ascii")
    command = (
        "$ErrorActionPreference='Stop';"
        "Add-Type -AssemblyName UIAutomationClient -ErrorAction Stop;"
        "Add-Type -AssemblyName UIAutomationTypes -ErrorAction Stop;"
        f"Import-Module '{_ps_single_quoted(DRIVER_MODULE)}' -Force;"
        f"$module=Import-Module '{_ps_single_quoted(SCENARIO_MODULE)}' -Force -PassThru;"
        f"$launchGate='{_ps_single_quoted(launch_gate)}';"
        f"$enableGate='{_ps_single_quoted(enable_gate)}';"
        f"$closeRequest='{_ps_single_quoted(close_request)}';"
        f"$closeAck='{_ps_single_quoted(close_ack)}';"
        f"$cleanupPath='{_ps_single_quoted(cleanup_path)}';"
        f"$resultPath='{_ps_single_quoted(result_path)}';"
        f"$errorPath='{_ps_single_quoted(error_path)}';"
        "$process=$null;$childStdoutTask=$null;$childStderrTask=$null;"
        "$childPid=$null;$childStartTicks=$null;$cleanupRequested=$false;"
        "$cleanupFallbackKill=$false;$cleanupExited=$false;"
        "$cleanupSameIdentityLive=$false;$controllerError=$null;"
        "try{"
        "$launchDeadline=[DateTime]::UtcNow.AddSeconds(10);"
        "while(-not(Test-Path -LiteralPath $launchGate -PathType Leaf)){"
        "if([DateTime]::UtcNow -ge $launchDeadline){throw 'UIA selection probe launch gate was not released'};"
        "Start-Sleep -Milliseconds 25};"
        "$psi=[Diagnostics.ProcessStartInfo]::new();"
        "$psi.FileName=(Join-Path $PSHOME 'powershell.exe');"
        f"$psi.Arguments='-NoLogo -NoProfile -NonInteractive -Sta -EncodedCommand {encoded_child}';"
        "$psi.UseShellExecute=$false;$psi.CreateNoWindow=$true;"
        "$psi.RedirectStandardOutput=$true;$psi.RedirectStandardError=$true;"
        "$process=[Diagnostics.Process]::Start($psi);"
        "if($null -eq $process){throw 'UIA selection probe process did not start'};"
        "$childStdoutTask=$process.StandardOutput.ReadToEndAsync();"
        "$childStderrTask=$process.StandardError.ReadToEndAsync();"
        "$childPid=[int]$process.Id;"
        "$childStartTicks=[int64]$process.StartTime.ToUniversalTime().Ticks;"
        "$enableDeadline=[DateTime]::UtcNow.AddSeconds(15);"
        "while(-not(Test-Path -LiteralPath $enableGate -PathType Leaf)){"
        "$process.Refresh();if($process.HasExited){throw 'UIA selection probe exited before Job Object enablement'};"
        "if([DateTime]::UtcNow -ge $enableDeadline){throw "
        "'UIA selection probe Job Object enable gate was not released'};"
        "Start-Sleep -Milliseconds 25};"
        f"if({force_controller_failure_literal}){{throw "
        "'UIA selection probe forced controller failure'};"
        f"if({force_outer_timeout_literal}){{Start-Sleep -Seconds 120}};"
        "$desktop=[System.Windows.Automation.AutomationElement]::RootElement;"
        "$pidCondition=[System.Windows.Automation.PropertyCondition]::new("
        "[System.Windows.Automation.AutomationElement]::ProcessIdProperty,[int]$process.Id);"
        "$nameCondition=[System.Windows.Automation.PropertyCondition]::new("
        "[System.Windows.Automation.AutomationElement]::NameProperty,"
        f"'{expected_title}');"
        "$windowAutomationCondition=[System.Windows.Automation.PropertyCondition]::new("
        "[System.Windows.Automation.AutomationElement]::AutomationIdProperty,"
        "'w4_selection_probe_window');"
        "$windowCondition=[System.Windows.Automation.AndCondition]::new("
        "[System.Windows.Automation.Condition[]]@($pidCondition,$nameCondition,$windowAutomationCondition));"
        f"$deadline=[DateTime]::UtcNow.AddSeconds({discovery_seconds});$window=$null;"
        "do{$window=$desktop.FindFirst([System.Windows.Automation.TreeScope]::Children,$windowCondition);"
        "if($null -ne $window){try{if(-not [bool]$window.Current.IsOffscreen){break}}"
        "catch [System.Windows.Automation.ElementNotAvailableException]{};$window=$null};"
        "$process.Refresh();if($process.HasExited){throw 'UIA selection probe exited before discovery'};"
        "Start-Sleep -Milliseconds 50}while([DateTime]::UtcNow -lt $deadline);"
        "if($null -eq $window){throw 'UIA selection probe window was not found'};"
        "$listCondition=[System.Windows.Automation.PropertyCondition]::new("
        "[System.Windows.Automation.AutomationElement]::AutomationIdProperty,'w4_selection_probe_list');"
        "$listDeadline=[DateTime]::UtcNow.AddSeconds(10);$list=$null;"
        "do{$window=$desktop.FindFirst([System.Windows.Automation.TreeScope]::Children,$windowCondition);"
        "if($null -ne $window){try{if(-not [bool]$window.Current.IsOffscreen){"
        "$listMatches=$window.FindAll([System.Windows.Automation.TreeScope]::Descendants,$listCondition);"
        "if($listMatches.Count -eq 1){$list=$listMatches.Item(0);break};"
        "if($listMatches.Count -gt 1){throw 'UIA selection probe list was duplicated'}}}"
        "catch [System.Windows.Automation.ElementNotAvailableException]{};$window=$null};"
        "Start-Sleep -Milliseconds 50}while([DateTime]::UtcNow -lt $listDeadline);"
        "if($null -eq $list){throw 'UIA selection probe list was not found'};"
        "$zeroPattern=$null;if(-not $list.TryGetCurrentPattern("
        "[System.Windows.Automation.SelectionPattern]::Pattern,[ref]$zeroPattern)){"
        "throw 'Probe list lacks SelectionPattern'};"
        "$zeroCount=@(([System.Windows.Automation.SelectionPattern]$zeroPattern).Current.GetSelection()).Count;"
        "if($zeroCount -ne 0){throw 'Probe list did not begin with zero selection'};"
        "$list=& $module {param($root) Wait-W4UiaSelectionCount -Root $root "
        "-AutomationId 'w4_selection_probe_list' -ExpectedCount 0 "
        "-TimeoutSeconds 10} $window;"
        "$item=& $module {param($root) Select-W4FirstListItem -Root $root} $list;"
        "$oneList=& $module {param($root) Wait-W4UiaSelectionCount -Root $root "
        "-AutomationId 'w4_selection_probe_list' -ExpectedCount 1 "
        "-TimeoutSeconds 10} $window;"
        "$onePattern=$null;if(-not $oneList.TryGetCurrentPattern("
        "[System.Windows.Automation.SelectionPattern]::Pattern,[ref]$onePattern)){"
        "throw 'Probe list lost SelectionPattern'};"
        "$selection=@(([System.Windows.Automation.SelectionPattern]$onePattern).Current.GetSelection());"
        "$itemRuntimeId=@($item.GetRuntimeId());$selectedRuntimeId=@($selection[0].GetRuntimeId());"
        "$runtimeIdsMatch=($itemRuntimeId.Count -gt 0 -and $itemRuntimeId.Count -eq $selectedRuntimeId.Count);"
        "if($runtimeIdsMatch){for($index=0;$index -lt $itemRuntimeId.Count;$index+=1){"
        "if([int]$itemRuntimeId[$index] -ne [int]$selectedRuntimeId[$index]){$runtimeIdsMatch=$false;break}}};"
        "$proof=& $module {param($root,$item) Get-W4UiaSelectionProof -Root $root -Item $item} $oneList $item;"
        "$result=[ordered]@{zero_count=[int]$zeroCount;one_count=[int]$selection.Count;"
        "runtime_ids_match=[bool]$runtimeIdsMatch;selected_runtime_id=@($selectedRuntimeId);proof=$proof};"
        "[IO.File]::WriteAllText($resultPath,"
        "($result|ConvertTo-Json -Depth 10 -Compress),"
        "[Text.UTF8Encoding]::new($false))"
        "}catch{$controllerError=$_.Exception.Message;"
        "try{[IO.File]::WriteAllText($errorPath,$controllerError,[Text.UTF8Encoding]::new($false))}catch{}}"
        "finally{if($null -ne $process){try{"
        "try{$process.Refresh();$cleanupExited=[bool]$process.HasExited}catch{$cleanupExited=$false};"
        "if(-not $cleanupExited){[IO.File]::WriteAllText($closeRequest,'close',[Text.UTF8Encoding]::new($false));"
        "$cleanupRequested=$true;$cleanupDeadline=[DateTime]::UtcNow.AddSeconds(5);"
        "do{try{if($process.WaitForExit(100)){$cleanupExited=$true;break}}"
        "catch{break}}while([DateTime]::UtcNow -lt $cleanupDeadline);"
        "if(-not $cleanupExited){$cleanupFallbackKill=$true;try{$process.Kill()}catch{};"
        "try{$cleanupExited=[bool]$process.WaitForExit(3000)}catch{$cleanupExited=$false}}};"
        "if(-not $cleanupExited){throw 'UIA selection probe child did not exit during bounded cleanup'};"
        "$freshProcess=$null;try{$freshProcess=[Diagnostics.Process]::GetProcessById($childPid);"
        "$freshStartTicks=[int64]$freshProcess.StartTime.ToUniversalTime().Ticks;"
        "$cleanupSameIdentityLive=($freshStartTicks -eq $childStartTicks)}"
        "catch [ArgumentException]{$cleanupSameIdentityLive=$false}"
        "finally{if($null -ne $freshProcess){$freshProcess.Dispose()}};"
        "if($cleanupSameIdentityLive){throw 'UIA selection probe child identity remained live after bounded cleanup'}"
        "}finally{$closeAckValue='';if(Test-Path -LiteralPath $closeAck -PathType Leaf){"
        "try{$closeAckValue=[IO.File]::ReadAllText($closeAck,[Text.Encoding]::UTF8).Trim()}catch{}};"
        "$cleanup=[ordered]@{child_pid=$childPid;child_start_time_utc_ticks=$childStartTicks;"
        "close_request_written=[bool]$cleanupRequested;close_ack=$closeAckValue;"
        "fallback_kill=[bool]$cleanupFallbackKill;process_exited=[bool]$cleanupExited;"
        "same_identity_live_after_cleanup=[bool]$cleanupSameIdentityLive};"
        "[IO.File]::WriteAllText($cleanupPath,($cleanup|ConvertTo-Json -Compress),[Text.UTF8Encoding]::new($false));"
        "if($null -ne $childStdoutTask){try{[void]$childStdoutTask.GetAwaiter().GetResult()}catch{}};"
        "if($null -ne $childStderrTask){try{[void]$childStderrTask.GetAwaiter()"
        ".GetResult()}catch{}};$process.Dispose()}}};"
        "if($null -ne $controllerError){exit 1}"
    )
    controller_args = [
        str(_windows_powershell()),
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-Command",
        command,
    ]
    controller = subprocess.Popen(
        controller_args,
        cwd=REPOSITORY_ROOT,
        env=os.environ.copy(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        text=True,
    )
    job: _W4KillOnCloseJob | None = None
    identity: tuple[int, int] | None = None
    try:
        job = _W4KillOnCloseJob()
        controller_start_ticks = job.assign_controller_handle(controller)
        if controller_start_ticks <= 0:
            raise AssertionError("UIA selection probe controller did not expose start ticks")
        launch_gate.write_text(nonce, encoding="utf-8")
        identity = _w4_wait_for_uia_probe_identity(
            controller, ready_path, nonce=nonce, title=title
        )
        job.assert_process_is_member(
            identity[0], expected_start_ticks=identity[1], label="child"
        )
        enable_gate.write_text(nonce, encoding="utf-8")
        try:
            controller.wait(timeout=outer_timeout_seconds)
        except subprocess.TimeoutExpired:
            try:
                _w4_reap_controller_after_job_failure(controller, job)
            finally:
                job = None
            _w4_wait_for_process_identity_absent(*identity)
            return None, cleanup_path, {
                "controller_timed_out": True,
                "job_terminated": True,
                "same_identity_live_after_job_cleanup": False,
            }
        job.close()
        job = None
        _w4_wait_for_process_identity_absent(*identity)
        stdout = result_path.read_text(encoding="utf-8") if result_path.is_file() else ""
        stderr = error_path.read_text(encoding="utf-8") if error_path.is_file() else ""
        return (
            subprocess.CompletedProcess(
                controller_args, int(controller.returncode), stdout, stderr
            ),
            cleanup_path,
            None,
        )
    except BaseException:
        try:
            _w4_reap_controller_after_job_failure(controller, job)
        finally:
            job = None
            if identity is not None:
                _w4_wait_for_process_identity_absent(*identity)
        raise
    finally:
        if job is not None:
            _w4_reap_controller_after_job_failure(controller, job)


def _run_window_capture_probe(
    tmp_path: Path, mode: str
) -> subprocess.CompletedProcess[str]:
    assert mode in {"positive", "wrong_process", "uniform"}
    title = f"W4WindowCaptureProbe-{mode}-{uuid.uuid4().hex}"
    screenshot = tmp_path / f"window-capture-{mode}.png"
    uniform_setup = """
$window.WindowStyle = [System.Windows.WindowStyle]::None
$window.ResizeMode = [System.Windows.ResizeMode]::NoResize
$window.Background = [System.Windows.Media.Brushes]::Magenta
"""
    positive_setup = """
$grid = [System.Windows.Controls.Grid]::new()
$grid.Background = [System.Windows.Media.Brushes]::DarkBlue
$left = [System.Windows.Controls.Border]::new()
$left.Width = 120
$left.Height = 120
$left.HorizontalAlignment = [System.Windows.HorizontalAlignment]::Left
$left.VerticalAlignment = [System.Windows.VerticalAlignment]::Top
$left.Background = [System.Windows.Media.Brushes]::OrangeRed
$right = [System.Windows.Controls.Border]::new()
$right.Width = 120
$right.Height = 120
$right.HorizontalAlignment = [System.Windows.HorizontalAlignment]::Right
$right.VerticalAlignment = [System.Windows.VerticalAlignment]::Bottom
$right.Background = [System.Windows.Media.Brushes]::Gold
$label = [System.Windows.Controls.TextBlock]::new()
$label.Text = 'BOUND PRINTWINDOW PROBE'
$label.Foreground = [System.Windows.Media.Brushes]::White
$label.FontSize = 18
$label.HorizontalAlignment = [System.Windows.HorizontalAlignment]::Center
$label.VerticalAlignment = [System.Windows.VerticalAlignment]::Center
[void]$grid.Children.Add($left)
[void]$grid.Children.Add($right)
[void]$grid.Children.Add($label)
$window.Content = $grid
"""
    child_script = f"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName PresentationFramework -ErrorAction Stop
Add-Type -AssemblyName PresentationCore -ErrorAction Stop
Add-Type -AssemblyName WindowsBase -ErrorAction Stop
$window = [System.Windows.Window]::new()
$window.Title = '{title}'
$window.Width = 360
$window.Height = 260
$window.WindowStartupLocation = [System.Windows.WindowStartupLocation]::Manual
$window.Left = 160
$window.Top = 160
$window.ShowInTaskbar = $false
$window.Topmost = $true
$window.ShowActivated = $true
$window.SizeToContent = [System.Windows.SizeToContent]::Manual
[System.Windows.Automation.AutomationProperties]::SetAutomationId(
    $window,
    'pkv_main_window'
)
{uniform_setup if mode == "uniform" else positive_setup}
$window.Add_Loaded({{
    [void]$window.Activate()
}})
[void]$window.ShowDialog()
"""
    encoded_child = base64.b64encode(child_script.encode("utf-16le")).decode("ascii")
    command = (
        "$ErrorActionPreference='Stop';"
        "Add-Type -AssemblyName UIAutomationClient -ErrorAction Stop;"
        "Add-Type -AssemblyName UIAutomationTypes -ErrorAction Stop;"
        f"$module=Import-Module '{_ps_single_quoted(DRIVER_MODULE)}' "
        "-Force -PassThru;"
        f"$mode='{mode}';$output='{_ps_single_quoted(screenshot)}';"
        "$process=$null;$other=$null;$caught=$null;$capture=$null;"
        "$stopwatch=[Diagnostics.Stopwatch]::StartNew();"
        "try {"
        "$psi=[Diagnostics.ProcessStartInfo]::new();"
        f"$psi.FileName='{_ps_single_quoted(_windows_powershell())}';"
        f"$psi.Arguments='-NoLogo -NoProfile -NonInteractive -Sta -EncodedCommand "
        f"{encoded_child}';"
        "$psi.UseShellExecute=$false;$psi.CreateNoWindow=$true;"
        "$process=[Diagnostics.Process]::Start($psi);"
        "if($null -eq $process){throw 'window capture probe did not start'};"
        "$desktop=[System.Windows.Automation.AutomationElement]::RootElement;"
        "$pidCondition=[System.Windows.Automation.PropertyCondition]::new("
        "[System.Windows.Automation.AutomationElement]::ProcessIdProperty,"
        "[int]$process.Id);"
        "$nameCondition=[System.Windows.Automation.PropertyCondition]::new("
        "[System.Windows.Automation.AutomationElement]::NameProperty,"
        f"'{title}');"
        "$automationCondition=[System.Windows.Automation.PropertyCondition]::new("
        "[System.Windows.Automation.AutomationElement]::AutomationIdProperty,"
        "'pkv_main_window');"
        "$windowCondition=[System.Windows.Automation.AndCondition]::new("
        "[System.Windows.Automation.Condition[]]@($pidCondition,$nameCondition,"
        "$automationCondition));"
        "$deadline=[DateTime]::UtcNow.AddSeconds(15);$window=$null;"
        "$observedAutomationId='';"
        "do {"
        "$window=$desktop.FindFirst("
        "[System.Windows.Automation.TreeScope]::Children,$windowCondition);"
        "if($null -ne $window){"
        "try {if([string]$window.Current.AutomationId -ceq 'pkv_main_window' "
        "-and -not [bool]$window.Current.IsOffscreen){"
        "$observedAutomationId='pkv_main_window';break}}"
        "catch [System.Windows.Automation.ElementNotAvailableException]{};"
        "$window=$null};"
        "$process.Refresh();"
        "if($process.HasExited){throw 'window capture probe exited before discovery'};"
        "Start-Sleep -Milliseconds 50"
        "}while([DateTime]::UtcNow -lt $deadline);"
        "if($null -eq $window){throw 'window capture probe was not found by UIA'};"
        "if($observedAutomationId -cne 'pkv_main_window'){"
        "throw 'probe window was not accepted by the exact AutomationId oracle'};"
        "if($mode -ceq 'wrong_process'){"
        "$otherInfo=[Diagnostics.ProcessStartInfo]::new();"
        "$otherInfo.FileName=(Join-Path $PSHOME 'powershell.exe');"
        "$otherInfo.Arguments='-NoLogo -NoProfile -NonInteractive "
        "-Command Start-Sleep -Seconds 20';"
        "$otherInfo.UseShellExecute=$false;$otherInfo.CreateNoWindow=$true;"
        "$other=[Diagnostics.Process]::Start($otherInfo);"
        "if($null -eq $other){throw 'wrong-process probe did not start'};"
        "try {& $module {param($path,$element,$target) "
        "Save-W4Screenshot -Path $path -Element $element -Process $target "
        "-MaximumAttempts 2 -TimeoutSeconds 6} $output $window $other}"
        "catch {$caught=$_.Exception.Message}"
        "}else{"
        "try {& $module {param($path,$element,$target) "
        "Save-W4Screenshot -Path $path -Element $element -Process $target "
        "-MaximumAttempts 2 -TimeoutSeconds 8} $output $window $process}"
        "catch {$caught=$_.Exception.Message}"
        "};"
        "$stopwatch.Stop();"
        "$evidencePath=$output+'.capture.json';"
        "if(Test-Path -LiteralPath $evidencePath -PathType Leaf){"
        "$capture=[IO.File]::ReadAllText($evidencePath)|ConvertFrom-Json};"
        "[ordered]@{mode=$mode;caught=$caught;"
        "elapsed_milliseconds=[int64]$stopwatch.ElapsedMilliseconds;"
        "screenshot_exists=[bool](Test-Path -LiteralPath $output -PathType Leaf);"
        "capture_evidence_exists="
        "[bool](Test-Path -LiteralPath $evidencePath -PathType Leaf);"
        "automation_id=$observedAutomationId;"
        "window_process_id=[int]$window.Current.ProcessId;"
        "target_process_id=[int]$process.Id;capture=$capture}"
        "|ConvertTo-Json -Depth 10 -Compress"
        "}finally{"
        "foreach($candidate in @($other,$process)){"
        "if($null -ne $candidate){try{$candidate.Refresh();"
        "if(-not $candidate.HasExited){$candidate.Kill();"
        "[void]$candidate.WaitForExit(5000)}}catch{};$candidate.Dispose()}}"
        "}"
    )
    return _run_powershell(
        ["-Command", command], cwd=REPOSITORY_ROOT, timeout=45
    )


def _run_bounded_capture_worker_timeout_probe() -> subprocess.CompletedProcess[str]:
    sleep_command = base64.b64encode(
        "Start-Sleep -Seconds 30".encode("utf-16le")
    ).decode("ascii")
    command = (
        "$ErrorActionPreference='Stop';"
        f"$module=Import-Module '{_ps_single_quoted(DRIVER_MODULE)}' "
        "-Force -PassThru;"
        "$worker=$null;$caught=$null;$workerWasRunning=$false;"
        "$workerExited=$false;$stopwatch=$null;"
        "try{"
        "$workerInfo=[Diagnostics.ProcessStartInfo]::new();"
        f"$workerInfo.FileName='{_ps_single_quoted(_windows_powershell())}';"
        "$workerInfo.Arguments='-NoLogo -NoProfile -NonInteractive "
        f"-EncodedCommand {sleep_command}';"
        "$workerInfo.UseShellExecute=$false;$workerInfo.CreateNoWindow=$true;"
        "$worker=[Diagnostics.Process]::Start($workerInfo);"
        "if($null -eq $worker){throw 'hung capture worker probe did not start'};"
        "Start-Sleep -Milliseconds 100;$worker.Refresh();"
        "$workerWasRunning=-not $worker.HasExited;"
        "if(-not $workerWasRunning){throw 'hung capture worker exited before timeout cleanup'};"
        "$deadline=[DateTime]::UtcNow.AddMilliseconds(1000);"
        "$deadlineRemainingBefore=[int][Math]::Floor("
        "($deadline-[DateTime]::UtcNow).TotalMilliseconds);"
        "if($deadlineRemainingBefore -le 0){"
        "throw 'hung capture worker probe lost its positive deadline'};"
        "$stopwatch=[Diagnostics.Stopwatch]::StartNew();"
        "try{& $module {param($target,$deadline) "
        "Stop-W4BoundedCaptureWorker -Process $target -DeadlineUtc $deadline} "
        "$worker $deadline}catch{$caught=$_.Exception.Message};"
        "$workerExited=$worker.WaitForExit(1500);$stopwatch.Stop();"
        "[ordered]@{caught=$caught;"
        "deadline_remaining_before_milliseconds=$deadlineRemainingBefore;"
        "elapsed_milliseconds=[int64]$stopwatch.ElapsedMilliseconds;"
        "worker_was_running=[bool]$workerWasRunning;"
        "worker_exited=[bool]$workerExited}|ConvertTo-Json -Compress"
        "}finally{"
        "if($null -ne $worker){try{$worker.Refresh();"
        "if(-not $worker.HasExited){$worker.Kill();"
        "[void]$worker.WaitForExit(1000)}}catch{};$worker.Dispose()}"
        "}"
    )
    return _run_powershell(
        ["-Command", command], cwd=REPOSITORY_ROOT, timeout=15
    )


def _run_loopback_retained_exit_observation_probe(
    tmp_path: Path,
    topology: str = "split",
    runtime_mode: str = "success",
    invalidate_runtime_before_native_read: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Exercise retained native exit observations through the real stop path."""
    assert topology in {"split", "same"}
    assert runtime_mode in {"success", "nonzero", "ignore_shutdown"}
    assert not invalidate_runtime_before_native_read or topology == "split"
    assert runtime_mode == "success" or not invalidate_runtime_before_native_read
    state = tmp_path / f"loopback-exit-state-{uuid.uuid4().hex}"
    evidence = tmp_path / f"loopback-exit-evidence-{uuid.uuid4().hex}"
    shutdown = state / "shutdown.request"
    runtime_shutdown_marker = state / "runtime-shutdown-observed.marker"

    def shutdown_child_script(exit_code: int) -> str:
        return (
            "$ErrorActionPreference='Stop';"
            f"$shutdown='{_ps_single_quoted(shutdown)}';"
            "$deadline=[DateTime]::UtcNow.AddSeconds(20);"
            "while(-not (Test-Path -LiteralPath $shutdown -PathType Leaf)){"
            "if([DateTime]::UtcNow -ge $deadline){exit 91};"
            "Start-Sleep -Milliseconds 20};"
            "$request=[IO.File]::ReadAllText($shutdown,[Text.Encoding]::UTF8);"
            "if($request -cne \"shutdown`n\"){exit 92};"
            f"exit {exit_code}"
        )

    def ignore_shutdown_child_script() -> str:
        return (
            "$ErrorActionPreference='Stop';"
            f"$shutdown='{_ps_single_quoted(shutdown)}';"
            f"$marker='{_ps_single_quoted(runtime_shutdown_marker)}';"
            "$deadline=[DateTime]::UtcNow.AddSeconds(20);"
            "while(-not (Test-Path -LiteralPath $shutdown -PathType Leaf)){"
            "if([DateTime]::UtcNow -ge $deadline){exit 91};"
            "Start-Sleep -Milliseconds 20};"
            "$request=[IO.File]::ReadAllText($shutdown,[Text.Encoding]::UTF8);"
            "if($request -cne \"shutdown`n\"){exit 92};"
            "[IO.File]::WriteAllText($marker,'observed',"
            "[Text.UTF8Encoding]::new($false));"
            "Start-Sleep -Seconds 20;exit 0"
        )

    launcher_script = shutdown_child_script(0)
    runtime_script = (
        shutdown_child_script(0)
        if runtime_mode == "success"
        else shutdown_child_script(17)
        if runtime_mode == "nonzero"
        else ignore_shutdown_child_script()
    )
    encoded_launcher = base64.b64encode(
        launcher_script.encode("utf-16le")
    ).decode("ascii")
    encoded_runtime = base64.b64encode(
        runtime_script.encode("utf-16le")
    ).decode("ascii")
    runtime_start = (
        "$runtimeInfo=[Diagnostics.ProcessStartInfo]::new();"
        f"$runtimeInfo.FileName='{_ps_single_quoted(_windows_powershell())}';"
        "$runtimeInfo.Arguments='-NoLogo -NoProfile -NonInteractive "
        f"-EncodedCommand {encoded_runtime}';"
        "$runtimeInfo.UseShellExecute=$false;$runtimeInfo.CreateNoWindow=$true;"
        "$runtimeSource=[Diagnostics.Process]::Start($runtimeInfo);"
        "if($null -eq $runtimeSource){throw 'loopback runtime did not start'};"
        "$runtime=[Diagnostics.Process]::GetProcessById([int]$runtimeSource.Id);"
        "$runtimeSource.Dispose();$runtimeSource=$null;"
        "$runtimeSourceAcquiredViaGetProcessById=$true;"
        if topology == "split"
        else "$runtime=$launcher;$runtimeSourceAcquiredViaGetProcessById=$false;"
    )
    runtime_ticks = (
        "$runtimeTicks=[int64]$runtime.StartTime.ToUniversalTime().Ticks;"
        if topology == "split"
        else ""
    )
    runtime_snapshot = (
        "$runtimeSnapshot=New-W4ProcessTreeIdentitySnapshot -Process $runtime;"
        if topology == "split"
        else "$runtimeSnapshot=$launcherSnapshot;"
    )
    runtime_observation = (
        "$runtimeObservation=New-W4RetainedProcessExitObservation "
        "-Process $runtime -ExpectedProcessId $runtimePid "
        "-ExpectedStartTimeUtcTicks $runtimeTicks -Label 'Harness runtime';"
        if topology == "split"
        else "$runtimeObservation=$null;"
    )
    wait_override_setup = (
        "& $module {"
        "$script:W4LoopbackProbeRuntimeToInvalidate=$null;"
        "$script:W4LoopbackProbeRuntimeWasInvalidated=$false;"
        "function script:Wait-W4RetainedProcessExit {"
        "param($ExitObservation,$TimeoutMilliseconds,$Label);"
        "$exited=[bool]$ExitObservation.WaitForExit($TimeoutMilliseconds);"
        "if($TimeoutMilliseconds -eq 5000 -and $exited -and "
        "$null -ne $script:W4LoopbackProbeRuntimeToInvalidate){"
        "$script:W4LoopbackProbeRuntimeToInvalidate.Dispose();"
        "$script:W4LoopbackProbeRuntimeToInvalidate=$null;"
        "$script:W4LoopbackProbeRuntimeWasInvalidated=$true};"
        "return [bool]$exited"
        "}"
        "};"
        "& $module {param($value) "
        "$script:W4LoopbackProbeRuntimeToInvalidate=$value} $runtime;"
        if invalidate_runtime_before_native_read
        else ""
    )
    wait_override_result = (
        "& $module {[bool]$script:W4LoopbackProbeRuntimeWasInvalidated};"
        if invalidate_runtime_before_native_read
        else "$false;"
    )
    use_real_tree_cleanup = runtime_mode != "success"
    tree_cleanup_setup = (
        "& $module {"
        "function script:Stop-W4ProcessTree {"
        "param([System.Diagnostics.Process]$Process,$IdentitySnapshot);"
        "$script:W4LoopbackProbeTreeReconcileCount += 1;"
        "try{$Process.Refresh();if(-not $Process.HasExited){$Process.Kill();"
        "[void]$Process.WaitForExit(2000)}}catch{};"
        "try{$Process.Dispose()}catch{}"
        "}"
        "};"
        "& $module {$script:W4LoopbackProbeTreeReconcileCount=0};"
        if not use_real_tree_cleanup
        else ""
    )
    tree_reconcile_count = (
        "& $module {[int]$script:W4LoopbackProbeTreeReconcileCount};"
        if not use_real_tree_cleanup
        else "$null;"
    )
    command = (
        "$ErrorActionPreference='Stop';"
        f"Import-Module '{_ps_single_quoted(DRIVER_MODULE)}' -Force;"
        f"$module=Import-Module '{_ps_single_quoted(SCENARIO_MODULE)}' "
        "-Force -PassThru;"
        f"$state='{_ps_single_quoted(state)}';"
        f"$evidence='{_ps_single_quoted(evidence)}';"
        "$launcher=$null;$runtime=$null;"
        "$runtimeObservation=$null;"
        "$caught=$null;$stopResult=$null;$processRecord=$null;"
        "$treeReconcileCount=$null;$runtimeSourceAcquiredViaGetProcessById=$false;"
        "try{"
        "[void][IO.Directory]::CreateDirectory($state);"
        "[void][IO.Directory]::CreateDirectory($evidence);"
        "$startInfo=[Diagnostics.ProcessStartInfo]::new();"
        f"$startInfo.FileName='{_ps_single_quoted(_windows_powershell())}';"
        "$startInfo.Arguments='-NoLogo -NoProfile -NonInteractive "
        f"-EncodedCommand {encoded_launcher}';"
        "$startInfo.UseShellExecute=$false;$startInfo.CreateNoWindow=$true;"
        "$launcher=[Diagnostics.Process]::Start($startInfo);"
        + runtime_start
        + "if($null -eq $launcher -or $null -eq $runtime){"
        "throw 'loopback retained-observation probe did not start both processes'};"
        "Start-Sleep -Milliseconds 100;$launcher.Refresh();$runtime.Refresh();"
        "if($launcher.HasExited -or $runtime.HasExited){"
        "throw 'loopback retained-observation probe child exited before capture'};"
        "$launcherPid=[int]$launcher.Id;$runtimePid=[int]$runtime.Id;"
        + runtime_ticks
        + "$launcherSnapshot=New-W4ProcessTreeIdentitySnapshot -Process $launcher;"
        + runtime_snapshot
        + runtime_observation
        + "$harnessResult=[ordered]@{"
        "schema_version='pkv.w3.loopback.result.v1';result='passed';"
        "completed_steps=3;total_steps=3}|ConvertTo-Json -Compress;"
        "[IO.File]::WriteAllText((Join-Path $state 'result.json'),"
        "$harnessResult,[Text.UTF8Encoding]::new($false));"
        "$harness=[pscustomobject]@{"
        "Process=$launcher;RuntimeProcess=$runtime;"
        "LauncherProcessTreeSnapshot=$launcherSnapshot;"
        "RuntimeProcessTreeSnapshot=$runtimeSnapshot;"
        "LauncherPid=$launcherPid;RuntimePid=$runtimePid;"
        "RuntimeExitObservation=$runtimeObservation;"
        "StdoutTask=[Threading.Tasks.Task[string]]::FromResult('');"
        "StderrTask=[Threading.Tasks.Task[string]]::FromResult('');"
        "StateDirectory=$state;Evidence=$evidence};"
        + wait_override_setup
        + tree_cleanup_setup
        + "$stopwatch=[Diagnostics.Stopwatch]::StartNew();"
        "try{$stopResult=& $module {param($value) "
        "Stop-W4LoopbackHarness -Harness $value} $harness}catch{"
        "$caught=$_.Exception.Message};"
        "$stopwatch.Stop();"
        "$runtimeWrapperInvalidated="
        + wait_override_result
        + "$runtimeObservationClosed=if($null -eq $runtimeObservation){$null}else{"
        "[bool]$runtimeObservation.IsClosed};"
        "$runtimeObservationReadAfterCloseRejected=$null;"
        "$runtimeObservationWaitAfterCloseRejected=$null;"
        "if($runtimeObservationClosed -eq $true){"
        "try{[void]$runtimeObservation.ReadExitedExitCode()}catch{"
        "$runtimeObservationReadAfterCloseRejected=$true};"
        "try{[void]$runtimeObservation.WaitForExit(0)}catch{"
        "$runtimeObservationWaitAfterCloseRejected=$true}};"
        "$treeReconcileCount="
        + tree_reconcile_count
        + "$evidenceResultExists=[bool](Test-Path -LiteralPath "
        "(Join-Path $evidence 'result.json') -PathType Leaf);"
        "$runtimeShutdownMarkerExists=[bool](Test-Path -LiteralPath "
        f"'{_ps_single_quoted(runtime_shutdown_marker)}' -PathType Leaf);"
        "if(Test-Path -LiteralPath (Join-Path $evidence 'process.json') -PathType Leaf){"
        "$processRecord=[IO.File]::ReadAllText("
        "(Join-Path $evidence 'process.json'))|ConvertFrom-Json};"
        f"[ordered]@{{topology='{topology}';runtime_mode='{runtime_mode}';"
        "runtime_source_acquired_via_get_process_by_id="
        "[bool]$runtimeSourceAcquiredViaGetProcessById;"
        "runtime_wrapper_invalidated=[bool]$runtimeWrapperInvalidated;"
        "runtime_observation_closed=$runtimeObservationClosed;"
        "runtime_observation_read_after_close_rejected="
        "$runtimeObservationReadAfterCloseRejected;"
        "runtime_observation_wait_after_close_rejected="
        "$runtimeObservationWaitAfterCloseRejected;"
        "runtime_shutdown_marker_exists=[bool]$runtimeShutdownMarkerExists;"
        "evidence_result_exists=[bool]$evidenceResultExists;"
        "stop_elapsed_milliseconds=[int64]$stopwatch.ElapsedMilliseconds;caught=$caught;"
        "stop_result=if($null -eq $stopResult){$null}else{[string]$stopResult.result};"
        "tree_reconcile_count=if($null -eq $treeReconcileCount){$null}else{"
        "[int]$treeReconcileCount};"
        f"tree_cleanup_is_real={'$true' if use_real_tree_cleanup else '$false'};"
        "process_record=$processRecord}|ConvertTo-Json -Depth 10 -Compress"
        "}finally{"
        "foreach($observation in @($runtimeObservation)){"
        "if($null -ne $observation){try{$observation.Dispose()}catch{}}"
        "};"
        "foreach($candidate in @($runtime,$launcher)){"
        "if($null -eq $candidate){continue};"
        "try{if(-not $candidate.HasExited){$candidate.Kill();"
        "[void]$candidate.WaitForExit(1000)}}catch{};"
        "try{$candidate.Dispose()}catch{}"
        "}"
        "}"
    )
    return _run_powershell(
        ["-Command", command], cwd=REPOSITORY_ROOT, timeout=30
    )


def _run_retained_process_exit_observation_lifecycle_probe(
    tmp_path: Path,
) -> subprocess.CompletedProcess[str]:
    """Prove a duplicated native exit handle survives managed-process disposal."""
    root = tmp_path / f"retained-exit-observation-{uuid.uuid4().hex}"
    release = root / "release.signal"
    child_script = (
        "$ErrorActionPreference='Stop';"
        f"$release='{_ps_single_quoted(release)}';"
        "$deadline=[DateTime]::UtcNow.AddSeconds(20);"
        "while(-not (Test-Path -LiteralPath $release -PathType Leaf)){"
        "if([DateTime]::UtcNow -ge $deadline){exit 91};"
        "Start-Sleep -Milliseconds 20};"
        "exit 17"
    )
    encoded_child = base64.b64encode(child_script.encode("utf-16le")).decode("ascii")
    command = (
        "$ErrorActionPreference='Stop';"
        f"Import-Module '{_ps_single_quoted(DRIVER_MODULE)}' -Force;"
        f"$root='{_ps_single_quoted(root)}';"
        f"$release='{_ps_single_quoted(release)}';"
        "$launch=$null;$child=$null;$observation=$null;$bootstrap=$null;"
        "$wrongPidRejections=0;$wrongTicksRejections=0;"
        "$stillActiveRejected=$false;$stillActiveWait=$null;$managedDisposed=$false;"
        "$managedPostExitUnavailable=$false;$managedPostExitCode=$null;"
        "$retainedExitCode=$null;$retainedExitedWait=$null;"
        "$observationDisposed=$false;$observationClosed=$false;"
        "$disposedReadRejected=$false;"
        "try{"
        "[void][IO.Directory]::CreateDirectory($root);"
        "$startInfo=[Diagnostics.ProcessStartInfo]::new();"
        f"$startInfo.FileName='{_ps_single_quoted(_windows_powershell())}';"
        "$startInfo.Arguments='-NoLogo -NoProfile -NonInteractive "
        f"-EncodedCommand {encoded_child}';"
        "$startInfo.UseShellExecute=$false;$startInfo.CreateNoWindow=$true;"
        "$launch=[Diagnostics.Process]::Start($startInfo);"
        "if($null -eq $launch){throw 'retained exit-observation probe child did not start'};"
        "$child=[Diagnostics.Process]::GetProcessById([int]$launch.Id);"
        "$launch.Dispose();$launch=$null;"
        "Start-Sleep -Milliseconds 100;$child.Refresh();"
        "if($child.HasExited){throw 'retained exit-observation probe child exited before capture'};"
        "$expectedPid=[int]$child.Id;"
        "$expectedTicks=[int64]$child.StartTime.ToUniversalTime().Ticks;"
        "$bootstrap=New-W4RetainedProcessExitObservation -Process $child "
        "-ExpectedProcessId $expectedPid -ExpectedStartTimeUtcTicks $expectedTicks "
        "-Label 'retained exit bootstrap';$bootstrap.Dispose();$bootstrap=$null;"
        "for($index=0;$index -lt 3;$index+=1){"
        "try{[void](New-W4RetainedProcessExitObservation -Process $child "
        "-ExpectedProcessId ($expectedPid + 1) "
        "-ExpectedStartTimeUtcTicks $expectedTicks -Label 'wrong pid')}"
        "catch{$wrongPidRejections+=1};"
        "try{[void](New-W4RetainedProcessExitObservation -Process $child "
        "-ExpectedProcessId $expectedPid "
        "-ExpectedStartTimeUtcTicks ($expectedTicks + 1) -Label 'wrong start ticks')}"
        "catch{$wrongTicksRejections+=1}"
        "};"
        "$Error.Clear();"
        "$observation=New-W4RetainedProcessExitObservation -Process $child "
        "-ExpectedProcessId $expectedPid -ExpectedStartTimeUtcTicks $expectedTicks "
        "-Label 'retained exit child';"
        "$stillActiveWait=[bool]$observation.WaitForExit(0);"
        "try{[void]$observation.ReadExitedExitCode()}catch{$stillActiveRejected=$true};"
        "[IO.File]::WriteAllText($release,'release',[Text.UTF8Encoding]::new($false));"
        "if(-not $child.WaitForExit(10000)){throw 'retained exit-observation child did not exit'};"
        "$child.Dispose();$managedDisposed=$true;"
        "try{$managedPostExitCode=[int]$child.ExitCode}catch{$managedPostExitUnavailable=$true};"
        "$retainedExitedWait=[bool]$observation.WaitForExit(0);"
        "$retainedExitCode=[int]$observation.ReadExitedExitCode();"
        "$observation.Dispose();$observationDisposed=$true;"
        "$observationClosed=[bool]$observation.IsClosed;"
        "try{[void]$observation.ReadExitedExitCode()}catch{$disposedReadRejected=$true};"
        "$paths=@(Get-ChildItem -LiteralPath $root -Force | "
        "ForEach-Object {$_.Name});"
        "[ordered]@{source_acquired_via_get_process_by_id=$true;"
        "wrong_pid_rejections=$wrongPidRejections;"
        "wrong_start_ticks_rejections=$wrongTicksRejections;"
        "still_active_rejected=[bool]$stillActiveRejected;"
        "still_active_wait=[bool]$stillActiveWait;"
        "managed_process_disposed=[bool]$managedDisposed;"
        "managed_post_exit_unavailable=[bool]$managedPostExitUnavailable;"
        "managed_post_exit_code=$managedPostExitCode;"
        "retained_exited_wait=[bool]$retainedExitedWait;"
        "retained_exit_code=$retainedExitCode;"
        "observation_disposed=[bool]$observationDisposed;"
        "observation_closed=[bool]$observationClosed;"
        "disposed_read_rejected=[bool]$disposedReadRejected;"
        "probe_paths=@($paths)}|ConvertTo-Json -Depth 10 -Compress"
        "}finally{"
        "if($null -ne $bootstrap){try{$bootstrap.Dispose()}catch{}};"
        "if($null -ne $observation -and -not $observationDisposed){"
        "try{$observation.Dispose()}catch{}};"
        "if($null -ne $launch){try{$launch.Dispose()}catch{}};"
        "if($null -ne $child){"
        "try{if(-not $child.HasExited){$child.Kill();"
        "[void]$child.WaitForExit(1000)}}catch{};"
        "if(-not $managedDisposed){try{$child.Dispose()}catch{}}"
        "}"
        "};exit 0"
    )
    return _run_powershell(
        ["-Command", command], cwd=REPOSITORY_ROOT, timeout=30
    )


def _write_hung_capture_worker_driver_module(tmp_path: Path) -> Path:
    source = _read(DRIVER_MODULE)
    worker_prelude = (
        "$ErrorActionPreference = 'Stop'\n"
        "try {\n"
        "    $module = Import-Module -Name $env:PKV_W4_CAPTURE_MODULE "
        "-Force -PassThru -ErrorAction Stop"
    )
    hung_worker_prelude = (
        "$ErrorActionPreference = 'Stop'\n"
        "try {\n"
        "    Start-Sleep -Seconds 30\n"
        "    $module = Import-Module -Name $env:PKV_W4_CAPTURE_MODULE "
        "-Force -PassThru -ErrorAction Stop"
    )
    assert source.count(worker_prelude) == 1
    mutated = source.replace(worker_prelude, hung_worker_prelude, 1)
    assert mutated != source
    module_path = tmp_path / "W4.Driver.hung-worker-timeout.psm1"
    module_path.write_text(mutated, encoding="utf-8-sig")
    return module_path


def _run_save_screenshot_hung_worker_probe(
    tmp_path: Path,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    module_path = _write_hung_capture_worker_driver_module(tmp_path)
    title = f"W4HungCaptureWorker-{uuid.uuid4().hex}"
    screenshot = tmp_path / "window-capture-hung-worker.png"
    child_script = f"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName PresentationFramework -ErrorAction Stop
Add-Type -AssemblyName PresentationCore -ErrorAction Stop
Add-Type -AssemblyName WindowsBase -ErrorAction Stop
$window = [System.Windows.Window]::new()
$window.Title = '{title}'
$window.Width = 360
$window.Height = 260
$window.WindowStartupLocation = [System.Windows.WindowStartupLocation]::Manual
$window.Left = 160
$window.Top = 160
$window.ShowInTaskbar = $false
$window.Topmost = $true
$window.ShowActivated = $true
$window.SizeToContent = [System.Windows.SizeToContent]::Manual
[System.Windows.Automation.AutomationProperties]::SetAutomationId(
    $window,
    'pkv_main_window'
)
$grid = [System.Windows.Controls.Grid]::new()
$grid.Background = [System.Windows.Media.Brushes]::DarkBlue
$left = [System.Windows.Controls.Border]::new()
$left.Width = 120
$left.Height = 120
$left.HorizontalAlignment = [System.Windows.HorizontalAlignment]::Left
$left.VerticalAlignment = [System.Windows.VerticalAlignment]::Top
$left.Background = [System.Windows.Media.Brushes]::OrangeRed
$right = [System.Windows.Controls.Border]::new()
$right.Width = 120
$right.Height = 120
$right.HorizontalAlignment = [System.Windows.HorizontalAlignment]::Right
$right.VerticalAlignment = [System.Windows.VerticalAlignment]::Bottom
$right.Background = [System.Windows.Media.Brushes]::Gold
$label = [System.Windows.Controls.TextBlock]::new()
$label.Text = 'HUNG PRINTWINDOW WORKER PROBE'
$label.Foreground = [System.Windows.Media.Brushes]::White
$label.FontSize = 18
$label.HorizontalAlignment = [System.Windows.HorizontalAlignment]::Center
$label.VerticalAlignment = [System.Windows.VerticalAlignment]::Center
[void]$grid.Children.Add($left)
[void]$grid.Children.Add($right)
[void]$grid.Children.Add($label)
$window.Content = $grid
$window.Add_Loaded({{
    [void]$window.Activate()
}})
[void]$window.ShowDialog()
"""
    encoded_child = base64.b64encode(child_script.encode("utf-16le")).decode("ascii")
    command = (
        "$ErrorActionPreference='Stop';"
        "Add-Type -AssemblyName UIAutomationClient -ErrorAction Stop;"
        "Add-Type -AssemblyName UIAutomationTypes -ErrorAction Stop;"
        f"$module=Import-Module '{_ps_single_quoted(module_path)}' -Force -PassThru;"
        f"$output='{_ps_single_quoted(screenshot)}';"
        "$process=$null;$window=$null;$caught=$null;$captureElapsedMilliseconds=-1;"
        "try{"
        "$psi=[Diagnostics.ProcessStartInfo]::new();"
        f"$psi.FileName='{_ps_single_quoted(_windows_powershell())}';"
        f"$psi.Arguments='-NoLogo -NoProfile -NonInteractive -Sta -EncodedCommand "
        f"{encoded_child}';"
        "$psi.UseShellExecute=$false;$psi.CreateNoWindow=$true;"
        "$process=[Diagnostics.Process]::Start($psi);"
        "if($null -eq $process){throw 'hung screenshot target did not start'};"
        "$desktop=[System.Windows.Automation.AutomationElement]::RootElement;"
        "$pidCondition=[System.Windows.Automation.PropertyCondition]::new("
        "[System.Windows.Automation.AutomationElement]::ProcessIdProperty,"
        "[int]$process.Id);"
        "$nameCondition=[System.Windows.Automation.PropertyCondition]::new("
        "[System.Windows.Automation.AutomationElement]::NameProperty,"
        f"'{title}');"
        "$automationCondition=[System.Windows.Automation.PropertyCondition]::new("
        "[System.Windows.Automation.AutomationElement]::AutomationIdProperty,"
        "'pkv_main_window');"
        "$windowCondition=[System.Windows.Automation.AndCondition]::new("
        "[System.Windows.Automation.Condition[]]@($pidCondition,$nameCondition,"
        "$automationCondition));"
        "$discoveryDeadline=[DateTime]::UtcNow.AddSeconds(15);$observedAutomationId='';"
        "do{"
        "$window=$desktop.FindFirst("
        "[System.Windows.Automation.TreeScope]::Children,$windowCondition);"
        "if($null -ne $window){"
        "try{if([string]$window.Current.AutomationId -ceq 'pkv_main_window' "
        "-and -not [bool]$window.Current.IsOffscreen){"
        "$observedAutomationId='pkv_main_window';break}}"
        "catch [System.Windows.Automation.ElementNotAvailableException]{};"
        "$window=$null};"
        "$process.Refresh();"
        "if($process.HasExited){throw 'hung screenshot target exited before discovery'};"
        "Start-Sleep -Milliseconds 50"
        "}while([DateTime]::UtcNow -lt $discoveryDeadline);"
        "if($null -eq $window){throw 'hung screenshot target was not found by UIA'};"
        "$captureStopwatch=[Diagnostics.Stopwatch]::StartNew();"
        "try{& $module {param($path,$element,$target) "
        "Save-W4Screenshot -Path $path -Element $element -Process $target "
        "-MaximumAttempts 3 -TimeoutSeconds 3} $output $window $process}"
        "catch{$caught=$_.Exception.Message};"
        "$captureStopwatch.Stop();"
        "$captureElapsedMilliseconds=[int64]$captureStopwatch.ElapsedMilliseconds;"
        "[ordered]@{caught=$caught;"
        "capture_elapsed_milliseconds=$captureElapsedMilliseconds;"
        "screenshot_exists=[bool](Test-Path -LiteralPath $output -PathType Leaf);"
        "capture_evidence_exists="
        "[bool](Test-Path -LiteralPath ($output+'.capture.json') -PathType Leaf);"
        "automation_id=$observedAutomationId;"
        "window_process_id=[int]$window.Current.ProcessId;"
        "target_process_id=[int]$process.Id}|ConvertTo-Json -Compress"
        "}finally{"
        "if($null -ne $process){try{$process.Refresh();"
        "if(-not $process.HasExited){$process.Kill();"
        "[void]$process.WaitForExit(5000)}}catch{};$process.Dispose()}"
        "}"
    )
    return (
        _run_powershell(["-Command", command], cwd=REPOSITORY_ROOT, timeout=45),
        screenshot,
    )


def _run_provider_gate_sequence_probe(
    mode: str, checkpoint_path: Path,
) -> subprocess.CompletedProcess[str]:
    assert mode in {"success", "wrong_line", "missing_third"}
    exact_line = "POST /v1/chat/completions HTTP/1.1"
    lines = {
        "success": [exact_line, exact_line, exact_line],
        "wrong_line": [
            "GET /v1/chat/completions?token=w4-sensitive-gate-canary HTTP/1.1"
        ],
        "missing_third": [exact_line, exact_line],
    }[mode]
    line_literals = ",".join(f"'{line}'" for line in lines)
    child_script = f"""
$ErrorActionPreference = 'Stop'
$port = [int]$env:PKV_W4_GATE_PROBE_PORT
$lines = @({line_literals})
foreach ($line in $lines) {{
    $client = [System.Net.Sockets.TcpClient]::new()
    try {{
        $client.Connect([System.Net.IPAddress]::Loopback, $port)
        $stream = $client.GetStream()
        $bytes = [System.Text.Encoding]::ASCII.GetBytes($line + "`r`n")
        $stream.Write($bytes, 0, $bytes.Length)
        $stream.Flush()
        $stream.ReadTimeout = 15000
        try {{
            while ($stream.ReadByte() -ge 0) {{}}
        }} catch [System.IO.IOException] {{}}
    }} finally {{
        $client.Dispose()
    }}
}}
"""
    encoded_child = base64.b64encode(child_script.encode("utf-16le")).decode("ascii")
    command = (
        "$ErrorActionPreference='Stop';"
        f"Import-Module '{_ps_single_quoted(DRIVER_MODULE)}' -Force;"
        f"$module=Import-Module '{_ps_single_quoted(SCENARIO_MODULE)}' "
        "-Force -PassThru;"
        f"$mode='{mode}';"
        f"$checkpointPath='{_ps_single_quoted(checkpoint_path)}';"
        "$listener=$null;$process=$null;"
        "$client1=$null;$client2=$null;$client3=$null;$client4=$null;"
        "$rows=[System.Collections.Generic.List[object]]::new();"
        "$prearmed=[System.Collections.Generic.List[int]]::new();"
        "$caught=$null;$listenerStoppedBeforeFinalRelease=$false;"
        "$fourthRequestProcessed=$false;"
        "$stopwatch=[Diagnostics.Stopwatch]::StartNew();"
        "try {"
        "$listener=[Net.Sockets.TcpListener]::new("
        "[Net.IPAddress]::Loopback,0);$listener.Start(1);"
        "$port=([Net.IPEndPoint]$listener.LocalEndpoint).Port;"
        "$accept1=$listener.AcceptTcpClientAsync();[void]$prearmed.Add(1);"
        "$psi=[Diagnostics.ProcessStartInfo]::new();"
        "$psi.FileName=(Join-Path $PSHOME 'powershell.exe');"
        f"$psi.Arguments='-NoLogo -NoProfile -NonInteractive -EncodedCommand "
        f"{encoded_child}';"
        "$psi.UseShellExecute=$false;$psi.CreateNoWindow=$true;"
        "$psi.EnvironmentVariables['PKV_W4_GATE_PROBE_PORT']=[string]$port;"
        "$process=[Diagnostics.Process]::Start($psi);"
        "if($null -eq $process){throw 'Provider gate probe process did not start'};"
        "try {"
        "$request1=& $module {param($task,$listener,$process)"
        "Receive-W4ExpectedProviderGateRequest -AcceptTask $task "
        "-Listener $listener -Process $process -Ordinal 1 "
        "-Stage 'text_fallback_summarize' -AcceptTimeoutMilliseconds 5000 "
        "-OwnerTimeoutMilliseconds 2000 -RequestLineTimeoutMilliseconds 5000 "
        "-RequestLineMaxBytes 2048} $accept1 $listener $process;"
        "$client1=$request1.Client;[void]$rows.Add($request1.Evidence);"
        "& $module {param($path,[object[]]$rows) "
        "Write-W4ProviderGateCheckpoint "
        "-Path $path -ExpectedProviderRequests 3 -ProviderRequests $rows "
        "-ListenerStopped $false} $checkpointPath $rows.ToArray();"
        "if($mode -ceq 'wrong_line'){"
        "throw 'Wrong Provider request line was unexpectedly accepted'};"
        "$accept2=$listener.AcceptTcpClientAsync();"
        "if($accept2.IsCompleted){throw 'Provider accept two was not prearmed'};"
        "[void]$prearmed.Add(2);$client1.Dispose();$client1=$null;"
        "$request2=& $module {param($task,$listener,$process)"
        "Receive-W4ExpectedProviderGateRequest -AcceptTask $task "
        "-Listener $listener -Process $process -Ordinal 2 "
        "-Stage 'workflow_summarize' -AcceptTimeoutMilliseconds 5000 "
        "-OwnerTimeoutMilliseconds 2000 -RequestLineTimeoutMilliseconds 5000 "
        "-RequestLineMaxBytes 2048} $accept2 $listener $process;"
        "$client2=$request2.Client;[void]$rows.Add($request2.Evidence);"
        "& $module {param($path,[object[]]$rows) "
        "Write-W4ProviderGateCheckpoint "
        "-Path $path -ExpectedProviderRequests 3 -ProviderRequests $rows "
        "-ListenerStopped $false} $checkpointPath $rows.ToArray();"
        "$accept3=$listener.AcceptTcpClientAsync();"
        "if($accept3.IsCompleted){throw 'Provider accept three was not prearmed'};"
        "[void]$prearmed.Add(3);$client2.Dispose();$client2=$null;"
        "$thirdAcceptTimeout=if($mode -ceq 'missing_third'){1200}else{5000};"
        "$request3=& $module {param($task,$listener,$process,$timeout)"
        "Receive-W4ExpectedProviderGateRequest -AcceptTask $task "
        "-Listener $listener -Process $process -Ordinal 3 "
        "-Stage 'workflow_extract_tags' -AcceptTimeoutMilliseconds $timeout "
        "-OwnerTimeoutMilliseconds 2000 -RequestLineTimeoutMilliseconds 5000 "
        "-RequestLineMaxBytes 2048 -StopListenerAfterAccept} "
        "$accept3 $listener $process $thirdAcceptTimeout;"
        "$client3=$request3.Client;[void]$rows.Add($request3.Evidence);"
        "$listenerStoppedBeforeFinalRelease="
        "[bool]$request3.Evidence.listener_stopped_after_accept;"
        "if($listener.Server.IsBound){throw 'Provider listener remained bound'};"
        "& $module {param($path,[object[]]$rows) "
        "Write-W4ProviderGateCheckpoint "
        "-Path $path -ExpectedProviderRequests 3 -ProviderRequests $rows "
        "-ListenerStopped $true} $checkpointPath $rows.ToArray();"
        "$client4=[Net.Sockets.TcpClient]::new();"
        "$completed4=$false;"
        "try{$connect4=$client4.ConnectAsync([Net.IPAddress]::Loopback,$port);"
        "try{$completed4=[bool]$connect4.Wait(2000)}"
        "catch [AggregateException]{$completed4=$false}"
        "catch [Net.Sockets.SocketException]{$completed4=$false};"
        "if($completed4 -and $client4.Connected){"
        "try{$stream4=$client4.GetStream();$stream4.ReadTimeout=2000;"
        "$line4=[Text.Encoding]::ASCII.GetBytes("
        "'POST /v1/chat/completions HTTP/1.1'+\"`r`n\");"
        "$stream4.Write($line4,0,$line4.Length);$stream4.Flush();"
        "$read4=$stream4.ReadByte();if($read4 -ge 0){"
        "$fourthRequestProcessed=$true;"
        "throw 'Fourth Provider request received an application response'};"
        "}catch [IO.IOException]{}catch [Net.Sockets.SocketException]{}}"
        "}finally{$client4.Dispose();$client4=$null};"
        "$client3.Dispose();$client3=$null"
        "}catch{$caught=$_.Exception.Message};"
        "$stopwatch.Stop();"
        "$checkpoint=$null;if(Test-Path -LiteralPath $checkpointPath -PathType Leaf){"
        "$checkpoint=Get-Content -LiteralPath $checkpointPath -Raw|ConvertFrom-Json};"
        "[ordered]@{mode=$mode;process_id=[int]$process.Id;"
        "prearmed_ordinals=@($prearmed);requests=@($rows);"
        "listener_stopped_before_final_release="
        "[bool]$listenerStoppedBeforeFinalRelease;"
        "fourth_request_processed=[bool]$fourthRequestProcessed;"
        "checkpoint=$checkpoint;"
        "caught=$caught;elapsed_milliseconds=[int64]$stopwatch.ElapsedMilliseconds}"
        "|ConvertTo-Json -Depth 10 -Compress"
        "}finally{"
        "if($null -ne $client1){$client1.Dispose()};"
        "if($null -ne $client2){$client2.Dispose()};"
        "if($null -ne $client3){$client3.Dispose()};"
        "if($null -ne $client4){$client4.Dispose()};"
        "if($null -ne $listener){$listener.Stop()};"
        "if($null -ne $process){try{$process.Refresh();"
        "if(-not $process.HasExited){$process.Kill();"
        "[void]$process.WaitForExit(5000)}}catch{};$process.Dispose()}"
        "}"
    )
    return _run_powershell(
        ["-Command", command], cwd=REPOSITORY_ROOT, timeout=30
    )


def _run_gated_request_line_validator(
    tmp_path: Path, request_line: str
) -> subprocess.CompletedProcess[str]:
    request_path = tmp_path / f"request-line-{uuid.uuid4().hex}.txt"
    request_path.write_text(request_line, encoding="utf-8")
    command = (
        "$ErrorActionPreference='Stop';"
        f"Import-Module '{_ps_single_quoted(DRIVER_MODULE)}' -Force;"
        f"$module=Import-Module '{_ps_single_quoted(SCENARIO_MODULE)}' "
        "-Force -PassThru;"
        f"$line=[IO.File]::ReadAllText('{_ps_single_quoted(request_path)}');"
        "& $module {param($value) "
        "Assert-W4GatedProviderRequestLine -RequestLine $value} $line"
    )
    return _run_powershell(["-Command", command], cwd=REPOSITORY_ROOT)


def _upgrade_rejection_envelope(**updates: object) -> dict[str, object]:
    envelope: dict[str, object] = {
        "adapter": "cli",
        "code": "database_upgrade_required",
        "recoverable": False,
        "stage": "runtime_bootstrap",
        "status": "error",
    }
    envelope.update(updates)
    return envelope


def _extract_single_quoted_values(block: str) -> set[str]:
    return set(re.findall(r"'([^']+)'", block))


def _powershell_function(source: str, name: str) -> str:
    match = re.search(
        rf"(?ms)^function\s+{re.escape(name)}\s*\{{.*?(?=^function\s+\S+\s*\{{|\Z)",
        source,
    )
    assert match is not None, f"PowerShell function was not found: {name}"
    return match.group(0)


def _compact_powershell(source: str) -> str:
    return re.sub(r"[`\s]+", " ", source).strip()


def _run_bm25_search_input_scope_probe() -> subprocess.CompletedProcess[str]:
    """Exercise PowerShell's nested ``$input`` scope and inspect the real Action AST."""
    command = (
        "$ErrorActionPreference='Stop';"
        "$outerSentinel=[pscustomobject]@{kind='outer-search-input'};"
        "$input=$outerSentinel;"
        "$nested=& {[pscustomobject]@{"
        "input_is_enumerator=[bool]($input -is [System.Collections.IEnumerator]);"
        "input_is_outer=[bool][object]::ReferenceEquals($input,$outerSentinel);"
        "input_type=if($null -eq $input){'null'}else{$input.GetType().FullName}"
        "}};"
        "$tokens=$null;$parseErrors=$null;"
        "$ast=[System.Management.Automation.Language.Parser]::ParseFile("
        f"'{_ps_single_quoted(SCENARIO_MODULE)}',[ref]$tokens,[ref]$parseErrors);"
        "if($parseErrors.Count -ne 0){throw (($parseErrors|ForEach-Object Message)-join '; ')};"
        "$scenario=@($ast.FindAll({param($node)"
        "$node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and "
        "$node.Name -ceq 'Invoke-W4Bm25SearchScenario'},$true));"
        "if($scenario.Count -ne 1){throw 'BM25 scenario AST was not unique'};"
        "$backendFault=@($scenario[0].FindAll({param($node)"
        "if($node -isnot [System.Management.Automation.Language.CommandAst]){return $false};"
        "$elements=@($node.CommandElements);"
        "return $elements.Count -gt 0 -and "
        "$elements[0] -is [System.Management.Automation.Language.StringConstantExpressionAst] -and "
        "$elements[0].Value -ceq 'Invoke-W4WithFilePathBlockedByDirectory' -and "
        "$node.Extent.Text.Contains(\"-Label 'BM25 Search backend fault'\")"
        "},$true));"
        "if($backendFault.Count -ne 1){throw 'BM25 backend-fault Action AST was not unique'};"
        "$action=@($backendFault[0].CommandElements|Where-Object {"
        "$_ -is [System.Management.Automation.Language.ScriptBlockExpressionAst]});"
        "if($action.Count -ne 1){throw 'BM25 backend-fault Action scriptblock was not unique'};"
        "$actionVariables=@($action[0].ScriptBlock.FindAll({param($node)"
        "$node -is [System.Management.Automation.Language.VariableExpressionAst]"
        "},$true)|ForEach-Object {$_.VariablePath.UserPath.ToLowerInvariant()});"
        "$scenarioVariables=@($scenario[0].Body.FindAll({param($node)"
        "$node -is [System.Management.Automation.Language.VariableExpressionAst]"
        "},$true)|ForEach-Object {$_.VariablePath.UserPath.ToLowerInvariant()});"
        "[ordered]@{"
        "input_is_enumerator=[bool]$nested.input_is_enumerator;"
        "input_is_outer=[bool]$nested.input_is_outer;"
        "input_type=[string]$nested.input_type;"
        "action_variables=@($actionVariables);"
        "scenario_variables=@($scenarioVariables)"
        "}|ConvertTo-Json -Compress"
    )
    return _run_powershell(["-Command", command], cwd=REPOSITORY_ROOT)


def _has_safe_capture_publication_contract(source: str) -> bool:
    compact = _compact_powershell(source)
    markers = [
        "$evidenceTemporaryPath =",
        "Write-W4JsonFile -Path $evidenceTemporaryPath -Value",
        "-Stage 'pre-sidecar-publish'",
        "[System.IO.File]::Move($evidenceTemporaryPath, $evidencePath)",
        "$evidencePublished = $true",
        "-Stage 'pre-png-commit'",
        "[System.IO.File]::Move($temporaryPath, $fullPath)",
        "$temporaryPublished = $true",
    ]
    positions = [compact.find(marker) for marker in markers]
    if any(position < 0 for position in positions):
        return False
    if positions != sorted(positions) or len(set(positions)) != len(positions):
        return False
    if compact.count("[System.IO.File]::Move(") != 2:
        return False
    if "Write-W4JsonFile -Path $evidencePath" in compact:
        return False

    rollback_start = compact.find("} catch {", positions[-1])
    rollback_guard = compact.find("if ($evidencePublished)", rollback_start)
    rollback = compact.find(
        "Remove-W4CaptureTemporaryFile -Path $evidencePath "
        "-Label 'window capture sidecar rollback'",
        rollback_guard,
    )
    rethrow = compact.find("throw", rollback)
    sidecar_temp_cleanup = compact.find(
        "Remove-W4CaptureTemporaryFile -Path $evidenceTemporaryPath",
        rethrow,
    )
    png_temp_cleanup = compact.find(
        "Remove-W4CaptureTemporaryFile -Path $temporaryPath",
        sidecar_temp_cleanup,
    )
    return (
        rollback_start >= 0
        and rollback_start < rollback_guard < rollback < rethrow < sidecar_temp_cleanup
        and png_temp_cleanup > sidecar_temp_cleanup
    )


def _has_strict_worker_capture_validation_contract(source: str) -> bool:
    try:
        attempt = _compact_powershell(
            _powershell_function(source, "Invoke-W4PrintWindowCaptureAttempt")
        )
        screenshot = _compact_powershell(
            _powershell_function(source, "Save-W4Screenshot")
        )
    except AssertionError:
        return False

    if "[Parameter(Mandatory = $true)][string]$MetadataPath" not in attempt:
        return False
    if "$width -gt 8192 -or $height -gt 8192" not in attempt:
        return False
    if "([int64]$width * [int64]$height) -gt 16777216" not in attempt:
        return False
    worker_markers = [
        "Test-W4BitmapPixelDiversity -Bitmap $bitmap",
        "$bitmap.Save($fullPath, [System.Drawing.Imaging.ImageFormat]::Png)",
        "$pngLength = [int64](Get-Item -LiteralPath $fullPath",
        "$pngLength -le 0 -or $pngLength -gt 134217728",
        "Write-W4JsonFile -Path $fullMetadataPath -Value",
    ]
    worker_positions = [attempt.find(marker) for marker in worker_markers]
    if any(position < 0 for position in worker_positions):
        return False
    if worker_positions != sorted(worker_positions):
        return False
    worker_metadata_match = re.search(
        r"Write-W4JsonFile -Path \$fullMetadataPath -Value "
        r"\(\[ordered\]@\{(.*?)\}\) -Compress",
        attempt,
    )
    if worker_metadata_match is None:
        return False
    worker_fields = re.findall(
        r"\b([a-z][a-z0-9_]*)\s*=", worker_metadata_match.group(1)
    )
    if worker_fields != [
        "schema_version",
        "method",
        "width",
        "height",
        "png_length",
        "pixel_diversity",
    ]:
        return False

    if "[System.Drawing.Bitmap]::new($temporaryPath)" in screenshot:
        return False
    if "Test-W4BitmapPixelDiversity" in screenshot:
        return False
    required_parent_markers = (
        "$workerMetadataPath = $temporaryPath + '.validation.json'",
        "PKV_W4_CAPTURE_METADATA = $workerMetadataPath",
        "-MetadataPath $metadataPath",
        "[int64]$pngItem.Length -gt 134217728",
        "[int64]$metadataItem.Length -gt 4096",
        "[PkvW4.FileIdentity]::GetLinkCount($pngItem.FullName) -ne 1",
        "[PkvW4.FileIdentity]::GetLinkCount($metadataItem.FullName) -ne 1",
        "$workerMetadata.width -isnot [int]",
        "$workerMetadata.width -isnot [int64]",
        "$workerMetadata.height -isnot [int]",
        "$workerMetadata.height -isnot [int64]",
        "$workerMetadata.pixel_diversity -isnot [bool]",
        "$metadataWidth -gt 8192 -or $metadataHeight -gt 8192",
        "($metadataWidth * $metadataHeight) -gt 16777216",
        "$capturedWidth = [int]$metadataWidth",
        "$capturedHeight = [int]$metadataHeight",
        "[int64]$workerMetadata.png_length -ne [int64]$pngItem.Length",
        "-Stage 'post-worker-metadata-validation'",
        "Remove-W4CaptureTemporaryFile -Path $workerMetadataPath",
    )
    if any(marker not in screenshot for marker in required_parent_markers):
        return False
    cleanup = "Remove-W4CaptureTemporaryFile -Path $workerMetadataPath"
    metadata_deadline = screenshot.find("-Stage 'post-worker-metadata-validation'")
    first_cleanup = screenshot.find(cleanup, metadata_deadline)
    evidence_temp = screenshot.find("$evidenceTemporaryPath =", first_cleanup)
    second_cleanup = screenshot.find(cleanup, evidence_temp)
    if (
        screenshot.count(cleanup) != 2
        or not (
            metadata_deadline >= 0
            and metadata_deadline < first_cleanup < evidence_temp < second_cleanup
        )
    ):
        return False
    if "$boundWidth -gt 8192 -or $boundHeight -gt 8192" not in screenshot:
        return False
    if "([int64]$boundWidth * [int64]$boundHeight) -gt 16777216" not in screenshot:
        return False
    expected_fields_match = re.search(
        r"\$expectedWorkerMetadataFields = @\((.*?)\)", screenshot
    )
    if expected_fields_match is None:
        return False
    return re.findall(r"'([^']+)'", expected_fields_match.group(1)) == [
        "schema_version",
        "method",
        "width",
        "height",
        "png_length",
        "pixel_diversity",
    ]


def _uia_contract_segments(source: str) -> dict[str, set[str]]:
    compact = _compact_powershell(source)
    marker = "Assert-W4UiaContractSegment"
    segments: dict[str, set[str]] = {}
    calls = list(
        re.finditer(
            rf"{marker}\s+-Gui\b(?:(?!{marker}).)*?"
            r"-AutomationIds @\((.*?)\) -EvidenceName '([^']+)'",
            compact,
        )
    )
    for match in calls:
        evidence_name = match.group(2)
        assert evidence_name not in segments, (
            f"duplicate UIA evidence segment: {evidence_name}"
        )
        segments[evidence_name] = _extract_single_quoted_values(match.group(1))
    assert len(segments) == len(re.findall(rf"{marker}\s+-Gui\b", compact))
    return segments


def _uia_contract_segment_ids(source: str, evidence_name: str) -> set[str]:
    segments = _uia_contract_segments(source)
    assert evidence_name in segments, (
        f"UIA contract segment was not found: {evidence_name}"
    )
    return segments[evidence_name]


def _uia_absence_proofs(source: str) -> dict[str, set[str]]:
    compact = _compact_powershell(source)
    marker = "Assert-W4UiaAutomationIdsAbsent"
    proofs: dict[str, set[str]] = {}
    calls = list(
        re.finditer(
            rf"{marker}\s+-Gui\b(?:(?!{marker}).)*?"
            r"-AutomationIds @\((.*?)\) -EvidenceName '([^']+)'",
            compact,
        )
    )
    for match in calls:
        evidence_name = match.group(2)
        assert evidence_name not in proofs, (
            f"duplicate UIA absence proof: {evidence_name}"
        )
        proofs[evidence_name] = _extract_single_quoted_values(match.group(1))
    assert len(proofs) == len(re.findall(rf"{marker}\s+-Gui\b", compact))
    return proofs


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
    scenario_contract = controller_root / "scenarios.v2.json"
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


def test_outer_tree_manifest_hash_matches_driver_for_mixed_case_paths(
    external_scratch: Path,
) -> None:
    tree_root = external_scratch / "mixed-case-controller-fixture-tree"
    contents = {
        "driver-manifest.json": "driver manifest\n",
        "driver-manifest.sha256": "driver sidecar\n",
        "Invoke-W4ArtifactE2E.ps1": "controller\n",
        "W4.Driver.psm1": "driver module\n",
        "W4.Scenarios.psm1": "scenario module\n",
        "fixtures/chat-success-prompt.v1.txt": "success\n",
        "fixtures/fixture-manifest.v1.json": "{}\n",
        "fixtures/offline-note.v1.txt": "offline\n",
    }
    for relative, content in contents.items():
        path = tree_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")

    rows_function = _powershell_function(_read(RUNNER), "Get-TreeManifestRows")
    assert "Get-Utf8SortedStrings -Values $rowsByPath.Keys" not in rows_function
    assert "$rowsByPath.Keys | Sort-Object" in rows_function

    result = _run_outer_tree_manifest_cross_hash_probe(tree_root)

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["legacy_utf8_paths"] != payload["driver_paths"]
    assert payload["outer_paths"] == payload["driver_paths"]
    assert payload["outer_manifest_json"] == payload["driver_manifest_json"]
    assert payload["outer_hash"] == payload["driver_hash"]
    assert payload["legacy_utf8_paths"][0] == "Invoke-W4ArtifactE2E.ps1"
    assert payload["driver_paths"][0] == "driver-manifest.json"


def test_scenario_contract_freezes_ten_scenarios_and_eleven_unique_rows() -> None:
    contract = json.loads(_read(SCENARIO_CONTRACT))
    scenarios = contract["ordered_scenarios"]
    scenario_ids = [item["scenario_id"] for item in scenarios]
    rows = [row for item in scenarios for row in item["matrix_rows"]]

    assert contract["schema_version"] == "pkv.m13.w4-driver-scenarios.v2"
    assert len(scenarios) == 10
    assert len(scenario_ids) == len(set(scenario_ids)) == 10
    assert len(rows) == len(set(rows)) == 11
    assert set(rows) == EXPECTED_MATRIX_ROWS
    assert set(contract["required_matrix_rows"]) == EXPECTED_MATRIX_ROWS
    harness_scenarios = [
        item["scenario_id"] for item in scenarios if item["requires_harness"]
    ]
    assert harness_scenarios == ["w4.chat_loopback.v1"]

    common_fields = {
        "scenario_id",
        "matrix_rows",
        "handler",
        "timeout_seconds",
        "requires_harness",
    }
    offline = next(
        item
        for item in scenarios
        if item["scenario_id"] == "w4.offline_text_archive.v1"
    )
    assert set(offline) == common_fields | {"expected_provider_requests"}
    assert type(offline["expected_provider_requests"]) is int
    assert offline["expected_provider_requests"] == 3
    assert all(set(item) == common_fields for item in scenarios if item is not offline)


def test_scenario_contract_v2_filename_and_schema_are_used_end_to_end() -> None:
    assert SCENARIO_CONTRACT.is_file()
    assert not (DRIVER_ROOT / "scenarios.v1.json").exists()

    sources = {
        "controller": _read(CONTROLLER),
        "exporter": _read(DRIVER_EXPORTER),
        "outer_runner": _read(RUNNER),
        "scenario_module": _read(SCENARIO_MODULE),
    }
    for source in sources.values():
        assert "scenarios.v1.json" not in source
        assert "pkv.m13.w4-driver-scenarios.v1" not in source

    assert "pkv.m13.w4-driver-scenarios.v2" in sources["controller"]
    assert "scenarios.v2.json" in sources["exporter"]
    assert "scenarios.v2.json" in sources["outer_runner"]
    assert "pkv.m13.w4-driver-scenarios.v2" in sources["outer_runner"]
    assert "scenarios.v2.json" in sources["scenario_module"]


def test_scenario_contract_v2_schema_is_case_sensitive(tmp_path: Path) -> None:
    contract = json.loads(_read(SCENARIO_CONTRACT))
    contract["schema_version"] = "pkv.m13.w4-driver-scenarios.V2"

    result = _run_scenario_contract_validator(tmp_path, contract)

    assert result.returncode != 0
    assert "schema/runner/Artifact version is not frozen v2/v2/0.8.1" in result.stderr


@pytest.mark.parametrize(
    ("mutation", "value"),
    [
        ("missing", None),
        ("wrong_count", 2),
        ("wrong_type", "3"),
    ],
)
def test_scenario_contract_rejects_missing_or_wrong_provider_request_count(
    tmp_path: Path, mutation: str, value: object
) -> None:
    contract = json.loads(_read(SCENARIO_CONTRACT))
    offline = next(
        item
        for item in contract["ordered_scenarios"]
        if item["scenario_id"] == "w4.offline_text_archive.v1"
    )
    if mutation == "missing":
        offline.pop("expected_provider_requests")
    else:
        offline["expected_provider_requests"] = value

    result = _run_scenario_contract_validator(tmp_path, contract)

    assert result.returncode != 0
    assert "offline" in result.stderr.lower()
    assert "provider" in result.stderr.lower()


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


def test_mcp_seed_calls_the_durable_archive_text_payload_validator() -> None:
    source = _read(SCENARIO_MODULE)
    seed_block = _powershell_function(source, "Invoke-W4McpSeedText")
    block = _powershell_function(source, "Assert-W4McpDurableSeedPayload")
    compact = _compact_powershell(block)
    required_call = re.search(
        r"Assert-W4JsonObjectFields\b.*?-Object \$Payload\b.*?-Label "
        r"(?:'archive_text[^']*'|\"archive_text[^\"]*\")",
        compact,
    )

    assert "Assert-W4McpDurableSeedPayload -Payload $payload" in _compact_powershell(
        seed_block
    )
    assert required_call is not None
    assert {
        "success",
        "terminal",
        "storage_status",
        "core_committed",
        "knowledge_id",
        "entry_locator",
    }.issubset(_extract_single_quoted_values(required_call.group(0)))
    assert (
        "Assert-W4ExactBoolean -Value $Payload.success -Expected $true" in compact
    )
    assert (
        "Assert-W4ExactBoolean -Value $Payload.core_committed -Expected $true"
        in compact
    )
    assert "$payload.status" not in block.casefold()
    assert "$payload.terminal" in block.casefold()
    assert "@('success', 'degraded')" in compact
    assert "$payload.storage_status" in block.casefold()
    assert "@('ready', 'degraded')" in compact
    assert re.search(
        r"\$Payload\.knowledge_id\s+-isnot\s+\[int(?:32|64)?\]", compact
    )
    assert "$Payload.knowledge_id -isnot [long]" in compact
    assert "[int64]$knowledgeId = $Payload.knowledge_id" in compact
    assert "$knowledgeId -le 0" in compact
    assert '$Payload.entry_locator -cne "pkv://entries/$knowledgeId"' in compact


@pytest.mark.parametrize(
    "payload",
    [
        {
            "success": True,
            "terminal": "success",
            "storage_status": "ready",
            "core_committed": True,
            "knowledge_id": 1,
            "entry_locator": "pkv://entries/1",
        },
        {
            "success": True,
            "terminal": "degraded",
            "storage_status": "ready",
            "core_committed": True,
            "knowledge_id": 6,
            "entry_locator": "pkv://entries/6",
        },
        {
            "success": True,
            "terminal": "degraded",
            "storage_status": "degraded",
            "core_committed": True,
            "knowledge_id": 7,
            "entry_locator": "pkv://entries/7",
        },
    ],
    ids=["success-ready", "degraded-ready", "degraded-degraded"],
)
def test_mcp_durable_seed_validator_accepts_coherent_success_envelopes(
    tmp_path: Path, payload: dict[str, object]
) -> None:
    result = _run_mcp_durable_seed_validator(tmp_path, payload)

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(
            {
                "success": True,
                "storage_status": "degraded",
                "core_committed": True,
                "knowledge_id": 1,
                "entry_locator": "pkv://entries/1",
            },
            id="missing-terminal",
        ),
        pytest.param(
            {
                "success": False,
                "terminal": "degraded",
                "storage_status": "degraded",
                "core_committed": True,
                "knowledge_id": 1,
                "entry_locator": "pkv://entries/1",
            },
            id="success-false",
        ),
        pytest.param(
            {
                "success": "true",
                "terminal": "degraded",
                "storage_status": "degraded",
                "core_committed": True,
                "knowledge_id": 1,
                "entry_locator": "pkv://entries/1",
            },
            id="success-string",
        ),
        pytest.param(
            {
                "success": True,
                "terminal": "error",
                "storage_status": "degraded",
                "core_committed": True,
                "knowledge_id": 1,
                "entry_locator": "pkv://entries/1",
            },
            id="terminal-error",
        ),
        pytest.param(
            {
                "success": True,
                "terminal": "success",
                "storage_status": "degraded",
                "core_committed": True,
                "knowledge_id": 1,
                "entry_locator": "pkv://entries/1",
            },
            id="storage-mismatch",
        ),
        pytest.param(
            {
                "success": True,
                "terminal": "degraded",
                "storage_status": "failed",
                "core_committed": True,
                "knowledge_id": 1,
                "entry_locator": "pkv://entries/1",
            },
            id="invalid-degraded-storage",
        ),
        pytest.param(
            {
                "success": True,
                "terminal": "degraded",
                "storage_status": "degraded",
                "core_committed": False,
                "knowledge_id": 1,
                "entry_locator": "pkv://entries/1",
            },
            id="core-not-committed",
        ),
        pytest.param(
            {
                "success": True,
                "terminal": "degraded",
                "storage_status": "degraded",
                "core_committed": True,
                "knowledge_id": 0,
                "entry_locator": "pkv://entries/0",
            },
            id="zero-id",
        ),
        pytest.param(
            {
                "success": True,
                "terminal": "degraded",
                "storage_status": "degraded",
                "core_committed": True,
                "knowledge_id": 1.0,
                "entry_locator": "pkv://entries/1",
            },
            id="floating-id",
        ),
        pytest.param(
            {
                "success": True,
                "terminal": "degraded",
                "storage_status": "degraded",
                "core_committed": True,
                "knowledge_id": 1,
                "entry_locator": "pkv://entries/2",
            },
            id="locator-mismatch",
        ),
    ],
)
def test_mcp_durable_seed_validator_rejects_incoherent_envelopes(
    tmp_path: Path, payload: dict[str, object]
) -> None:
    result = _run_mcp_durable_seed_validator(tmp_path, payload)

    assert result.returncode != 0


def test_mcp_relation_template_contract_includes_relation_source_type() -> None:
    contract = json.loads(_read(SCENARIO_CONTRACT))
    relation_templates = [
        value
        for value in contract["mcp"]["resource_templates"]
        if value.startswith("pkv://relations/by-edge/")
    ]

    assert relation_templates == [
        "pkv://relations/by-edge/{source_knowledge_id}/{target_knowledge_id}/"
        "{relation_type}/{relation_source_type}"
    ]


def test_upgrade_rejection_uses_empty_stdout_and_a_strict_stderr_envelope() -> None:
    source = _read(SCENARIO_MODULE)
    scenario_block = _powershell_function(source, "Invoke-W4UpgradeRejectionScenario")
    block = _powershell_function(
        source, "ConvertFrom-W4UpgradeRejectionResult"
    )
    compact = _compact_powershell(block)
    stdout_guard = compact.index("[string]$Result.StandardOutput -cne ''")
    stderr_parse = compact.index(
        "ConvertFrom-W4StrictJsonText -Text ([string]$Result.StandardError)"
    )
    fields_match = re.search(
        r"Assert-W4ExactObjectFields -Object \$payload -Fields @\((.*?)\) "
        r"-Label (?:\$Label|\([^)]*\)|'[^']*'|\"[^\"]*\")",
        compact,
    )

    assert stdout_guard < stderr_parse
    assert (
        "ConvertFrom-W4UpgradeRejectionResult -Result $result"
        in _compact_powershell(scenario_block)
    )
    assert "ConvertFrom-W4StrictJsonText -Text $Result.StandardOutput" not in compact
    assert fields_match is not None
    assert _extract_single_quoted_values(fields_match.group(1)) == {
        "adapter",
        "code",
        "recoverable",
        "stage",
        "status",
    }
    assert (
        "Assert-W4ExactBoolean -Value $payload.recoverable -Expected $false"
        in compact
    )
    assert "[string]$payload.adapter -cne 'cli'" in compact
    assert "[string]$payload.status -cne 'error'" in compact
    assert "[string]$payload.stage -cne 'runtime_bootstrap'" in compact
    assert "[string]$payload.code -cne $ExpectedCode" in compact


def test_upgrade_rejection_result_validator_accepts_exact_stderr_envelope(
    tmp_path: Path,
) -> None:
    result = _run_upgrade_rejection_result_validator(
        tmp_path,
        stdout="",
        stderr=json.dumps(_upgrade_rejection_envelope()),
    )

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("stdout", "stderr", "expected_code"),
    [
        pytest.param(
            "unexpected",
            json.dumps(_upgrade_rejection_envelope()),
            "database_upgrade_required",
            id="nonempty-stdout",
        ),
        pytest.param("", "", "database_upgrade_required", id="empty-stderr"),
        pytest.param("", "not-json", "database_upgrade_required", id="non-json-stderr"),
        pytest.param(
            "",
            json.dumps(_upgrade_rejection_envelope(extra="undeclared")),
            "database_upgrade_required",
            id="extra-field",
        ),
        pytest.param(
            "",
            json.dumps(_upgrade_rejection_envelope(recoverable="false")),
            "database_upgrade_required",
            id="wrong-boolean-type",
        ),
        pytest.param(
            "",
            json.dumps(_upgrade_rejection_envelope()),
            "database_future_version",
            id="wrong-code",
        ),
    ],
)
def test_upgrade_rejection_result_validator_rejects_wrong_channel_or_envelope(
    tmp_path: Path, stdout: str, stderr: str, expected_code: str
) -> None:
    result = _run_upgrade_rejection_result_validator(
        tmp_path,
        stdout=stdout,
        stderr=stderr,
        expected_code=expected_code,
    )

    assert result.returncode != 0


def test_offline_archive_requires_exact_modal_and_keeps_durable_oracles() -> None:
    block = _powershell_function(
        _read(SCENARIO_MODULE), "Invoke-W4OfflineTextArchiveScenario"
    )
    compact = _compact_powershell(block)
    result_wait = compact.index(
        "Wait-W4UiaTextContains -Element $resultTitle -Text '归档成功（降级）'"
    )
    required_modal = compact.index("Dismiss-W4ProcessModal", result_wait)
    modal_evidence = compact.index("'archive-modal-observation.json'", required_modal)
    terminal_absence = compact.index(
        "'uia-absence-archive-terminal.json'", modal_evidence
    )
    enabled_check = compact.index(
        "Wait-W4UiaElementEnabledState -Root $gui.Window", terminal_absence
    )
    warning_read = compact.index("-AutomationId 'archive_result_warning'", enabled_check)
    path_read = compact.index("-AutomationId 'archive_result_path'", warning_read)

    assert (
        result_wait
        < required_modal
        < modal_evidence
        < terminal_absence
        < enabled_check
        < warning_read
        < path_read
    )
    modal_call = compact[required_modal : compact.index(")", required_modal) + 1]
    assert "-MainWindow $gui.Window" in modal_call
    assert "-ExpectedTitle '归档降级警告'" in modal_call
    assert "-TimeoutSeconds 10" in modal_call
    assert "-AllowAbsent" not in modal_call
    assert "schema_version = 'pkv.w4.required-modal-observation.v1'" in compact
    assert "expected_title = '归档降级警告'" in compact
    assert "is_modal = $true" in compact
    assert "modal_visible = $true" in compact
    assert "action_visible = $true" in compact
    assert "action_enabled = $true" in compact
    assert "exact_ok_button_dismissed = $modalDismissed" in compact
    assert "unrecognized_process_window_accepted = $false" in compact
    expected_warning_match = re.search(
        r"\$expectedWarning\s*=\s*@\((.*?)\)\s*-join\s*''",
        block,
        flags=re.DOTALL,
    )
    assert expected_warning_match is not None
    assert re.findall(r"'([^']+)'", expected_warning_match.group(1)) == [
        "核心归档已完成，但本次结果处于降级状态。",
        "辅助索引需要修复（修复动作: rebuild_vectors_for_entry）。",
        (
            "部分可选工作流步骤未完成（问题代码: workflow_step_failed, "
            "storage_vector_failed）。"
        ),
        "请勿盲目重试归档。",
    ]
    assert (
        "-not $warning.Equals($expectedWarning, "
        "[System.StringComparison]::Ordinal)" in compact
    )
    assert "$warning.StartsWith(" not in compact
    assert "$warning.IndexOf(" not in compact
    assert "$warning.EndsWith(" not in compact
    assert "$warning -notmatch 'provider'" not in compact
    assert "$idText -notmatch '^ID:\\s*\\d+$'" in compact
    assert "$pathText -notmatch '^文件:\\s*(.+)$'" in compact
    assert "Test-W4PathContainedBy -Candidate $savedPath" in compact
    assert "Test-Path -LiteralPath $savedPath -PathType Leaf" in compact
    assert "workflow_terminal = 'degraded'" in compact
    assert "saved_path_sha256 = Get-W4FileSha256 -Path $savedPath" in compact
    assert "degraded_warning = $warning" in compact
    assert (
        "workflow_issue_codes = @('workflow_step_failed', "
        "'storage_vector_failed')" in compact
    )
    assert (
        "storage_repair_actions = @('rebuild_vectors_for_entry')" in compact
    )
    assert (
        "induced_workflow_issue_codes = "
        "@( 'workflow_step_failed', 'storage_vector_failed' )" in compact
    )
    assert (
        "expected_storage_repair_actions = "
        "@( 'rebuild_vectors_for_entry' )" in compact
    )
    assert "workflow_issue_code =" not in compact
    assert "induced_workflow_issue_code =" not in compact
    assert "restart_opened_saved_entry = $true" in compact


def test_required_process_modal_rejects_ambiguous_or_unrecognized_windows() -> None:
    helper = _compact_powershell(
        _powershell_function(_read(SCENARIO_MODULE), "Dismiss-W4ProcessModal")
    )

    assert (
        "[Parameter(Mandatory = $true)]"
        "[System.Windows.Automation.AutomationElement]$MainWindow" in helper
    )
    assert "[Parameter(Mandatory = $true)][string]$ExpectedTitle" in helper
    assert "AllowAbsent" not in helper
    assert "[string]::IsNullOrWhiteSpace($ExpectedTitle)" in helper
    assert "AutomationElement]::ProcessIdProperty" in helper
    assert "[int]$MainWindow.Current.ProcessId -ne $ProcessId" in helper
    assert "$MainWindow.Current.AutomationId -cne 'pkv_main_window'" in helper
    assert (
        "$MainWindow.Current.ControlType -ne "
        "[System.Windows.Automation.ControlType]::Window" in helper
    )
    assert "$mainWindowRuntimeId = @($MainWindow.GetRuntimeId())" in helper
    assert "$mainWindowRuntimeId.Count -eq 0" in helper
    assert "[System.Windows.Automation.AndCondition]::new(" in helper
    assert "$pidCondition, $windowType" in helper
    assert "$pidCondition, $buttonType" in helper
    descendant_scan = helper.index(
        "$desktop.FindAll( [System.Windows.Automation.TreeScope]::Descendants, "
        "$processWindowCondition )"
    )
    runtime_read = helper.index(
        "$windowRuntimeId = @($window.GetRuntimeId())", descendant_scan
    )
    runtime_count = helper.index(
        "$windowRuntimeId.Count -eq $mainWindowRuntimeId.Count", runtime_read
    )
    runtime_compare = helper.index(
        "[int]$windowRuntimeId[$runtimeIndex] -ne "
        "[int]$mainWindowRuntimeId[$runtimeIndex]",
        runtime_count,
    )
    main_window_filter = helper.index("if ($isMainWindow)", runtime_compare)
    modal_append = helper.index("$processModals += $window", main_window_filter)
    unique_guard = helper.index("$processModals.Count -gt 1", modal_append)
    assert (
        descendant_scan
        < runtime_read
        < runtime_count
        < runtime_compare
        < main_window_filter
        < modal_append
        < unique_guard
    )
    assert "$processModals.Count -gt 1" in helper
    assert "$processModals.Count -eq 1" in helper
    assert (
        "$modal.Current.ControlType -ne "
        "[System.Windows.Automation.ControlType]::Window" in helper
    )
    assert "$actualTitle -cne $ExpectedTitle" in helper
    assert "[bool]$modal.Current.IsOffscreen" in helper
    assert "WindowPattern]::Pattern" in helper
    assert "WindowPattern]$windowPattern).Current.IsModal" in helper
    assert "Expected process window is not modal" in helper
    assert (
        "$modal.FindAll( [System.Windows.Automation.TreeScope]::Descendants, "
        "$processButtonCondition )" in helper
    )
    assert "$buttons.Count -gt 1" in helper
    assert "$buttons.Count -eq 1" in helper
    assert "[int]$button.Current.ProcessId -ne $ProcessId" in helper
    assert "[bool]$button.Current.IsOffscreen" in helper
    assert "-not [bool]$button.Current.IsEnabled" in helper
    assert "@('OK', '确定') -ccontains [string]$button.Current.Name" in helper
    assert "InvokePattern]::Pattern" in helper
    assert "InvokePattern]$invokePattern).Invoke()" in helper
    assert "return $true" in helper
    assert "return $false" not in helper
    assert "if ($sawProcessModal)" in helper
    assert "did not expose one exact OK/确定 Invoke button" in helper
    assert "No process modal with exact title appeared" in helper


def test_required_modal_invoke_proves_original_runtime_id_disappeared() -> None:
    helper = _compact_powershell(
        _powershell_function(_read(SCENARIO_MODULE), "Dismiss-W4ProcessModal")
    )
    runtime_capture = helper.index(
        "$dismissedModalRuntimeId = @($modal.GetRuntimeId())"
    )
    runtime_nonempty = helper.index(
        "$dismissedModalRuntimeId.Count -eq 0", runtime_capture
    )
    invoke = helper.index("InvokePattern]$invokePattern).Invoke()", runtime_nonempty)
    post_invoke_loop = helper.index("do {", invoke)
    fresh_windows = helper.index(
        "$remainingWindows = $desktop.FindAll( "
        "[System.Windows.Automation.TreeScope]::Descendants, "
        "$processWindowCondition )",
        post_invoke_loop,
    )
    main_runtime_compare = helper.index(
        "[int]$remainingRuntimeId[$runtimeIndex] -ne "
        "[int]$mainWindowRuntimeId[$runtimeIndex]",
        fresh_windows,
    )
    non_main_append = helper.index(
        "$remainingNonMain += [pscustomobject]@{", main_runtime_compare
    )
    exact_zero = helper.index("$remainingNonMain.Count -eq 0", non_main_append)
    success = helper.index("return $true", exact_zero)
    ambiguity = helper.index("$remainingNonMain.Count -gt 1", success)
    dismissed_runtime_compare = helper.index(
        "$remainingId.Count -eq $dismissedModalRuntimeId.Count", ambiguity
    )
    replacement = helper.index("if (-not $sameDismissedModal)", dismissed_runtime_compare)
    bounded_poll = helper.index("Start-Sleep -Milliseconds 50", replacement)
    original_deadline = helper.index(
        "} while ([DateTime]::UtcNow -lt $deadline)", bounded_poll
    )
    remained = helper.index(
        "Expected process modal remained after InvokePattern", original_deadline
    )

    assert (
        runtime_capture
        < runtime_nonempty
        < invoke
        < post_invoke_loop
        < fresh_windows
        < main_runtime_compare
        < non_main_append
        < exact_zero
        < success
        < ambiguity
        < dismissed_runtime_compare
        < replacement
        < bounded_poll
        < original_deadline
        < remained
    )
    assert "Expected process modal RuntimeId is empty" in helper
    assert "Modal dismissal left ambiguous non-main process windows" in helper
    assert "Modal dismissal produced a replacement process window" in helper
    assert "Expected process modal remained after InvokePattern" in helper
    assert helper.count("$deadline = [DateTime]::UtcNow.AddSeconds") == 1


def test_required_process_modal_rejects_empty_expected_title() -> None:
    command = (
        "$ErrorActionPreference='Stop';"
        f"Import-Module '{_ps_single_quoted(DRIVER_MODULE)}' -Force;"
        f"$module=Import-Module '{_ps_single_quoted(SCENARIO_MODULE)}' "
        "-Force -PassThru;"
        "Add-Type -AssemblyName UIAutomationClient -ErrorAction Stop;"
        "Add-Type -AssemblyName UIAutomationTypes -ErrorAction Stop;"
        "& $module {Dismiss-W4ProcessModal -ProcessId $PID "
        "-MainWindow ([System.Windows.Automation.AutomationElement]::RootElement) "
        "-ExpectedTitle '   ' -TimeoutSeconds 1}"
    )

    result = _run_powershell(["-Command", command], cwd=REPOSITORY_ROOT)

    assert result.returncode != 0
    assert "Expected modal title must be non-empty" in result.stderr


@pytest.mark.parametrize(
    "warning",
    [
        (
            "核心归档已完成，但本次结果处于降级状态。"
            "辅助索引需要修复（修复动作: rebuild_vectors_for_entry）。"
            "部分可选工作流步骤未完成（问题代码: workflow_step_failed）。"
            "请勿盲目重试归档。"
        ),
        (
            "核心归档已完成，但本次结果处于降级状态。"
            "辅助索引需要修复（修复动作: rebuild_vectors_for_entry）。"
            "部分可选工作流步骤未完成（问题代码: storage_vector_failed, "
            "workflow_step_failed）。"
            "请勿盲目重试归档。"
        ),
        (
            "核心归档已完成，但本次结果处于降级状态。"
            "部分可选工作流步骤未完成（问题代码: workflow_step_failed, "
            "storage_vector_failed）。"
            "请勿盲目重试归档。"
        ),
    ],
    ids=["missing-second-code", "wrong-code-order", "missing-repair-action"],
)
def test_archive_warning_projection_rejects_missing_or_wrong_ordered_oracle(
    warning: str,
) -> None:
    expected = (
        "核心归档已完成，但本次结果处于降级状态。"
        "辅助索引需要修复（修复动作: rebuild_vectors_for_entry）。"
        "部分可选工作流步骤未完成（问题代码: workflow_step_failed, "
        "storage_vector_failed）。"
        "请勿盲目重试归档。"
    )
    compact = _compact_powershell(
        _powershell_function(
            _read(SCENARIO_MODULE), "Invoke-W4OfflineTextArchiveScenario"
        )
    )

    assert warning != expected
    assert (
        "-not $warning.Equals($expectedWarning, "
        "[System.StringComparison]::Ordinal)" in compact
    )
    assert "induced_workflow_issue_codes" in compact
    assert "expected_storage_repair_actions" in compact
    assert "workflow_issue_codes" in compact
    assert "storage_repair_actions" in compact


def test_chat_stop_is_resolved_only_after_the_stop_request_is_running() -> None:
    block = _powershell_function(_read(SCENARIO_MODULE), "Invoke-W4ChatLoopbackScenario")
    compact = _compact_powershell(block)
    initial_contract = re.search(
        r"Assert-W4UiaContractSegment\b.*?"
        r"-EvidenceName 'uia-contract-chat\.json'",
        compact,
    )

    assert initial_contract is not None
    assert "'chat_stop'" not in initial_contract.group(0)
    stop_prompt = compact.index("Set-W4UiaValue -Element $input -Value $stopPrompt")
    stop_send = compact.index("Invoke-W4UiaElement -Element $send", stop_prompt)
    running = compact.index(
        "Wait-W4UiaText -Element $status -Expected @('请求中')", stop_send
    )
    active_contract = compact.index(
        "Assert-W4UiaContractSegment -Gui $gui -AutomationIds @('chat_stop')",
        running,
    )
    active_evidence = compact.index(
        "-EvidenceName 'uia-contract-chat-stop-active.json'", active_contract
    )
    stop_lookup = compact.index(
        "Get-W4UiaElementById -Root $gui.Window -AutomationId 'chat_stop'",
        active_evidence,
    )
    stop_invoke = compact.index("Invoke-W4UiaElement -Element $stop", stop_lookup)

    assert (
        stop_prompt
        < stop_send
        < running
        < active_contract
        < active_evidence
        < stop_lookup
        < stop_invoke
    )
    assert "Get-W4UiaElementById" in compact[stop_lookup:stop_invoke]
    assert compact[:stop_prompt].count("-AutomationId 'chat_stop'") == 0


def test_bm25_success_uia_segments_exclude_hidden_status_placeholders() -> None:
    block = _powershell_function(_read(SCENARIO_MODULE), "Invoke-W4Bm25SearchScenario")
    browser_ids = _uia_contract_segment_ids(block, "uia-contract-browser.json")
    search_ids = _uia_contract_segment_ids(block, "uia-contract-search.json")

    assert browser_ids == {
        "browser_view",
        "browser_entry_count",
        "browser_entry_table",
        "browser_preview_title",
        "browser_preview_text",
    }
    assert browser_ids.isdisjoint(
        {"browser_entry_status", "browser_preview_status"}
    )
    assert search_ids == {
        "search_view",
        "search_input",
        "search_strategy",
        "search_submit",
        "search_result_status",
        "search_result_table",
        "search_preview_title",
        "search_preview_text",
    }
    assert "search_preview_status" not in search_ids

    registry = json.loads(_read(SCENARIO_CONTRACT))["uia"][
        "required_automation_ids"
    ]
    for hidden_id in (
        "browser_entry_status",
        "browser_preview_status",
        "search_preview_status",
    ):
        assert registry.count(hidden_id) == 1


def test_bm25_visible_segments_keep_all_search_terminal_oracles() -> None:
    block = _powershell_function(_read(SCENARIO_MODULE), "Invoke-W4Bm25SearchScenario")
    compact = _compact_powershell(block)
    status_lookup = compact.index(
        "Get-W4UiaElementById -Root $gui.Window -AutomationId "
        "'search_result_status'"
    )
    hit = compact.index(
        "$hitStatus = Wait-W4UiaTextContains -Element $status "
        "-Text '找到 1 条结果'",
        status_lookup,
    )
    no_hit = compact.index(
        "$noHitStatus = Wait-W4UiaTextContains -Element $status "
        "-Text '未找到匹配结果'",
        hit,
    )
    invalid = compact.index(
        "$invalidStatus = Wait-W4UiaTextContains -Element $status "
        "-Text '查询无效'",
        no_hit,
    )
    backend_fault = compact.index(
        "$errorStatus = Invoke-W4WithFilePathBlockedByDirectory", invalid
    )
    backend_error = compact.index(
        "$observed = Wait-W4UiaTextContains -Element $status "
        "-Text '搜索失败'",
        backend_fault,
    )

    assert status_lookup < hit < no_hit < invalid < backend_fault < backend_error
    assert "$observed -match '未找到'" in compact
    assert "hit = $hitStatus" in compact
    assert "no_hits = $noHitStatus" in compact
    assert "invalid = $invalidStatus" in compact
    assert "backend_error = $errorStatus" in compact


def test_uia_registry_is_the_exact_observed_runtime_segment_union() -> None:
    segments = _uia_contract_segments(_read(SCENARIO_MODULE))
    registry = json.loads(_read(SCENARIO_CONTRACT))["uia"][
        "required_automation_ids"
    ]
    observed: set[str] = set()
    for automation_ids in segments.values():
        observed.update(automation_ids)

    assert len(registry) == len(set(registry))
    assert observed == set(registry)

    conditional_segments = {
        "browser_entry_status": "uia-contract-browser-entry-error.json",
        "browser_preview_status": "uia-contract-browser-preview-degraded.json",
        "search_preview_status": "uia-contract-search-preview-degraded.json",
        "archive_progress_status": "uia-contract-archive-running.json",
    }
    expected_segment_ids = {
        "uia-contract-browser-entry-error.json": {
            "browser_view",
            "browser_entry_status",
        },
        "uia-contract-browser-preview-degraded.json": {
            "browser_view",
            "browser_preview_status",
        },
        "uia-contract-search-preview-degraded.json": {
            "search_view",
            "search_preview_status",
        },
        "uia-contract-archive-running.json": {
            "archive_view",
            "archive_progress_status",
        },
    }
    for automation_id, expected_evidence in conditional_segments.items():
        containing_segments = [
            evidence_name
            for evidence_name, ids in segments.items()
            if automation_id in ids
        ]
        assert containing_segments == [expected_evidence]
        assert segments[expected_evidence] == expected_segment_ids[expected_evidence]


def test_uia_success_terminals_emit_exact_zero_absence_proofs() -> None:
    source = _read(SCENARIO_MODULE)
    helper = _compact_powershell(
        _powershell_function(source, "Assert-W4UiaAutomationIdsAbsent")
    )
    assert "HashSet[string]]::new( [System.StringComparer]::Ordinal )" in helper
    assert "[string]::IsNullOrWhiteSpace($automationId)" in helper
    assert "-not $seen.Add($automationId)" in helper
    assert "AutomationElement]::AutomationIdProperty" in helper
    assert (
        "$Gui.Window.FindAll( [System.Windows.Automation.TreeScope]::Descendants, "
        "$condition )" in helper
    )
    assert "$elements.Count -ne 0" in helper
    assert "schema_version = 'pkv.w4.uia-absence-proof.v1'" in helper
    assert "process_id = [int]$Gui.Process.Id" in helper
    assert "automation_ids = @($AutomationIds)" in helper
    assert "exact_zero = $true" in helper

    proofs = _uia_absence_proofs(source)
    assert proofs == {
        "uia-absence-archive-terminal.json": {"archive_progress_status"},
        "uia-absence-browser-success.json": {
            "browser_entry_status",
            "browser_preview_status",
        },
        "uia-absence-search-success.json": {"search_preview_status"},
    }

    archive = _compact_powershell(
        _powershell_function(source, "Invoke-W4OfflineTextArchiveScenario")
    )
    archive_result = archive.index(
        "Wait-W4UiaTextContains -Element $resultTitle -Text '归档成功（降级）'"
    )
    archive_absence = archive.index("'uia-absence-archive-terminal.json'", archive_result)
    archive_contract = archive.index("'uia-contract-archive-result.json'", archive_absence)
    assert archive_result < archive_absence < archive_contract

    bm25 = _compact_powershell(
        _powershell_function(source, "Invoke-W4Bm25SearchScenario")
    )
    browser_absence = bm25.index("'uia-absence-browser-success.json'")
    browser_contract = bm25.index("'uia-contract-browser.json'", browser_absence)
    search_absence = bm25.index(
        "'uia-absence-search-success.json'", browser_contract
    )
    search_contract = bm25.index("'uia-contract-search.json'", search_absence)
    assert browser_absence < browser_contract < search_absence < search_contract


def test_bm25_conditional_uia_faults_restore_exact_synthetic_inputs() -> None:
    block = _powershell_function(_read(SCENARIO_MODULE), "Invoke-W4Bm25SearchScenario")
    compact = _compact_powershell(block)

    assert "Assert-W4SafePathChain -Path $vaultRoot" in compact
    assert (
        "Get-ChildItem -LiteralPath $vaultRoot -File -Recurse -Force "
        "-ErrorAction Stop" in compact
    )
    assert "Where-Object { [string]$_.Extension -ceq '.md' }" in compact
    assert "$previewFiles.Count -ne 1" in compact
    assert "Test-W4PathContainedBy -Candidate $previewFile -Root $vaultRoot" in compact
    assert "Assert-W4SafePathChain -Path $previewFile" in compact
    assert "Test-Path -LiteralPath $previewBackup" in compact

    entry_search = compact.index("Select-W4NavigationItem -Gui $gui -Name '搜索'")
    entry_fault = compact.index(
        "$browserEntryError = Invoke-W4WithFilePathBlockedByDirectory",
        entry_search,
    )
    assert (
        "-FilePath $database -BackupPath $entryFailureBackup "
        "-Label 'BM25 Browser entry-status fault' -Action {"
        in compact[entry_fault:]
    )
    entry_browser = compact.index(
        "Select-W4NavigationItem -Gui $gui -Name '浏览'", entry_fault
    )
    entry_error = compact.index(
        "-Expected @('条目加载失败（错误代码：browser_entry_count_failed）')",
        entry_browser,
    )
    entry_segment = compact.index("'uia-contract-browser-entry-error.json'", entry_error)
    browser_fault = compact.index(
        "$browserPreviewDegraded = Invoke-W4WithTemporarilyMissingFile",
        entry_segment,
    )
    assert (
        "-FilePath $previewFile -BackupPath $previewBackup "
        "-Label 'BM25 Browser preview degradation' -Action {"
        in compact[browser_fault:]
    )
    browser_error = compact.index(
        "-Expected @('预览已降级：正在显示安全摘要（错误代码：resource_missing）')",
        browser_fault,
    )
    browser_segment = compact.index(
        "'uia-contract-browser-preview-degraded.json'", browser_error
    )
    browser_success = compact.index("'uia-contract-browser.json'", browser_segment)
    browser_refresh = compact[browser_segment:browser_success]
    assert "Select-W4NavigationItem -Gui $gui -Name '搜索'" in browser_refresh
    assert "Select-W4NavigationItem -Gui $gui -Name '浏览'" in browser_refresh
    assert "'uia-absence-browser-success.json'" in browser_refresh

    search_fault = compact.index(
        "$searchPreviewDegraded = Invoke-W4WithTemporarilyMissingFile",
        browser_success,
    )
    search_error = compact.index(
        "-Expected @('预览降级：Markdown 正文不可用，以下显示安全摘要"
        "（问题代码：resource_missing）')",
        search_fault,
    )
    search_segment = compact.index(
        "'uia-contract-search-preview-degraded.json'", search_error
    )
    search_success = compact.index("'uia-contract-search.json'", search_segment)
    assert "Invoke-W4UiaElement -Element $submit" in compact[
        search_segment:search_success
    ]
    assert "'uia-absence-search-success.json'" in compact[
        search_segment:search_success
    ]

    backend_fault = compact.index(
        "$errorStatus = Invoke-W4WithFilePathBlockedByDirectory",
        search_success,
    )
    backend_oracle = compact.index(
        "$observed = Wait-W4UiaTextContains -Element $status -Text '搜索失败'",
        backend_fault,
    )
    assert (
        entry_search
        < entry_fault
        < entry_browser
        < entry_error
        < entry_segment
        < browser_fault
        < browser_error
        < browser_segment
        < browser_success
        < search_fault
        < search_error
        < search_segment
        < search_success
        < backend_fault
        < backend_oracle
    )

    assert "Start-Sleep" not in block
    assert "Invoke-W4SqliteStatement" not in block
    assert re.search(r"ReadAll(?:Text|Bytes)\([^)]*\$database", compact) is None
    assert re.search(r"(?i)\b(?:sendkeys|keyboard|ocr|tesseract)\b", block) is None


def test_bm25_browser_recovery_crosses_zero_selection_before_fresh_preview() -> None:
    block = _powershell_function(_read(SCENARIO_MODULE), "Invoke-W4Bm25SearchScenario")
    compact = _compact_powershell(block)
    degraded = compact.index("'uia-contract-browser-preview-degraded.json'")
    search_navigation = compact.index(
        "Select-W4NavigationItem -Gui $gui -Name '搜索'", degraded
    )
    browser_navigation = compact.index(
        "Select-W4NavigationItem -Gui $gui -Name '浏览'", search_navigation
    )
    zero_selection = compact.index(
        "Wait-W4UiaSelectionCount -Root $gui.Window "
        "-AutomationId 'browser_entry_table' -ExpectedCount 0 -TimeoutSeconds 30",
        browser_navigation,
    )
    absence = compact.index("'uia-absence-browser-success.json'", zero_selection)
    fresh_table = compact.index(
        "$browserTable = Get-W4UiaElementById -Root $gui.Window "
        "-AutomationId 'browser_entry_table'",
        absence,
    )
    fresh_select = compact.index(
        "Select-W4FirstDataItem -Root $browserTable", fresh_table
    )
    fresh_body = compact.index(
        "Get-W4UiaElementById -Root $gui.Window "
        "-AutomationId 'browser_preview_text'",
        fresh_select,
    )
    body_oracle = compact.index(
        "-Text 'artifact-e2e-orchid' -TimeoutSeconds 30", fresh_body
    )
    success = compact.index("'uia-contract-browser.json'", body_oracle)

    assert (
        degraded
        < search_navigation
        < browser_navigation
        < zero_selection
        < absence
        < fresh_table
        < fresh_select
        < fresh_body
        < body_oracle
        < success
    )
    assert "Select-W4FirstDataItem" not in compact[degraded:zero_selection]
    assert compact[zero_selection:success].count(
        "Get-W4UiaElementById -Root $gui.Window "
        "-AutomationId 'browser_entry_table'"
    ) == 1


def test_selection_count_wait_freshly_resolves_exact_selection_container() -> None:
    helper = _compact_powershell(
        _powershell_function(_read(SCENARIO_MODULE), "Wait-W4UiaSelectionCount")
    )
    loop_start = helper.index("do {")
    fresh_find = helper.index(
        "$elements = $Root.FindAll("
        "[System.Windows.Automation.TreeScope]::Descendants, $condition)",
        loop_start,
    )
    duplicate_guard = helper.index("$elements.Count -gt 1", fresh_find)
    exact_one = helper.index("$elements.Count -eq 1", duplicate_guard)
    selection_pattern = helper.index(
        "[System.Windows.Automation.SelectionPattern]::Pattern", exact_one
    )
    current_selection = helper.index(".Current.GetSelection()", selection_pattern)
    expected = helper.index("$actualCount -eq $ExpectedCount", current_selection)
    fresh_return = helper.index("return $elements.Item(0)", expected)
    loop_end = helper.index("} while ([DateTime]::UtcNow -lt $deadline)", fresh_return)

    assert "AutomationElement]::AutomationIdProperty" in helper
    assert "[ValidateRange(0, 100)][int]$ExpectedCount" in helper
    assert (
        loop_start
        < fresh_find
        < duplicate_guard
        < exact_one
        < selection_pattern
        < current_selection
        < expected
        < fresh_return
        < loop_end
    )
    assert "does not expose SelectionPattern" in helper
    assert "$deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)" in helper
    assert "Start-Sleep -Milliseconds 50" in helper
    assert "selection count did not reach expected value" in helper
    assert ".GetCurrentSelection()" not in helper


def test_bm25_search_recovery_crosses_distinct_terminal_before_target_rerun() -> None:
    block = _powershell_function(_read(SCENARIO_MODULE), "Invoke-W4Bm25SearchScenario")
    compact = _compact_powershell(block)
    degraded = compact.index("'uia-contract-search-preview-degraded.json'")
    distinct_value = compact.index(
        "Set-W4UiaValue -Element $searchInput -Value $noHitToken",
        degraded,
    )
    distinct_invoke = compact.index(
        "Invoke-W4UiaElement -Element $submit", distinct_value
    )
    distinct_terminal = compact.index(
        "Wait-W4UiaTextContains -Element $status "
        "-Text '未找到匹配结果' -TimeoutSeconds 30",
        distinct_invoke,
    )
    zero_selection = compact.index(
        "Wait-W4UiaSelectionCount -Root $gui.Window "
        "-AutomationId 'search_result_table' -ExpectedCount 0 -TimeoutSeconds 10",
        distinct_terminal,
    )
    target_value = compact.index(
        "Set-W4UiaValue -Element $searchInput -Value 'artifact-e2e-orchid'",
        zero_selection,
    )
    target_invoke = compact.index(
        "Invoke-W4UiaElement -Element $submit", target_value
    )
    found_terminal = compact.index(
        "$hitStatus = Wait-W4UiaTextContains -Element $status "
        "-Text '找到 1 条结果' -TimeoutSeconds 30",
        target_invoke,
    )
    fresh_table = compact.index(
        "$resultTable = Get-W4UiaElementById -Root $gui.Window "
        "-AutomationId 'search_result_table'",
        found_terminal,
    )
    fresh_select = compact.index(
        "Select-W4FirstDataItem -Root $resultTable", fresh_table
    )
    fresh_body = compact.index(
        "Get-W4UiaElementById -Root $gui.Window "
        "-AutomationId 'search_preview_text'",
        fresh_select,
    )
    body_oracle = compact.index(
        "-Text 'artifact-e2e-orchid' -TimeoutSeconds 30", fresh_body
    )
    absence = compact.index("'uia-absence-search-success.json'", body_oracle)
    success = compact.index("'uia-contract-search.json'", absence)

    assert (
        degraded
        < distinct_value
        < distinct_invoke
        < distinct_terminal
        < zero_selection
        < target_value
        < target_invoke
        < found_terminal
        < fresh_table
        < fresh_select
        < fresh_body
        < body_oracle
        < absence
        < success
    )
    recovery = compact[degraded:success]
    assert recovery.count("Invoke-W4UiaElement -Element $submit") == 2
    assert recovery.count(
        "Set-W4UiaValue -Element $searchInput -Value $noHitToken"
    ) == 1
    assert recovery.count(
        "Set-W4UiaValue -Element $searchInput -Value 'artifact-e2e-orchid'"
    ) == 1
    assert "$hitStatus = Wait-W4UiaTextContains" not in compact[degraded:zero_selection]


def test_bm25_no_hit_oracle_uses_one_unsplit_term_twice_with_zero_barrier() -> None:
    block = _powershell_function(_read(SCENARIO_MODULE), "Invoke-W4Bm25SearchScenario")
    compact = _compact_powershell(block)
    assignment = re.search(r"\$noHitToken\s*=\s*'([^']+)'", block)

    assert assignment is not None
    token = assignment.group(1)
    assert re.fullmatch(r"[a-z0-9]+", token) is not None
    assert "w4" not in token
    assert "-" not in token
    assert "_" not in token
    assert len(token) >= 24
    assert block.count(f"'{token}'") == 1
    assert compact.count(
        "Set-W4UiaValue -Element $searchInput -Value $noHitToken"
    ) == 2
    assert "$noHitToken -notmatch '^[a-z0-9]+$'" in compact
    assert "w4-no-hit-5f37c22a" not in block

    first_value = compact.index(
        "Set-W4UiaValue -Element $searchInput -Value $noHitToken"
    )
    first_invoke = compact.index(
        "Invoke-W4UiaElement -Element $submit", first_value
    )
    first_terminal = compact.index(
        "Wait-W4UiaTextContains -Element $status "
        "-Text '未找到匹配结果' -TimeoutSeconds 30",
        first_invoke,
    )
    selection_zero = compact.index(
        "Wait-W4UiaSelectionCount -Root $gui.Window "
        "-AutomationId 'search_result_table' -ExpectedCount 0 -TimeoutSeconds 10",
        first_terminal,
    )
    target_value = compact.index(
        "Set-W4UiaValue -Element $searchInput -Value 'artifact-e2e-orchid'",
        selection_zero,
    )
    second_value = compact.index(
        "Set-W4UiaValue -Element $searchInput -Value $noHitToken",
        target_value,
    )
    second_terminal = compact.index(
        "$noHitStatus = Wait-W4UiaTextContains -Element $status "
        "-Text '未找到匹配结果' -TimeoutSeconds 30",
        second_value,
    )
    assert (
        first_value
        < first_invoke
        < first_terminal
        < selection_zero
        < target_value
        < second_value
        < second_terminal
    )


def test_bm25_scenario_forbids_powershell_automatic_input_variable() -> None:
    block = _powershell_function(_read(SCENARIO_MODULE), "Invoke-W4Bm25SearchScenario")
    compact = _compact_powershell(block)
    result = _run_bm25_search_input_scope_probe()

    assert result.returncode == 0, result.stderr or result.stdout
    probe = json.loads(result.stdout)
    # AST semantic analysis ignores explanatory comments and literals. Every
    # executable VariableExpressionAst in this scenario must avoid PowerShell's
    # automatic pipeline enumerator.
    assert "input" not in probe["scenario_variables"]
    assert "searchinput" in probe["scenario_variables"]
    assert (
        "$searchInput = Get-W4UiaElementById -Root $gui.Window "
        "-AutomationId 'search_input'"
    ) in compact
    assert compact.count("Set-W4UiaValue -Element $searchInput -Value") == 6


def test_bm25_backend_fault_action_closes_over_search_input_not_automatic_input() -> None:
    result = _run_bm25_search_input_scope_probe()

    assert result.returncode == 0, result.stderr or result.stdout
    probe = json.loads(result.stdout)
    assert probe["input_is_enumerator"] is True
    assert probe["input_is_outer"] is False
    assert "enumerator" in probe["input_type"].lower()
    assert "searchinput" in probe["action_variables"]
    assert "input" not in probe["action_variables"]


def test_bm25_mutation_helpers_wrap_mutation_and_restore_in_finally() -> None:
    source = _read(SCENARIO_MODULE)
    missing = _compact_powershell(
        _powershell_function(source, "Invoke-W4WithTemporarilyMissingFile")
    )
    blocked = _compact_powershell(
        _powershell_function(source, "Invoke-W4WithFilePathBlockedByDirectory")
    )

    for helper in (missing, blocked):
        assert "Assert-W4SafePathChain -Path $source" in helper
        assert "Assert-W4SafePathChain -Path $backup" in helper
        assert "Test-Path -LiteralPath $source -PathType Leaf" in helper
        assert "Test-Path -LiteralPath $backup" in helper
        assert "Test-Path -LiteralPath $unexpected" in helper
        assert "$expectedSha256 = Get-W4FileSha256 -Path $source" in helper
        mutation_try = helper.index("try {")
        move = helper.index("[System.IO.File]::Move($source, $backup)")
        action = helper.index("return (& $Action)", move)
        finally_block = helper.index("} finally {", action)
        restore = helper.index("[System.IO.File]::Move($backup, $source)", finally_block)
        hash_check = helper.index(
            "(Get-W4FileSha256 -Path $source) -cne $expectedSha256", restore
        )
        assert mutation_try < move < action < finally_block < restore < hash_check

    missing_quarantine = missing.index(
        "[System.IO.File]::Move($source, $unexpected)"
    )
    missing_restore = missing.index("[System.IO.File]::Move($backup, $source)")
    missing_fail = missing.index(
        "produced an unexpected replacement while the original was gated"
    )
    assert missing_quarantine < missing_restore < missing_fail

    blocked_try = blocked.index("try {")
    blocked_move = blocked.index(
        "[System.IO.File]::Move($source, $backup)", blocked_try
    )
    blocked_create = blocked.index(
        "[void][System.IO.Directory]::CreateDirectory($source)", blocked_move
    )
    blocked_action = blocked.index("return (& $Action)", blocked_create)
    blocked_finally = blocked.index("} finally {", blocked_action)
    empty_delete = blocked.index(
        "[System.IO.Directory]::Delete($source, $false)", blocked_finally
    )
    content_quarantine = blocked.index(
        "[System.IO.Directory]::Move($source, $unexpected)", blocked_finally
    )
    blocked_restore = blocked.index(
        "[System.IO.File]::Move($backup, $source)", blocked_finally
    )
    blocked_fail = blocked.index(
        "produced unexpected content inside the blocked database path",
        blocked_restore,
    )
    assert (
        blocked_try
        < blocked_move
        < blocked_create
        < blocked_action
        < blocked_finally
        < empty_delete
        < content_quarantine
        < blocked_restore
        < blocked_fail
    )


@pytest.mark.parametrize(
    ("helper", "mode", "expected_error", "unexpected_is_directory"),
    [
        ("Invoke-W4WithTemporarilyMissingFile", "success", None, False),
        (
            "Invoke-W4WithTemporarilyMissingFile",
            "action_throw",
            "action-sentinel",
            False,
        ),
        (
            "Invoke-W4WithTemporarilyMissingFile",
            "replacement",
            "unexpected replacement",
            False,
        ),
        ("Invoke-W4WithFilePathBlockedByDirectory", "success", None, False),
        (
            "Invoke-W4WithFilePathBlockedByDirectory",
            "action_throw",
            "action-sentinel",
            False,
        ),
        (
            "Invoke-W4WithFilePathBlockedByDirectory",
            "replacement",
            "unexpected content",
            True,
        ),
    ],
)
def test_bm25_mutation_helpers_restore_after_success_throw_and_replacement(
    tmp_path: Path,
    helper: str,
    mode: str,
    expected_error: str | None,
    unexpected_is_directory: bool,
) -> None:
    result = _run_w4_mutation_helper(tmp_path, helper=helper, mode=mode)

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["source_text"] == "original-synthetic-bytes"
    assert payload["backup_exists"] is False
    if expected_error is None:
        assert payload["action_result"] == "success"
        assert payload["caught"] is None
        assert payload["unexpected_exists"] is False
    else:
        assert expected_error in payload["caught"]
        assert payload["unexpected_exists"] is (mode == "replacement")
    assert payload["unexpected_is_directory"] is unexpected_is_directory


def test_archive_tabs_use_exact_child_selection_not_container_selection() -> None:
    source = _read(SCENARIO_MODULE)
    segment_block = _powershell_function(source, "Assert-W4UiaContractSegment")
    selection_match = re.search(
        r"\$selectionIds = @\((.*?)\) \$expandCollapseIds",
        _compact_powershell(segment_block),
    )
    assert selection_match is not None
    assert _extract_single_quoted_values(selection_match.group(1)) == {
        "nav_list",
        "browser_entry_table",
        "search_result_table",
        "session_list",
    }

    offline_block = _powershell_function(source, "Invoke-W4OfflineTextArchiveScenario")
    offline_compact = _compact_powershell(offline_block)
    assert _uia_contract_segment_ids(
        offline_block, "uia-contract-archive-url-default.json"
    ) == {
        "archive_view",
        "archive_tabs",
        "archive_url_input",
        "archive_url_submit",
    }
    assert _uia_contract_segment_ids(
        offline_block, "uia-contract-archive-text-active.json"
    ) == {
        "archive_view",
        "archive_tabs",
        "archive_text_title",
        "archive_text_content",
        "archive_text_submit",
    }
    assert _uia_contract_segment_ids(
        offline_block, "uia-contract-archive-running.json"
    ) == {
        "archive_view",
        "archive_progress_status",
    }
    assert _uia_contract_segment_ids(
        offline_block, "uia-contract-archive-result.json"
    ) == {
        "archive_view",
        "archive_tabs",
        "archive_result_title",
        "archive_result_id",
        "archive_result_path",
        "archive_result_warning",
        "archive_go_browser",
    }

    registry = json.loads(_read(SCENARIO_CONTRACT))["uia"][
        "required_automation_ids"
    ]
    for conditional_id in (
        "archive_url_input",
        "archive_url_submit",
        "archive_progress_status",
    ):
        assert registry.count(conditional_id) == 1

    navigation = offline_compact.index("Select-W4NavigationItem -Gui $gui -Name '归档'")
    url_default = offline_compact.index("'uia-contract-archive-url-default.json'")
    tabs_lookup = offline_compact.index(
        "$tabs = Get-W4UiaElementById -Root $gui.Window "
        "-AutomationId 'archive_tabs'",
        url_default,
    )
    tab_selection = offline_compact.index(
        "Select-W4UiaItemByName -Root $tabs -Name '文本归档'", tabs_lookup
    )
    text_active = offline_compact.index(
        "'uia-contract-archive-text-active.json'", tab_selection
    )
    text_input = offline_compact.index(
        "Set-W4UiaValue -Element (Get-W4UiaElementById -Root $gui.Window "
        "-AutomationId 'archive_text_title')",
        text_active,
    )
    text_submit_lookup = offline_compact.index(
        "$textSubmit = Get-W4UiaElementById -Root $gui.Window "
        "-AutomationId 'archive_text_submit'",
        text_input,
    )
    accept_task = offline_compact.index(
        "$providerAcceptTask = $providerGate.AcceptTcpClientAsync()",
        text_submit_lookup,
    )
    archive_submit = offline_compact.index(
        "Invoke-W4UiaElement -Element $textSubmit", accept_task
    )
    running_contract = offline_compact.index(
        "'uia-contract-archive-running.json'", archive_submit
    )
    result_wait = offline_compact.index(
        "Wait-W4UiaTextContains -Element $resultTitle", running_contract
    )
    result_contract = offline_compact.index(
        "'uia-contract-archive-result.json'", result_wait
    )
    assert (
        navigation
        < url_default
        < tabs_lookup
        < tab_selection
        < text_active
        < text_input
        < text_submit_lookup
        < accept_task
        < archive_submit
        < running_contract
        < result_wait
        < result_contract
    )
    assert re.search(
        r"(?i)\b(?:sendkeys|keyboard|ocr|tesseract)\b", offline_block
    ) is None
    assert (
        "$tabs = Get-W4UiaElementById -Root $gui.Window "
        "-AutomationId 'archive_tabs'" in offline_compact
    )
    assert (
        "Select-W4UiaItemByName -Root $tabs -Name '文本归档'" in offline_compact
    )

    selection_action = _compact_powershell(
        _powershell_function(_read(DRIVER_MODULE), "Select-W4UiaItemByName")
    )
    assert "AutomationElement]::NameProperty" in selection_action
    assert "$matches.Count -ne 1" in selection_action
    assert "SelectionItemPattern]::Pattern" in selection_action
    assert "SelectionItemPattern]$pattern).Select()" in selection_action

    assert "Wait-W4UiaTextContains -Element $resultTitle" in offline_compact
    assert (
        "-not $warning.Equals($expectedWarning, "
        "[System.StringComparison]::Ordinal)" in offline_compact
    )
    assert "$warning -notmatch 'provider'" not in offline_compact
    assert "Test-W4PathContainedBy -Candidate $savedPath" in offline_compact
    assert "saved_path_sha256 = Get-W4FileSha256 -Path $savedPath" in offline_compact
    assert "restart_opened_saved_entry = $true" in offline_compact


def test_native_tcp_owner_inspector_uses_bounded_ipv4_owner_table_contract() -> None:
    initializer = _compact_powershell(
        _powershell_function(
            _read(DRIVER_MODULE), "Initialize-W4FileIdentityInspector"
        )
    )

    assert "$tcpConnectionType = 'PkvW4.TcpConnectionInspector' -as [type]" in initializer
    assert (
        "$fileIdentityType -and $processTreeType -and $reparsePointType "
        "-and $tcpConnectionType" in initializer
    )
    assert (
        "$fileIdentityType -or $processTreeType -or $reparsePointType "
        "-or $tcpConnectionType" in initializer
    )
    assert "partial or stale PkvW4 native-inspector type set" in initializer
    assert "public static class TcpConnectionInspector" in initializer
    assert "private const int AF_INET = 2;" in initializer
    assert "private const int TCP_TABLE_OWNER_PID_CONNECTIONS = 4;" in initializer
    assert "private const uint MIB_TCP_STATE_ESTAB = 5;" in initializer
    assert "[StructLayout(LayoutKind.Sequential, Pack = 4)]" in initializer
    assert initializer.count("result = GetExtendedTcpTable(") == 2
    assert "IntPtr.Zero, ref size, true, AF_INET, " in initializer
    assert "buffer, ref size, true, AF_INET, " in initializer
    assert "TCP_TABLE_OWNER_PID_CONNECTIONS, 0" in initializer
    assert "private static extern uint ntohl(uint networkLong);" in initializer
    assert "private static extern ushort ntohs(ushort networkShort);" in initializer
    assert "uint hostAddress = ntohl(networkAddress);" in initializer
    assert "return (int)ntohs((ushort)(networkPort & 0xffff));" in initializer
    assert "size < sizeof(uint)" in initializer
    assert "long requiredSize = checked(4L + checked((long)count * rowSize));" in initializer
    assert "count < 0 || requiredSize > size" in initializer
    assert "row.State == MIB_TCP_STATE_ESTAB" in initializer
    assert "Marshal.FreeHGlobal(buffer);" in initializer
    assert "AddressFamily.InterNetwork" in initializer
    assert "localPort < 1 || localPort > 65535" in initializer
    assert "remotePort < 1 || remotePort > 65535" in initializer


def test_tcp_owner_helper_binds_exact_tuple_and_fresh_root_identity() -> None:
    helper = _compact_powershell(
        _powershell_function(_read(DRIVER_MODULE), "Assert-W4TcpClientOwnedByProcess")
    )
    snapshot = helper.index("New-W4ProcessTreeIdentitySnapshot -Process $Process")
    initialize = helper.index("Initialize-W4FileIdentityInspector", snapshot)
    lookup = helper.index(
        "[PkvW4.TcpConnectionInspector]::FindEstablishedOwners( "
        "$peerEndpoint.Address.ToString(), $peerEndpoint.Port, "
        "$serverEndpoint.Address.ToString(), $serverEndpoint.Port )",
        initialize,
    )
    exact_row = helper.index("$ownerPids.Count -eq 1", lookup)
    exact_pid = helper.index("$ownerPid -ne $Process.Id", exact_row)
    live_root = helper.index(
        "Get-W4LiveProcessForIdentity -Identity $rootIdentity[0]", exact_pid
    )

    assert "$serverEndpoint = $Client.Client.LocalEndPoint" in helper
    assert "$peerEndpoint = $Client.Client.RemoteEndPoint" in helper
    assert "$serverEndpoint.Address.Equals($ExpectedServerEndpoint.Address)" in helper
    assert "$serverEndpoint.Port -ne $ExpectedServerEndpoint.Port" in helper
    assert "AddressFamily]::InterNetwork" in helper
    assert "$rootIdentity.Count -ne 1" in helper
    assert "$rootIdentity[0].ProcessId -ne $Process.Id" in helper
    assert (
        "$rootIdentity[0].StartTimeUtcTicks -ne "
        "[int64]$identitySnapshot.RootStartTimeUtcTicks" in helper
    )
    assert "$ownerPids.Count -gt 1" in helper
    assert "$ownerPids.Count -ne 1" in helper
    assert snapshot < initialize < lookup < exact_row < exact_pid < live_root
    assert "OwnerVerified = $true" in helper
    assert "OwnerProcessId = $ownerPid" in helper
    assert "OwnerStartTimeUtcTicks = [int64]$rootIdentity[0].StartTimeUtcTicks" in helper

    fixed_errors = set(re.findall(r"throw '([^']+)'", helper))
    assert fixed_errors == {
        "W4 TCP owner identity checks require Windows",
        "TCP owner root process exited before identity verification",
        "Accepted Provider connection endpoints do not match the exact IPv4 listener",
        "TCP owner root process identity was not exact",
        "Accepted Provider connection did not have one exact TCP owner row",
        "Accepted Provider connection TCP owner was not observable",
        "Accepted Provider connection owner was not the GUI process identity",
        "Accepted Provider connection owner changed process identity",
    }
    assert all("$" not in message for message in fixed_errors)


def test_tcp_owner_helper_accepts_self_client_and_rejects_different_live_root() -> None:
    result = _run_tcp_owner_contract_probe()

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["owner_verified"] is True
    assert payload["owner_pid"] == payload["self_pid"]
    assert payload["owner_start_ticks"] > 0
    assert payload["different_live_root_rejected"] is True
    assert payload["different_live_root_error"] == (
        "Accepted Provider connection owner was not the GUI process identity"
    )
    assert payload["zero_owner_rejected"] is True
    assert payload["zero_owner_error"] == (
        "Accepted Provider connection TCP owner was not observable"
    )


def test_archive_running_state_is_bound_to_a_loopback_provider_gate() -> None:
    source = _read(SCENARIO_MODULE)
    block = _powershell_function(source, "Invoke-W4OfflineTextArchiveScenario")
    compact = _compact_powershell(block)
    receiver = _compact_powershell(
        _powershell_function(source, "Receive-W4ExpectedProviderGateRequest")
    )

    contract_guard = compact.index(
        "Assert-W4ExactObjectFields -Object $ScenarioContext.Scenario"
    )
    expected_count = compact.index(
        "$expectedProviderRequests = "
        "[int]$ScenarioContext.Scenario.expected_provider_requests",
        contract_guard,
    )
    listener = compact.index(
        "[System.Net.Sockets.TcpListener]::new( "
        "[System.Net.IPAddress]::Parse('127.0.0.1'), 0 )"
    )
    listener_start = compact.index("$providerGate.Start(1)", listener)
    local_config = compact.index(
        'Write-W4ChatLocalConfig -ScenarioContext $ScenarioContext '
        '-BaseUrl "http://127.0.0.1:$providerGatePort/v1"',
        listener_start,
    )
    text_submit = compact.index(
        "$textSubmit = Get-W4UiaElementById -Root $gui.Window "
        "-AutomationId 'archive_text_submit'",
        local_config,
    )
    accept_task = compact.index(
        "$providerAcceptTask = $providerGate.AcceptTcpClientAsync()", text_submit
    )
    submit = compact.index("Invoke-W4UiaElement -Element $textSubmit", accept_task)
    receive_one = compact.index(
        "Receive-W4ExpectedProviderGateRequest "
        "-AcceptTask $providerAcceptTask -Listener $providerGate "
        "-Process $gui.Process -Ordinal 1 -Stage 'text_fallback_summarize'",
        submit,
    )
    bind_one = compact.index(
        "$providerGateClient = $providerRequest.Client", receive_one
    )
    evidence_one = compact.index(
        "$providerRequestEvidence.Add($providerRequest.Evidence)", bind_one
    )
    checkpoint_one = compact.index(
        "Write-W4ProviderGateCheckpoint -Path $providerCheckpointPath", evidence_one
    )
    progress_lookup = compact.index(
        "$progressStatus = Get-W4UiaElementById -Root $gui.Window "
        "-AutomationId 'archive_progress_status'",
        checkpoint_one,
    )
    progress_text = compact.index(
        "$progressText = Get-W4UiaText -Element $progressStatus", progress_lookup
    )
    progress_pid = compact.index(
        "[int]$progressStatus.Current.ProcessId -ne [int]$gui.Process.Id",
        progress_text,
    )
    progress_visible = compact.index(
        "[bool]$progressStatus.Current.IsOffscreen", progress_pid
    )
    progress_nonempty = compact.index(
        "[string]::IsNullOrWhiteSpace($progressText)", progress_visible
    )
    text_disabled = compact.index(
        "Wait-W4UiaElementEnabledState -Root $gui.Window "
        "-AutomationId 'archive_text_submit' -Expected $false -TimeoutSeconds 10",
        progress_nonempty,
    )
    url_disabled = compact.index(
        "Wait-W4UiaElementEnabledState -Root $gui.Window "
        "-AutomationId 'archive_url_submit' -Expected $false -TimeoutSeconds 10",
        text_disabled,
    )
    text_still_disabled = compact.index(
        "Wait-W4UiaElementEnabledState -Root $gui.Window "
        "-AutomationId 'archive_text_submit' -Expected $false -TimeoutSeconds 10",
        url_disabled,
    )
    running = compact.index("'uia-contract-archive-running.json'", text_still_disabled)
    accept_two = compact.index(
        "$providerAcceptTask = $providerGate.AcceptTcpClientAsync()", running
    )
    close_one = compact.index("$providerGateClient.Dispose()", accept_two)
    receive_two = compact.index(
        "Receive-W4ExpectedProviderGateRequest "
        "-AcceptTask $providerAcceptTask -Listener $providerGate "
        "-Process $gui.Process -Ordinal 2 -Stage 'workflow_summarize'",
        close_one,
    )
    bind_two = compact.index(
        "$providerGateClient = $providerRequest.Client", receive_two
    )
    evidence_two = compact.index(
        "$providerRequestEvidence.Add($providerRequest.Evidence)", bind_two
    )
    checkpoint_two = compact.index(
        "Write-W4ProviderGateCheckpoint -Path $providerCheckpointPath", evidence_two
    )
    accept_three = compact.index(
        "$providerAcceptTask = $providerGate.AcceptTcpClientAsync()", checkpoint_two
    )
    close_two = compact.index("$providerGateClient.Dispose()", accept_three)
    receive_three = compact.index(
        "Receive-W4ExpectedProviderGateRequest "
        "-AcceptTask $providerAcceptTask -Listener $providerGate "
        "-Process $gui.Process -Ordinal 3 -Stage 'workflow_extract_tags'",
        close_two,
    )
    final_stop_switch = compact.index("-StopListenerAfterAccept", receive_three)
    bind_three = compact.index(
        "$providerGateClient = $providerRequest.Client", final_stop_switch
    )
    evidence_three = compact.index(
        "$providerRequestEvidence.Add($providerRequest.Evidence)", bind_three
    )
    checkpoint_three = compact.index(
        "Write-W4ProviderGateCheckpoint -Path $providerCheckpointPath", evidence_three
    )
    exact_count_guard = compact.index(
        "$providerRequestEvidence.Count -ne $expectedProviderRequests",
        checkpoint_three,
    )
    close_three = compact.index("$providerGateClient.Dispose()", exact_count_guard)
    result_deadline = compact.index(
        "$resultDeadline = [DateTime]::UtcNow.AddSeconds(90)", close_three
    )
    lookup_timeout = compact.index(
        "-AutomationId 'archive_result_title' "
        "-TimeoutSeconds $resultLookupSeconds",
        result_deadline,
    )
    result = compact.index(
        "Wait-W4UiaTextContains -Element $resultTitle "
        "-Text '归档成功（降级）' -TimeoutSeconds $resultRemainingSeconds",
        lookup_timeout,
    )
    terminal_absence = compact.index(
        "'uia-absence-archive-terminal.json'", result
    )
    text_enabled = compact.index(
        "Wait-W4UiaElementEnabledState -Root $gui.Window "
        "-AutomationId 'archive_text_submit' -Expected $true -TimeoutSeconds 10",
        terminal_absence,
    )
    url_enabled = compact.index(
        "Wait-W4UiaElementEnabledState -Root $gui.Window "
        "-AutomationId 'archive_url_submit' -Expected $true -TimeoutSeconds 10",
        text_enabled,
    )
    text_still_enabled = compact.index(
        "Wait-W4UiaElementEnabledState -Root $gui.Window "
        "-AutomationId 'archive_text_submit' -Expected $true -TimeoutSeconds 10",
        url_enabled,
    )
    evidence = compact.index("'archive-provider-gate.json'", text_still_enabled)
    release_action = compact.index("release_action = 'close_without_response'", evidence)
    issue_evidence = compact.index(
        "induced_workflow_issue_codes =", release_action
    )
    repair_evidence = compact.index(
        "expected_storage_repair_actions =", issue_evidence
    )
    warning_read = compact.index("$warning = Get-W4UiaText", issue_evidence)
    expected_warning = compact.index(
        "$expectedWarning = @(",
        warning_read,
    )
    warning_oracle = compact.index(
        "-not $warning.Equals($expectedWarning, "
        "[System.StringComparison]::Ordinal)",
        expected_warning,
    )

    assert (
        contract_guard
        < expected_count
        < listener
        < listener_start
        < local_config
        < text_submit
        < accept_task
        < submit
        < receive_one
        < bind_one
        < evidence_one
        < checkpoint_one
        < progress_lookup
        < progress_text
        < progress_pid
        < progress_visible
        < progress_nonempty
        < text_disabled
        < url_disabled
        < text_still_disabled
        < running
        < accept_two
        < close_one
        < receive_two
        < bind_two
        < evidence_two
        < checkpoint_two
        < accept_three
        < close_two
        < receive_three
        < final_stop_switch
        < bind_three
        < evidence_three
        < checkpoint_three
        < exact_count_guard
        < close_three
        < result_deadline
        < lookup_timeout
        < result
        < terminal_absence
        < text_enabled
        < url_enabled
        < text_still_enabled
        < evidence
        < release_action
        < issue_evidence
        < repair_evidence
        < warning_read
        < expected_warning
        < warning_oracle
    )
    assert "Start-Sleep" not in block
    assert compact.count("$providerGate.AcceptTcpClientAsync()") == 3
    assert compact.count("Receive-W4ExpectedProviderGateRequest") == 3
    assert compact.count("$providerRequestEvidence.Add($providerRequest.Evidence)") == 3
    assert compact.count("Write-W4ProviderGateCheckpoint") == 3
    assert compact.count("$providerGateClient.Dispose()") >= 4
    assert "expected_provider_requests -isnot [int]" in compact
    assert "expected_provider_requests -ne 3" in compact
    assert (
        "throw 'Offline archive scenario contract must require exactly three "
        "Provider requests'" in compact
    )
    assert (
        "throw 'Offline archive Provider request count did not match the "
        "versioned scenario contract'" in compact
    )
    assert compact.count("[DateTime]::UtcNow.AddSeconds(90)") == 1
    assert compact.count(
        "($resultDeadline - [DateTime]::UtcNow).TotalSeconds"
    ) == 2
    assert "-TimeoutSeconds $resultLookupSeconds" in compact
    assert "-TimeoutSeconds $resultRemainingSeconds" in compact

    stage_match = re.search(
        r"\[ValidateSet\((.*?)\)\]\s*\[string\]\$Stage", receiver
    )
    assert stage_match is not None
    assert _extract_single_quoted_values(stage_match.group(1)) == {
        "text_fallback_summarize",
        "workflow_summarize",
        "workflow_extract_tags",
    }
    assert "[ValidateRange(1, 3)][int]$Ordinal" in receiver
    endpoint_cache = receiver.index(
        "$expectedServerEndpoint = "
        "[System.Net.IPEndPoint]$Listener.LocalEndpoint"
    )
    accept_wait = receiver.index(
        "$AcceptTask.Wait($AcceptTimeoutMilliseconds)", endpoint_cache
    )
    accept_result = receiver.index("$AcceptTask.GetAwaiter().GetResult()", accept_wait)
    listener_stop = receiver.index("$Listener.Stop()", accept_result)
    peer = receiver.index("$client.Client.RemoteEndPoint", listener_stop)
    ipv4 = receiver.index(
        "$peer.AddressFamily -ne "
        "[System.Net.Sockets.AddressFamily]::InterNetwork",
        peer,
    )
    loopback = receiver.index(
        "[System.Net.IPAddress]::IsLoopback($peer.Address)", ipv4
    )
    owner = receiver.index(
        "Assert-W4TcpClientOwnedByProcess -Client $client "
        "-ExpectedServerEndpoint $expectedServerEndpoint "
        "-Process $Process -TimeoutMilliseconds $OwnerTimeoutMilliseconds",
        loopback,
    )
    owner_exact = receiver.index(
        "[int]$owner.OwnerProcessId -ne [int]$Process.Id", owner
    )
    request_read = receiver.index(
        "Read-W4BoundedHttpRequestLine -Client $client "
        "-TimeoutMilliseconds $RequestLineTimeoutMilliseconds "
        "-MaxBytes $RequestLineMaxBytes",
        owner_exact,
    )
    request_assert = receiver.index(
        "Assert-W4GatedProviderRequestLine -RequestLine $requestLine",
        request_read,
    )
    evidence_return = receiver.index(
        "Evidence = [pscustomobject][ordered]@{", request_assert
    )
    assert (
        endpoint_cache
        < accept_wait
        < accept_result
        < listener_stop
        < peer
        < ipv4
        < loopback
        < owner
        < owner_exact
        < request_read
        < request_assert
        < evidence_return
    )

    receiver_evidence_match = re.search(
        r"Evidence = \[pscustomobject\]\[ordered\]@\{(.*?)\}\s*\}\s*\}\s*catch",
        receiver,
    )
    assert receiver_evidence_match is not None
    receiver_evidence = receiver_evidence_match.group(1)
    receiver_evidence_fields = re.findall(
        r"\b([a-z][a-z0-9_]*)\s*=", receiver_evidence
    )
    assert receiver_evidence_fields == [
        "ordinal",
        "stage",
        "owner_verified",
        "owner_process_id",
        "request_method",
        "request_path",
        "request_http_version",
        "accept_timeout_milliseconds",
        "owner_timeout_milliseconds",
        "request_line_timeout_milliseconds",
        "request_line_max_bytes",
        "listener_stopped_after_accept",
    ]
    assert "$requestLine" not in receiver_evidence
    assert "$peer" not in receiver_evidence
    assert "LocalEndpoint" not in receiver_evidence

    classified_throws = " ".join(
        re.findall(r"throw\s+(?:'[^']*'|\"[^\"]*\")", receiver)
    )
    assert classified_throws
    assert "$requestLine" not in classified_throws
    assert "$peer" not in classified_throws
    assert "$_.Exception" not in receiver
    for sensitive_marker in ("raw", "header", "body", "token"):
        assert sensitive_marker not in receiver_evidence.casefold()

    assert "schema_version = 'pkv.w4.archive-provider-gate.v2'" in compact
    gate_evidence_match = re.search(
        r"'archive-provider-gate\.json'\) -Value \(\[ordered\]@\{(.*?)\}\)",
        compact,
    )
    assert gate_evidence_match is not None
    gate_evidence = gate_evidence_match.group(1)
    gate_evidence_fields = re.findall(
        r"\b([a-z][a-z0-9_]*)\s*=", gate_evidence
    )
    assert gate_evidence_fields == [
        "schema_version",
        "expected_provider_requests",
        "accepted_provider_requests",
        "provider_requests",
        "listener_stopped_after_final_accept",
        "unexpected_provider_requests_processed",
        "result_fresh_reacquire_deadline_seconds",
        "progress_visible",
        "progress_text_nonempty",
        "submits_disabled_while_running",
        "submits_enabled_after_terminal",
        "release_action",
        "induced_workflow_issue_codes",
        "expected_storage_repair_actions",
    ]
    assert "expected_provider_requests = $expectedProviderRequests" in gate_evidence
    assert (
        "accepted_provider_requests = $providerRequestEvidence.Count" in gate_evidence
    )
    assert "provider_requests = @($providerRequestEvidence)" in gate_evidence
    assert "listener_stopped_after_final_accept = $true" in gate_evidence
    assert "unexpected_provider_requests_processed = $false" in gate_evidence
    assert "result_fresh_reacquire_deadline_seconds = 90" in gate_evidence
    issue_codes_match = re.search(
        r"induced_workflow_issue_codes\s*=\s*@\((.*?)\)",
        gate_evidence,
        flags=re.DOTALL,
    )
    repair_actions_match = re.search(
        r"expected_storage_repair_actions\s*=\s*@\((.*?)\)",
        gate_evidence,
        flags=re.DOTALL,
    )
    assert issue_codes_match is not None
    assert repair_actions_match is not None
    assert re.findall(r"'([^']+)'", issue_codes_match.group(1)) == [
        "workflow_step_failed",
        "storage_vector_failed",
    ]
    assert re.findall(r"'([^']+)'", repair_actions_match.group(1)) == [
        "rebuild_vectors_for_entry"
    ]
    assert (
        "workflow_issue_codes = @('workflow_step_failed', "
        "'storage_vector_failed')" in compact
    )
    assert "storage_repair_actions = @('rebuild_vectors_for_entry')" in compact
    assert "$warning -notmatch 'provider'" not in compact
    for sensitive_marker in (
        "endpoint",
        "raw",
        "header",
        "body",
        "token",
        "$requestline",
        "$peer",
        "$providergateport",
    ):
        assert sensitive_marker not in gate_evidence.casefold()
    request_validator = _compact_powershell(
        _powershell_function(
            _read(SCENARIO_MODULE), "Assert-W4GatedProviderRequestLine"
        )
    )
    assert "$RequestLine -cne 'POST /v1/chat/completions HTTP/1.1'" in request_validator
    assert (
        "throw 'Offline archive Provider request-line contract mismatch'"
        in request_validator
    )
    mismatch_throw = request_validator.split("throw ", maxsplit=1)[1]
    assert "$RequestLine" not in mismatch_throw
    assert "unexpected Provider request line" not in request_validator
    assert (
        "} finally { if ($null -ne $providerGateClient) { "
        "$providerGateClient.Dispose() } $providerGate.Stop() }" in compact
    )

    enabled_wait = _compact_powershell(
        _powershell_function(
            _read(SCENARIO_MODULE), "Wait-W4UiaElementEnabledState"
        )
    )
    assert "AutomationElement]::AutomationIdProperty" in enabled_wait
    assert "$Root.FindAll([System.Windows.Automation.TreeScope]::Descendants" in enabled_wait
    assert "$elements.Count -gt 1" in enabled_wait
    assert "$elements.Count -eq 1" in enabled_wait
    assert "$actual = [bool]$elements.Item(0).Current.IsEnabled" in enabled_wait
    assert "$actual -eq $Expected" in enabled_wait
    assert "return $elements.Item(0)" in enabled_wait
    assert "$deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)" in enabled_wait
    assert "Start-Sleep -Milliseconds 50" in enabled_wait


def test_provider_gate_receiver_handles_three_prearmed_external_requests(
    tmp_path: Path,
) -> None:
    checkpoint_path = tmp_path / "provider-gate-checkpoint.json"
    result = _run_provider_gate_sequence_probe("success", checkpoint_path)

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["mode"] == "success"
    assert payload["caught"] is None
    assert payload["prearmed_ordinals"] == [1, 2, 3]
    assert payload["listener_stopped_before_final_release"] is True
    assert payload["fourth_request_processed"] is False
    assert payload["elapsed_milliseconds"] < 15000

    requests = payload["requests"]
    assert [row["ordinal"] for row in requests] == [1, 2, 3]
    assert [row["stage"] for row in requests] == [
        "text_fallback_summarize",
        "workflow_summarize",
        "workflow_extract_tags",
    ]
    expected_fields = {
        "ordinal",
        "stage",
        "owner_verified",
        "owner_process_id",
        "request_method",
        "request_path",
        "request_http_version",
        "accept_timeout_milliseconds",
        "owner_timeout_milliseconds",
        "request_line_timeout_milliseconds",
        "request_line_max_bytes",
        "listener_stopped_after_accept",
    }
    for index, row in enumerate(requests):
        assert set(row) == expected_fields
        assert row["owner_verified"] is True
        assert row["owner_process_id"] == payload["process_id"]
        assert row["request_method"] == "POST"
        assert row["request_path"] == "/v1/chat/completions"
        assert row["request_http_version"] == "HTTP/1.1"
        assert row["accept_timeout_milliseconds"] == 5000
        assert row["owner_timeout_milliseconds"] == 2000
        assert row["request_line_timeout_milliseconds"] == 5000
        assert row["request_line_max_bytes"] == 2048
        assert row["listener_stopped_after_accept"] is (index == 2)
    checkpoint = payload["checkpoint"]
    assert set(checkpoint) == {
        "schema_version",
        "expected_provider_requests",
        "validated_provider_requests",
        "last_validated_ordinal",
        "last_validated_stage",
        "all_peers_ipv4_loopback",
        "all_tcp_owners_verified",
        "all_request_lines_validated",
        "listener_stopped",
        "provider_requests",
    }
    assert checkpoint["schema_version"] == (
        "pkv.w4.archive-provider-gate-checkpoint.v1"
    )
    assert checkpoint["expected_provider_requests"] == 3
    assert checkpoint["validated_provider_requests"] == 3
    assert checkpoint["last_validated_ordinal"] == 3
    assert checkpoint["last_validated_stage"] == "workflow_extract_tags"
    assert checkpoint["all_peers_ipv4_loopback"] is True
    assert checkpoint["all_tcp_owners_verified"] is True
    assert checkpoint["all_request_lines_validated"] is True
    assert checkpoint["listener_stopped"] is True
    assert checkpoint["provider_requests"] == requests
    serialized = json.dumps(payload, sort_keys=True).casefold()
    for sensitive_marker in ("endpoint", "raw", "header", "body", "token"):
        assert sensitive_marker not in serialized


@pytest.mark.parametrize(
    ("mode", "expected_error", "expected_rows"),
    [
        (
            "wrong_line",
            "Offline archive Provider request-line contract failed at expected "
            "request ordinal 1",
            0,
        ),
        (
            "missing_third",
            "Offline archive Provider request was missing at expected ordinal 3",
            2,
        ),
    ],
)
def test_provider_gate_receiver_rejects_wrong_or_missing_request_without_leak(
    tmp_path: Path, mode: str, expected_error: str, expected_rows: int
) -> None:
    checkpoint_path = tmp_path / f"provider-gate-checkpoint-{mode}.json"
    result = _run_provider_gate_sequence_probe(mode, checkpoint_path)

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["mode"] == mode
    assert payload["caught"] == expected_error
    assert len(payload["requests"]) == expected_rows
    assert payload["elapsed_milliseconds"] < 15000
    if mode == "wrong_line":
        assert payload["prearmed_ordinals"] == [1]
        assert payload["checkpoint"] is None
    else:
        assert payload["prearmed_ordinals"] == [1, 2, 3]
        assert [row["ordinal"] for row in payload["requests"]] == [1, 2]
        checkpoint = payload["checkpoint"]
        assert checkpoint["validated_provider_requests"] == 2
        assert checkpoint["last_validated_ordinal"] == 2
        assert checkpoint["last_validated_stage"] == "workflow_summarize"
        assert checkpoint["listener_stopped"] is False
        assert [
            row["listener_stopped_after_accept"]
            for row in checkpoint["provider_requests"]
        ] == [False, False]
    combined_output = result.stdout + result.stderr
    assert "w4-sensitive-gate-canary" not in combined_output
    assert "GET /v1/chat/completions?token=" not in combined_output


def test_gated_request_line_mismatch_is_fixed_and_does_not_leak_raw_input(
    tmp_path: Path,
) -> None:
    valid = _run_gated_request_line_validator(
        tmp_path, "POST /v1/chat/completions HTTP/1.1"
    )
    sensitive_line = "GET /v1/chat/completions?token=w4-sensitive-canary HTTP/1.1"
    invalid = _run_gated_request_line_validator(tmp_path, sensitive_line)

    assert valid.returncode == 0, valid.stderr
    assert invalid.returncode != 0
    assert "Offline archive Provider request-line contract mismatch" in invalid.stderr
    assert sensitive_line not in invalid.stderr
    assert "w4-sensitive-canary" not in invalid.stderr
    assert sensitive_line not in invalid.stdout
    assert "w4-sensitive-canary" not in invalid.stdout


def test_archive_provider_gate_reads_one_bounded_strict_http_request_line() -> None:
    block = _compact_powershell(
        _powershell_function(
            _read(SCENARIO_MODULE), "Read-W4BoundedHttpRequestLine"
        )
    )

    assert "$stream = $Client.GetStream()" in block
    deadline = block.index(
        "$deadline = [DateTime]::UtcNow.AddMilliseconds($TimeoutMilliseconds)"
    )
    loop = block.index(
        "for ($index = 0; $index -lt $MaxBytes; $index += 1)", deadline
    )
    first_remaining = block.index(
        "$remaining = [int][Math]::Ceiling("
        "($deadline - [DateTime]::UtcNow).TotalMilliseconds)",
        loop,
    )
    first_guard = block.index("if ($remaining -le 0)", first_remaining)
    first_timeout = block.index(
        "$stream.ReadTimeout = [Math]::Max(1, "
        "[Math]::Min($remaining, $TimeoutMilliseconds))",
        first_guard,
    )
    first_read = block.index("$value = $stream.ReadByte()", first_timeout)
    carriage_return = block.index("if ($value -eq 13)", first_read)
    crlf_remaining = block.index(
        "$remaining = [int][Math]::Ceiling("
        "($deadline - [DateTime]::UtcNow).TotalMilliseconds)",
        carriage_return,
    )
    crlf_guard = block.index("if ($remaining -le 0)", crlf_remaining)
    crlf_timeout = block.index(
        "$stream.ReadTimeout = [Math]::Max(1, "
        "[Math]::Min($remaining, $TimeoutMilliseconds))",
        crlf_guard,
    )
    crlf_read = block.index("$lineFeed = $stream.ReadByte()", crlf_timeout)

    assert (
        deadline
        < loop
        < first_remaining
        < first_guard
        < first_timeout
        < first_read
        < carriage_return
        < crlf_remaining
        < crlf_guard
        < crlf_timeout
        < crlf_read
    )
    assert block.count("($deadline - [DateTime]::UtcNow).TotalMilliseconds") == 2
    assert block.count("[Math]::Min($remaining, $TimeoutMilliseconds)") == 2
    assert "$stream.ReadTimeout = $TimeoutMilliseconds" not in block
    assert "request-line deadline expired" in block
    assert "request-line deadline expired before CRLF" in block
    assert "request-line read failed before the absolute deadline" in block
    assert "request-line CRLF read failed before the absolute deadline" in block
    assert "$value -lt 0" in block
    assert "$value -eq 13" in block
    assert "$lineFeed -ne 10" in block
    assert "$value -eq 10 -or $value -lt 32 -or $value -gt 126" in block
    assert "$bytes.Add([byte]$value)" in block
    assert "$terminated = $true" in block
    assert "if (-not $terminated)" in block
    assert "request line exceeded $MaxBytes bytes" in block
    assert "[System.Text.Encoding]::ASCII.GetString($bytes.ToArray())" in block


def test_chat_restart_selection_proof_binds_container_item_and_runtime_id() -> None:
    source = _read(SCENARIO_MODULE)
    select_block = _compact_powershell(
        _powershell_function(source, "Select-W4FirstListItem")
    )
    proof_block = _compact_powershell(
        _powershell_function(source, "Get-W4UiaSelectionProof")
    )

    assert "SelectionItemPattern]$pattern).Select()" in select_block
    assert "return $item" in select_block
    assert "SelectionPattern]::Pattern" in proof_block
    assert "SelectionItemPattern]::Pattern" in proof_block
    assert "SelectionItemPattern]$itemPattern).Current.IsSelected" in proof_block
    assert "SelectionPattern]$containerPattern).Current.GetSelection()" in proof_block
    assert "$currentSelection.Count -ne 1" in proof_block
    assert len(re.findall(r"\.GetRuntimeId\(\)", proof_block)) >= 2
    assert "$expectedRuntimeId.Count -ne $actualRuntimeId.Count" in proof_block
    assert (
        "[int]$expectedRuntimeId[$index] -ne [int]$actualRuntimeId[$index]"
        in proof_block
    )
    assert "pkv.w4.uia-selection-proof.v1" in proof_block
    assert ".GetCurrentSelection()" not in source


def test_selection_helpers_use_real_windows_uia_current_selection(
    tmp_path: Path,
) -> None:
    for _ in range(5):
        result, cleanup_path, timeout = _run_uia_selection_pattern_probe_bounded(
            tmp_path
        )

        assert timeout is None
        assert result is not None
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout.strip().splitlines()[-1])
        assert payload["zero_count"] == 0
        assert payload["one_count"] == 1
        assert payload["runtime_ids_match"] is True
        assert payload["selected_runtime_id"]
        assert payload["proof"]["schema_version"] == "pkv.w4.uia-selection-proof.v1"
        assert payload["proof"]["selection_count"] == 1
        assert payload["proof"]["selection_item_is_selected"] is True
        assert payload["proof"]["selected_runtime_id"] == payload["selected_runtime_id"]

        cleanup = json.loads(cleanup_path.read_text(encoding="utf-8"))
        assert cleanup["child_pid"] > 0
        assert cleanup["child_start_time_utc_ticks"] > 0
        assert cleanup["close_request_written"] is True
        assert cleanup["close_ack"] == "close_request"
        assert cleanup["fallback_kill"] is False
        assert cleanup["process_exited"] is True
        assert cleanup["same_identity_live_after_cleanup"] is False

    failed, cleanup_path, timeout = _run_uia_selection_pattern_probe_bounded(
        tmp_path, force_controller_failure=True
    )
    assert timeout is None
    assert failed is not None
    assert failed.returncode != 0
    assert "UIA selection probe forced controller failure" in failed.stderr
    cleanup = json.loads(cleanup_path.read_text(encoding="utf-8"))
    assert cleanup["child_pid"] > 0
    assert cleanup["child_start_time_utc_ticks"] > 0
    assert cleanup["close_request_written"] is True
    assert cleanup["close_ack"] == "close_request"
    assert cleanup["fallback_kill"] is False
    assert cleanup["process_exited"] is True
    assert cleanup["same_identity_live_after_cleanup"] is False

    timed_out, timeout_cleanup, timeout = _run_uia_selection_pattern_probe_bounded(
        tmp_path, force_outer_timeout=True
    )
    assert timed_out is None
    assert not timeout_cleanup.exists()
    assert timeout == {
        "controller_timed_out": True,
        "job_terminated": True,
        "same_identity_live_after_job_cleanup": False,
    }


def test_uia_selection_probe_assignment_failure_reaps_unassigned_controller(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _read(Path(__file__))
    helper_start = source.index("def _w4_reap_controller_after_job_failure")
    helper_end = source.index("\n\ndef _w4_read_uia_probe_identity", helper_start)
    helper = source[helper_start:helper_end]
    assert "job.terminate()" in helper
    assert "job.close()" in helper
    assert "controller.kill()" in helper
    assert "controller.wait(timeout=5)" in helper
    assert helper.index("job.close()") < helper.index("controller.kill()")
    assert "taskkill" not in helper.lower()

    observed: dict[str, object] = {}

    def reject_before_assignment(
        self: _W4KillOnCloseJob, controller: subprocess.Popen[str]
    ) -> int:
        del self
        controller_handle = wintypes.HANDLE(int(controller._handle))
        observed["controller"] = controller
        observed["process_id"] = controller.pid
        observed["start_ticks"] = _w4_process_start_ticks(controller_handle)
        raise RuntimeError("injected controller Job assignment failure")

    monkeypatch.setattr(
        _W4KillOnCloseJob, "assign_controller_handle", reject_before_assignment
    )
    started = time.monotonic()
    with pytest.raises(RuntimeError, match="injected controller Job assignment failure"):
        _run_uia_selection_pattern_probe_bounded(tmp_path)
    assert time.monotonic() - started < 5
    controller = observed["controller"]
    assert isinstance(controller, subprocess.Popen)
    assert controller.poll() is not None
    assert controller.wait(timeout=0) is not None
    assert not _w4_same_process_identity_is_live(
        int(observed["process_id"]), int(observed["start_ticks"])
    )
    assert not list(tmp_path.glob("uia-selection-launch-*.gate"))
    assert not list(tmp_path.glob("uia-selection-ready-*.json"))


def test_uia_selection_probe_terminate_failure_reaps_assigned_controller(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _W4_KERNEL32 is not None
    observed: dict[str, object] = {}
    original_assign = _W4KillOnCloseJob.assign_controller_handle
    termination_calls: list[tuple[wintypes.HANDLE, wintypes.UINT]] = []

    def reject_after_assignment(
        self: _W4KillOnCloseJob, controller: subprocess.Popen[str]
    ) -> int:
        start_ticks = original_assign(self, controller)
        observed["controller"] = controller
        observed["process_id"] = controller.pid
        observed["start_ticks"] = start_ticks
        raise RuntimeError("injected controller post-assignment failure")

    def reject_job_termination(
        handle: wintypes.HANDLE, exit_code: wintypes.UINT
    ) -> wintypes.BOOL:
        termination_calls.append((handle, exit_code))
        ctypes.set_last_error(5)
        return wintypes.BOOL(False)

    monkeypatch.setattr(
        _W4KillOnCloseJob, "assign_controller_handle", reject_after_assignment
    )
    monkeypatch.setattr(_W4_KERNEL32, "TerminateJobObject", reject_job_termination)
    started = time.monotonic()
    with pytest.raises(RuntimeError, match="UIA selection probe cleanup failed closed"):
        _run_uia_selection_pattern_probe_bounded(tmp_path)
    assert time.monotonic() - started < 5
    assert len(termination_calls) == 1
    controller = observed["controller"]
    assert isinstance(controller, subprocess.Popen)
    assert controller.poll() is not None
    assert controller.wait(timeout=0) is not None
    assert not _w4_same_process_identity_is_live(
        int(observed["process_id"]), int(observed["start_ticks"])
    )
    assert not list(tmp_path.glob("uia-selection-launch-*.gate"))
    assert not list(tmp_path.glob("uia-selection-enable-*.gate"))
    assert not list(tmp_path.glob("uia-selection-ready-*.json"))


def test_loopback_harness_uses_retained_identity_bound_native_exit_handles() -> None:
    driver_source = _read(DRIVER_MODULE)
    scenario_source = _read(SCENARIO_MODULE)
    initializer = _compact_powershell(
        _powershell_function(driver_source, "Initialize-W4FileIdentityInspector")
    )
    observation = _compact_powershell(
        _powershell_function(
            driver_source, "New-W4RetainedProcessExitObservation"
        )
    )
    retained_capture = _compact_powershell(
        _powershell_function(
            scenario_source, "Get-W4RetainedExitedProcessExitCode"
        )
    )
    retained_wait = _compact_powershell(
        _powershell_function(scenario_source, "Wait-W4RetainedProcessExit")
    )
    start = _compact_powershell(
        _powershell_function(scenario_source, "Start-W4LoopbackHarness")
    )
    stop = _compact_powershell(
        _powershell_function(scenario_source, "Stop-W4LoopbackHarness")
    )

    assert "$processExitType = 'PkvW4.ProcessExitInspector' -as [type]" in initializer
    assert "$processExitType -and" in initializer
    assert "$processExitType -or" in initializer
    assert "public sealed class RetainedProcessExitHandle : IDisposable" in initializer
    assert "DuplicateHandle(" in initializer
    assert 'EntryPoint = "GetProcessId"' in initializer
    assert 'EntryPoint = "GetProcessTimes"' in initializer
    assert 'EntryPoint = "GetExitCodeProcess"' in initializer
    assert "STILL_ACTIVE = 259" in initializer
    assert "DateTime.FromFileTimeUtc" in initializer
    assert ".Ticks" in initializer
    assert "ReadExitedExitCode" in initializer
    assert "public bool WaitForExit(int timeoutMilliseconds)" in initializer
    assert "CloseHandle(" in initializer
    assert "SafeHandleZeroOrMinusOneIsInvalid" in initializer
    assert "WaitForSingleObject(" in initializer
    assert "sourceProcess.SafeHandle" in initializer
    assert "DangerousAddRef" in initializer
    assert "DangerousRelease" in initializer
    assert "DangerousGetHandle" not in initializer
    assert "AssertIdentity(retained" in initializer
    assert "retained.Dispose();" in initializer
    assert "exitCode == STILL_ACTIVE" in initializer

    assert (
        "[Parameter(Mandatory = $true)]"
        "[System.Diagnostics.Process]$Process" in observation
    )
    assert "[ValidateRange(1, [int]::MaxValue)][int]$ExpectedProcessId" in observation
    assert "$ExpectedStartTimeUtcTicks" in observation
    assert "[Parameter(Mandatory = $true)][string]$Label" in observation
    assert "$hasExited = $Process.HasExited" in observation
    assert "$hasExited -isnot [bool] -or $hasExited -ne $false" in observation
    assert "Initialize-W4FileIdentityInspector" in observation
    assert "[PkvW4.ProcessExitInspector]::Capture(" in observation

    assert "[Parameter(Mandatory = $true)]$ExitObservation" in retained_capture
    assert "[Parameter(Mandatory = $true)][string]$Label" in retained_capture
    assert "$ExitObservation.ReadExitedExitCode()" in retained_capture
    assert "$Process.ExitCode" not in retained_capture
    assert "$Process.HasExited" not in retained_capture
    assert "[Parameter(Mandatory = $true)]$ExitObservation" in retained_wait
    assert "[ValidateRange(0, 30000)][int]$TimeoutMilliseconds" in retained_wait
    assert "$ExitObservation.WaitForExit($TimeoutMilliseconds)" in retained_wait
    assert "$Process" not in retained_wait

    identity = start.index("$runtimeIdentity = Get-W4ValidatedProcessIdentity")
    launcher_snapshot = start.index(
        "$launcherTreeSnapshot = New-W4ProcessTreeIdentitySnapshot", identity
    )
    runtime_snapshot = start.index("$runtimeTreeSnapshot = if", launcher_snapshot)
    runtime_guard = start.index(
        "if (-not [bool]$runtimeIdentity.RuntimeIsLauncher) {", runtime_snapshot
    )
    runtime_observation = start.index(
        "$runtimeExitObservation = New-W4RetainedProcessExitObservation",
        runtime_guard,
    )
    returned_observations = start.index(
        "RuntimeExitObservation = $runtimeExitObservation", runtime_observation
    )
    assert "$runtimeExitObservation.Dispose()" in start
    assert "LauncherExitObservation" not in start
    assert (
        identity
        < launcher_snapshot
        < runtime_snapshot
        < runtime_guard
        < runtime_observation
        < returned_observations
    )

    assert "$launcherPid = [int]$Harness.LauncherPid" in stop
    assert "$runtimePid = [int]$Harness.RuntimePid" in stop
    assert "$launcherPid -lt 1 -or $runtimePid -lt 1" in stop
    assert "$runtimeIsLauncher = $runtimePid -eq $launcherPid" in stop
    assert "$runtimeExitObservation = $Harness.RuntimeExitObservation" in stop
    assert ".Id" not in stop
    assert ".ExitCode" not in stop
    assert "$launcherExitCode = $null" in stop
    assert "$runtimeExitCode = $null" in stop
    assert "$processRecord = [ordered]@{" in stop
    assert "launcher_pid = $launcherPid" in stop
    assert "runtime_pid = $runtimePid" in stop
    assert "exit_code = $launcherExitCode" in stop
    assert "runtime_exit_code = $runtimeExitCode" in stop
    assert "$forced -or $null -eq $processRecord.exit_code" in stop
    assert "$null -eq $processRecord.runtime_exit_code" in stop
    assert "[int]$processRecord.exit_code -ne 0" in stop
    assert "[int]$processRecord.runtime_exit_code -ne 0" in stop
    assert "launcher=$launcherExitDisplay runtime=$runtimeExitDisplay" in stop
    assert "'<unconfirmed>'" in stop
    assert "$runtimeExitObservation.Dispose()" in stop
    assert "runtime_exit_source = if ($runtimeIsLauncher)" in stop
    assert "'retained_native_handle'" in stop
    assert "Get-W4RetainedExitedProcessExitCode" in stop
    assert "Wait-W4RetainedProcessExit" in stop
    assert "$runtimeProcess.HasExited" not in stop
    assert "$runtimeProcess.WaitForExit" not in stop

    pre_runtime_wait = stop.index("Wait-W4RetainedProcessExit")
    pre_runtime_timeout = stop.index("-TimeoutMilliseconds 0", pre_runtime_wait)
    runtime_snapshot_refresh = stop.index(
        "$runtimeTreeSnapshot = New-W4ProcessTreeIdentitySnapshot", pre_runtime_wait
    )
    launcher_wait = stop.index("$process.WaitForExit()")
    launcher_capture = stop.index(
        "$launcherExitCode = Get-W4ExitedProcessExitCode", launcher_wait
    )
    same_runtime = stop.index("if ($runtimeIsLauncher) {", launcher_capture)
    same_runtime_reuse = stop.index("$runtimeExitCode = $launcherExitCode", same_runtime)
    runtime_wait = stop.index("Wait-W4RetainedProcessExit", same_runtime_reuse)
    runtime_wait_timeout = stop.index("-TimeoutMilliseconds 5000", runtime_wait)
    runtime_capture = stop.index(
        "$runtimeExitCode = Get-W4RetainedExitedProcessExitCode", runtime_wait
    )
    runtime_reconcile = stop.index("if (-not $runtimeIsLauncher) {", runtime_capture)
    runtime_stop = stop.index(
        "Stop-W4ProcessTree -Process $runtimeProcess", runtime_reconcile
    )
    launcher_stop = stop.index("Stop-W4ProcessTree -Process $process", runtime_stop)
    process_record = stop.index("$processRecord = [ordered]@{", runtime_capture)
    normality = stop.index("if ($forced -or", process_record)
    assert (
        pre_runtime_wait
        < pre_runtime_timeout
        < runtime_snapshot_refresh
        < launcher_wait
        < launcher_capture
        < same_runtime
        < same_runtime_reuse
        < runtime_wait
        < runtime_capture
        < runtime_reconcile
        < runtime_stop
        < launcher_stop
        < process_record
        < normality
    )
    assert runtime_wait < runtime_wait_timeout < runtime_capture


def test_loopback_harness_uses_retained_exit_handles_after_managed_handles_become_unavailable(
    tmp_path: Path,
) -> None:
    result = _run_loopback_retained_exit_observation_probe(
        tmp_path, invalidate_runtime_before_native_read=True
    )

    assert result.returncode == 0, result.stderr or result.stdout
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["topology"] == "split"
    assert payload["runtime_mode"] == "success"
    assert payload["runtime_source_acquired_via_get_process_by_id"] is True
    assert payload["runtime_wrapper_invalidated"] is True
    assert payload["runtime_observation_closed"] is True
    assert payload["runtime_observation_read_after_close_rejected"] is True
    assert payload["runtime_observation_wait_after_close_rejected"] is True
    assert payload["runtime_shutdown_marker_exists"] is False
    assert payload["evidence_result_exists"] is True
    assert payload["tree_cleanup_is_real"] is False
    assert payload["caught"] is None
    assert payload["stop_result"] == "passed"
    assert payload["tree_reconcile_count"] >= 4
    assert payload["process_record"] == {
        "launcher_pid": payload["process_record"]["launcher_pid"],
        "runtime_pid": payload["process_record"]["runtime_pid"],
        "exit_code": 0,
        "runtime_exit_code": 0,
        "runtime_exit_source": "retained_native_handle",
        "forced_termination": False,
        "timed_out": False,
    }
    assert payload["process_record"]["launcher_pid"] > 0
    assert payload["process_record"]["runtime_pid"] > 0
    assert (
        payload["process_record"]["launcher_pid"]
        != payload["process_record"]["runtime_pid"]
    )


def test_loopback_harness_same_pid_uses_launcher_managed_exit_authority(
    tmp_path: Path,
) -> None:
    result = _run_loopback_retained_exit_observation_probe(tmp_path, topology="same")

    assert result.returncode == 0, result.stderr or result.stdout
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["topology"] == "same"
    assert payload["runtime_mode"] == "success"
    assert payload["runtime_source_acquired_via_get_process_by_id"] is False
    assert payload["runtime_wrapper_invalidated"] is False
    assert payload["runtime_observation_closed"] is None
    assert payload["runtime_observation_read_after_close_rejected"] is None
    assert payload["runtime_observation_wait_after_close_rejected"] is None
    assert payload["runtime_shutdown_marker_exists"] is False
    assert payload["evidence_result_exists"] is True
    assert payload["tree_cleanup_is_real"] is False
    assert payload["caught"] is None
    assert payload["stop_result"] == "passed"
    assert payload["tree_reconcile_count"] >= 2
    assert payload["process_record"] == {
        "launcher_pid": payload["process_record"]["launcher_pid"],
        "runtime_pid": payload["process_record"]["runtime_pid"],
        "exit_code": 0,
        "runtime_exit_code": 0,
        "runtime_exit_source": "launcher_managed_exit_code",
        "forced_termination": False,
        "timed_out": False,
    }
    assert payload["process_record"]["launcher_pid"] > 0
    assert (
        payload["process_record"]["launcher_pid"]
        == payload["process_record"]["runtime_pid"]
    )


def test_loopback_harness_split_nonzero_runtime_fails_and_closes_observation(
    tmp_path: Path,
) -> None:
    result = _run_loopback_retained_exit_observation_probe(
        tmp_path, runtime_mode="nonzero"
    )

    assert result.returncode == 0, result.stderr or result.stdout
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["topology"] == "split"
    assert payload["runtime_mode"] == "nonzero"
    assert payload["runtime_source_acquired_via_get_process_by_id"] is True
    assert payload["runtime_wrapper_invalidated"] is False
    assert payload["runtime_observation_closed"] is True
    assert payload["runtime_observation_read_after_close_rejected"] is True
    assert payload["runtime_observation_wait_after_close_rejected"] is True
    assert payload["runtime_shutdown_marker_exists"] is False
    assert payload["evidence_result_exists"] is False
    assert payload["tree_cleanup_is_real"] is True
    assert payload["stop_result"] is None
    assert payload["caught"] == (
        "Harness did not exit normally after exact shutdown request: "
        "launcher=0 runtime=17"
    )
    assert payload["tree_reconcile_count"] is None
    assert payload["process_record"] == {
        "launcher_pid": payload["process_record"]["launcher_pid"],
        "runtime_pid": payload["process_record"]["runtime_pid"],
        "exit_code": 0,
        "runtime_exit_code": 17,
        "runtime_exit_source": "retained_native_handle",
        "forced_termination": False,
        "timed_out": False,
    }


def test_loopback_harness_split_native_timeout_forces_failure_and_closes_observation(
    tmp_path: Path,
) -> None:
    result = _run_loopback_retained_exit_observation_probe(
        tmp_path, runtime_mode="ignore_shutdown"
    )

    assert result.returncode == 0, result.stderr or result.stdout
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["topology"] == "split"
    assert payload["runtime_mode"] == "ignore_shutdown"
    assert payload["runtime_source_acquired_via_get_process_by_id"] is True
    assert payload["runtime_wrapper_invalidated"] is False
    assert payload["runtime_observation_closed"] is True
    assert payload["runtime_observation_read_after_close_rejected"] is True
    assert payload["runtime_observation_wait_after_close_rejected"] is True
    assert payload["runtime_shutdown_marker_exists"] is True
    assert payload["evidence_result_exists"] is False
    assert payload["tree_cleanup_is_real"] is True
    assert 4500 <= payload["stop_elapsed_milliseconds"] < 10000
    assert payload["stop_result"] is None
    assert payload["caught"] == (
        "Harness did not exit normally after exact shutdown request: "
        "launcher=0 runtime=<unconfirmed>"
    )
    assert payload["tree_reconcile_count"] is None
    assert payload["process_record"] == {
        "launcher_pid": payload["process_record"]["launcher_pid"],
        "runtime_pid": payload["process_record"]["runtime_pid"],
        "exit_code": 0,
        "runtime_exit_code": None,
        "runtime_exit_source": "retained_native_handle",
        "forced_termination": True,
        "timed_out": True,
    }


def test_retained_native_exit_observation_survives_managed_process_disposal(
    tmp_path: Path,
) -> None:
    result = _run_retained_process_exit_observation_lifecycle_probe(tmp_path)

    assert result.returncode == 0, result.stderr or result.stdout
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["source_acquired_via_get_process_by_id"] is True
    assert payload["wrong_pid_rejections"] == 3
    assert payload["wrong_start_ticks_rejections"] == 3
    assert payload["still_active_rejected"] is True
    assert payload["still_active_wait"] is False
    assert payload["managed_process_disposed"] is True
    assert (
        payload["managed_post_exit_unavailable"] is True
        or payload["managed_post_exit_code"] == 0
    )
    assert payload["retained_exited_wait"] is True
    assert payload["retained_exit_code"] == 17
    assert payload["observation_disposed"] is True
    assert payload["observation_closed"] is True
    assert payload["disposed_read_rejected"] is True
    assert payload["probe_paths"] == ["release.signal"]


def test_screenshot_is_hwnd_pid_bound_bounded_and_nonuniform_before_publish() -> None:
    source = _read(DRIVER_MODULE)
    initializer = _compact_powershell(
        _powershell_function(source, "Initialize-W4FileIdentityInspector")
    )
    attempt = _compact_powershell(
        _powershell_function(source, "Invoke-W4PrintWindowCaptureAttempt")
    )
    deadline = _compact_powershell(
        _powershell_function(source, "Assert-W4CaptureDeadline")
    )
    termination_source = _powershell_function(
        source, "Stop-W4BoundedCaptureWorker"
    )
    termination = _compact_powershell(termination_source)
    termination_code = "\n".join(
        line.split("#", 1)[0] for line in termination_source.splitlines()
    )
    screenshot = _compact_powershell(
        _powershell_function(source, "Save-W4Screenshot")
    )

    assert "$windowCaptureType = 'PkvW4.WindowCaptureInspector' -as [type]" in initializer
    assert "private const uint PW_RENDERFULLCONTENT = 0x00000002;" in initializer
    assert "private static extern bool IsWindow(IntPtr window);" in initializer
    assert "private static extern uint GetWindowThreadProcessId(" in initializer
    assert "private static extern bool GetWindowRect(" in initializer
    assert "EntryPoint = \"PrintWindow\"" in initializer
    assert "EntryPoint = \"DwmFlush\"" in initializer
    assert "private static extern int DwmFlushNative();" in initializer
    assert "actualProcessId != checked((uint)processId)" in initializer
    assert "PrintWindowNative(window, destination, PW_RENDERFULLCONTENT)" in initializer

    assert "[PkvW4.WindowCaptureInspector]::FlushDesktopComposition()" in attempt
    assert "[PkvW4.WindowCaptureInspector]::PrintOwnedWindow(" in attempt
    assert "if (-not $printed)" in attempt
    assert "Test-W4BitmapPixelDiversity -Bitmap $bitmap" in attempt
    assert "$bitmap.Save($fullPath, [System.Drawing.Imaging.ImageFormat]::Png)" in attempt
    assert "[Parameter(Mandatory = $true)][string]$MetadataPath" in attempt
    assert "$width -gt 8192 -or $height -gt 8192" in attempt
    assert "([int64]$width * [int64]$height) -gt 16777216" in attempt
    worker_diversity = attempt.index("Test-W4BitmapPixelDiversity -Bitmap $bitmap")
    worker_png = attempt.index(
        "$bitmap.Save($fullPath, [System.Drawing.Imaging.ImageFormat]::Png)",
        worker_diversity,
    )
    worker_length = attempt.index(
        "$pngLength = [int64](Get-Item -LiteralPath $fullPath", worker_png
    )
    worker_length_bound = attempt.index(
        "$pngLength -le 0 -or $pngLength -gt 134217728", worker_length
    )
    worker_metadata = attempt.index(
        "Write-W4JsonFile -Path $fullMetadataPath -Value", worker_length_bound
    )
    assert (
        worker_diversity
        < worker_png
        < worker_length
        < worker_length_bound
        < worker_metadata
    )
    assert "schema_version = 'pkv.w4.window-capture-worker.v1'" in attempt
    assert "method = 'PrintWindow(PW_RENDERFULLCONTENT)'" in attempt
    assert "png_length = $pngLength" in attempt
    assert "pixel_diversity = $true" in attempt
    assert "}) -Compress" in attempt[worker_metadata:]
    assert "[Parameter(Mandatory = $true)][DateTime]$DeadlineUtc" in deadline
    assert "[DateTime]::UtcNow -ge $DeadlineUtc" in deadline
    assert "bounded window capture deadline expired at stage: $Stage" in deadline

    assert (
        "[Parameter(Mandatory = $true)]"
        "[System.Diagnostics.Process]$Process" in termination
    )
    assert "[Parameter(Mandatory = $true)][DateTime]$DeadlineUtc" in termination
    assert "$Process.Refresh()" in termination
    assert termination.count("$Process.Kill()") == 1
    assert "$Process.WaitForExit($remainingMilliseconds)" in termination
    assert "Stop-W4ProcessTree" not in termination
    assert re.search(r"(?i)\btaskkill(?:\.exe)?\b", termination_code) is None
    assert ".Id" not in termination
    refresh = termination.index("$Process.Refresh()")
    kill = termination.index("$Process.Kill()", refresh)
    remaining = termination.index(
        "($DeadlineUtc - [DateTime]::UtcNow).TotalMilliseconds", kill
    )
    deadline_guard = termination.index("if ($remainingMilliseconds -le 0)", remaining)
    deadline_return = termination.index("return", deadline_guard)
    deadline_wait = termination.index(
        "$Process.WaitForExit($remainingMilliseconds)", deadline_return
    )
    assert refresh < kill < remaining < deadline_guard < deadline_return < deadline_wait

    assert (
        "[Parameter(Mandatory = $true)]"
        "[System.Diagnostics.Process]$Process" in screenshot
    )
    assert "[ValidateRange(1, 3)][int]$MaximumAttempts = 3" in screenshot
    assert "$captureDeadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)" in screenshot
    assert (
        "for ($attempt = 1; $attempt -le $MaximumAttempts; $attempt += 1)"
        in screenshot
    )
    assert "$Element.Current.NativeWindowHandle" in screenshot
    assert "$Element.Current.ProcessId -ne $expectedProcessId" in screenshot
    assert "Get-W4LiveProcessForIdentity -Identity $identity" in screenshot
    assert "GetOwnedWindowBounds( $rawNativeWindowHandle, $expectedProcessId )" in screenshot
    assert "$boundWidth -gt 8192 -or $boundHeight -gt 8192" in screenshot
    assert (
        "([int64]$boundWidth * [int64]$boundHeight) -gt 16777216"
        in screenshot
    )
    assert (
        "$powerShellPath = Join-Path $systemRoot "
        "'System32\\WindowsPowerShell\\v1.0\\powershell.exe'" in screenshot
    )
    assert "New-W4ProcessStartInfo -FileName $powerShellPath" in screenshot
    assert "Invoke-W4PrintWindowCaptureAttempt -Path $outputPath" in screenshot
    assert "-MetadataPath $metadataPath" in screenshot
    assert "$workerMetadataPath = $temporaryPath + '.validation.json'" in screenshot
    assert "PKV_W4_CAPTURE_METADATA = $workerMetadataPath" in screenshot
    assert "$worker.WaitForExit($attemptWaitMilliseconds)" in screenshot
    worker_start = screenshot.index(
        "$workerStarted = Start-W4RedirectedProcess -Process $worker"
    )
    recomputed_wait = screenshot.index(
        "$attemptWaitMilliseconds = [Math]::Min( 8000, "
        "[int][Math]::Floor( ($captureDeadline - "
        "[DateTime]::UtcNow).TotalMilliseconds ) )",
        worker_start,
    )
    worker_wait = screenshot.index(
        "$worker.WaitForExit($attemptWaitMilliseconds)", recomputed_wait
    )
    assert worker_start < recomputed_wait < worker_wait
    assert (
        "$attemptWaitMilliseconds = [Math]::Min(8000, $remainingMilliseconds)"
        not in screenshot
    )
    timeout_branch = screenshot.index("if (-not $workerCompleted)")
    timeout_throw = screenshot.index(
        "throw 'bounded PrintWindow capture worker timed out'", timeout_branch
    )
    helper_call = screenshot.index(
        "Stop-W4BoundedCaptureWorker -Process $worker -DeadlineUtc $captureDeadline",
        timeout_branch,
    )
    assert worker_wait < timeout_branch < helper_call < timeout_throw
    assert "Stop-W4ProcessTree" not in screenshot[timeout_branch:timeout_throw]
    assert "$worker.Kill()" not in screenshot[timeout_branch:timeout_throw]
    assert "workerSnapshot" not in screenshot[timeout_branch:timeout_throw]
    assert "continue" not in screenshot[timeout_branch:timeout_throw]
    assert "if ($workerExitCode -ne 0)" in screenshot
    assert "continue" in screenshot

    post_worker = screenshot.index("-Stage 'post-worker-exit'", worker_wait)
    post_identity = screenshot.index("-Stage 'post-process-identity'", post_worker)
    post_window = screenshot.index("-Stage 'post-uia-window-binding'", post_identity)
    post_terminal = screenshot.index(
        "-Stage 'post-terminal-uia-binding'", post_window
    )
    normal_files = screenshot.index(
        "$pngItem.PSIsContainer -or $metadataItem.PSIsContainer",
        post_terminal,
    )
    strict_sizes = screenshot.index(
        "[int64]$pngItem.Length -le 0 -or "
        "[int64]$pngItem.Length -gt 134217728 -or",
        normal_files,
    )
    strict_metadata_size = screenshot.index(
        "[int64]$metadataItem.Length -le 0 -or "
        "[int64]$metadataItem.Length -gt 4096",
        strict_sizes,
    )
    link_count = screenshot.index(
        "[PkvW4.FileIdentity]::GetLinkCount($pngItem.FullName) -ne 1",
        strict_metadata_size,
    )
    metadata_read = screenshot.index(
        "$workerMetadata = Read-W4JsonFile -Path $workerMetadataPath",
        link_count,
    )
    exact_fields = screenshot.index(
        "$expectedWorkerMetadataFields = @( 'schema_version', 'method', "
        "'width', 'height', 'png_length', 'pixel_diversity' )",
        metadata_read,
    )
    type_checks = screenshot.index(
        "$workerMetadata.width -isnot [int]", exact_fields
    )
    int64_width = screenshot.index(
        "$workerMetadata.width -isnot [int64]", type_checks
    )
    int64_height = screenshot.index(
        "$workerMetadata.height -isnot [int64]", int64_width
    )
    metadata_bounds = screenshot.index(
        "$metadataWidth -le 1 -or $metadataHeight -le 1 -or "
        "$metadataWidth -gt 8192 -or $metadataHeight -gt 8192 -or "
        "($metadataWidth * $metadataHeight) -gt 16777216",
        int64_height,
    )
    checked_conversion = screenshot.index(
        "$capturedWidth = [int]$metadataWidth", metadata_bounds
    )
    length_binding = screenshot.index(
        "[int64]$workerMetadata.png_length -ne [int64]$pngItem.Length",
        checked_conversion,
    )
    post_metadata_validation = screenshot.index(
        "-Stage 'post-worker-metadata-validation'", length_binding
    )
    worker_metadata_removed = screenshot.index(
        "Remove-W4CaptureTemporaryFile -Path $workerMetadataPath",
        post_metadata_validation,
    )
    assert (
        worker_wait
        < post_worker
        < post_identity
        < post_window
        < post_terminal
        < normal_files
        < strict_sizes
        < strict_metadata_size
        < link_count
        < metadata_read
        < exact_fields
        < type_checks
        < int64_width
        < int64_height
        < metadata_bounds
        < checked_conversion
        < length_binding
        < post_metadata_validation
        < worker_metadata_removed
    )
    assert "[System.Drawing.Bitmap]::new($temporaryPath)" not in screenshot
    assert "Test-W4BitmapPixelDiversity" not in screenshot
    assert "Add-Type -AssemblyName System.Drawing" not in screenshot

    evidence_temp = screenshot.index(
        "$evidenceTemporaryPath =", worker_metadata_removed
    )
    assert (
        "$evidenceTemporaryPath = \"$evidencePath.capture-"
        "$([Guid]::NewGuid().ToString('N')).tmp.json\"" in screenshot
    )
    sidecar_write = screenshot.index(
        "Write-W4JsonFile -Path $evidenceTemporaryPath -Value", evidence_temp
    )
    pre_sidecar = screenshot.index("-Stage 'pre-sidecar-publish'", sidecar_write)
    sidecar_publish = screenshot.index(
        "[System.IO.File]::Move($evidenceTemporaryPath, $evidencePath)",
        pre_sidecar,
    )
    sidecar_committed = screenshot.index(
        "$evidencePublished = $true", sidecar_publish
    )
    pre_png_commit = screenshot.index(
        "-Stage 'pre-png-commit'", sidecar_committed
    )
    atomic_publish = screenshot.index(
        "[System.IO.File]::Move($temporaryPath, $fullPath)", pre_png_commit
    )
    png_committed = screenshot.index(
        "$temporaryPublished = $true", atomic_publish
    )
    assert (
        post_metadata_validation
        < worker_metadata_removed
        < evidence_temp
        < sidecar_write
        < pre_sidecar
        < sidecar_publish
        < sidecar_committed
        < pre_png_commit
        < atomic_publish
        < png_committed
    )
    assert _has_safe_capture_publication_contract(
        _powershell_function(source, "Save-W4Screenshot")
    )
    assert _has_strict_worker_capture_validation_contract(source)
    assert screenshot.count("[System.IO.File]::Move($temporaryPath, $fullPath)") == 1
    assert screenshot.count(
        "Remove-W4CaptureTemporaryFile -Path $workerMetadataPath"
    ) == 2
    assert "CopyFromScreen" not in source
    assert "SystemInformation]::VirtualScreen" not in source
    assert re.search(r"(?i)\b(?:ocr|tesseract)\b", source) is None

    evidence_source = _powershell_function(source, "Save-W4Screenshot")
    evidence_match = re.search(
        r"Write-W4JsonFile\s+-Path\s+\$evidenceTemporaryPath\s+-Value\s+"
        r"\(\[ordered\]@\{(.*?)\n\s{20}\}\)",
        evidence_source,
        flags=re.DOTALL,
    )
    assert evidence_match is not None
    evidence = evidence_match.group(1)
    top_level_fields = re.findall(r"(?m)^\s{24}([a-z][a-z0-9_]*)\s*=", evidence)
    assert top_level_fields == [
        "schema_version",
        "method",
        "attempt",
        "result",
        "window_binding",
        "process_id",
        "size",
        "pixel_diversity",
    ]
    assert "method = 'PrintWindow(PW_RENDERFULLCONTENT)'" in evidence
    assert "result = 'nonuniform_png_published'" in evidence
    assert "window_binding = 'uia_hwnd_exact_process_identity'" in evidence
    assert "pixel_diversity = 'nonuniform'" in evidence
    for forbidden in ("hwnd", "handle", "path", "title", "text", "raw", "pixel_data"):
        assert re.search(rf"(?i)\b{re.escape(forbidden)}\b", evidence) is None


def test_screenshot_hung_worker_timeout_cleanup_is_deadline_bounded() -> None:
    result = _run_bounded_capture_worker_timeout_probe()

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["caught"] is None
    assert payload["deadline_remaining_before_milliseconds"] > 0
    assert payload["worker_was_running"] is True
    assert payload["worker_exited"] is True
    assert payload["elapsed_milliseconds"] < 3000


def test_screenshot_hung_worker_timeout_path_is_bounded_and_removes_pixels(
    tmp_path: Path,
) -> None:
    result, screenshot = _run_save_screenshot_hung_worker_probe(tmp_path)

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["automation_id"] == "pkv_main_window"
    assert payload["window_process_id"] == payload["target_process_id"]
    assert payload["caught"] == (
        "Application-window screenshot capture failed: "
        "bounded PrintWindow capture worker timed out"
    )
    assert payload["capture_elapsed_milliseconds"] < 5000
    assert payload["screenshot_exists"] is False
    assert payload["capture_evidence_exists"] is False
    assert list(tmp_path.glob(f"{screenshot.name}.capture-*")) == []


@pytest.mark.parametrize(
    "mutation",
    [
        "png_before_sidecar",
        "direct_sidecar_write",
        "missing_pre_png_deadline",
        "missing_sidecar_rollback",
    ],
)
def test_screenshot_publication_contract_rejects_partial_commit_mutations(
    mutation: str,
) -> None:
    block = _powershell_function(_read(DRIVER_MODULE), "Save-W4Screenshot")
    assert _has_safe_capture_publication_contract(block)

    if mutation == "png_before_sidecar":
        sidecar_move = "[System.IO.File]::Move($evidenceTemporaryPath, $evidencePath)"
        png_move = "[System.IO.File]::Move($temporaryPath, $fullPath)"
        sentinel = "__W4_CAPTURE_PUBLICATION_MOVE_SENTINEL__"
        mutated = block.replace(sidecar_move, sentinel, 1)
        mutated = mutated.replace(png_move, sidecar_move, 1)
        mutated = mutated.replace(sentinel, png_move, 1)
    elif mutation == "direct_sidecar_write":
        mutated = block.replace(
            "Write-W4JsonFile -Path $evidenceTemporaryPath -Value",
            "Write-W4JsonFile -Path $evidencePath -Value",
            1,
        )
    elif mutation == "missing_pre_png_deadline":
        mutated = block.replace("-Stage 'pre-png-commit'", "-Stage 'removed'", 1)
    else:
        mutated = block.replace(
            "Remove-W4CaptureTemporaryFile -Path $evidencePath `",
            "Write-Verbose 'rollback removed' `",
            1,
        )

    assert mutated != block
    assert not _has_safe_capture_publication_contract(mutated)


@pytest.mark.parametrize(
    "mutation",
    [
        "parent_bitmap_decode",
        "relaxed_metadata_size",
        "relaxed_pixel_count",
        "reordered_metadata_fields",
        "missing_worker_metadata_cleanup",
    ],
)
def test_screenshot_worker_validation_contract_rejects_parent_decode_or_drift(
    mutation: str,
) -> None:
    source = _read(DRIVER_MODULE)
    assert _has_strict_worker_capture_validation_contract(source)

    if mutation == "parent_bitmap_decode":
        marker = "function Save-W4Screenshot {"
        mutated = source.replace(
            marker,
            marker + "\n    [System.Drawing.Bitmap]::new($temporaryPath)",
            1,
        )
    elif mutation == "relaxed_metadata_size":
        mutated = source.replace(
            "[int64]$metadataItem.Length -gt 4096",
            "[int64]$metadataItem.Length -gt 8192",
            1,
        )
    elif mutation == "relaxed_pixel_count":
        mutated = source.replace("16777216", "16777217")
    elif mutation == "reordered_metadata_fields":
        mutated = source.replace(
            "'schema_version', 'method', 'width', 'height',",
            "'method', 'schema_version', 'width', 'height',",
            1,
        )
    else:
        mutated = source.replace(
            "Remove-W4CaptureTemporaryFile -Path $workerMetadataPath",
            "Write-Verbose 'worker metadata cleanup removed'",
            1,
        )

    assert mutated != source
    assert not _has_strict_worker_capture_validation_contract(mutated)


def test_chat_capture_is_bound_to_the_exact_terminal_uia_text() -> None:
    block = _compact_powershell(
        _powershell_function(_read(SCENARIO_MODULE), "Invoke-W4ChatLoopbackScenario")
    )
    error_terminal = block.index(
        "Wait-W4UiaText -Element $status "
        "-Expected @('失败（错误代码：chat_provider_failed）') -TimeoutSeconds 60"
    )
    round_terminal = block.index(
        "Wait-W4UiaText -Element $rounds "
        "-Expected @('轮数: 1 / 3') -TimeoutSeconds 10",
        error_terminal,
    )
    capture = block.index("Save-W4Screenshot", round_terminal)
    terminal_element = block.index("-TerminalElement $status", capture)
    terminal_text = block.index(
        "-ExpectedTerminalText @('失败（错误代码：chat_provider_failed）')",
        terminal_element,
    )

    assert error_terminal < round_terminal < capture < terminal_element < terminal_text
    assert "-Element $gui.Window -Process $gui.Process" in block[capture:terminal_text]


@pytest.mark.parametrize("mode", ["positive", "wrong_process", "uniform"])
def test_screenshot_capture_uses_real_windows_uia_and_fails_closed(
    tmp_path: Path, mode: str
) -> None:
    result = _run_window_capture_probe(tmp_path, mode)

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["mode"] == mode
    assert payload["automation_id"] == "pkv_main_window"
    assert payload["window_process_id"] == payload["target_process_id"]
    assert payload["elapsed_milliseconds"] < 20000
    if mode == "positive":
        assert payload["caught"] is None
        assert payload["screenshot_exists"] is True
        assert payload["capture_evidence_exists"] is True
        capture = payload["capture"]
        assert set(capture) == {
            "schema_version",
            "method",
            "attempt",
            "result",
            "window_binding",
            "process_id",
            "size",
            "pixel_diversity",
        }
        assert capture["schema_version"] == "pkv.w4.window-capture.v1"
        assert capture["method"] == "PrintWindow(PW_RENDERFULLCONTENT)"
        assert 1 <= capture["attempt"] <= 2
        assert capture["result"] == "nonuniform_png_published"
        assert capture["window_binding"] == "uia_hwnd_exact_process_identity"
        assert capture["process_id"] == payload["target_process_id"]
        assert capture["size"]["width"] > 1
        assert capture["size"]["height"] > 1
        assert capture["pixel_diversity"] == "nonuniform"
    else:
        assert payload["caught"] is not None
        assert "Application-window screenshot capture failed" in payload["caught"]
        if mode == "wrong_process":
            assert "process identity mismatch" in payload["caught"]
        else:
            assert "nonuniform" in payload["caught"] or "single-color" in payload["caught"]
        assert payload["screenshot_exists"] is False
        assert payload["capture_evidence_exists"] is False
        assert payload["capture"] is None


def test_chat_restart_waits_for_round_then_freshly_resolves_messages() -> None:
    source = _read(SCENARIO_MODULE)
    fresh_block = _compact_powershell(
        _powershell_function(source, "Wait-W4FreshUiaTextByIdContains")
    )
    loop_start = fresh_block.index("do {")
    resolve = fresh_block.index(
        "$matches = $Root.FindAll([System.Windows.Automation.TreeScope]::Descendants, "
        "$condition)",
        loop_start,
    )
    reject_duplicate = fresh_block.index("$matches.Count -gt 1", resolve)
    exact_one = fresh_block.index("$matches.Count -eq 1", reject_duplicate)
    read = fresh_block.index("Get-W4UiaText -Element $matches.Item(0)", exact_one)
    loop_end = fresh_block.index("} while", read)

    assert "AutomationElement]::AutomationIdProperty" in fresh_block
    assert (
        loop_start
        < resolve
        < reject_duplicate
        < exact_one
        < read
        < loop_end
    )
    assert "ElementNotAvailableException" in fresh_block

    chat_block = _compact_powershell(
        _powershell_function(source, "Invoke-W4ChatLoopbackScenario")
    )
    restart = chat_block.index("$restart = Start-W4GuiApplication")
    session_list = chat_block.index(
        "$sessionList = Get-W4UiaElementById -Root $restart.Window "
        "-AutomationId 'session_list'",
        restart,
    )
    selected = chat_block.index(
        "$selectedSession = Select-W4FirstListItem -Root $sessionList",
        session_list,
    )
    proof = chat_block.index(
        "Get-W4UiaSelectionProof -Root $sessionList -Item $selectedSession",
        selected,
    )
    evidence = chat_block.index("'chat-session-selection.json'", proof)
    round_count = chat_block.index(
        "-AutomationId 'chat_round_count'", evidence
    )
    round_terminal = chat_block.index("-Expected @('轮数: 1 / 3')", round_count)
    fresh_messages = chat_block.index(
        "Wait-W4FreshUiaTextByIdContains -Root $restart.Window "
        "-AutomationId 'chat_messages'",
        round_terminal,
    )
    rejected = chat_block.index(
        "Stopped/error Chat turn leaked into durable restart state", fresh_messages
    )

    assert (
        restart
        < session_list
        < selected
        < proof
        < evidence
        < round_count
        < round_terminal
        < fresh_messages
        < rejected
    )
    assert "$restartMessages" not in chat_block
    assert "$persisted.Contains($stopPrompt)" in chat_block
    assert "$persisted.Contains('PKV_W4_STOP_PARTIAL_V1')" in chat_block
    assert "$persisted.Contains($errorPrompt)" in chat_block
