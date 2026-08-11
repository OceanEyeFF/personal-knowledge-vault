"""Focused runtime-safety contracts for the artifact-only W4 controller."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest


pytestmark = pytest.mark.artifact

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
OUTER_RUNNER = REPOSITORY_ROOT / "scripts" / "run-artifact-e2e.ps1"
DRIVER_MODULE = REPOSITORY_ROOT / "packaging" / "w4_driver" / "W4.Driver.psm1"
SCENARIO_MODULE = (
    REPOSITORY_ROOT / "packaging" / "w4_driver" / "W4.Scenarios.psm1"
)
CONTROLLER = (
    REPOSITORY_ROOT / "packaging" / "w4_driver" / "Invoke-W4ArtifactE2E.ps1"
)
SEMANTIC_BUNDLE = (
    REPOSITORY_ROOT / "tests" / "fixtures" / "w4" / "semantic-vector-index.v1"
)


def _windows_powershell() -> Path:
    system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    executable = (
        system_root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    )
    assert executable.is_file()
    return executable


def _ps_quote(value: Path | str) -> str:
    return str(value).replace("'", "''")


def _run_ps(command: str, *, timeout: int = 60) -> subprocess.CompletedProcess[str]:
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
        timeout=timeout,
        check=False,
    )


def _create_junction(junction: Path, target: Path) -> None:
    command_processor = shutil.which("cmd.exe")
    assert command_processor is not None
    result = subprocess.run(
        [command_processor, "/d", "/c", "mklink", "/J", str(junction), str(target)],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=10,
        check=False,
    )
    assert result.returncode == 0 and junction.exists(), result.stderr


def _remove_junction(junction: Path) -> None:
    if not junction.exists():
        return
    command_processor = shutil.which("cmd.exe")
    assert command_processor is not None
    result = subprocess.run(
        [command_processor, "/d", "/c", "rmdir", str(junction)],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=10,
        check=False,
    )
    assert result.returncode == 0 and not junction.exists(), result.stderr


def test_redirected_stdin_is_utf8_without_bom_on_both_start_paths(
    tmp_path: Path,
) -> None:
    probe = tmp_path / "raw-stdin-probe.ps1"
    probe.write_text(
        """
$stream = [Console]::OpenStandardInput()
$bytes = [System.Collections.Generic.List[byte]]::new()
while (($value = $stream.ReadByte()) -ge 0) { $bytes.Add([byte]$value) }
[Console]::Out.Write(([BitConverter]::ToString($bytes.ToArray())).Replace('-', ''))
[Console]::Error.Write('raw-probe-stderr')
""".strip()
        + "\n",
        encoding="utf-8",
    )
    evidence = tmp_path / "invoke-evidence"
    input_text = '{"jsonrpc":"2.0"}'
    expected_hex = input_text.encode("utf-8").hex().upper()
    command = (
        "$ErrorActionPreference='Stop';"
        f"Import-Module '{_ps_quote(DRIVER_MODULE)}' -Force;"
        "$environment=@{};"
        "[Environment]::GetEnvironmentVariables().GetEnumerator()|ForEach-Object{"
        "$environment[[string]$_.Key]=[string]$_.Value};"
        "$original=[Console]::InputEncoding;"
        "try {"
        "[Console]::InputEncoding=[Text.Encoding]::UTF8;"
        f"$inputText='{input_text}';$expected='{expected_hex}';"
        f"$hostPath='{_ps_quote(_windows_powershell())}';"
        f"$probe='{_ps_quote(probe)}';"
        f"$evidence='{_ps_quote(evidence)}';"
        "$result=Invoke-W4Process -FileName $hostPath -Arguments @("
        "'-NoLogo','-NoProfile','-NonInteractive','-File',$probe) "
        f"-WorkingDirectory '{_ps_quote(tmp_path)}' -Environment $environment "
        "-EvidenceDirectory $evidence -StandardInput $inputText;"
        "if($result.StandardOutput -cne $expected){throw "
        "\"Invoke-W4Process stdin bytes=$($result.StandardOutput)\"};"
        "if([IO.File]::ReadAllText((Join-Path $evidence 'stderr.txt')) -cne "
        "'raw-probe-stderr'){throw 'Invoke-W4Process stderr was not saved'};"
        "if([Console]::InputEncoding.GetPreamble().Length -ne 3){throw "
        "'Invoke-W4Process did not restore ambient input encoding'};"
        "$process=Start-W4LongRunningProcess -FileName $hostPath -Arguments @("
        "'-NoLogo','-NoProfile','-NonInteractive','-File',$probe) "
        f"-WorkingDirectory '{_ps_quote(tmp_path)}' -Environment $environment "
        "-RedirectInput;"
        "$stdoutTask=$process.StandardOutput.ReadToEndAsync();"
        "$stderrTask=$process.StandardError.ReadToEndAsync();"
        "$process.StandardInput.Write($inputText);$process.StandardInput.Close();"
        "if(-not $process.WaitForExit(10000)){Stop-W4ProcessTree -Process $process;"
        "throw 'long-running probe timed out'};$process.WaitForExit();"
        "$stdout=$stdoutTask.GetAwaiter().GetResult();"
        "$stderr=$stderrTask.GetAwaiter().GetResult();"
        "if($stdout -cne $expected){throw "
        "\"Start-W4LongRunningProcess stdin bytes=$stdout\"};"
        "if($stderr -cne 'raw-probe-stderr'){throw "
        "'long-running stderr was not drainable'};"
        "if([Console]::InputEncoding.GetPreamble().Length -ne 3){throw "
        "'long-running start did not restore ambient input encoding'};"
        "$process.Dispose();'UTF8_NO_BOM_OK'"
        "} finally {[Console]::InputEncoding=$original}"
    )

    result = _run_ps(command)

    assert result.returncode == 0, result.stderr
    assert "UTF8_NO_BOM_OK" in result.stdout


def test_harness_runtime_pid_must_be_launcher_or_real_descendant() -> None:
    command = (
        "$ErrorActionPreference='Stop';"
        f"Import-Module '{_ps_quote(DRIVER_MODULE)}' -Force;"
        f"$module=Import-Module '{_ps_quote(SCENARIO_MODULE)}' -Force -PassThru;"
        "$hostPath=(Get-Process -Id $PID).Path;"
        "$child1=Start-Process -FilePath $hostPath -ArgumentList @("
        "'-NoLogo','-NoProfile','-NonInteractive','-Command',"
        "'Start-Sleep -Seconds 30') -WindowStyle Hidden -PassThru;"
        "$child2=Start-Process -FilePath $hostPath -ArgumentList @("
        "'-NoLogo','-NoProfile','-NonInteractive','-Command',"
        "'Start-Sleep -Seconds 30') -WindowStyle Hidden -PassThru;"
        "try {"
        "$launcher=Get-Process -Id $PID;"
        "$self=& $module {param($p,$id) Get-W4ValidatedProcessIdentity "
        "-LauncherProcess $p -RuntimeProcessId $id} $launcher $PID;"
        "if(-not $self.RuntimeIsLauncher -or $self.AncestryPids.Count -ne 1){"
        "throw 'launcher self identity was rejected'};"
        "$child=& $module {param($p,$id) Get-W4ValidatedProcessIdentity "
        "-LauncherProcess $p -RuntimeProcessId $id} $launcher $child1.Id;"
        "if($child.RuntimeIsLauncher -or $child.AncestryPids[0] -ne $child1.Id -or "
        "$child.AncestryPids[-1] -ne $PID){throw 'child ancestry was not exact'};"
        "$rejected=$false;try {& $module {param($p,$id) "
        "Get-W4ValidatedProcessIdentity -LauncherProcess $p -RuntimeProcessId $id} "
        "$child1 $child2.Id|Out-Null}catch{$rejected=$true};"
        "if(-not $rejected){throw 'unrelated runtime pid was accepted'};"
        "if($child.RuntimeProcess.Id -ne $PID){$child.RuntimeProcess.Dispose()};"
        "'PROCESS_TREE_OK'"
        "} finally {foreach($p in @($child1,$child2)){if(-not $p.HasExited){"
        "$p.Kill()};$p.Dispose()}}"
    )

    result = _run_ps(command)

    assert result.returncode == 0, result.stderr
    assert "PROCESS_TREE_OK" in result.stdout


def test_process_tree_stop_accepts_launcher_exit_race_only_after_child_is_gone(
    tmp_path: Path,
) -> None:
    child_pid_file = tmp_path / "child.pid"
    exit_request = tmp_path / "exit.request"
    launcher_script = tmp_path / "short-lived-launcher.ps1"
    launcher_script.write_text(
        (
            "$hostPath=(Get-Process -Id $PID).Path\n"
            "$child=Start-Process -FilePath $hostPath -ArgumentList @("
            "'-NoLogo','-NoProfile','-NonInteractive','-Command',"
            "'Start-Sleep -Seconds 30') -WindowStyle Hidden -PassThru\n"
            f"[IO.File]::WriteAllText('{_ps_quote(child_pid_file)}',"
            "$child.Id.ToString(),[Text.UTF8Encoding]::new($false))\n"
            f"while(-not(Test-Path -LiteralPath '{_ps_quote(exit_request)}' "
            "-PathType Leaf)){[Threading.Thread]::Sleep(20)}\n"
        ),
        encoding="utf-8",
    )
    command = (
        "$ErrorActionPreference='Stop';"
        f"Import-Module '{_ps_quote(DRIVER_MODULE)}' -Force;"
        "$hostPath=(Get-Process -Id $PID).Path;"
        "$launcher=Start-Process -FilePath $hostPath -ArgumentList @("
        "'-NoLogo','-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass',"
        f"'-File','{_ps_quote(launcher_script)}') -WindowStyle Hidden -PassThru;"
        f"$pidFile='{_ps_quote(child_pid_file)}';"
        "$deadline=[DateTime]::UtcNow.AddSeconds(5);"
        "while(-not(Test-Path -LiteralPath $pidFile -PathType Leaf)){"
        "if([DateTime]::UtcNow -ge $deadline){throw 'child pid was not published'};"
        "[Threading.Thread]::Sleep(20)};"
        "$childPid=[int][IO.File]::ReadAllText($pidFile);"
        "if($null -eq (Get-Process -Id $childPid -ErrorAction SilentlyContinue)){"
        "throw 'child exited before stop regression'};"
        "$snapshot=New-W4ProcessTreeIdentitySnapshot -Process $launcher;"
        f"[IO.File]::WriteAllText('{_ps_quote(exit_request)}','exit',"
        "[Text.UTF8Encoding]::new($false));"
        "if(-not $launcher.WaitForExit(10000)){throw 'launcher did not exit'};"
        "Stop-W4ProcessTree -Process $launcher -IdentitySnapshot $snapshot;"
        "if($null -ne (Get-Process -Id $childPid -ErrorAction SilentlyContinue)){"
        "throw 'snapshotted child survived process-tree stop'};"
        "$launcher.Dispose();'PROCESS_EXIT_RACE_OK'"
    )

    result = _run_ps(command, timeout=30)

    assert result.returncode == 0, result.stderr
    assert "PROCESS_EXIT_RACE_OK" in result.stdout


def test_process_tree_stop_rejects_exited_root_without_prior_snapshot(
    tmp_path: Path,
) -> None:
    child_pid_file = tmp_path / "untrusted-child.pid"
    launcher_script = tmp_path / "exited-launcher.ps1"
    launcher_script.write_text(
        (
            "$hostPath=(Get-Process -Id $PID).Path\n"
            "$child=Start-Process -FilePath $hostPath -ArgumentList @("
            "'-NoLogo','-NoProfile','-NonInteractive','-Command',"
            "'Start-Sleep -Seconds 30') -WindowStyle Hidden -PassThru\n"
            f"[IO.File]::WriteAllText('{_ps_quote(child_pid_file)}',"
            "$child.Id.ToString(),[Text.UTF8Encoding]::new($false))\n"
        ),
        encoding="utf-8",
    )
    command = (
        "$ErrorActionPreference='Stop';"
        f"Import-Module '{_ps_quote(DRIVER_MODULE)}' -Force;"
        "$hostPath=(Get-Process -Id $PID).Path;"
        "$launcher=Start-Process -FilePath $hostPath -ArgumentList @("
        "'-NoLogo','-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass',"
        f"'-File','{_ps_quote(launcher_script)}') -WindowStyle Hidden -PassThru;"
        "if(-not $launcher.WaitForExit(10000)){throw 'launcher did not exit'};"
        f"$childPid=[int][IO.File]::ReadAllText('{_ps_quote(child_pid_file)}');"
        "$rejected=$false;try{Stop-W4ProcessTree -Process $launcher}catch{"
        "$rejected=$_.Exception.Message -like '*exited before its identity snapshot*'};"
        "try{if(-not $rejected){throw 'exited root was not rejected'};"
        "if($null -eq(Get-Process -Id $childPid -ErrorAction SilentlyContinue)){"
        "throw 'untrusted descendant was killed through a stale root PID'};"
        "'EXITED_ROOT_REJECTED'}finally{"
        "$child=Get-Process -Id $childPid -ErrorAction SilentlyContinue;"
        "if($null -ne $child){$child.Kill();$child.Dispose()};$launcher.Dispose()}"
    )

    result = _run_ps(command, timeout=30)

    assert result.returncode == 0, result.stderr
    assert "EXITED_ROOT_REJECTED" in result.stdout


def test_process_tree_snapshot_rejects_wrong_root_start_tick_without_killing() -> None:
    command = (
        "$ErrorActionPreference='Stop';"
        f"Import-Module '{_ps_quote(DRIVER_MODULE)}' -Force;"
        "$hostPath=(Get-Process -Id $PID).Path;"
        "$root=Start-Process -FilePath $hostPath -ArgumentList @("
        "'-NoLogo','-NoProfile','-NonInteractive','-Command',"
        "'Start-Sleep -Seconds 30') -WindowStyle Hidden -PassThru;"
        "try{$valid=New-W4ProcessTreeIdentitySnapshot -Process $root;"
        "$forged=[pscustomobject]@{RootProcessId=$valid.RootProcessId;"
        "RootStartTimeUtcTicks=([int64]$valid.RootStartTimeUtcTicks+1);"
        "Identities=$valid.Identities};$rejected=$false;"
        "try{Stop-W4ProcessTree -Process $root -IdentitySnapshot $forged}catch{"
        "$rejected=$_.Exception.Message -like '*does not bind the supplied root*'};"
        "if(-not $rejected){throw 'wrong root start tick was accepted'};"
        "if($root.HasExited){throw 'wrong root identity killed the live process'};"
        "Stop-W4ProcessTree -Process $root -IdentitySnapshot $valid;"
        "'REUSED_PID_IDENTITY_REJECTED'}finally{"
        "if(-not $root.HasExited){$root.Kill()};$root.Dispose()}"
    )

    result = _run_ps(command, timeout=30)

    assert result.returncode == 0, result.stderr
    assert "REUSED_PID_IDENTITY_REJECTED" in result.stdout


def test_scenario_cleanup_removes_only_exact_install_root(tmp_path: Path) -> None:
    workspace = tmp_path / "scenario-workspace"
    install = (
        workspace
        / "profile"
        / "AppData"
        / "Local"
        / "Programs"
        / "PersonalKnowledgeVault"
    )
    user_data = workspace / "user-data"
    evidence = tmp_path / "scenario-evidence"
    install.mkdir(parents=True)
    user_data.mkdir(parents=True)
    evidence.mkdir(parents=True)
    (install / "installed.txt").write_text("installed\n", encoding="utf-8")
    sentinel = user_data / "preserve.txt"
    sentinel.write_text("preserve\n", encoding="utf-8")
    command = (
        "$ErrorActionPreference='Stop';"
        f"Import-Module '{_ps_quote(DRIVER_MODULE)}' -Force;"
        f"$module=Import-Module '{_ps_quote(SCENARIO_MODULE)}' -Force -PassThru;"
        "$context=[pscustomobject]@{"
        f"Workspace='{_ps_quote(workspace)}';InstallRoot='{_ps_quote(install)}';"
        f"UserDataRoot='{_ps_quote(user_data)}';Evidence='{_ps_quote(evidence)}'"
        "};& $module {param($context) Remove-W4ScenarioInstallRoot "
        "-ScenarioContext $context} $context;"
        f"if(Test-Path -LiteralPath '{_ps_quote(install)}'){{throw "
        "'install root remained'};"
        f"if(-not(Test-Path -LiteralPath '{_ps_quote(sentinel)}' -PathType Leaf)){{"
        "throw 'user data was removed'};"
        f"$record=[IO.File]::ReadAllText('{_ps_quote(evidence / 'install-cleanup.json')}')"
        "|ConvertFrom-Json;"
        "if($record.status -cne 'removed' -or $record.removed_file_count -ne 1 -or "
        "-not $record.user_data_preserved_by_cleanup -or "
        "-not $record.evidence_preserved_by_cleanup){throw 'cleanup record invalid'};"
        "'INSTALL_CLEANUP_OK'"
    )

    result = _run_ps(command)

    assert result.returncode == 0, result.stderr
    assert "INSTALL_CLEANUP_OK" in result.stdout
    assert sentinel.is_file()


def test_known_windows_cache_cleanup_unlinks_only_exact_mount_point(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "scenario-workspace"
    evidence = tmp_path / "scenario-evidence"
    cache_root = (
        workspace
        / "profile"
        / "AppData"
        / "Local"
        / "Microsoft"
        / "Windows"
        / "INetCache"
    )
    target = cache_root / "IE"
    target.mkdir(parents=True)
    evidence.mkdir()
    sentinel = target / "container.dat"
    sentinel.write_bytes(b"known-cache-target\n")
    junction = cache_root / "Content.IE5"
    _create_junction(junction, target)
    command = (
        "$ErrorActionPreference='Stop';"
        f"Import-Module '{_ps_quote(DRIVER_MODULE)}' -Force;"
        f"$module=Import-Module '{_ps_quote(SCENARIO_MODULE)}' -Force -PassThru;"
        "$context=[pscustomobject]@{"
        f"Workspace='{_ps_quote(workspace)}';Evidence='{_ps_quote(evidence)}'"
        "};& $module {param($context) Remove-W4KnownWindowsInternetCacheJunction "
        "-ScenarioContext $context} $context;'CACHE_JUNCTION_OK'"
    )

    result = _run_ps(command)

    assert result.returncode == 0, result.stderr
    assert "CACHE_JUNCTION_OK" in result.stdout
    assert not junction.exists()
    assert sentinel.read_bytes() == b"known-cache-target\n"
    record = json.loads((evidence / "windows-cache-cleanup.json").read_text("utf-8"))
    assert record == {
        "schema_version": "pkv.m13.w4-windows-cache-cleanup.v1",
        "status": "removed_known_junction",
        "alias_relative_path": (
            "profile/AppData/Local/Microsoft/Windows/INetCache/Content.IE5"
        ),
        "target_relative_path": "profile/AppData/Local/Microsoft/Windows/INetCache/IE",
        "reparse_tag": "0xa0000003",
        "substitute_name": "\\??\\" + str(target),
        "target_preserved": True,
        "cache_tree_sha256_after": record["cache_tree_sha256_after"],
    }
    assert len(record["cache_tree_sha256_after"]) == 64


@pytest.mark.parametrize(
    ("mode", "expected_status"),
    [("absent", "already_absent"), ("ordinary", "ordinary_directory_preserved")],
)
def test_known_windows_cache_cleanup_preserves_safe_nonjunction_states(
    tmp_path: Path,
    mode: str,
    expected_status: str,
) -> None:
    workspace = tmp_path / "scenario-workspace"
    evidence = tmp_path / "scenario-evidence"
    cache_root = (
        workspace
        / "profile"
        / "AppData"
        / "Local"
        / "Microsoft"
        / "Windows"
        / "INetCache"
    )
    evidence.mkdir()
    sentinel: Path | None = None
    if mode == "ordinary":
        ordinary = cache_root / "Content.IE5"
        ordinary.mkdir(parents=True)
        sentinel = ordinary / "preserve.txt"
        sentinel.write_text("preserve\n", encoding="utf-8")
    else:
        (workspace / "profile").mkdir(parents=True)
    command = (
        "$ErrorActionPreference='Stop';"
        f"Import-Module '{_ps_quote(DRIVER_MODULE)}' -Force;"
        f"$module=Import-Module '{_ps_quote(SCENARIO_MODULE)}' -Force -PassThru;"
        "$context=[pscustomobject]@{"
        f"Workspace='{_ps_quote(workspace)}';Evidence='{_ps_quote(evidence)}'"
        "};& $module {param($context) Remove-W4KnownWindowsInternetCacheJunction "
        "-ScenarioContext $context} $context"
    )

    result = _run_ps(command)

    assert result.returncode == 0, result.stderr
    record = json.loads((evidence / "windows-cache-cleanup.json").read_text("utf-8"))
    assert record["status"] == expected_status
    assert record["reparse_tag"] is None
    assert record["substitute_name"] is None
    if sentinel is not None:
        assert sentinel.read_text(encoding="utf-8") == "preserve\n"


@pytest.mark.parametrize("target_kind", ["wrong-sibling", "outside-profile"])
def test_known_windows_cache_cleanup_rejects_wrong_or_external_target(
    tmp_path: Path,
    target_kind: str,
) -> None:
    workspace = tmp_path / "scenario-workspace"
    evidence = tmp_path / "scenario-evidence"
    cache_root = (
        workspace
        / "profile"
        / "AppData"
        / "Local"
        / "Microsoft"
        / "Windows"
        / "INetCache"
    )
    expected_target = cache_root / "IE"
    expected_target.mkdir(parents=True)
    evidence.mkdir()
    target = (
        cache_root / "Unexpected"
        if target_kind == "wrong-sibling"
        else tmp_path / "outside-cache-target"
    )
    target.mkdir(parents=True)
    sentinel = target / "preserve.txt"
    sentinel.write_text("preserve\n", encoding="utf-8")
    junction = cache_root / "Content.IE5"
    _create_junction(junction, target)
    command = (
        "$ErrorActionPreference='Stop';"
        f"Import-Module '{_ps_quote(DRIVER_MODULE)}' -Force;"
        f"$module=Import-Module '{_ps_quote(SCENARIO_MODULE)}' -Force -PassThru;"
        "$context=[pscustomobject]@{"
        f"Workspace='{_ps_quote(workspace)}';Evidence='{_ps_quote(evidence)}'"
        "};& $module {param($context) Remove-W4KnownWindowsInternetCacheJunction "
        "-ScenarioContext $context} $context"
    )
    try:
        result = _run_ps(command)
        assert result.returncode != 0
        assert "not the exact sibling IE directory" in result.stderr
        assert junction.exists()
        assert sentinel.read_text(encoding="utf-8") == "preserve\n"
        assert not (evidence / "windows-cache-cleanup.json").exists()
    finally:
        _remove_junction(junction)


def test_semantic_fixture_manifest_cannot_escape_bundle_or_target(tmp_path: Path) -> None:
    fixture_root = tmp_path / "fixtures"
    bundle = fixture_root / "semantic-vector-index.v1"
    shutil.copytree(SEMANTIC_BUNDLE, bundle)
    manifest_path = bundle / "manifest.v1.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"][0]["path"] = "../escape.idx"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    user_data = tmp_path / "user-data"
    user_data.mkdir()
    escaped_target = user_data / "escape.idx"
    command = (
        "$ErrorActionPreference='Stop';"
        f"Import-Module '{_ps_quote(DRIVER_MODULE)}' -Force;"
        f"$module=Import-Module '{_ps_quote(SCENARIO_MODULE)}' -Force -PassThru;"
        f"$run=[pscustomobject]@{{FixtureRoot='{_ps_quote(fixture_root)}'}};"
        f"$context=[pscustomobject]@{{UserDataRoot='{_ps_quote(user_data)}'}};"
        "& $module {param($run,$context) Expand-W4FixtureVectorIndexes "
        "-RunContext $run -ScenarioContext $context} $run $context"
    )

    result = _run_ps(command)

    assert result.returncode != 0
    assert "exact canonical set/order" in result.stderr
    assert not escaped_target.exists()


def test_safe_path_chain_rejects_precreated_mutable_runs_junction(
    tmp_path: Path,
) -> None:
    command_processor = shutil.which("cmd.exe")
    assert command_processor is not None
    mutable_root = tmp_path / "evidence"
    outside = tmp_path / "outside"
    mutable_root.mkdir()
    outside.mkdir()
    junction = mutable_root / "runs"
    creation = subprocess.run(
        [command_processor, "/d", "/c", "mklink", "/J", str(junction), str(outside)],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=10,
        check=False,
    )
    assert creation.returncode == 0 and junction.exists(), creation.stderr
    try:
        derived = junction / "synthetic-run"
        command = (
            "$ErrorActionPreference='Stop';"
            f"Import-Module '{_ps_quote(DRIVER_MODULE)}' -Force;"
            f"Assert-W4SafePathChain -Path '{_ps_quote(derived)}' "
            "-Label 'synthetic mutable child'"
        )
        result = _run_ps(command)
        assert result.returncode != 0
        assert "Unsafe ReparsePoint rejected" in result.stderr
        assert not (outside / "synthetic-run").exists()
    finally:
        removal = subprocess.run(
            [command_processor, "/d", "/c", "rmdir", str(junction)],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=10,
            check=False,
        )
        assert removal.returncode == 0 and not junction.exists(), removal.stderr


def test_controller_statically_requires_disk_preflight_and_finally_cleanup() -> None:
    controller_text = CONTROLLER.read_text(encoding="utf-8")
    scenario_text = SCENARIO_MODULE.read_text(encoding="utf-8")
    driver_text = DRIVER_MODULE.read_text(encoding="utf-8")
    outer_text = OUTER_RUNNER.read_text(encoding="utf-8-sig")

    assert "function Invoke-W4DiskPreflight" in controller_text
    assert "artifact_expanded_bytes" in controller_text
    assert "sequential_install_cleanup = $true" in controller_text
    assert "Invoke-W4DiskPreflight -ZipPath $distributionZip" in controller_text
    assert "function Remove-W4ScenarioInstallRoot" in scenario_text
    assert "Remove-W4ScenarioInstallRoot -ScenarioContext $context" in scenario_text
    assert "pkv.m13.w4-install-cleanup.v1" in scenario_text
    assert "pkv.m13.w4-harness-process-identity.v1" in scenario_text
    assert "[int]$ready.pid -ne $process.Id" not in scenario_text
    assert "function Start-W4RedirectedProcess" in driver_text
    assert driver_text.count("Start-W4RedirectedProcess -Process $process") == 2
    assert "mcp-stderr.txt" in driver_text
    assert "Get-FileHash" not in driver_text
    assert "Get-FileHash" not in outer_text
    assert "function Get-LocalFileHash" in outer_text
    assert "function Assert-W4SafePathChain" in driver_text
    assert "Assert-W4SafePathChain -Path $candidate[0]" in controller_text
    assert "Get-W4TreeManifest -Root $mutable[0]" in controller_text
    assert "Assert-SafeTree -Root $resolvedEvidence" in outer_text
    assert "Assert-SafeTree -Root $resolvedWorkspace" in outer_text
    assert "W4 launcher evidence root escaped its mutable authority" in outer_text
    assert "FSCTL_GET_REPARSE_POINT" in driver_text
    assert "CreateToolhelp32Snapshot" in driver_text
    assert "function Remove-W4KnownWindowsInternetCacheJunction" in scenario_text
    assert "Remove-W4KnownWindowsInternetCacheJunction -ScenarioContext $context" in scenario_text
    assert "[System.IO.Directory]::Delete($junctionPath, $false)" in scenario_text
    assert "Content.IE5" not in outer_text
    assert "Content.IE5" not in CONTROLLER.read_text(encoding="utf-8")


def test_runtime_safety_evidence_schema_is_json_serializable() -> None:
    # Keep the public evidence field spelling stable for outer evidence hashing.
    value = {
        "schema_version": "pkv.m13.w4-harness-process-identity.v1",
        "launcher_pid": 10,
        "runtime_pid": 11,
        "runtime_is_launcher": False,
        "ancestry_pids": [11, 10],
    }
    assert json.loads(json.dumps(value, separators=(",", ":"))) == value
