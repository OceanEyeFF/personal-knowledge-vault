"""Behavioral contracts for the isolated PowerShell test wrapper."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
from typing import Iterable
from uuid import uuid4

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
RUN_TEST_SCRIPT = PROJECT_ROOT / "scripts" / "run-test.ps1"
ALLOWED_TEST_ROOT = PROJECT_ROOT / ".data-test"
POWERSHELL = shutil.which("powershell.exe") or shutil.which("pwsh")


def _is_reparse_point(path: Path) -> bool:
    """Inspect the path itself without following a symlink or junction."""

    if not os.path.lexists(path):
        return False
    attributes = getattr(os.lstat(path), "st_file_attributes", 0)
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _assert_safe_allowed_test_root() -> None:
    """Fail before a fixture can create files through an unsafe test root."""

    assert not _is_reparse_point(PROJECT_ROOT), (
        "wrapper contract tests refuse a reparse-point project root"
    )
    if os.path.lexists(ALLOWED_TEST_ROOT):
        assert not _is_reparse_point(ALLOWED_TEST_ROOT), (
            "wrapper contract tests refuse a reparse-point .data-test root"
        )
        assert ALLOWED_TEST_ROOT.is_dir()


def _ps_literal(value: object) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _invoke_wrapper(
    data_root: Path,
    command: Iterable[str],
    *,
    env: dict[str, str] | None = None,
    direct: bool = True,
) -> subprocess.CompletedProcess[str]:
    assert POWERSHELL is not None
    command_array = ",".join(_ps_literal(item) for item in command)
    direct_switch = "-Direct " if direct else ""
    expression = (
        "& { "
        f"& {_ps_literal(RUN_TEST_SCRIPT)} "
        f"{direct_switch}-DataRoot {_ps_literal(data_root)} "
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
    _assert_safe_allowed_test_root()
    ALLOWED_TEST_ROOT.mkdir(parents=True, exist_ok=True)
    root = ALLOWED_TEST_ROOT / "wrapper-contract" / f"case-{uuid4().hex}"
    try:
        yield root
    finally:
        if not os.path.lexists(root):
            return
        root_stat = os.lstat(root)
        if _is_reparse_point(root):
            if stat.S_ISDIR(root_stat.st_mode):
                root.rmdir()
            else:
                root.unlink()
            return
        resolved_root = root.resolve()
        resolved_allowed = ALLOWED_TEST_ROOT.resolve(strict=True)
        assert resolved_root.is_relative_to(resolved_allowed)
        if root.is_dir():
            shutil.rmtree(root)
        else:
            root.unlink()


pytestmark = pytest.mark.skipif(
    POWERSHELL is None or os.name != "nt",
    reason="Windows PowerShell wrapper contract",
)


def test_wrapper_exposes_only_isolated_runtime_paths(
    wrapper_data_root: Path,
) -> None:
    keys = (
        "DATA_DIR",
        "DB_PATH",
        "VAULT_DIR",
        "VECTOR_DIR",
        "LOG_DIR",
        "TMP_DIR",
        "COVERAGE_FILE",
        "PYTHONDONTWRITEBYTECODE",
        "PYTEST_ADDOPTS",
        "PKV_RUN_LIVE",
        "PKV_TEST_OFFLINE",
        "PKV_TEST_LOAD_LOCAL",
        "PKV_TEST_PROJECT_ROOT",
    )
    probe = (
        "import json,os;"
        f"keys={keys!r};"
        "print('PKV_ENV_JSON=' + json.dumps({k:os.environ[k] for k in keys},"
        "sort_keys=True))"
    )

    hostile_parent_env = os.environ.copy()
    hostile_parent_env["PYTEST_ADDOPTS"] = "--noconftest"
    result = _invoke_wrapper(
        wrapper_data_root,
        ["python", "-c", probe],
        env=hostile_parent_env,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    line = next(
        item
        for item in result.stdout.splitlines()
        if item.startswith("PKV_ENV_JSON=")
    )
    payload = json.loads(line.removeprefix("PKV_ENV_JSON="))
    expected_paths = {
        "DATA_DIR": wrapper_data_root,
        "DB_PATH": wrapper_data_root / "db" / "knowledge_vault.db",
        "VAULT_DIR": wrapper_data_root / "vault",
        "VECTOR_DIR": wrapper_data_root / "vectors",
        "LOG_DIR": wrapper_data_root / "logs",
        "TMP_DIR": wrapper_data_root / "tmp",
        "COVERAGE_FILE": wrapper_data_root / "reports" / ".coverage",
    }
    assert set(payload) == set(keys)
    for key, expected_path in expected_paths.items():
        actual_path = Path(payload[key]).resolve()
        assert actual_path == expected_path.resolve()
        assert actual_path.is_relative_to(ALLOWED_TEST_ROOT.resolve())
    assert payload["PYTHONDONTWRITEBYTECODE"] == "1"
    assert payload["PYTEST_ADDOPTS"] == "--strict-markers"
    assert payload["PKV_RUN_LIVE"] == "0"
    assert payload["PKV_TEST_OFFLINE"] == "1"
    assert payload["PKV_TEST_LOAD_LOCAL"] == "0"
    assert Path(payload["PKV_TEST_PROJECT_ROOT"]).resolve() == PROJECT_ROOT.resolve()
    assert Path(payload["DB_PATH"]).parent.is_dir()
    for key in ("VAULT_DIR", "VECTOR_DIR", "LOG_DIR", "TMP_DIR"):
        assert Path(payload[key]).is_dir()
    assert Path(payload["COVERAGE_FILE"]).parent.is_dir()


def test_wrapper_default_cli_uses_base_only_entrypoint(
    wrapper_data_root: Path,
) -> None:
    result = _invoke_wrapper(
        wrapper_data_root,
        ["--help"],
        direct=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    invocation_line = next(
        line for line in result.stdout.splitlines() if line.startswith("[执行命令]")
    )
    assert "python tests/offline_entrypoint.py cli --help" in invocation_line


def test_wrapper_forces_pytest_outputs_under_requested_data_root(
    wrapper_data_root: Path,
) -> None:
    untrusted = wrapper_data_root.parent / "untrusted-pytest-output"
    result = _invoke_wrapper(
        wrapper_data_root,
        [
            sys.executable,
            "-m",
            "pytest",
            "--version",
            f"--basetemp={untrusted}",
            "-o",
            f"cache_dir={untrusted / 'cache'}",
            "--",
        ],
    )

    assert result.returncode == 0, result.stdout + result.stderr
    output = result.stdout + result.stderr
    trusted_basetemp = f"--basetemp={wrapper_data_root / 'tmp' / 'pytest'}"
    trusted_cache = f"cache_dir={wrapper_data_root / 'tmp' / 'pytest-cache'}"
    assert output.rfind(trusted_basetemp) > output.rfind(f"--basetemp={untrusted}")
    assert output.rfind(trusted_cache) > output.rfind(f"cache_dir={untrusted / 'cache'}")
    invocation_line = next(
        line for line in output.splitlines() if line.startswith("[执行命令]")
    )
    assert invocation_line.index(trusted_basetemp) < invocation_line.rindex(" --")
    assert invocation_line.index(trusted_cache) < invocation_line.rindex(" --")
    assert not untrusted.exists()


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
            ["python", "scripts/migrate.py", "--auto", "--no-backup"],
            "base-only",
        ),
        (
            ["python", "scripts/migrate.py", "--version"],
            "base-only",
        ),
        (
            ["python", "-m", "pytest", "tests/unit", "--noconftest"],
            "conftest/config",
        ),
        (
            [
                "python",
                "-m",
                "pytest",
                "tests/unit",
                "--confcutdir=tests/unit",
            ],
            "conftest/config",
        ),
        (
            ["pytest", "tests/unit", "-c", "pytest.ini"],
            "conftest/config",
        ),
        (
            ["pytest", "tests/unit", "--rootdir", "tests/unit"],
            "conftest/config",
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
