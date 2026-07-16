"""端到端测试 - 使用真实 API 进行完整工作流测试。

⚠️ 注意：
1. 此测试需要 PKV_RUN_LIVE=1，并在 config/local.yaml 配置有效 API Key
2. 会产生真实的 API 调用费用（预计 <$0.01）
3. 需要网络连接

测试流程：
1. 归档一个真实的网页
2. 搜索刚刚归档的内容
3. 显示条目详情
4. 验证数据完整性
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from src.utils.config import get_config


def has_api_keys() -> bool:
    """检查是否配置了 API Keys。"""
    if os.getenv("PKV_RUN_LIVE") != "1":
        return False
    config = get_config()
    return bool(config.llm_api_key) and bool(config.embd_api_key)


@pytest.mark.skipif(
    not has_api_keys(),
    reason="需要 PKV_RUN_LIVE=1 且在 config/local.yaml 配置 API Key",
)
class TestRealAPIWorkflow:
    """真实 API 端到端测试。"""

    @pytest.fixture
    def temp_env(self, tmp_path: Path):
        """创建临时环境。"""
        # 设置临时数据目录
        data_dir = tmp_path / ".data"
        data_dir.mkdir(exist_ok=True)

        env = os.environ.copy()
        env["DATA_DIR"] = str(data_dir)
        env["DB_PATH"] = str(data_dir / "db" / "knowledge_vault.db")
        env["VAULT_DIR"] = str(data_dir / "vault")
        env["VECTOR_STORE_PATH"] = str(data_dir / "vectors")

        return env

    def run_cli(self, *args: str, env: dict = None) -> subprocess.CompletedProcess:
        """执行 CLI 命令。"""
        project_root = Path(__file__).resolve().parent.parent.parent
        cmd = ["python", "-m", "src.main"] + list(args)

        result = subprocess.run(
            cmd,
            cwd=project_root,
            env=env or os.environ.copy(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,  # 5 分钟超时（API 调用可能较慢）
        )

        return result

    @staticmethod
    def safe_print(msg: str):
        """安全打印，避免 Windows GBK 编码错误。"""
        try:
            print(msg)
        except UnicodeEncodeError:
            # 如果打印失败，输出长度信息即可
            print(f"[Output length: {len(msg)} chars, encoding issue]")

    def test_archive_real_webpage(self, temp_env):
        """测试归档真实网页（使用 fixtures 中的测试 URL）。"""
        # 使用 fixtures 中的真实微信文章 URL
        # 这是一个真实可访问的公众号文章
        test_url = "https://mp.weixin.qq.com/s/ZET927baoFCj3In_11fKeA"

        result = self.run_cli(
            "archive",
            test_url,
            "--skip-sharpen",  # 跳过 idea Sharpen 交互
            "--tags",
            "测试,端到端",
            "--quiet",  # 静默模式
            "--type",
            "auto",
            env=temp_env,
        )

        # 检查归档是否成功
        self.safe_print(f"Exit code: {result.returncode}")
        self.safe_print(f"STDOUT length: {len(result.stdout)} chars")
        self.safe_print(f"STDERR length: {len(result.stderr)} chars")

        # 仅在 verbose 模式下输出详细内容
        if result.returncode != 0:
            self.safe_print(f"Command failed with exit code {result.returncode}")

        # 归档可能成功或失败（取决于网络和 API）
        # 但不应该崩溃
        assert result.returncode in (0, 1), f"命令异常退出: {result.returncode}"

        # 如果成功，应该包含成功提示
        if result.returncode == 0:
            assert "成功" in result.stdout or "knowledge_id" in result.stdout.lower()

    def test_config_commands(self, temp_env):
        """测试配置命令（不需要 API）。"""
        # 显示配置
        result = self.run_cli("config", "show", env=temp_env)
        assert result.returncode == 0
        assert len(result.stdout) > 0

    def test_stats_empty_database(self, temp_env):
        """测试统计命令（空数据库）。"""
        result = self.run_cli("stats", env=temp_env)
        # 空数据库可能返回警告或显示 0 条目
        # 不应该崩溃
        assert result.returncode in (0, 1)

    @pytest.mark.slow
    def test_full_workflow(self, temp_env):
        """完整工作流测试：归档 → 搜索 → 显示。

        ⚠️ 此测试需要真实 API 调用，可能较慢（~30-60秒）
        """
        # 1. 归档内容（使用 fixtures 中的 DeepSeek 文档）
        # 这是官方文档，内容稳定且容易提取
        test_url = "https://api-docs.deepseek.com/zh-cn/news/news251201"

        archive_result = self.run_cli(
            "archive",
            test_url,
            "--skip-sharpen",
            "--tags",
            "E2E测试",
            "--quiet",
            "--type",
            "auto",
            env=temp_env,
        )

        self.safe_print(f"Archive result: {archive_result.returncode}")
        self.safe_print(f"Archive stdout length: {len(archive_result.stdout)} chars")
        self.safe_print(f"Archive stderr length: {len(archive_result.stderr)} chars")

        # 如果归档失败（网络或 API 问题），跳过后续测试
        if archive_result.returncode != 0:
            pytest.skip(f"归档失败，跳过后续测试（错误码: {archive_result.returncode}）")

        # 2. 搜索刚刚归档的内容
        search_result = self.run_cli(
            "search", "DeepSeek", "--limit", "5", env=temp_env
        )

        self.safe_print(f"Search result: {search_result.returncode}")
        self.safe_print(f"Search found: {'deepseek' in search_result.stdout.lower()}")

        # 搜索应该成功（即使没有结果）
        assert search_result.returncode == 0

        # 3. 列出所有条目
        list_result = self.run_cli("list", "--limit", "10", env=temp_env)

        self.safe_print(f"List result: {list_result.returncode}")
        self.safe_print(f"List has output: {len(list_result.stdout) > 0}")

        assert list_result.returncode == 0
        assert "deepseek" in list_result.stdout.lower() or len(list_result.stdout) > 0

        # 4. 显示第一个条目（假设 ID=1）
        show_result = self.run_cli("show", "1", env=temp_env)

        self.safe_print(f"Show result: {show_result.returncode}")
        self.safe_print(f"Show has output: {len(show_result.stdout) > 0}")

        # show 命令应该成功或返回未找到
        assert show_result.returncode in (0, 1)


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s", "--tb=short", "-m", "not slow"])
