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
import subprocess
import sys
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# 子进程只加载版本化基础配置，避免读取开发机的 config/local.yaml 和 API Key。
ISOLATED_CLI_BOOTSTRAP = """
import sys
from pathlib import Path

import src.utils.config as config_module

base_config_path = Path(sys.argv.pop(1))


def load_base_config():
    return config_module.Config(str(base_config_path))


config_module._config_instance = load_base_config()

import src.cli.commands as commands_module

commands_module.Config = load_base_config

from src.main import main

main()
"""


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
        self.project_root = PROJECT_ROOT
        self.data_dir = test_dir / "test-data"
        self.db_path = self.data_dir / "db" / "knowledge_vault.db"
        self.vault_dir = self.data_dir / "vault"
        self.vector_dir = self.data_dir / "vectors"
        self.log_dir = self.data_dir / "logs"
        self.tmp_dir = self.data_dir / "tmp"

        # 创建必要的目录
        self.data_dir.mkdir(parents=True, exist_ok=True)
        for path in (
            self.db_path.parent,
            self.vault_dir,
            self.vector_dir,
            self.log_dir,
            self.tmp_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)

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
            "-c",
            ISOLATED_CLI_BOOTSTRAP,
            str(self.project_root / "config" / "config.yaml"),
            *args,
        ]
        env = os.environ.copy()
        env.update(
            {
                "DATA_DIR": str(self.data_dir),
                "DB_PATH": str(self.db_path),
                "VAULT_DIR": str(self.vault_dir),
                "VECTOR_DIR": str(self.vector_dir),
                "LOG_DIR": str(self.log_dir),
                "TMP_DIR": str(self.tmp_dir),
            }
        )

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
        from src.storage.sqlite_store import SQLiteStore

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


def test_cli_version_command(tmp_path: Path):
    """测试 --version 命令。"""
    cli_tester = CLIBlackboxTester(tmp_path)
    result = cli_tester.run_cli("--version")
    assert result.returncode == 0
    assert "0.8.0-alpha" in result.stdout


def test_search_command_with_results(cli_tester: CLIBlackboxTester):
    """测试 search 命令（有结果）。"""
    result = cli_tester.run_cli("search", "Python", "--strategy", "bm25")
    assert result.returncode == 0
    assert "找到 1 条结果 (bm25 策略)" in result.stdout
    assert "Python 装饰器详解" in result.stdout


def test_search_command_with_json_output(cli_tester: CLIBlackboxTester):
    """测试 search 命令（JSON 输出）。"""
    result = cli_tester.run_cli(
        "search",
        "Docker",
        "--strategy",
        "bm25",
        "--format",
        "json",
        "--limit",
        "5",
    )
    assert result.returncode == 0

    data = json.loads(result.stdout)
    assert set(data) == {"query", "strategy", "total", "results"}
    assert data["query"] == "Docker"
    assert data["strategy"] == "bm25"
    assert data["total"] == 1
    assert len(data["results"]) == data["total"]
    assert set(data["results"][0]) == {
        "entry_id",
        "title",
        "snippet",
        "score",
        "metadata",
    }
    assert data["results"][0]["title"] == "Docker 容器化实践"


def test_search_command_no_results(cli_tester: CLIBlackboxTester):
    """测试 search 命令（无结果）。"""
    result = cli_tester.run_cli(
        "search",
        "unfindabletoken9f8e7d6c",
        "--strategy",
        "bm25",
    )
    assert result.returncode == 0
    assert "找到 0 条结果 (bm25 策略)" in result.stdout
    assert "Python 装饰器详解" not in result.stdout


def test_list_command(cli_tester: CLIBlackboxTester):
    """测试 list 命令。"""
    result = cli_tester.run_cli("list", "--limit", "10")
    assert result.returncode == 0
    assert "Python 装饰器详解" in result.stdout
    assert "Docker 容器化实践" in result.stdout
    assert "React Hooks 使用指南" in result.stdout


def test_list_command_with_tag_filter(cli_tester: CLIBlackboxTester):
    """测试 list 命令（标签过滤）。"""
    result = cli_tester.run_cli("list", "--tag", "Python")
    assert result.returncode == 0
    assert "知识条目列表 (标签: Python)" in result.stdout
    assert "Python 装饰器详解" in result.stdout
    assert "Docker 容器化实践" not in result.stdout
    assert "React Hooks 使用指南" not in result.stdout


def test_show_command_by_id(cli_tester: CLIBlackboxTester):
    """测试 show 命令（通过 ID）。"""
    # 先获取一个条目的 ID
    cli_tester.run_cli("list", "--limit", "1")
    # 假设第一条是 ID=1
    result = cli_tester.run_cli("show", "1")
    assert result.returncode == 0
    assert "知识条目 #1" in result.stdout
    assert "Python 装饰器详解" in result.stdout
    assert "https://example.com/python-decorators" in result.stdout


def test_show_command_by_url(cli_tester: CLIBlackboxTester):
    """测试 show 命令（通过 URL）。"""
    result = cli_tester.run_cli("show", "--url", "https://example.com/python-decorators")
    assert result.returncode == 0
    assert "Python 装饰器详解" in result.stdout
    assert "https://example.com/python-decorators" in result.stdout


def test_show_command_not_found(cli_tester: CLIBlackboxTester):
    """测试 show 命令（条目不存在）。"""
    result = cli_tester.run_cli("show", "99999", check=False)
    assert result.returncode == 1
    assert "警告: 未找到对应条目" in result.stdout


def test_config_show_command(cli_tester: CLIBlackboxTester):
    """测试 config show 命令。"""
    result = cli_tester.run_cli("config", "show")
    assert result.returncode == 0
    for key in (
        "data_dir",
        "vault_dir",
        "db_path",
        "storage.vector_index_dir",
        "storage.log_dir",
        "storage.tmp_dir",
    ):
        assert key in result.stdout
    assert "ai.llm.api_key" in result.stdout
    assert "未设置" in result.stdout


def test_config_get_command(cli_tester: CLIBlackboxTester):
    """测试 config get 命令。"""
    result = cli_tester.run_cli("config", "get", "data_dir")
    assert result.returncode == 0
    assert "".join(result.stdout.splitlines()) == str(cli_tester.data_dir)


def test_stats_command(cli_tester: CLIBlackboxTester):
    """测试 stats 命令。"""
    result = cli_tester.run_cli("stats")
    assert result.returncode == 0
    assert "知识库统计" in result.stdout
    assert "总条目数: 3" in result.stdout
    assert "- text: 3" in result.stdout
    assert "Python (1)" in result.stdout


def test_invalid_command(cli_tester: CLIBlackboxTester):
    """测试无效命令。"""
    result = cli_tester.run_cli("invalid-command-xyz", check=False)
    assert result.returncode != 0
    # 应该有错误提示


def test_search_bm25_strategy(cli_tester: CLIBlackboxTester):
    """测试不依赖外部向量服务的 BM25 搜索策略。"""
    result = cli_tester.run_cli(
        "search",
        "Python",
        "--strategy",
        "bm25",
        "--limit",
        "5",
    )
    assert result.returncode == 0
    assert "找到 1 条结果 (bm25 策略)" in result.stdout
    assert "Python 装饰器详解" in result.stdout


def test_verbose_mode(cli_tester: CLIBlackboxTester):
    """测试 --verbose 模式。"""
    result = cli_tester.run_cli(
        "--verbose",
        "search",
        "Docker",
        "--strategy",
        "bm25",
    )
    assert result.returncode == 0
    assert "Docker 容器化实践" in result.stdout


def test_debug_mode(cli_tester: CLIBlackboxTester):
    """测试 --debug 模式。"""
    result = cli_tester.run_cli(
        "--debug",
        "search",
        "React",
        "--strategy",
        "bm25",
    )
    assert result.returncode == 0
    assert "React Hooks 使用指南" in result.stdout


# ========== 集成场景测试 ==========


def test_full_workflow_search_show(cli_tester: CLIBlackboxTester):
    """测试完整工作流：搜索 -> 显示详情。"""
    # 1. 搜索
    search_result = cli_tester.run_cli(
        "search",
        "Python",
        "--strategy",
        "bm25",
        "--limit",
        "1",
    )
    assert search_result.returncode == 0
    assert "Python 装饰器详解" in search_result.stdout

    # 2. 显示详情（假设第一条是 ID=1）
    show_result = cli_tester.run_cli("show", "1")
    assert show_result.returncode == 0
    assert "Python 装饰器详解" in show_result.stdout


def test_list_and_filter(cli_tester: CLIBlackboxTester):
    """测试列出并过滤。"""
    # 1. 列出所有
    list_all = cli_tester.run_cli("list")
    assert list_all.returncode == 0
    assert all(
        title in list_all.stdout
        for title in (
            "Python 装饰器详解",
            "Docker 容器化实践",
            "React Hooks 使用指南",
        )
    )

    # 2. 按标签过滤
    list_filtered = cli_tester.run_cli("list", "--tag", "Python")
    assert list_filtered.returncode == 0
    assert "Python 装饰器详解" in list_filtered.stdout
    assert "Docker 容器化实践" not in list_filtered.stdout


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
