from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
TEST_CONDA_SCRIPT = PROJECT_ROOT / "scripts" / "test-conda.ps1"
CI_WORKFLOW = PROJECT_ROOT / ".github" / "workflows" / "mcp-test.yml"
POWERSHELL = shutil.which("powershell.exe") or shutil.which("pwsh")


def _read_test_conda_script() -> str:
    return TEST_CONDA_SCRIPT.read_text(encoding="utf-8-sig")


def test_containment_guard_precedes_process_environment_mutation() -> None:
    script = _read_test_conda_script()

    guard = script.index("if (-not $testRunPath.StartsWith(")
    previous_environment = script.index("$previousEnvironmentName = $env:PKV_CONDA_ENV")
    guarded_try = script.index("try {", previous_environment)
    environment_mutation = script.index("$env:PKV_CONDA_ENV = $envName")
    guarded_finally = script.index("} finally {", environment_mutation)

    assert guard < previous_environment < guarded_try
    assert guarded_try < environment_mutation < guarded_finally


def test_version_probe_checks_empty_output_before_trimming() -> None:
    script = _read_test_conda_script()

    candidate = script.index("$pythonMinorCandidate = (")
    empty_guard = script.index(
        "[string]::IsNullOrWhiteSpace([string]$pythonMinorCandidate)"
    )
    trim = script.index("$pythonMinor = ([string]$pythonMinorCandidate).Trim()")

    assert candidate < empty_guard < trim
    assert "empty version output" in script[candidate:trim]


def test_success_cleanup_revalidates_and_rejects_unsafe_links() -> None:
    script = _read_test_conda_script()

    audit_helper = script.index("function Assert-NoUnsafeCleanupLinks")
    cleanup_helper = script.index("function Assert-SafeTestRunCleanup")
    cleanup_condition = script.index(
        "if ($exitCode -eq 0 -and (Test-Path -LiteralPath $testRunPath))"
    )
    audit_call = script.index("Assert-SafeTestRunCleanup", cleanup_condition)
    delete = script.index("Remove-Item `", cleanup_condition)
    helpers = script[audit_helper:cleanup_condition]

    assert audit_helper < cleanup_helper < cleanup_condition < audit_call < delete
    assert "[IO.Path]::GetFullPath($DataRoot)" in helpers
    assert "[IO.Path]::GetFullPath($RunPath)" in helpers
    assert "if (-not $normalizedRunPath.StartsWith(" in helpers
    assert "[IO.Path]::GetFileName($normalizedRunPath) -ne $ExpectedLeaf" in helpers
    assert "[System.IO.FileAttributes]::ReparsePoint" in helpers
    assert 'PSObject.Properties["LinkType"]' in helpers
    assert '$item.LinkType -eq "HardLink"' in helpers
    assert "Get-ChildItem -LiteralPath $Path -Force -ErrorAction Stop" in helpers
    assert "Assert-NoUnsafeCleanupLinks -Path $child.FullName -Recurse" in helpers
    assert "Assert-NoUnsafeCleanupLinks -Path $normalizedDataRoot" in helpers
    assert "Assert-NoUnsafeCleanupLinks -Path $normalizedRunPath -Recurse" in helpers


def test_test_conda_uses_setup_default_and_delegates_pytest_isolation_to_wrapper() -> None:
    script = _read_test_conda_script()
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")

    assert 'else {\n    "py311-private"\n}' in script
    assert "pkv-test-py311" not in script
    assert "--basetemp" not in script
    assert "cache_dir=" not in script
    assert workflow.count("cache_dir=$TMP_DIR/pytest-cache") == 3
    assert "paths: tests/test_basic_syntax.py tests/unit" in workflow
    assert "paths: tests/test_*.py tests/unit" not in workflow


def test_test_conda_offline_selectors_preserve_default_opt_in_exclusions() -> None:
    script = _read_test_conda_script()

    selector = "not manual and not network and not artifact and not windows_release_env"
    assert script.count(f'"{selector}",') == 2


def test_ci_pins_the_formal_data_root_in_every_isolated_job() -> None:
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")

    assert 'PKV_TEST_OFFLINE: "1"' in workflow
    assert workflow.count('echo "PKV_DATA_ROOT=$data_dir"') == 3
    assert workflow.count('echo "DATA_DIR=$data_dir"') == 3


@pytest.mark.skipif(POWERSHELL is None, reason="PowerShell is unavailable")
def test_empty_successful_version_probe_has_clear_error(tmp_path: Path) -> None:
    if os.name == "nt":
        fake_conda = tmp_path / "conda.cmd"
        fake_conda.write_text("@echo off\r\nexit /b 0\r\n", encoding="ascii")
        system_path = str(Path(os.environ["SystemRoot"]) / "System32")
        child_path = os.pathsep.join((str(tmp_path), system_path))
    else:
        fake_conda = tmp_path / "conda"
        fake_conda.write_text("#!/bin/sh\nexit 0\n", encoding="ascii")
        fake_conda.chmod(
            fake_conda.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
        )
        child_path = os.pathsep.join((str(tmp_path), "/usr/bin", "/bin"))

    child_env = os.environ.copy()
    child_env["PATH"] = child_path
    result = subprocess.run(
        [
            str(POWERSHELL),
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(TEST_CONDA_SCRIPT),
            "-EnvironmentName",
            "empty-version-probe",
            "-Suite",
            "Smoke",
        ],
        cwd=PROJECT_ROOT,
        env=child_env,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    assert b"empty version output" in result.stdout + result.stderr
