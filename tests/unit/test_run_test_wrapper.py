"""Behavioral contracts for the isolated PowerShell test wrapper."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Iterable
from uuid import uuid4

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
RUN_TEST_SCRIPT = PROJECT_ROOT / "scripts" / "run-test.ps1"
ALLOWED_TEST_ROOT = PROJECT_ROOT / ".data-test"
POWERSHELL = shutil.which("powershell.exe") or shutil.which("pwsh")


def _ps_literal(value: object) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _invoke_wrapper(
    data_root: Path,
    command: Iterable[str],
    *,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    assert POWERSHELL is not None
    command_array = ",".join(_ps_literal(item) for item in command)
    expression = (
        "& { "
        f"& {_ps_literal(RUN_TEST_SCRIPT)} "
        f"-Direct -DataRoot {_ps_literal(data_root)} "
        f"-Command @({command_array}); "
        "exit $LASTEXITCODE"
        " }"
    )
    return subprocess.run(
        [
            POWERSHELL,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            expression,
        ],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


@pytest.fixture
def wrapper_data_root() -> Iterable[Path]:
    ALLOWED_TEST_ROOT.mkdir(parents=True, exist_ok=True)
    root = ALLOWED_TEST_ROOT / "wrapper-contract" / f"case-{uuid4().hex}"
    try:
        yield root
    finally:
        resolved_root = root.resolve(strict=False)
        resolved_allowed = ALLOWED_TEST_ROOT.resolve()
        assert resolved_root.is_relative_to(resolved_allowed)
        if root.is_symlink():
            root.unlink(missing_ok=True)
        elif root.exists():
            shutil.rmtree(root)


pytestmark = pytest.mark.skipif(
    POWERSHELL is None or os.name != "nt",
    reason="Windows PowerShell wrapper contract",
)


def test_wrapper_exposes_only_isolated_runtime_paths(
    wrapper_data_root: Path,
) -> None:
    keys = ("DATA_DIR", "DB_PATH", "VAULT_DIR", "VECTOR_DIR", "LOG_DIR", "TMP_DIR")
    probe = (
        "import json,os;"
        f"keys={keys!r};"
        "print('PKV_ENV_JSON=' + json.dumps({k:os.environ[k] for k in keys},"
        "sort_keys=True))"
    )

    result = _invoke_wrapper(
        wrapper_data_root,
        ["python", "-c", probe],
    )

    assert result.returncode == 0, result.stdout + result.stderr
    line = next(
        item
        for item in result.stdout.splitlines()
        if item.startswith("PKV_ENV_JSON=")
    )
    payload = json.loads(line.removeprefix("PKV_ENV_JSON="))
    expected = {
        "DATA_DIR": wrapper_data_root,
        "DB_PATH": wrapper_data_root / "db" / "knowledge_vault.db",
        "VAULT_DIR": wrapper_data_root / "vault",
        "VECTOR_DIR": wrapper_data_root / "vectors",
        "LOG_DIR": wrapper_data_root / "logs",
        "TMP_DIR": wrapper_data_root / "tmp",
    }
    assert set(payload) == set(expected)
    for key, expected_path in expected.items():
        actual_path = Path(payload[key]).resolve()
        assert actual_path == expected_path.resolve()
        assert actual_path.is_relative_to(ALLOWED_TEST_ROOT.resolve())
    assert Path(payload["DB_PATH"]).parent.is_dir()
    for key in ("VAULT_DIR", "VECTOR_DIR", "LOG_DIR", "TMP_DIR"):
        assert Path(payload[key]).is_dir()


def test_wrapper_rejects_data_root_outside_repository_test_area(
) -> None:
    outside_root = (
        Path(tempfile.gettempdir())
        / "pkv-wrapper-outside"
        / f"case-{uuid4().hex}"
    )
    assert not outside_root.resolve(strict=False).is_relative_to(
        ALLOWED_TEST_ROOT.resolve()
    )

    result = _invoke_wrapper(outside_root, ["python", "--version"])

    assert result.returncode == 2
    assert ".data-test" in result.stdout + result.stderr
    assert not outside_root.exists()


@pytest.mark.parametrize(
    ("command", "expected_message"),
    [
        (
            [
                "python",
                "-m",
                "src.cli.commands",
                "config",
                "set",
                "ai.llm.api_key",
                "PKV_WRAPPER_SECRET",
            ],
            "config set",
        ),
        (
            ["powershell.exe", "-File", "scripts/backup-data.ps1"],
            "备份/恢复",
        ),
        (
            ["python", "scripts/migrate.py", "--auto"],
            "--no-backup",
        ),
    ],
)
def test_wrapper_blocks_unsafe_commands_before_creating_runtime_paths(
    wrapper_data_root: Path,
    command: list[str],
    expected_message: str,
) -> None:
    result = _invoke_wrapper(wrapper_data_root, command)
    combined = result.stdout + result.stderr

    assert result.returncode == 2
    assert expected_message in combined
    assert "PKV_WRAPPER_SECRET" not in combined
    assert not wrapper_data_root.exists()


def test_wrapper_redacts_sensitive_arguments_and_propagates_exit_code(
    wrapper_data_root: Path,
) -> None:
    secret = "pkv-wrapper-sentinel-7d4f"
    result = _invoke_wrapper(
        wrapper_data_root,
        [
            "python",
            "-c",
            "import sys;sys.exit(7)",
            f"--api-key={secret}",
        ],
    )
    combined = result.stdout + result.stderr

    assert result.returncode == 7
    assert secret not in combined
    assert "--api-key=<redacted>" in combined
    assert "测试失败 (退出码: 7)" in combined


def test_wrapper_rejects_hard_link_inside_requested_root(
    wrapper_data_root: Path,
    tmp_path: Path,
) -> None:
    wrapper_data_root.mkdir(parents=True)
    source = tmp_path / "outside-sentinel.txt"
    source.write_text("outside", encoding="utf-8")
    os.link(source, wrapper_data_root / "linked-sentinel.txt")

    result = _invoke_wrapper(wrapper_data_root, ["python", "--version"])

    assert result.returncode == 2
    assert "硬链接" in result.stdout + result.stderr
    assert source.read_text(encoding="utf-8") == "outside"
