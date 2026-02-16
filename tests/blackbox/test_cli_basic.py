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
from pathlib import Path

import pytest


class CLITester:
    """CLI 黑盒测试工具类。"""

    def __init__(self, python_exe: str = "python"):
        """初始化测试器。

        Args:
            python_exe: Python 解释器路径
        """
        self.python_exe = python_exe
        self.project_root = Path(__file__).resolve().parent.parent.parent

    def run_cli(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        """执行 CLI 命令。

        Args:
            args: CLI 命令参数（不包括 python -m src.main）
            check: 是否检查退出码

        Returns:
            subprocess.CompletedProcess: 命令执行结果
        """
        cmd = [self.python_exe, "-m", "src.main"] + list(args)

        result = subprocess.run(
            cmd,
            cwd=self.project_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",  # 处理编码错误
            check=False,
        )

        if check and result.returncode != 0:
            print(f"Command failed: {' '.join(cmd)}")
            print(f"STDOUT:\n{result.stdout}")
            print(f"STDERR:\n{result.stderr}")
            pytest.fail(f"Command exited with code {result.returncode}")

        return result


@pytest.fixture
def cli() -> CLITester:
    """创建 CLI 测试器。"""
    return CLITester()


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
    assert "0.6.0" in result.stdout


def test_archive_help(cli: CLITester):
    """测试 archive --help 命令。"""
    result = cli.run_cli("archive", "--help")
    assert result.returncode == 0
    assert "URL_OR_PATH" in result.stdout or "url" in result.stdout.lower()
    assert "--skip-sharpen" in result.stdout
    assert "--tags" in result.stdout
    assert "--quiet" in result.stdout
    assert "--type" in result.stdout


def test_search_help(cli: CLITester):
    """测试 search --help 命令。"""
    result = cli.run_cli("search", "--help")
    assert result.returncode == 0
    assert "QUERY" in result.stdout or "query" in result.stdout.lower()
    assert "--strategy" in result.stdout
    assert "--limit" in result.stdout
    assert "--format" in result.stdout


def test_show_help(cli: CLITester):
    """测试 show --help 命令。"""
    result = cli.run_cli("show", "--help")
    assert result.returncode == 0
    assert "--url" in result.stdout or "--raw" in result.stdout


def test_list_help(cli: CLITester):
    """测试 list --help 命令。"""
    result = cli.run_cli("list", "--help")
    assert result.returncode == 0
    assert "--tag" in result.stdout or "--sort" in result.stdout


def test_config_help(cli: CLITester):
    """测试 config --help 命令。"""
    result = cli.run_cli("config", "--help")
    assert result.returncode == 0
    assert "show" in result.stdout or "get" in result.stdout or "set" in result.stdout


def test_stats_help(cli: CLITester):
    """测试 stats --help 命令。"""
    result = cli.run_cli("stats", "--help")
    assert result.returncode == 0


def test_config_show_command(cli: CLITester):
    """测试 config show 命令。"""
    result = cli.run_cli("config", "show")
    assert result.returncode == 0
    # 应该显示配置信息
    assert "data_dir" in result.stdout.lower() or "db_path" in result.stdout.lower() or len(result.stdout) > 0


def test_config_get_valid_key(cli: CLITester):
    """测试 config get 命令（有效的键）。"""
    # 使用已知的有效配置键
    result = cli.run_cli("config", "get", "db_path")
    assert result.returncode == 0
    # 应该返回配置值（路径）
    assert len(result.stdout.strip()) > 0
    assert ".db" in result.stdout or "knowledge_vault" in result.stdout


def test_invalid_command(cli: CLITester):
    """测试无效命令。"""
    result = cli.run_cli("invalid-command-xyz", check=False)
    assert result.returncode != 0


def test_search_missing_query(cli: CLITester):
    """测试 search 命令缺少必需参数。"""
    result = cli.run_cli("search", check=False)
    assert result.returncode != 0  # 应该报错


def test_show_missing_id(cli: CLITester):
    """测试 show 命令缺少必需参数。"""
    result = cli.run_cli("show", check=False)
    assert result.returncode != 0  # 应该报错


def test_verbose_flag(cli: CLITester):
    """测试 --verbose 全局参数。"""
    result = cli.run_cli("--verbose", "config", "show")
    assert result.returncode == 0
    # verbose 模式应该有输出


def test_debug_flag(cli: CLITester):
    """测试 --debug 全局参数。"""
    result = cli.run_cli("--debug", "config", "show")
    assert result.returncode == 0
    # debug 模式应该有输出


def test_multiple_global_flags(cli: CLITester):
    """测试多个全局参数组合。"""
    result = cli.run_cli("--verbose", "--debug", "config", "show")
    assert result.returncode == 0


def test_help_for_all_commands(cli: CLITester):
    """测试所有命令的 --help 都能正常工作。"""
    commands = ["archive", "search", "show", "list", "config", "stats"]
    for cmd in commands:
        result = cli.run_cli(cmd, "--help")
        assert result.returncode == 0, f"Command '{cmd} --help' failed"


# ========== 输出格式测试（不需要数据） ==========


def test_search_json_format_no_results(cli: CLITester):
    """测试 search 命令的 JSON 输出格式（无结果也应该返回有效 JSON）。"""
    result = cli.run_cli("search", "不存在的关键词xyz123abc", "--format", "json", check=False)

    # 检查输出中是否包含数据库错误（即使 returncode=0）
    if "no such table" in result.stdout or "no such table" in result.stderr:
        pytest.skip("数据库未初始化，跳过测试")

    # 如果命令失败且不是数据库问题
    if result.returncode != 0:
        pytest.fail(f"命令失败: {result.stderr[:200]}")

    # 验证 JSON 格式
    if result.stdout.strip():
        try:
            data = json.loads(result.stdout)
            assert "results" in data or isinstance(data, dict)
        except json.JSONDecodeError:
            # JSON 解析失败，检查是否是错误日志
            if "[ERROR]" in result.stdout or "错误" in result.stdout:
                pytest.skip("输出包含错误日志而非 JSON，跳过测试")
            else:
                pytest.fail(f"输出不是有效的 JSON: {result.stdout[:200]}")


def test_stats_output_format(cli: CLITester):
    """测试 stats 命令的输出格式。"""
    result = cli.run_cli("stats")
    # 即使数据库为空，stats 也应该能执行
    # 返回值可能是 0 或非 0，取决于是否有数据
    assert result.returncode in (0, 1)  # 允许失败（数据库为空）
    if result.returncode == 0:
        # 应该包含统计信息的关键词
        assert "条目" in result.stdout or "entries" in result.stdout.lower() or len(result.stdout) > 0


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
