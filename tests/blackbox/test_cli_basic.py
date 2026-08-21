"""CLI 基础黑盒测试 - 不依赖数据库内容的命令测试。

测试策略：
1. 使用真实的 subprocess 执行 CLI 命令
2. 验证命令的退出码、输出内容
3. 仅测试不需要数据库内容的基础命令
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from src.storage.sqlite_store import SQLiteStore
from tests.offline_runtime import prepare_offline_child_env


class CLITester:
    """CLI 黑盒测试工具类。"""

    def __init__(self, env: dict[str, str], python_exe: str = sys.executable):
        """初始化测试器。

        Args:
            env: 传递给 CLI 子进程的隔离环境
            python_exe: Python 解释器路径
        """
        self.python_exe = python_exe
        self.env = env
        self.project_root = Path(__file__).resolve().parent.parent.parent

    def run_cli(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        """执行 CLI 命令。

        Args:
            args: CLI 命令参数（不包括 python -m src.main）
            check: 是否检查退出码

        Returns:
            subprocess.CompletedProcess: 命令执行结果
        """
        cmd = [
            self.python_exe,
            str(self.project_root / "tests" / "offline_entrypoint.py"),
            "cli",
            *args,
        ]

        result = subprocess.run(
            cmd,
            cwd=self.project_root,
            env=self.env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",  # 处理编码错误
            timeout=30,
            check=False,
        )

        if check and result.returncode != 0:
            print(f"Command failed: {' '.join(cmd)}")
            print(f"STDOUT:\n{result.stdout}")
            print(f"STDERR:\n{result.stderr}")
            pytest.fail(f"Command exited with code {result.returncode}")

        return result


@pytest.fixture
def cli(tmp_path: Path) -> CLITester:
    """创建带空白数据库与显式隔离环境的离线 CLI 测试器。"""
    data_dir = tmp_path / "data"
    db_path = data_dir / "db" / "knowledge_vault.db"
    runtime_paths = {
        "DATA_DIR": data_dir,
        "DB_PATH": db_path,
        "VAULT_DIR": data_dir / "vault",
        "VECTOR_DIR": data_dir / "vectors",
        "LOG_DIR": data_dir / "logs",
        "TMP_DIR": data_dir / "tmp",
    }
    for key, path in runtime_paths.items():
        if key == "DB_PATH":
            path.parent.mkdir(parents=True, exist_ok=True)
        else:
            path.mkdir(parents=True, exist_ok=True)

    SQLiteStore(db_path).initialize()
    env = prepare_offline_child_env(
        project_root=Path(__file__).resolve().parents[2],
        runtime_overrides=runtime_paths,
    )
    # Business commands now correctly require a READY runtime.  This fixture
    # deliberately seeds an already initialized, isolated database and asks
    # the child-only harness to add its matching secret-free runtime snapshot.
    env["PKV_TEST_SYNTHETIC_RUNTIME_READY"] = "1"
    return CLITester(env=env)


@pytest.fixture
def unready_cli(tmp_path: Path) -> CLITester:
    """A deliberately uninitialized isolated root for parse-order contracts."""
    data_dir = tmp_path / "unready-data"
    runtime_paths = {
        "DATA_DIR": data_dir,
        "DB_PATH": data_dir / "db" / "knowledge_vault.db",
        "VAULT_DIR": data_dir / "vault",
        "VECTOR_DIR": data_dir / "vectors",
        "LOG_DIR": data_dir / "logs",
        "TMP_DIR": data_dir / "tmp",
    }
    env = prepare_offline_child_env(
        project_root=Path(__file__).resolve().parents[2],
        runtime_overrides=runtime_paths,
    )
    return CLITester(env=env)


@pytest.fixture
def absent_root_cli(tmp_path: Path) -> tuple[CLITester, Path]:
    """Build an L3 child whose selected data root does not exist at all."""

    data_dir = tmp_path / "absent-data-root"
    runtime_paths = {
        "DATA_DIR": data_dir,
        "DB_PATH": data_dir / "db" / "knowledge_vault.db",
        "VAULT_DIR": data_dir / "vault",
        "VECTOR_DIR": data_dir / "vectors",
        "LOG_DIR": data_dir / "logs",
        "TMP_DIR": data_dir / "tmp",
    }
    assert not os.path.lexists(data_dir)
    env = prepare_offline_child_env(
        project_root=Path(__file__).resolve().parents[2],
        runtime_overrides=runtime_paths,
    )
    # The test-only entrypoint switch preserves the otherwise nonexistent root
    # instead of running its normal Config.ensure_dirs() setup step.
    env.pop("PKV_TEST_SYNTHETIC_RUNTIME_READY", None)
    env["PKV_TEST_ABSENT_DATA_ROOT"] = "1"
    assert not os.path.lexists(data_dir)
    return CLITester(env=env), data_dir


# ========== 基础命令测试 ==========


def test_cli_help_command(cli: CLITester):
    """测试 --help 命令。"""
    result = cli.run_cli("--help")
    assert result.returncode == 0
    assert "Personal Knowledge Vault" in result.stdout
    assert "archive" in result.stdout
    assert "search" in result.stdout
    assert "show" in result.stdout
    assert "list" in result.stdout
    assert "config" in result.stdout
    assert "stats" in result.stdout


def test_cli_version_command(cli: CLITester):
    """测试 --version 命令。"""
    result = cli.run_cli("--version")
    assert result.returncode == 0
    assert "0.8.1" in result.stdout


def test_archive_help(cli: CLITester):
    """测试 archive --help 命令。"""
    result = cli.run_cli("archive", "--help")
    assert result.returncode == 0
    assert "URL_OR_PATH" in result.stdout
    assert "--skip-sharpen" in result.stdout
    assert "--tags" in result.stdout
    assert "--quiet" in result.stdout
    assert "--type" in result.stdout


def test_search_help(cli: CLITester):
    """测试 search --help 命令。"""
    result = cli.run_cli("search", "--help")
    assert result.returncode == 0
    assert "QUERY" in result.stdout
    assert "--strategy" in result.stdout
    assert "--limit" in result.stdout
    assert "--format" in result.stdout


def test_show_help(cli: CLITester):
    """测试 show --help 命令。"""
    result = cli.run_cli("show", "--help")
    assert result.returncode == 0
    assert "ID_OR_URL" in result.stdout
    assert "--url" in result.stdout
    assert "--raw" in result.stdout


def test_list_help(cli: CLITester):
    """测试 list --help 命令。"""
    result = cli.run_cli("list", "--help")
    assert result.returncode == 0
    assert "--tag" in result.stdout
    assert "--sort" in result.stdout
    assert "--limit" in result.stdout


def test_config_help(cli: CLITester):
    """测试 config --help 命令。"""
    result = cli.run_cli("config", "--help")
    assert result.returncode == 0
    assert "show" in result.stdout
    assert "get" in result.stdout
    assert "set" in result.stdout


def test_stats_help(cli: CLITester):
    """测试 stats --help 命令。"""
    result = cli.run_cli("stats", "--help")
    assert result.returncode == 0


def test_config_show_command(cli: CLITester):
    """测试 config show 命令。"""
    result = cli.run_cli("config", "show")
    assert result.returncode == 0
    assert "当前配置" in result.stdout
    for key in ("data_dir", "vault_dir", "db_path", "storage.vector_index_dir"):
        assert key in result.stdout


def test_config_get_valid_key(cli: CLITester):
    """测试 config get 命令（有效的键）。"""
    # 使用已知的有效配置键
    result = cli.run_cli("config", "get", "db_path")
    assert result.returncode == 0
    assert "".join(result.stdout.splitlines()) == cli.env["DB_PATH"]


def test_invalid_command(cli: CLITester):
    """测试无效命令。"""
    result = cli.run_cli("invalid-command-xyz", check=False)
    assert result.returncode == 2
    assert "No such command 'invalid-command-xyz'" in result.stderr


def test_search_missing_query(cli: CLITester):
    """测试 search 命令缺少必需参数。"""
    result = cli.run_cli("search", check=False)
    assert result.returncode == 2
    assert "Missing argument 'QUERY'" in result.stderr


def test_unready_runtime_does_not_block_help_or_click_validation(
    unready_cli: CLITester,
):
    """Click must finish eager help/required-argument handling before READY gate."""
    help_result = unready_cli.run_cli("archive", "--help", check=False)
    version_result = unready_cli.run_cli("--version", check=False)
    validation_result = unready_cli.run_cli("search", check=False)

    assert help_result.returncode == 0
    assert "URL_OR_PATH" in help_result.stdout
    assert version_result.returncode == 0
    assert "0.8.1" in version_result.stdout
    assert validation_result.returncode == 2
    assert "Missing argument 'QUERY'" in validation_result.stderr
    assert "runtime_readiness" not in validation_result.stderr


def test_absent_data_root_cli_is_status_only_without_mutation(
    absent_root_cli: tuple[CLITester, Path],
):
    """A real CLI child must inspect an absent root without materializing it."""

    cli, data_root = absent_root_cli
    inspect_result = cli.run_cli("inspect")

    inspection = json.loads(inspect_result.stdout)
    assert inspection["status"] == "success"
    assert inspection["readiness"] == "setup_required"
    assert inspection["inspection"]["readiness"] == "setup_required"
    assert inspection["plan"] is not None
    assert not os.path.lexists(data_root)

    rejected = cli.run_cli("stats", check=False)
    assert rejected.returncode == 1
    assert json.loads(rejected.stderr) == {
        "adapter": "cli",
        "code": "setup_required",
        "recoverable": True,
        "stage": "runtime_readiness",
        "status": "error",
    }
    assert not os.path.lexists(data_root)


def test_show_missing_id(cli: CLITester):
    """测试 show 命令缺少必需参数。"""
    result = cli.run_cli("show", check=False)
    assert result.returncode == 1
    assert "错误: 请提供 knowledge_id 或 --url" in result.stdout


def test_verbose_flag(cli: CLITester):
    """测试 --verbose 全局参数。"""
    result = cli.run_cli("--verbose", "config", "show")
    assert result.returncode == 0
    assert "当前配置" in result.stdout


def test_debug_flag(cli: CLITester):
    """测试 --debug 全局参数。"""
    result = cli.run_cli("--debug", "config", "show")
    assert result.returncode == 0
    assert "当前配置" in result.stdout


def test_multiple_global_flags(cli: CLITester):
    """测试多个全局参数组合。"""
    result = cli.run_cli("--verbose", "--debug", "config", "show")
    assert result.returncode == 0
    assert "当前配置" in result.stdout


def test_help_for_all_commands(cli: CLITester):
    """测试所有命令的 --help 都能正常工作。"""
    commands = ["archive", "search", "show", "list", "config", "stats"]
    for cmd in commands:
        result = cli.run_cli(cmd, "--help")
        assert result.returncode == 0, f"Command '{cmd} --help' failed"


# ========== 输出格式测试（不需要数据） ==========


def test_search_json_format_no_results(cli: CLITester):
    """测试 search 命令的 JSON 输出格式（无结果也应该返回有效 JSON）。"""
    result = cli.run_cli(
        "search",
        "不存在的关键词xyz123abc",
        "--strategy",
        "bm25",
        "--format",
        "json",
    )

    assert result.returncode == 0
    data = json.loads(result.stdout)
    assert data == {
        "query": "不存在的关键词xyz123abc",
        "status": "no_hits",
        "strategy": "bm25",
        "total": 0,
        "issues": [],
        "results": [],
    }


def test_stats_output_format(cli: CLITester):
    """测试 stats 命令的输出格式。"""
    result = cli.run_cli("stats")
    assert result.returncode == 0
    assert "知识库统计" in result.stdout
    assert "总条目数: 0" in result.stdout
    assert "暂无标签" in result.stdout
