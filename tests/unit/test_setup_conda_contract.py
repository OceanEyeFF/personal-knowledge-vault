from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SETUP_CONDA = PROJECT_ROOT / "scripts" / "setup-conda.ps1"
RUN_WINDOWS = PROJECT_ROOT / "scripts" / "run-windows.ps1"
POWERSHELL = shutil.which("powershell.exe") or shutil.which("pwsh")
SCRIPT_DOCS = (
    PROJECT_ROOT / "scripts" / "CLAUDE.md",
    PROJECT_ROOT / "scripts" / "README.md",
)


def _read_powershell(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def test_setup_conda_bootstraps_only_the_profile_config() -> None:
    source = _read_powershell(SETUP_CONDA)

    assert '$userPkvRoot = Join-Path $userProfileRoot ".pkv"' in source
    assert '$userConfigPath = Join-Path $userPkvRoot "config.yaml"' in source
    assert '$defaultDataRoot = Join-Path $userPkvRoot "data"' in source
    assert "[System.IO.File]::Copy($configTemplatePath, $userConfigPath, $false)" in source
    assert 'Copy-Item "config\\config.yaml" "config\\local.yaml"' not in source
    assert '".data\\db"' not in source
    assert '".data\\vectors"' not in source
    assert 'New-Item -ItemType Directory -Path $defaultDataRoot' not in source


def test_setup_conda_preserves_legacy_checkout_paths_and_requires_plan() -> None:
    source = _read_powershell(SETUP_CONDA)

    assert 'Path = Join-Path $projectRoot "config\\local.yaml"' in source
    assert 'Path = Join-Path $projectRoot ".data"' in source
    assert "本脚本不会读取、复制、删除或迁移这些路径。" in source
    assert "由用户确认单独迁移方案" in source
    assert "python -m src.cli.commands inspect" in source
    assert "python -m src.cli.commands setup" in source
    assert "setup --apply --confirm <PLAN_ID>" in source


def test_run_windows_never_directs_sensitive_values_to_runtime_snapshot() -> None:
    source = _read_powershell(RUN_WINDOWS)

    assert "%USERPROFILE%\\.pkv\\config.yaml" in source
    assert (
        "<data-root>\\config\\local.yaml 是 PKV 管理的无密钥运行时快照，不能作为用户配置。"
        in source
    )
    assert "请直接编辑已被 Git 忽略的 config/local.yaml" not in source


def test_active_setup_docs_match_the_single_config_and_explicit_plan_contract() -> None:
    for document in SCRIPT_DOCS:
        source = document.read_text(encoding="utf-8")

        assert "%USERPROFILE%\\.pkv\\config.yaml" in source
        assert "<data-root>/config/local.yaml" in source
        assert "PKV_DATA_ROOT" in source
        assert "PLAN_ID" in source
        assert "不读取内容、不复制、不删除、不迁移" in source


@pytest.mark.skipif(POWERSHELL is None, reason="PowerShell is unavailable")
@pytest.mark.parametrize("script", (SETUP_CONDA, RUN_WINDOWS))
def test_changed_powershell_scripts_parse_without_execution(script: Path) -> None:
    script_path = str(script).replace("'", "''")
    parser_command = (
        "$tokens = $null; $errors = $null; "
        "[void][System.Management.Automation.Language.Parser]::ParseFile("
        f"'{script_path}', [ref]$tokens, [ref]$errors); "
        "if ($errors.Count -gt 0) { "
        "$errors | ForEach-Object { [Console]::Error.WriteLine($_.Message) }; exit 1 "
        "}"
    )
    completed = subprocess.run(
        [
            str(POWERSHELL),
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            parser_command,
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, (
        completed.stdout + completed.stderr
    ).decode("utf-8", errors="replace")
