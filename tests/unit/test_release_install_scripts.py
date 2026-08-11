"""W3 packaging-contract tests for per-user install and uninstall scripts."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from scripts import build_release


PROJECT_ROOT = Path(__file__).parents[2]
INSTALL_TEMPLATE = PROJECT_ROOT / "scripts" / "install" / "Install.ps1"
UNINSTALL_TEMPLATE = PROJECT_ROOT / "scripts" / "install" / "Uninstall.ps1"
COMPLIANCE_HOLD_TOKEN = "W4-TEST-CANDIDATE-NOT-FOR-DISTRIBUTION"
COMPLIANCE_BLOCKERS = [
    "conda-native-license-materials-and-spdx",
    "html2text-gpl-compliance",
    "native-msvc-license-and-provenance",
    "qt-corresponding-source-location",
    "qt-linkage-and-replacement-not-proven",
    "qt-module-license-audit",
    "qt-notice-placeholders",
]
pytestmark = [
    pytest.mark.packaging_contract,
    pytest.mark.windows_release_env,
    pytest.mark.skipif(os.name != "nt", reason="Windows installer contract"),
]


def _package(
    root: Path,
    *,
    build_info_overrides: dict[str, object] | None = None,
) -> Path:
    package = root / "package"
    (package / "app").mkdir(parents=True)
    (package / "app" / "pkv.exe").write_bytes(b"synthetic-installed-cli")
    shutil.copyfile(INSTALL_TEMPLATE, package / "Install.ps1")
    shutil.copyfile(UNINSTALL_TEMPLATE, package / "Uninstall.ps1")
    build_info: dict[str, object] = {
        "schema_version": "pkv.build-info.v1",
        "version": "0.8.1",
        "build_fingerprint": "a" * 64,
        "artifact_kind": "release",
        "artifact_status": "release",
        "release_eligible": True,
        "release_blockers": [],
    }
    if build_info_overrides:
        build_info.update(build_info_overrides)
    build_release.write_canonical_json(package / "build-info.json", build_info)
    manifest = build_release.generate_payload_manifest(package, "a" * 64)
    build_release.write_canonical_json(package / "payload-manifest.json", manifest)
    return package


def _candidate_package(root: Path) -> Path:
    return _package(
        root,
        build_info_overrides={
            "artifact_kind": "test_candidate",
            "artifact_status": "test-candidate-on-compliance-hold",
            "release_eligible": False,
            "release_blockers": COMPLIANCE_BLOCKERS,
        },
    )


def _candidate_install_arguments() -> list[str]:
    return [
        "-AllowComplianceHoldTestCandidate",
        "-ComplianceHoldConfirmation",
        COMPLIANCE_HOLD_TOKEN,
    ]


def _invoke(
    script: Path,
    *,
    local_app_data: Path,
    arguments: list[str] | None = None,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["LOCALAPPDATA"] = str(local_app_data)
    return subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            *(arguments or []),
        ],
        env=environment,
        check=False,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _start_invoke(
    script: Path,
    *,
    local_app_data: Path,
    arguments: list[str] | None = None,
) -> subprocess.Popen[str]:
    environment = os.environ.copy()
    environment["LOCALAPPDATA"] = str(local_app_data)
    return subprocess.Popen(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            *(arguments or []),
        ],
        env=environment,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _install_mutex_name(install_root: Path) -> str:
    normalized = str(install_root.resolve(strict=False)).rstrip("\\/").lower()
    suffix = hashlib.sha256(normalized.encode("utf-8")).hexdigest().upper()
    return f"Local\\PersonalKnowledgeVault-InstallRoot-{suffix}"


def _start_mutex_holder(name: str) -> subprocess.Popen[str]:
    escaped_name = name.replace("'", "''")
    command = (
        f"$mutex=[System.Threading.Mutex]::new($false,'{escaped_name}');"
        "$acquired=$false;"
        "try{$acquired=$mutex.WaitOne();"
        "[Console]::Out.WriteLine('mutex-ready');"
        "[Console]::Out.Flush();"
        "[void][Console]::In.ReadLine()}"
        "finally{if($acquired){[void]$mutex.ReleaseMutex()};$mutex.Dispose()}"
    )
    holder = subprocess.Popen(
        ["powershell.exe", "-NoProfile", "-Command", command],
        text=True,
        encoding="utf-8",
        errors="replace",
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert holder.stdout is not None
    ready = holder.stdout.readline().strip()
    if ready != "mutex-ready":
        _, stderr = holder.communicate(timeout=10)
        raise AssertionError(f"mutex holder failed: {ready!r}; {stderr}")
    return holder


def _start_exclusive_file_holder(path: Path) -> subprocess.Popen[str]:
    escaped_path = str(path).replace("'", "''")
    command = (
        f"$stream=[System.IO.File]::Open('{escaped_path}',"
        "[System.IO.FileMode]::Open,[System.IO.FileAccess]::ReadWrite,"
        "[System.IO.FileShare]::None);"
        "try{[Console]::Out.WriteLine('file-ready');"
        "[Console]::Out.Flush();[void][Console]::In.ReadLine()}"
        "finally{$stream.Dispose()}"
    )
    holder = subprocess.Popen(
        ["powershell.exe", "-NoProfile", "-Command", command],
        text=True,
        encoding="utf-8",
        errors="replace",
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert holder.stdout is not None
    ready = holder.stdout.readline().strip()
    if ready != "file-ready":
        _, stderr = holder.communicate(timeout=10)
        raise AssertionError(f"file holder failed: {ready!r}; {stderr}")
    return holder


def _release_holder(holder: subprocess.Popen[str]) -> None:
    if holder.poll() is not None:
        assert holder.stderr is not None
        raise AssertionError(
            f"mutex holder exited early ({holder.returncode}): {holder.stderr.read()}"
        )
    assert holder.stdin is not None
    holder.stdin.write("\n")
    holder.stdin.flush()
    holder.stdin.close()
    holder.wait(timeout=10)
    assert holder.returncode == 0


def test_fresh_install_and_same_version_are_noninteractive_and_idempotent(
    tmp_path: Path,
) -> None:
    package = _package(tmp_path)
    local_app_data = tmp_path / "profile" / "AppData" / "Local"
    install_root = local_app_data / "Programs" / "PersonalKnowledgeVault"
    data_root = local_app_data / "PersonalKnowledgeVault"

    first = _invoke(package / "Install.ps1", local_app_data=local_app_data)
    second = _invoke(package / "Install.ps1", local_app_data=local_app_data)

    assert first.returncode == 0, first.stderr
    first_result = json.loads(first.stdout)
    assert first_result["status"] == "installed"
    assert first_result["artifact_kind"] == "release"
    assert first_result["artifact_status"] == "release"
    assert first_result["release_eligible"] is True
    assert first_result["release_blockers"] == []
    assert first_result["compliance_hold"] is False
    assert second.returncode == 0, second.stderr
    assert json.loads(second.stdout)["status"] == "already_installed"
    assert (install_root / "app" / "pkv.exe").read_bytes() == b"synthetic-installed-cli"
    assert not data_root.exists()


def test_compliance_hold_candidate_is_default_deny_and_requires_w4_token(
    tmp_path: Path,
) -> None:
    package = _candidate_package(tmp_path)
    local_app_data = tmp_path / "profile" / "AppData" / "Local"
    install_root = local_app_data / "Programs" / "PersonalKnowledgeVault"

    denied = _invoke(package / "Install.ps1", local_app_data=local_app_data)
    switch_only = _invoke(
        package / "Install.ps1",
        local_app_data=local_app_data,
        arguments=["-AllowComplianceHoldTestCandidate"],
    )
    wrong_token = _invoke(
        package / "Install.ps1",
        local_app_data=local_app_data,
        arguments=[
            "-AllowComplianceHoldTestCandidate",
            "-ComplianceHoldConfirmation",
            "NOT-THE-W4-TOKEN",
        ],
    )

    assert denied.returncode == 1
    assert switch_only.returncode == 1
    assert wrong_token.returncode == 1
    assert "not installable by default" in denied.stderr
    assert not install_root.exists()

    accepted = _invoke(
        package / "Install.ps1",
        local_app_data=local_app_data,
        arguments=_candidate_install_arguments(),
    )

    assert accepted.returncode == 0, accepted.stderr
    result = json.loads(accepted.stdout)
    assert result["status"] == "installed"
    assert result["artifact_kind"] == "test_candidate"
    assert result["artifact_status"] == "test-candidate-on-compliance-hold"
    assert result["release_eligible"] is False
    assert result["release_blockers"] == COMPLIANCE_BLOCKERS
    assert result["compliance_hold"] is True

    repeated = _invoke(
        package / "Install.ps1",
        local_app_data=local_app_data,
        arguments=_candidate_install_arguments(),
    )
    assert repeated.returncode == 0, repeated.stderr
    repeated_result = json.loads(repeated.stdout)
    assert repeated_result["status"] == "already_installed"
    assert repeated_result["compliance_hold"] is True


@pytest.mark.parametrize(
    "overrides",
    [
        {"artifact_kind": "test_candidate"},
        {"artifact_status": "test-candidate-on-compliance-hold"},
        {"release_blockers": ["unexpected-blocker"]},
        {"release_blockers": None},
        {"release_eligible": "true"},
    ],
)
def test_inconsistent_release_eligibility_tuple_is_rejected(
    tmp_path: Path,
    overrides: dict[str, object],
) -> None:
    package = _package(tmp_path, build_info_overrides=overrides)
    local_app_data = tmp_path / "profile" / "AppData" / "Local"

    result = _invoke(package / "Install.ps1", local_app_data=local_app_data)

    assert result.returncode == 1
    assert "build-info" in result.stderr
    assert not (local_app_data / "Programs" / "PersonalKnowledgeVault").exists()


def test_release_artifact_rejects_candidate_override_arguments(tmp_path: Path) -> None:
    package = _package(tmp_path)
    local_app_data = tmp_path / "profile" / "AppData" / "Local"

    result = _invoke(
        package / "Install.ps1",
        local_app_data=local_app_data,
        arguments=_candidate_install_arguments(),
    )

    assert result.returncode == 1
    assert "forbidden for a release Artifact" in result.stderr


def test_concurrent_installers_are_atomic_and_one_is_idempotent(tmp_path: Path) -> None:
    package = _package(tmp_path)
    local_app_data = tmp_path / "profile" / "AppData" / "Local"
    install_root = local_app_data / "Programs" / "PersonalKnowledgeVault"

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(
                _invoke,
                package / "Install.ps1",
                local_app_data=local_app_data,
            )
            for _ in range(2)
        ]
    results = [future.result() for future in futures]

    assert all(result.returncode == 0 for result in results), [
        result.stderr for result in results
    ]
    assert sorted(json.loads(result.stdout)["status"] for result in results) == [
        "already_installed",
        "installed",
    ]
    assert not list((local_app_data / "Programs").glob(".pkv-install-*"))
    assert (install_root / "install-state.json").is_file()


def test_install_and_uninstall_share_one_root_mutex_critical_section() -> None:
    install_source = INSTALL_TEMPLATE.read_text(encoding="utf-8")
    uninstall_source = UNINSTALL_TEMPLATE.read_text(encoding="utf-8")
    prefix_contract = (
        '$InstallMutexPrefix = "Local\\PersonalKnowledgeVault-InstallRoot-"'
    )

    assert install_source.count(prefix_contract) == 1
    assert uninstall_source.count(prefix_contract) == 1
    assert "$installMutex = New-InstallRootMutex $InstallRoot" in install_source
    assert "$uninstallMutex = New-InstallRootMutex $InstallRoot" in uninstall_source
    assert (
        '$UserDataMutexPrefix = "Local\\PersonalKnowledgeVault-UserDataRoot-"'
        in uninstall_source
    )
    assert "$dataMutex = New-UserDataRootMutex $dataMutexRoot" in uninstall_source

    install_critical = install_source[install_source.index(".WaitOne(") :]
    uninstall_critical = uninstall_source[uninstall_source.index(".WaitOne(") :]
    assert install_critical.index("Assert-ContainedPath") < install_critical.index(
        "Complete-ExistingInstall"
    )
    assert install_critical.index("Complete-ExistingInstall") < install_critical.index(
        "ReleaseMutex"
    )
    assert uninstall_critical.index("Assert-ContainedPath") < uninstall_critical.index(
        "Assert-InstalledPayload"
    )
    assert uninstall_critical.index(
        "Assert-InstalledPayload"
    ) < uninstall_critical.index(
        "[System.IO.Directory]::Move($InstallRoot, $programTombstone)"
    )
    assert uninstall_critical.index(
        "[System.IO.Directory]::Move($InstallRoot, $programTombstone)"
    ) < uninstall_critical.index("ReleaseMutex")
    assert uninstall_critical.index(
        "[System.IO.Directory]::Move($InstallRoot, $programTombstone)"
    ) < uninstall_critical.index(
        "[System.IO.Directory]::Move($stateDataRoot, $dataTombstone)"
    )
    assert uninstall_critical.index(
        "[System.IO.Directory]::Move($stateDataRoot, $dataTombstone)"
    ) < uninstall_critical.index("Remove-Item -LiteralPath $programTombstone")
    assert (
        "[System.IO.Directory]::Move($programTombstone, $InstallRoot)"
        in uninstall_critical
    )
    assert "Move-Item -LiteralPath $InstallRoot" not in uninstall_source


def test_install_and_uninstall_wait_on_the_same_named_mutex(tmp_path: Path) -> None:
    package = _package(tmp_path)
    local_app_data = tmp_path / "profile" / "AppData" / "Local"
    install_root = local_app_data / "Programs" / "PersonalKnowledgeVault"
    installed = _invoke(package / "Install.ps1", local_app_data=local_app_data)
    assert installed.returncode == 0, installed.stderr
    mutex_name = _install_mutex_name(install_root)

    holder = _start_mutex_holder(mutex_name)
    install_process = _start_invoke(
        package / "Install.ps1",
        local_app_data=local_app_data,
    )
    try:
        time.sleep(1.0)
        assert install_process.poll() is None, install_process.stderr.read()
    finally:
        _release_holder(holder)
    install_stdout, install_stderr = install_process.communicate(timeout=30)
    assert install_process.returncode == 0, install_stderr
    assert json.loads(install_stdout)["status"] == "already_installed"

    holder = _start_mutex_holder(mutex_name)
    uninstall_process = _start_invoke(
        package / "Uninstall.ps1",
        local_app_data=local_app_data,
    )
    try:
        time.sleep(1.0)
        assert uninstall_process.poll() is None, uninstall_process.stderr.read()
    finally:
        _release_holder(holder)
    uninstall_stdout, uninstall_stderr = uninstall_process.communicate(timeout=30)
    assert uninstall_process.returncode == 0, uninstall_stderr
    assert json.loads(uninstall_stdout)["status"] == "uninstalled"
    assert not install_root.exists()


def test_concurrent_install_and_uninstall_have_a_serializable_final_state(
    tmp_path: Path,
) -> None:
    package = _package(tmp_path)
    local_app_data = tmp_path / "profile" / "AppData" / "Local"
    programs_root = local_app_data / "Programs"
    install_root = programs_root / "PersonalKnowledgeVault"
    installed = _invoke(package / "Install.ps1", local_app_data=local_app_data)
    assert installed.returncode == 0, installed.stderr

    with ThreadPoolExecutor(max_workers=2) as pool:
        install_future = pool.submit(
            _invoke,
            package / "Install.ps1",
            local_app_data=local_app_data,
        )
        uninstall_future = pool.submit(
            _invoke,
            package / "Uninstall.ps1",
            local_app_data=local_app_data,
        )
    install_result = install_future.result()
    uninstall_result = uninstall_future.result()

    assert install_result.returncode == 0, install_result.stderr
    assert uninstall_result.returncode == 0, uninstall_result.stderr
    install_status = json.loads(install_result.stdout)["status"]
    assert json.loads(uninstall_result.stdout)["status"] == "uninstalled"
    if install_status == "installed":
        assert (install_root / "install-state.json").is_file()
        assert (install_root / "app" / "pkv.exe").is_file()
    else:
        assert install_status == "already_installed"
        assert not install_root.exists()
    assert not list(programs_root.glob(".pkv-install-*"))
    assert not list(programs_root.glob(".pkv-uninstall-*"))


def test_custom_install_roots_serialize_shared_user_data_deletion(
    tmp_path: Path,
) -> None:
    package = _package(tmp_path)
    local_app_data = tmp_path / "profile" / "AppData" / "Local"
    programs_root = local_app_data / "Programs"
    install_roots = [programs_root / "PKV-A", programs_root / "PKV-B"]
    for install_root in install_roots:
        installed = _invoke(
            package / "Install.ps1",
            local_app_data=local_app_data,
            arguments=["-InstallRoot", str(install_root)],
        )
        assert installed.returncode == 0, installed.stderr
    data_root = local_app_data / "PersonalKnowledgeVault"
    data_root.mkdir(parents=True)
    (data_root / "shared.txt").write_text("shared", encoding="utf-8")

    def uninstall_arguments(install_root: Path) -> list[str]:
        return [
            "-InstallRoot",
            str(install_root),
            "-DeleteUserData",
            "-ConfirmDataDeletion",
            "DELETE-PKV-USER-DATA",
        ]

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(
                _invoke,
                package / "Uninstall.ps1",
                local_app_data=local_app_data,
                arguments=uninstall_arguments(install_root),
            )
            for install_root in install_roots
        ]
    results = [future.result() for future in futures]

    assert all(result.returncode == 0 for result in results), [
        result.stderr for result in results
    ]
    assert all(
        json.loads(result.stdout)["user_data"] == "deleted" for result in results
    )
    assert not data_root.exists()
    assert all(not install_root.exists() for install_root in install_roots)
    assert not list(programs_root.glob(".pkv-uninstall-*"))
    assert not list(local_app_data.glob(".pkv-data-delete-*"))


def test_cross_version_install_is_rejected_without_payload_mutation(
    tmp_path: Path,
) -> None:
    package = _package(tmp_path)
    local_app_data = tmp_path / "profile" / "AppData" / "Local"
    install_root = local_app_data / "Programs" / "PersonalKnowledgeVault"
    installed = _invoke(package / "Install.ps1", local_app_data=local_app_data)
    assert installed.returncode == 0, installed.stderr
    state_path = install_root / "install-state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["version"] = "0.8.0"
    state_path.write_text(json.dumps(state), encoding="utf-8")
    before = (install_root / "app" / "pkv.exe").read_bytes()

    result = _invoke(package / "Install.ps1", local_app_data=local_app_data)

    assert result.returncode == 20
    assert json.loads(result.stdout)["status"] == "upgrade_unsupported"
    assert (install_root / "app" / "pkv.exe").read_bytes() == before


def test_uninstall_retains_user_data_by_default(tmp_path: Path) -> None:
    package = _package(tmp_path)
    local_app_data = tmp_path / "profile" / "AppData" / "Local"
    install_root = local_app_data / "Programs" / "PersonalKnowledgeVault"
    data_root = local_app_data / "PersonalKnowledgeVault"
    installed = _invoke(package / "Install.ps1", local_app_data=local_app_data)
    assert installed.returncode == 0, installed.stderr
    data_root.mkdir(parents=True)
    (data_root / "keep.txt").write_text("keep", encoding="utf-8")

    # Exercise the installed entrypoint itself; it must leave its cwd and be
    # able to detach/remove the directory containing the running script.
    result = _invoke(install_root / "Uninstall.ps1", local_app_data=local_app_data)

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["user_data"] == "retained"
    assert not install_root.exists()
    assert (data_root / "keep.txt").read_text(encoding="utf-8") == "keep"


def test_user_data_delete_requires_switch_and_exact_confirmation(
    tmp_path: Path,
) -> None:
    package = _package(tmp_path)
    local_app_data = tmp_path / "profile" / "AppData" / "Local"
    install_root = local_app_data / "Programs" / "PersonalKnowledgeVault"
    data_root = local_app_data / "PersonalKnowledgeVault"
    neighbour = local_app_data / "PersonalKnowledgeVault-neighbour"
    installed = _invoke(package / "Install.ps1", local_app_data=local_app_data)
    assert installed.returncode == 0, installed.stderr
    data_root.mkdir(parents=True)
    (data_root / "delete.txt").write_text("delete", encoding="utf-8")
    neighbour.mkdir()
    (neighbour / "keep.txt").write_text("keep", encoding="utf-8")

    denied = _invoke(
        package / "Uninstall.ps1",
        local_app_data=local_app_data,
        arguments=["-DeleteUserData"],
    )
    assert denied.returncode == 1
    assert install_root.exists()
    assert data_root.exists()

    deleted = _invoke(
        package / "Uninstall.ps1",
        local_app_data=local_app_data,
        arguments=[
            "-DeleteUserData",
            "-ConfirmDataDeletion",
            "DELETE-PKV-USER-DATA",
        ],
    )

    assert deleted.returncode == 0, deleted.stderr
    assert json.loads(deleted.stdout)["user_data"] == "deleted"
    assert not install_root.exists()
    assert not data_root.exists()
    assert (neighbour / "keep.txt").read_text(encoding="utf-8") == "keep"


def test_user_data_detach_failure_rolls_program_root_back_before_error(
    tmp_path: Path,
) -> None:
    package = _package(tmp_path)
    local_app_data = tmp_path / "profile" / "AppData" / "Local"
    programs_root = local_app_data / "Programs"
    install_root = programs_root / "PersonalKnowledgeVault"
    data_root = local_app_data / "PersonalKnowledgeVault"
    installed = _invoke(package / "Install.ps1", local_app_data=local_app_data)
    assert installed.returncode == 0, installed.stderr
    data_root.mkdir(parents=True)
    locked_file = data_root / "locked.txt"
    locked_file.write_text("locked", encoding="utf-8")

    holder = _start_exclusive_file_holder(locked_file)
    try:
        failed = _invoke(
            package / "Uninstall.ps1",
            local_app_data=local_app_data,
            arguments=[
                "-DeleteUserData",
                "-ConfirmDataDeletion",
                "DELETE-PKV-USER-DATA",
            ],
        )
    finally:
        _release_holder(holder)

    assert failed.returncode == 1
    assert "program rollback completed" in failed.stderr
    assert (install_root / "install-state.json").is_file()
    assert (install_root / "app" / "pkv.exe").is_file()
    assert locked_file.is_file()
    assert not list(programs_root.glob(".pkv-uninstall-*"))
    assert not list(local_app_data.glob(".pkv-data-delete-*"))

    completed = _invoke(
        package / "Uninstall.ps1",
        local_app_data=local_app_data,
        arguments=[
            "-DeleteUserData",
            "-ConfirmDataDeletion",
            "DELETE-PKV-USER-DATA",
        ],
    )
    assert completed.returncode == 0, completed.stderr


def test_install_root_outside_per_user_programs_is_rejected(tmp_path: Path) -> None:
    package = _package(tmp_path)
    local_app_data = tmp_path / "profile" / "AppData" / "Local"
    outside = tmp_path / "outside" / "PersonalKnowledgeVault"

    result = _invoke(
        package / "Install.ps1",
        local_app_data=local_app_data,
        arguments=["-InstallRoot", str(outside)],
    )

    assert result.returncode == 1
    assert "must be a child" in result.stderr
    assert not outside.exists()
