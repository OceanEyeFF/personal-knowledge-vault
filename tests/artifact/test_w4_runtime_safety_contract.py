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
