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
import src.utils.config as config_module
from src.utils.config import get_config


def _require_live_api_config(config: object) -> None:
    """显式 live 模式下，缺少凭据必须失败，且错误中不得包含凭据值。"""
    required_fields = {
        "ai.llm.api_key": getattr(config, "llm_api_key", None),
        "ai.embedding.api_key": getattr(config, "embd_api_key", None),
    }
    missing = [name for name, value in required_fields.items() if not value]
    if missing:
        pytest.fail(
            "PKV_RUN_LIVE=1 需要在 config/local.yaml 配置: " + ", ".join(missing),
            pytrace=False,
        )


def _assert_cli_success(
    result: subprocess.CompletedProcess[str], operation: str
) -> None:
    """断言 CLI 成功；失败摘要不回显可能含敏感信息的命令输出。"""
    assert result.returncode == 0, (
        f"{operation}失败: exit_code={result.returncode}, "
        f"stdout_length={len(result.stdout)}, stderr_length={len(result.stderr)}"
    )


@pytest.mark.skipif(
    os.getenv("PKV_RUN_LIVE") != "1",
    reason="需要 PKV_RUN_LIVE=1 才运行真实 API 测试",
)
class TestRealAPIWorkflow:
    """真实 API 端到端测试。"""

    @pytest.fixture
    def temp_env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """创建临时环境，并在隔离路径生效后检查真实 API 配置。"""
        # 设置临时数据目录
        data_dir = tmp_path / ".data"
        data_dir.mkdir(exist_ok=True)

        env = os.environ.copy()
        runtime_paths = {
            "DATA_DIR": data_dir,
            "DB_PATH": data_dir / "db" / "knowledge_vault.db",
            "VAULT_DIR": data_dir / "vault",
            "VECTOR_DIR": data_dir / "vectors",
            "LOG_DIR": data_dir / "logs",
            "TMP_DIR": data_dir / "tmp",
        }
        for key, path in runtime_paths.items():
            value = str(path)
            env[key] = value
            monkeypatch.setenv(key, value)

        previous_config = config_module._config_instance
        config_module._config_instance = None
        try:
            config = get_config()
            _require_live_api_config(config)
            yield env
        finally:
            config_module._config_instance = previous_config

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

        _assert_cli_success(result, "归档真实网页")
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
        _assert_cli_success(result, "空数据库统计")

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

        _assert_cli_success(archive_result, "完整流程归档")

        # 2. 搜索刚刚归档的内容
        search_result = self.run_cli(
            "search", "DeepSeek", "--limit", "5", env=temp_env
        )

        self.safe_print(f"Search result: {search_result.returncode}")
        self.safe_print(f"Search found: {'deepseek' in search_result.stdout.lower()}")

        _assert_cli_success(search_result, "检索完整流程归档条目")
        assert "deepseek" in search_result.stdout.lower(), (
            "搜索未命中刚归档的 DeepSeek 条目"
        )

        # 3. 列出所有条目
        list_result = self.run_cli("list", "--limit", "10", env=temp_env)

        self.safe_print(f"List result: {list_result.returncode}")
        self.safe_print(f"List has output: {len(list_result.stdout) > 0}")

        _assert_cli_success(list_result, "列出完整流程归档条目")
        assert "deepseek" in list_result.stdout.lower(), "列表未包含刚归档的 DeepSeek 条目"

        # 4. 显示第一个条目（假设 ID=1）
        show_result = self.run_cli("show", "1", env=temp_env)

        self.safe_print(f"Show result: {show_result.returncode}")
        self.safe_print(f"Show has output: {len(show_result.stdout) > 0}")

        _assert_cli_success(show_result, "显示完整流程归档条目")
        assert "deepseek" in show_result.stdout.lower(), "详情未命中刚归档的 DeepSeek 条目"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s", "--tb=short", "-m", "not slow"])
