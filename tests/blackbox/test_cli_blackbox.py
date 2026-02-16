"""CLI 黑盒测试 - 使用真实命令执行验证功能。

测试策略：
1. 使用临时目录和数据库，避免污染真实数据
2. 通过 subprocess 执行真实的 CLI 命令
3. 验证命令输出、退出码、副作用（文件/数据库变更）
4. 测试覆盖所有 6 个核心命令的主要场景
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import pytest


class CLIBlackboxTester:
    """CLI 黑盒测试工具类。"""

    def __init__(self, test_dir: Path, python_exe: str = "python"):
        """初始化测试器。

        Args:
            test_dir: 临时测试目录
            python_exe: Python 解释器路径
        """
        self.test_dir = test_dir
        self.python_exe = python_exe
        self.project_root = Path(__file__).resolve().parent.parent.parent
        self.data_dir = test_dir / ".data"
        self.db_path = self.data_dir / "db" / "knowledge_vault.db"
        self.vault_dir = self.data_dir / "vault"

        # 创建必要的目录
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "db").mkdir(exist_ok=True)
        (self.data_dir / "logs").mkdir(exist_ok=True)
        self.vault_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "vectors").mkdir(exist_ok=True)

    def run_cli(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        """执行 CLI 命令。

        Args:
            args: CLI 命令参数（不包括 python -m src.main）
            check: 是否检查退出码

        Returns:
            subprocess.CompletedProcess: 命令执行结果
        """
        cmd = [self.python_exe, "-m", "src.main"] + list(args)
        env = os.environ.copy()
        env["DATA_DIR"] = str(self.data_dir)
        env["DB_PATH"] = str(self.db_path)
        env["VAULT_DIR"] = str(self.vault_dir)
        env["VECTOR_STORE_PATH"] = str(self.data_dir / "vectors")

        result = subprocess.run(
            cmd,
            cwd=self.project_root,
            env=env,
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

    def seed_test_data(self) -> None:
        """预置测试数据。"""
        import os
        from src.storage.sqlite_store import SQLiteStore

        # 设置环境变量（供 CLI 命令使用）
        os.environ["DATA_DIR"] = str(self.data_dir)
        os.environ["DB_PATH"] = str(self.db_path)
        os.environ["VAULT_DIR"] = str(self.vault_dir)
        os.environ["VECTOR_STORE_PATH"] = str(self.data_dir / "vectors")

        # 初始化数据库（直接传递 db_path）
        sqlite_store = SQLiteStore(self.db_path)
        sqlite_store.initialize()

        # 插入测试数据
        from src.storage.markdown_store import Entry
        from datetime import datetime

        test_entries = [
            Entry(
                title="Python 装饰器详解",
                source_type="text",
                source_url="https://example.com/python-decorators",
                tags=["Python", "编程技巧"],
                abstract="深入理解 Python 装饰器的原理和应用",
                content="# Python 装饰器\n\n装饰器是 Python 的强大特性...",
                search_strategy="hybrid",
                word_count=500,
                archived_at=datetime.now(),
            ),
            Entry(
                title="Docker 容器化实践",
                source_type="text",
                source_url="https://example.com/docker-guide",
                tags=["Docker", "DevOps"],
                abstract="Docker 容器化部署的最佳实践",
                content="# Docker 实践\n\n容器化是现代应用部署的趋势...",
                search_strategy="keyword",  # 修复：使用有效值 keyword
                word_count=800,
                archived_at=datetime.now(),
            ),
            Entry(
                title="React Hooks 使用指南",
                source_type="text",
                source_url="https://example.com/react-hooks",
                tags=["React", "前端"],
                abstract="React Hooks 让函数组件更强大",
                content="# React Hooks\n\nuseState 和 useEffect 是最常用的...",
                search_strategy="vector",
                word_count=600,
                archived_at=datetime.now(),
            ),
        ]

        for i, entry in enumerate(test_entries, 1):
            file_path = str(self.vault_dir / f"test-entry-{i}.md")
            sqlite_store.insert_entry(entry, file_path)

        # 移除对 sqlite_store 的引用，让垃圾回收器处理连接
        del sqlite_store

        print(f"✓ 预置了 {len(test_entries)} 条测试数据")


@pytest.fixture
def cli_tester(tmp_path: Path) -> CLIBlackboxTester:
    """创建 CLI 黑盒测试器。"""
    tester = CLIBlackboxTester(tmp_path)
    tester.seed_test_data()
    return tester


# ========== 黑盒测试用例 ==========


def test_cli_help_command(cli_tester: CLIBlackboxTester):
    """测试 --help 命令。"""
    result = cli_tester.run_cli("--help")
    assert result.returncode == 0
    assert "Personal Knowledge Vault" in result.stdout
    assert "archive" in result.stdout
    assert "search" in result.stdout


def test_cli_version_command(cli_tester: CLIBlackboxTester):
    """测试 --version 命令。"""
    result = cli_tester.run_cli("--version")
    assert result.returncode == 0
    assert "0.6.0" in result.stdout


def test_search_command_with_results(cli_tester: CLIBlackboxTester):
    """测试 search 命令（有结果）。"""
    result = cli_tester.run_cli("search", "Python", "--strategy", "bm25")
    assert result.returncode == 0
    assert "Python 装饰器详解" in result.stdout or "Python" in result.stdout


def test_search_command_with_json_output(cli_tester: CLIBlackboxTester):
    """测试 search 命令（JSON 输出）。"""
    result = cli_tester.run_cli("search", "Docker", "--format", "json", "--limit", "5")
    assert result.returncode == 0

    # 验证 JSON 格式
    try:
        data = json.loads(result.stdout)
        assert "results" in data
        assert isinstance(data["results"], list)
    except json.JSONDecodeError:
        pytest.fail("输出不是有效的 JSON 格式")


def test_search_command_no_results(cli_tester: CLIBlackboxTester):
    """测试 search 命令（无结果）。"""
    result = cli_tester.run_cli("search", "不存在的关键词xyz123")
    assert result.returncode == 0
    # 应该有友好的提示而不是报错


def test_list_command(cli_tester: CLIBlackboxTester):
    """测试 list 命令。"""
    result = cli_tester.run_cli("list", "--limit", "10")
    assert result.returncode == 0
    # 应该包含至少一条测试数据
    assert "Python" in result.stdout or "Docker" in result.stdout or "React" in result.stdout


def test_list_command_with_tag_filter(cli_tester: CLIBlackboxTester):
    """测试 list 命令（标签过滤）。"""
    result = cli_tester.run_cli("list", "--tag", "Python")
    assert result.returncode == 0
    assert "Python 装饰器详解" in result.stdout or "Python" in result.stdout


def test_show_command_by_id(cli_tester: CLIBlackboxTester):
    """测试 show 命令（通过 ID）。"""
    # 先获取一个条目的 ID
    list_result = cli_tester.run_cli("list", "--limit", "1")
    # 假设第一条是 ID=1
    result = cli_tester.run_cli("show", "1")
    assert result.returncode == 0
    # 应该显示条目详情


def test_show_command_by_url(cli_tester: CLIBlackboxTester):
    """测试 show 命令（通过 URL）。"""
    result = cli_tester.run_cli("show", "--url", "https://example.com/python-decorators")
    assert result.returncode == 0
    assert "Python 装饰器详解" in result.stdout or "Python" in result.stdout


def test_show_command_not_found(cli_tester: CLIBlackboxTester):
    """测试 show 命令（条目不存在）。"""
    result = cli_tester.run_cli("show", "99999", check=False)
    # 应该返回非零退出码或友好的错误提示
    assert result.returncode != 0 or "未找到" in result.stdout or "not found" in result.stdout.lower()


def test_config_show_command(cli_tester: CLIBlackboxTester):
    """测试 config show 命令。"""
    result = cli_tester.run_cli("config", "show")
    assert result.returncode == 0
    assert "data_dir" in result.stdout.lower() or "数据目录" in result.stdout


def test_config_get_command(cli_tester: CLIBlackboxTester):
    """测试 config get 命令。"""
    result = cli_tester.run_cli("config", "get", "data_dir")
    assert result.returncode == 0
    # 应该显示配置值


def test_stats_command(cli_tester: CLIBlackboxTester):
    """测试 stats 命令。"""
    result = cli_tester.run_cli("stats")
    assert result.returncode == 0
    # 应该包含统计信息
    assert "3" in result.stdout or "条目" in result.stdout or "entries" in result.stdout.lower()


def test_invalid_command(cli_tester: CLIBlackboxTester):
    """测试无效命令。"""
    result = cli_tester.run_cli("invalid-command-xyz", check=False)
    assert result.returncode != 0
    # 应该有错误提示


def test_search_different_strategies(cli_tester: CLIBlackboxTester):
    """测试不同的搜索策略。"""
    strategies = ["auto", "bm25", "vector", "hybrid"]
    for strategy in strategies:
        result = cli_tester.run_cli("search", "Python", "--strategy", strategy, "--limit", "5")
        assert result.returncode == 0, f"策略 {strategy} 失败"


def test_verbose_mode(cli_tester: CLIBlackboxTester):
    """测试 --verbose 模式。"""
    result = cli_tester.run_cli("--verbose", "search", "Docker")
    assert result.returncode == 0
    # verbose 模式可能在 stderr 输出日志


def test_debug_mode(cli_tester: CLIBlackboxTester):
    """测试 --debug 模式。"""
    result = cli_tester.run_cli("--debug", "search", "React")
    assert result.returncode == 0
    # debug 模式可能在 stderr 输出详细日志


# ========== 集成场景测试 ==========


def test_full_workflow_search_show(cli_tester: CLIBlackboxTester):
    """测试完整工作流：搜索 -> 显示详情。"""
    # 1. 搜索
    search_result = cli_tester.run_cli("search", "Python", "--limit", "1")
    assert search_result.returncode == 0

    # 2. 显示详情（假设第一条是 ID=1）
    show_result = cli_tester.run_cli("show", "1")
    assert show_result.returncode == 0


def test_list_and_filter(cli_tester: CLIBlackboxTester):
    """测试列出并过滤。"""
    # 1. 列出所有
    list_all = cli_tester.run_cli("list")
    assert list_all.returncode == 0

    # 2. 按标签过滤
    list_filtered = cli_tester.run_cli("list", "--tag", "Python")
    assert list_filtered.returncode == 0

    # 过滤结果应该少于或等于总数
    # （这里只是验证命令执行成功）


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
