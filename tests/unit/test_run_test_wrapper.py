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
    tmp_path: Path,
) -> None:
    keys = (
        "DATA_DIR",
        "DB_PATH",
        "VAULT_DIR",
        "VECTOR_DIR",
        "LOG_DIR",
        "TMP_DIR",
        "COVERAGE_FILE",
        "TEMP",
        "TMP",
        "TMPDIR",
        "HOME",
        "USERPROFILE",
        "APPDATA",
        "LOCALAPPDATA",
        "XDG_CONFIG_HOME",
        "XDG_CACHE_HOME",
        "XDG_DATA_HOME",
        "PYTHONDONTWRITEBYTECODE",
        "PYTEST_ADDOPTS",
        "PKV_DATA_ROOT",
        "PKV_RUN_LIVE",
        "PKV_TEST_OFFLINE",
        "PKV_TEST_LOAD_LOCAL",
        "PKV_TEST_PROJECT_ROOT",
    )
    hostile_parent_env = os.environ.copy()
    hostile_parent_env["PYTEST_ADDOPTS"] = "--noconftest"
    hostile_parent_env["PKV_RUN_LIVE"] = "1"
    hostile_parent_env["PKV_TEST_LOAD_LOCAL"] = "1"
    hostile_parent_env["PKV_E2E_ARCHIVE_URL"] = "https://live.example/sentinel"
    hostile_parent_env["OPENAI_API_KEY"] = "provider-secret-sentinel"
    hostile_parent_env["https_proxy"] = "http://proxy.example/sentinel"
    hostile_parent_env["HOME"] = str(tmp_path / "hostile-home")
    hostile_parent_env["USERPROFILE"] = str(tmp_path / "hostile-userprofile")
    hostile_parent_env["APPDATA"] = str(tmp_path / "hostile-appdata")
    hostile_parent_env["LOCALAPPDATA"] = str(tmp_path / "hostile-localappdata")
    # A Config constructor reads the runtime embedding cache before the child
    # probe can compare final paths.  Make an inherited product root fail
    # closed if it is even considered: the hard-linked cache leaf is rejected
    # by RuntimeLayout.  A successful wrapper invocation therefore proves the
    # wrapper replaced the hostile product override before Python imported PKV.
    inherited_root = tmp_path / "inherited-pkv-data-root"
    inherited_runtime = inherited_root / "runtime"
    inherited_runtime.mkdir(parents=True)
    sentinel_source = tmp_path / "inherited-pkv-sentinel-source.json"
    sentinel_source.write_text("{}", encoding="utf-8")
    inherited_cache = inherited_runtime / "embedding_dim.json"
    os.link(sentinel_source, inherited_cache)
    try:
        assert not inherited_root.resolve().is_relative_to(wrapper_data_root.resolve())
        hostile_parent_env["PKV_DATA_ROOT"] = str(inherited_root)
        result = _invoke_wrapper(
            wrapper_data_root,
            [
                "python",
                "tests/fixtures/offline_direct_probe.py",
                "--print-env",
            ],
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
            "TEMP": wrapper_data_root / "tmp",
            "TMP": wrapper_data_root / "tmp",
            "TMPDIR": wrapper_data_root / "tmp",
            "PKV_DATA_ROOT": wrapper_data_root,
            "HOME": wrapper_data_root / "profile",
            "USERPROFILE": wrapper_data_root / "profile",
            "APPDATA": wrapper_data_root / "profile" / "AppData" / "Roaming",
            "LOCALAPPDATA": wrapper_data_root / "profile" / "AppData" / "Local",
            "XDG_CONFIG_HOME": wrapper_data_root / "profile" / ".config",
            "XDG_CACHE_HOME": wrapper_data_root / "profile" / ".cache",
            "XDG_DATA_HOME": wrapper_data_root / "profile" / ".local" / "share",
        }
        assert set(payload) == set(keys)
        for key, expected_path in expected_paths.items():
            actual_path = Path(payload[key]).resolve()
            assert actual_path == expected_path.resolve()
            assert actual_path.is_relative_to(ALLOWED_TEST_ROOT.resolve())
        assert payload["PYTHONDONTWRITEBYTECODE"] == "1"
        assert payload["PYTEST_ADDOPTS"] == "--strict-markers"
        assert Path(payload["PKV_DATA_ROOT"]).resolve() == wrapper_data_root.resolve()
        assert payload["PKV_RUN_LIVE"] == "0"
        assert payload["PKV_TEST_OFFLINE"] == "1"
        assert payload["PKV_TEST_LOAD_LOCAL"] == "0"
        assert Path(payload["PKV_TEST_PROJECT_ROOT"]).resolve() == PROJECT_ROOT.resolve()
        assert Path(payload["DB_PATH"]).parent.is_dir()
        for key in (
            "VAULT_DIR",
            "VECTOR_DIR",
            "LOG_DIR",
            "TMP_DIR",
            "HOME",
            "APPDATA",
            "LOCALAPPDATA",
            "XDG_CONFIG_HOME",
            "XDG_CACHE_HOME",
            "XDG_DATA_HOME",
        ):
            assert Path(payload[key]).is_dir()
        assert Path(payload["COVERAGE_FILE"]).parent.is_dir()
        invocation_line = next(
            line for line in result.stdout.splitlines() if line.startswith("[执行命令]")
        )
        assert "python tests/offline_entrypoint.py python" in invocation_line
        assert "tests/fixtures/offline_direct_probe.py" in invocation_line
    finally:
        inherited_cache.unlink(missing_ok=True)


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
    result = _invoke_wrapper(
        wrapper_data_root,
        [
            sys.executable,
            "-m",
            "pytest",
            "--version",
            "--",
        ],
    )

    assert result.returncode == 0, result.stdout + result.stderr
    output = result.stdout + result.stderr
    trusted_basetemp = f"--basetemp={wrapper_data_root / 'tmp' / 'pytest'}"
    trusted_cache = f"cache_dir={wrapper_data_root / 'tmp' / 'pytest-cache'}"
    invocation_line = next(
        line for line in output.splitlines() if line.startswith("[执行命令]")
    )
    assert "python tests/offline_entrypoint.py pytest" in invocation_line
    assert invocation_line.index(trusted_basetemp) < invocation_line.rindex(" --")
    assert invocation_line.index(trusted_cache) < invocation_line.rindex(" --")


def test_pytest_bootstrap_scrubs_parent_plugin_injection(
    wrapper_data_root: Path,
) -> None:
    hostile_env = os.environ.copy()
    hostile_env["PYTEST_PLUGINS"] = "module_that_must_not_be_imported"
    hostile_env["PYTEST_ADDOPTS"] = "--noconftest"

    result = _invoke_wrapper(
        wrapper_data_root,
        ["pytest", "--version"],
        env=hostile_env,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "module_that_must_not_be_imported" not in result.stdout + result.stderr


def test_wrapper_scrubs_prebootstrap_python_and_coverage_injection(
    wrapper_data_root: Path,
) -> None:
    with tempfile.TemporaryDirectory(prefix="pkv-hostile-bootstrap-") as temp_dir:
        outside_root = Path(temp_dir)
        site_sentinel = outside_root / "sitecustom-ran.txt"
        coverage_debug = outside_root / "coverage-debug.txt"
        coverage_data = outside_root / "coverage-data"
        coverage_config = outside_root / "hostile-coveragerc"
        hostile_temp = outside_root / "hostile-temp"
        (outside_root / "sitecustom.py").write_text(
            "from pathlib import Path\n"
            f"Path({str(site_sentinel)!r}).write_text('ran', encoding='utf-8')\n",
            encoding="utf-8",
        )
        coverage_config.write_text("[run]\nbranch = true\n", encoding="utf-8")

        hostile_env = os.environ.copy()
        hostile_env.update(
            {
                "PYTHONPATH": str(outside_root),
                "PYTHONHOME": str(outside_root / "invalid-python-home"),
                "PYTHONSTARTUP": str(outside_root / "sitecustom.py"),
                "PYTHONINSPECT": "1",
                "PYTHONWARNINGS": "error",
                "PYTHONUSERBASE": str(outside_root / "user-base"),
                "TEMP": str(hostile_temp),
                "TMP": str(hostile_temp),
                "TMPDIR": str(hostile_temp),
                "COVERAGE_PROCESS_START": str(coverage_config),
                "COVERAGE_PROCESS_CONFIG": str(coverage_config),
                "COVERAGE_RCFILE": str(coverage_config),
                "COVERAGE_FORCE_CONFIG": str(coverage_config),
                "COVERAGE_DEBUG": "process",
                "COVERAGE_DEBUG_FILE": str(coverage_debug),
                "COV_CORE_SOURCE": "src",
                "COV_CORE_CONFIG": str(coverage_config),
                "COV_CORE_DATAFILE": str(coverage_data),
                "COV_CORE_BRANCH": "1",
            }
        )

        result = _invoke_wrapper(
            wrapper_data_root,
            ["python", "tests/fixtures/offline_direct_probe.py"],
            env=hostile_env,
        )

        assert result.returncode == 0, result.stdout + result.stderr
        assert not site_sentinel.exists()
        assert not coverage_debug.exists()
        assert not coverage_data.exists()
        assert not list(hostile_temp.glob("pkv-command-*.json"))


@pytest.mark.parametrize(
    "unsafe_option",
    [
        ["--basetemp", "outside"],
        ["--basetemp=outside"],
        ["-o", "cache_dir=outside"],
        ["-ocache_dir=outside"],
        ["--junitxml=outside.xml"],
        ["--log-file=outside.log"],
    ],
)
def test_wrapper_rejects_untrusted_pytest_output_options(
    wrapper_data_root: Path,
    unsafe_option: list[str],
) -> None:
    result = _invoke_wrapper(
        wrapper_data_root,
        ["pytest", "--version", *unsafe_option],
    )

    assert result.returncode == 2
    assert not wrapper_data_root.exists()


@pytest.mark.parametrize(
    ("arguments", "expected_message"),
    [
        (["src", "--collect-only"], "collection targets"),
        (["@outside-args.txt"], "response files"),
        (["tests/unit", "--cov-report=xml:outside.xml"], "terminal-only"),
    ],
)
def test_pytest_bootstrap_rejects_collection_and_report_escape(
    wrapper_data_root: Path,
    arguments: list[str],
    expected_message: str,
) -> None:
    result = _invoke_wrapper(wrapper_data_root, ["pytest", *arguments])

    assert result.returncode != 0
    assert expected_message in result.stdout + result.stderr


def test_wrapper_rejects_data_root_outside_repository_test_area(
) -> None:
    outside_root = (
        PROJECT_ROOT.parent
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
            ["python", "scripts/backfill_chunks.py", "--apply"],
            "legacy maintenance",
        ),
        (
            ["python", "-m", "scripts.backfill_relations", "--apply"],
            "legacy maintenance",
        ),
        (
            ["python", "scripts/init_db.py"],
            "legacy maintenance",
        ),
        (
            [
                "python",
                "scripts/audit_rearchive_urls.py",
                "--vault",
                "E:\\not-a-test-vault",
            ],
            "legacy maintenance",
        ),
        (
            [
                "python",
                "-m",
                "scripts.check_chunk_index_consistency",
                "--db-path",
                "E:\\not-a-test-vault\\knowledge_vault.db",
            ],
            "外部路径",
        ),
        (
            [
                "python",
                "scripts/run_conda_command.py",
                "--args-file",
                "E:\\not-a-test-args.json",
            ],
            "显式离线测试白名单",
        ),
        (
            [
                "python",
                "-m",
                "scripts.run_conda_command",
                "--args-file",
                "E:\\not-a-test-args.json",
            ],
            "显式离线测试白名单",
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
            ["pytest", "-ctests/alternate.ini", "tests/unit"],
            "conftest/config",
        ),
        (
            ["pytest", "-c=pytest.ini", "tests/unit"],
            "conftest/config",
        ),
        (
            ["pytest", "tests/unit", "--rootdir", "tests/unit"],
            "conftest/config",
        ),
        (
            ["pytest", "src", "--doctest-modules"],
            "conftest/config",
        ),
        (
            ["pytest", "--pyargs", "src"],
            "conftest/config",
        ),
        (["python"], "Direct Python"),
        (["py"], "Direct Python"),
        (["pyw"], "Direct Python"),
        (["python", "-c", "print('unsafe')"], "仅允许"),
        (["pythonw", "-c", "print('unsafe')"], "仅允许"),
        (["pypy3", "-c", "print('unsafe')"], "仅允许"),
        (["python", "-I", "scripts/setup-test-db.py"], "仅允许"),
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


def test_consistency_checker_missing_database_fails_closed_without_creating_it(
    wrapper_data_root: Path,
) -> None:
    """The diagnostic script uses SQLite mode=ro rather than auto-creating DB."""

    db_path = wrapper_data_root / "db" / "missing.db"
    result = _invoke_wrapper(
        wrapper_data_root,
        [
            "python",
            "scripts/check_chunk_index_consistency.py",
            "--db-path",
            str(db_path),
            "--vector-dir",
            str(wrapper_data_root / "vectors"),
        ],
    )

    assert result.returncode == 2, result.stdout + result.stderr
    assert "安全只读" in result.stdout + result.stderr
    assert not db_path.exists()


@pytest.mark.parametrize("option", ("--db-p", "--vector-d", "--report-j"))
def test_wrapper_rejects_abbreviated_consistency_checker_path_options(
    wrapper_data_root: Path,
    tmp_path: Path,
    option: str,
) -> None:
    """Argparse aliases must not bypass the wrapper's path containment gate."""

    outside_path = tmp_path / "outside" / "user-owned-input-or-output"
    result = _invoke_wrapper(
        wrapper_data_root,
        [
            "python",
            "scripts/check_chunk_index_consistency.py",
            option,
            str(outside_path),
        ],
    )

    assert result.returncode == 2
    assert "consistency checker" in result.stdout + result.stderr
    assert not wrapper_data_root.exists()
    assert not outside_path.exists()


def test_wrapper_redacts_sensitive_arguments_and_propagates_exit_code(
    wrapper_data_root: Path,
) -> None:
    secret = "pkv-wrapper-sentinel-7d4f"
    result = _invoke_wrapper(
        wrapper_data_root,
        [
            "python",
            "tests/fixtures/offline_direct_probe.py",
            "--exit-code",
            "7",
            f"--api-key={secret}",
        ],
    )
    combined = result.stdout + result.stderr

    assert result.returncode == 7
    assert secret not in combined
    assert "--api-key=<redacted>" in combined
    assert "测试失败 (退出码: 7)" in combined


@pytest.mark.parametrize(
    "command",
    [
        ["python", "-m", "evals.mcp_quality", "--help"],
        ["python", "scripts/setup-test-db.py", "--help"],
        ["py", "scripts/setup-test-db.py", "--help"],
        ["pyw", "scripts/setup-test-db.py", "--help"],
        ["pythonw", "-m", "evals.mcp_quality", "--help"],
        ["pypy3", "scripts/setup-test-db.py", "--help"],
    ],
)
def test_wrapper_routes_repository_modules_and_scripts_through_ft7(
    wrapper_data_root: Path,
    command: list[str],
) -> None:
    result = _invoke_wrapper(wrapper_data_root, command)

    assert result.returncode == 0, result.stdout + result.stderr
    invocation_line = next(
        line for line in result.stdout.splitlines() if line.startswith("[执行命令]")
    )
    assert "python tests/offline_entrypoint.py python" in invocation_line


@pytest.mark.parametrize(
    "command",
    [
        ["python", "scripts/rebuild-dev-vault.py", "--help"],
        ["python", "-m", "src.cli.commands", "--help"],
        ["python", "-m", "src.mcp.server", "--help"],
        ["python", "-m", "src.utils.verify_setup"],
        ["python", "scripts/check_chunk_index_consistency.py", "--help"],
    ],
)
def test_wrapper_routes_each_supported_product_direct_python_target_through_ft7(
    wrapper_data_root: Path,
    command: list[str],
) -> None:
    """The wrapper's explicit target allowlist retains each public test seam."""

    result = _invoke_wrapper(wrapper_data_root, command)

    assert result.returncode == 0, result.stdout + result.stderr
    invocation_line = next(
        line for line in result.stdout.splitlines() if line.startswith("[执行命令]")
    )
    assert "python tests/offline_entrypoint.py python" in invocation_line


def test_wrapper_rejects_outside_python_script_without_executing_it(
    wrapper_data_root: Path,
) -> None:
    with tempfile.TemporaryDirectory(prefix="pkv-direct-outside-") as temp_dir:
        outside_dir = Path(temp_dir)
        outside_script = outside_dir / "outside-direct.py"
        outside_sentinel = outside_dir / "outside-direct-ran.txt"
        outside_script.write_text(
            f"from pathlib import Path\nPath({str(outside_sentinel)!r}).write_text('ran')\n",
            encoding="utf-8",
        )

        result = _invoke_wrapper(
            wrapper_data_root,
            ["python", str(outside_script)],
        )

        assert result.returncode != 0
        assert not outside_sentinel.exists()


def test_wrapper_rejects_non_repository_python_module(
    wrapper_data_root: Path,
) -> None:
    result = _invoke_wrapper(
        wrapper_data_root,
        ["python", "-m", "json.tool", "--help"],
    )

    assert result.returncode != 0
    assert "显式离线测试白名单" in result.stdout + result.stderr


def test_rebuild_rejects_sibling_data_root_before_creating_it(
    wrapper_data_root: Path,
) -> None:
    sibling = wrapper_data_root.parent / f"sibling-{uuid4().hex}"

    result = _invoke_wrapper(
        wrapper_data_root,
        [
            "python",
            "scripts/rebuild-dev-vault.py",
            "--root",
            str(sibling),
            "--force",
            "--json",
        ],
    )

    assert result.returncode == 2
    assert "当前 Direct Python DATA_DIR" in result.stdout + result.stderr
    assert not sibling.exists()


def test_wrapper_rejects_hard_link_inside_requested_root(
    wrapper_data_root: Path,
    tmp_path: Path,
) -> None:
    wrapper_data_root.mkdir(parents=True)
    source = tmp_path / "outside-sentinel.txt"
    source.write_text("outside", encoding="utf-8")
    os.link(source, wrapper_data_root / "linked-sentinel.txt")

    result = _invoke_wrapper(
        wrapper_data_root,
        ["python", "tests/fixtures/offline_direct_probe.py"],
    )

    assert result.returncode == 2
    assert "硬链接" in result.stdout + result.stderr
    assert source.read_text(encoding="utf-8") == "outside"
