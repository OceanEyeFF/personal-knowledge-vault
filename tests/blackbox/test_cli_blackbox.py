"""CLI 黑盒测试 - 使用真实命令执行验证功能。

测试策略：
1. 使用临时目录和数据库，避免污染真实数据
2. 通过 subprocess 执行真实的 CLI 命令
3. 验证命令输出、退出码、副作用（文件/数据库变更）
4. 测试覆盖所有 6 个核心命令的主要场景
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import pytest


PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
from tests.offline_runtime import prepare_offline_child_env


def _snapshot_vector_artifacts(vector_dir: Path) -> dict[str, tuple[str, int]]:
    """Return a complete content-and-mtime snapshot of a published vector tree."""

    snapshot: dict[str, tuple[str, int]] = {}
    for artifact in sorted(vector_dir.rglob("*")):
        if artifact.is_dir():
            continue
        assert artifact.is_file(), f"unexpected vector artifact type: {artifact}"
        stat_result = artifact.stat()
        snapshot[artifact.relative_to(vector_dir).as_posix()] = (
            hashlib.sha256(artifact.read_bytes()).hexdigest(),
            stat_result.st_mtime_ns,
        )
    return snapshot


def _remove_vector_writer_lock_sidecars(vector_dir: Path) -> None:
    """Normalize a freshly written fixture before checking a read-only CLI call."""

    for sidecar in vector_dir.rglob(".*.lock"):
        sidecar.unlink()


_WRITE_LEASE_HOLDER_SCRIPT = """
from pathlib import Path
import sys

from src.runtime.layout import RuntimeLayout
from src.runtime.write_lease import VaultWriteLease

layout = RuntimeLayout.resolve(
    resources_root=Path(sys.argv[1]),
    user_data_root=Path(sys.argv[2]),
    environment={},
)
ready_path = Path(sys.argv[3])
lease = VaultWriteLease(layout)
try:
    lease.acquire()
    ready_path.write_text("LEASE_HELD", encoding="utf-8")
    print("LEASE_HELD", flush=True)
    sys.stdin.readline()
finally:
    lease.release()
"""


def _persistent_file_state(root: Path) -> dict[str, tuple[str, int]]:
    """Capture every durable artifact except the expected lease anchor."""

    state: dict[str, tuple[str, int]] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if relative == "runtime/write.lease":
            continue
        stat_result = path.stat()
        state[relative] = (
            hashlib.sha256(path.read_bytes()).hexdigest(),
            stat_result.st_mtime_ns,
        )
    return state


def _holder_output(holder: subprocess.Popen[str]) -> str:
    """Collect diagnostics without leaving a failed holder process alive."""

    try:
        stdout, stderr = holder.communicate(timeout=2)
    except subprocess.TimeoutExpired:
        holder.kill()
        stdout, stderr = holder.communicate(timeout=10)
    return f"stdout={stdout!r}; stderr={stderr!r}"


def _start_external_write_lease_holder(tester: "CLIBlackboxTester") -> subprocess.Popen[str]:
    """Acquire the real OS lease in a scrubbed, independently spawned child."""

    ready_path = tester.test_dir / "cli-write-lease-ready"
    assert not ready_path.exists()
    environment = prepare_offline_child_env(
        project_root=PROJECT_ROOT,
        runtime_overrides={
            "DATA_DIR": tester.data_dir,
            "DB_PATH": tester.db_path,
            "VAULT_DIR": tester.vault_dir,
            "VECTOR_DIR": tester.vector_dir,
            "LOG_DIR": tester.log_dir,
            "TMP_DIR": tester.tmp_dir,
        },
    )
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            _WRITE_LEASE_HOLDER_SCRIPT,
            str(PROJECT_ROOT),
            str(tester.data_dir),
            str(ready_path),
        ],
        cwd=PROJECT_ROOT,
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if ready_path.is_file():
            if ready_path.read_text(encoding="utf-8") == "LEASE_HELD":
                return holder
            if holder.poll() is None:
                holder.terminate()
            raise AssertionError(
                "external CLI write-lease holder published an invalid readiness token: "
                + _holder_output(holder)
            )
        if holder.poll() is not None:
            raise AssertionError(
                "external CLI write-lease holder exited before readiness: "
                + _holder_output(holder)
            )
        time.sleep(0.05)

    if holder.poll() is None:
        holder.terminate()
    raise AssertionError(
        "external CLI write-lease holder timed out before readiness: "
        + _holder_output(holder)
    )


def _release_external_write_lease_holder(holder: subprocess.Popen[str]) -> None:
    """Release the holder deterministically and surface diagnostics on failure."""

    if holder.poll() is None:
        assert holder.stdin is not None
        holder.stdin.write("\n")
        holder.stdin.flush()
    output = _holder_output(holder)
    assert holder.returncode == 0, output



class CLIBlackboxTester:
    """CLI 黑盒测试工具类。"""

    def __init__(self, test_dir: Path, python_exe: str = sys.executable):
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
        self.entry_ids: dict[str, int] = {}

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
            str(self.project_root / "tests" / "offline_entrypoint.py"),
            "cli",
            *args,
        ]
        env = prepare_offline_child_env(
            project_root=self.project_root,
            runtime_overrides={
                "DATA_DIR": self.data_dir,
                "DB_PATH": self.db_path,
                "VAULT_DIR": self.vault_dir,
                "VECTOR_DIR": self.vector_dir,
                "LOG_DIR": self.log_dir,
                "TMP_DIR": self.tmp_dir,
            },
        )
        # The fixture database was initialized directly and needs the matching
        # synthetic runtime snapshot before a business command may use it.
        env["PKV_TEST_SYNTHETIC_RUNTIME_READY"] = "1"

        result = subprocess.run(
            cmd,
            cwd=self.project_root,
            env=env,
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
            knowledge_id = sqlite_store.insert_entry(entry, file_path)
            self.entry_ids[entry.title] = knowledge_id

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
    assert "archive-text" in result.stdout
    assert "search" in result.stdout
    assert "tags" in result.stdout
    assert "related" in result.stdout


def test_cli_version_command(tmp_path: Path):
    """测试 --version 命令。"""
    cli_tester = CLIBlackboxTester(tmp_path)
    result = cli_tester.run_cli("--version")
    assert result.returncode == 0
    assert "0.8.1" in result.stdout


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
    assert set(data) == {
        "query",
        "status",
        "strategy",
        "total",
        "issues",
        "results",
    }
    assert data["query"] == "Docker"
    assert data["status"] == "success"
    assert data["strategy"] == "bm25"
    assert data["total"] == 1
    assert data["issues"] == []
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
    knowledge_id = cli_tester.entry_ids["Python 装饰器详解"]
    result = cli_tester.run_cli("show", str(knowledge_id))
    assert result.returncode == 0
    assert f"知识条目 #{knowledge_id}" in result.stdout
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
    # This shared fixture deliberately requests a synthetic READY runtime, so
    # both Provider keys are structurally populated.  The subprocess contract
    # is that their values remain redacted, not that this fixture resembles an
    # unconfigured user profile.
    assert "已设置" in result.stdout
    assert "offline-test-placeholder" not in result.stdout


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


def test_cli_archive_text_returns_write_busy_while_reads_remain_available(
    cli_tester: CLIBlackboxTester,
) -> None:
    """A real CLI process must preserve single-writer and concurrent-read semantics."""

    # Seed the isolated fixture before an independent process owns its writer
    # lease.  This avoids mistaking child setup for a write attempted by stats.
    seeded_stats = cli_tester.run_cli("stats")
    assert seeded_stats.returncode == 0
    assert "总条目数: 3" in seeded_stats.stdout
    before = _persistent_file_state(cli_tester.data_dir)

    holder = _start_external_write_lease_holder(cli_tester)
    try:
        # A separate CLI child may still read the same READY root while another
        # application holds the real OS writer lease.
        stats = cli_tester.run_cli("stats")
        assert stats.returncode == 0
        assert "总条目数: 3" in stats.stdout
        assert _persistent_file_state(cli_tester.data_dir) == before

        archive = cli_tester.run_cli(
            "archive-text",
            "busy archive must not persist; api_key=cli-write-busy-secret",
            "--title",
            "must not persist",
            "--format",
            "json",
            check=False,
        )
        assert archive.returncode == 1
        payload = json.loads(archive.stdout)
        assert payload == {
            "terminal": "error",
            "status": "error",
            "knowledge_id": None,
            "title": "",
            "tags": [],
            "file_path": "",
            "issues": [
                {
                    "code": "write_busy",
                    "message": "另一个应用正在写入知识库，请稍后重试",
                    "severity": "error",
                    "stage": "write_lease",
                    "recoverable": True,
                }
            ],
        }
        assert "cli-write-busy-secret" not in archive.stdout
        # The busy request must stop before Processor/Provider, journaling, or
        # any Markdown/SQLite/vector mutation.  The lease anchor is intentionally
        # excluded because the external holder owns it.
        assert _persistent_file_state(cli_tester.data_dir) == before
    finally:
        _release_external_write_lease_holder(holder)

    assert _persistent_file_state(cli_tester.data_dir) == before


def test_archive_text_then_tags_json_via_offline_cli(
    cli_tester: CLIBlackboxTester,
):
    """A degraded archive keeps committed reads available but blocks new writes."""
    title = "CLI 文本归档链路"
    archive_result = cli_tester.run_cli(
        "archive-text",
        "离线 CLI 文本归档用于验证 archive-text 到 tags 的完整调用通路。",
        "--title",
        title,
        "--format",
        "json",
    )

    assert archive_result.returncode == 0
    archive_payload = json.loads(archive_result.stdout)
    # The offline fixture deliberately blocks the Provider-backed vector phase.
    # Core storage still commits and records a degraded journal, which a fresh
    # CLI child must expose to reads without silently repairing it.
    assert archive_payload["terminal"] == "degraded"
    assert archive_payload["status"] == "degraded"
    assert isinstance(archive_payload["knowledge_id"], int)
    assert archive_payload["knowledge_id"] > 0
    assert archive_payload["title"] == title
    assert isinstance(archive_payload["tags"], list)
    assert archive_payload["tags"]
    assert isinstance(archive_payload["file_path"], str)
    assert archive_payload["file_path"]
    assert isinstance(archive_payload["issues"], list)

    journal_dir = cli_tester.data_dir / "runtime" / "operations"
    journal_before_reads = {
        path.name: path.read_bytes()
        for path in sorted(journal_dir.glob("*.json"))
    }
    assert journal_before_reads
    read_sidecars = (
        cli_tester.data_dir / "logs" / "pkv.log",
        cli_tester.data_dir / "runtime" / "write.lease",
    )
    sidecars_before_reads = {
        path.relative_to(cli_tester.data_dir).as_posix(): (
            path.read_bytes(),
            path.stat().st_mtime_ns,
        )
        for path in read_sidecars
    }
    assert len(sidecars_before_reads) == len(read_sidecars)

    show_result = cli_tester.run_cli("show", str(archive_payload["knowledge_id"]))
    assert show_result.returncode == 0
    assert title in show_result.stdout

    tags_result = cli_tester.run_cli("tags", "--format", "json")
    assert tags_result.returncode == 0
    tags_payload = json.loads(tags_result.stdout)
    assert tags_payload["status"] == "success"
    assert tags_payload["total"] == len(tags_payload["tags"])
    assert tags_payload["total"] > 0
    tag_names = {item["name"] for item in tags_payload["tags"]}
    assert tag_names.intersection(archive_payload["tags"])

    # A degraded journal means no second mutation is admitted until the user
    # reviews a lifecycle repair plan.  The allowed reads above must not repair
    # or rewrite that record as a side effect.
    blocked_write = cli_tester.run_cli(
        "archive-text",
        "第二次写入必须被降级运行态门禁拒绝。",
        "--format",
        "json",
        check=False,
    )
    assert blocked_write.returncode == 1
    assert json.loads(blocked_write.stderr) == {
        "adapter": "cli",
        "code": "repair_required",
        "recoverable": True,
        "stage": "runtime_readiness",
        "status": "error",
    }
    assert {
        path.name: path.read_bytes()
        for path in sorted(journal_dir.glob("*.json"))
    } == journal_before_reads
    assert {
        path.relative_to(cli_tester.data_dir).as_posix(): (
            path.read_bytes(),
            path.stat().st_mtime_ns,
        )
        for path in read_sidecars
    } == sidecars_before_reads


def test_related_command_degrades_without_vector_index(
    cli_tester: CLIBlackboxTester,
):
    """真实离线 CLI 在存在条目但没有向量索引时应可观察地退化。"""
    knowledge_id = cli_tester.entry_ids["Python 装饰器详解"]

    result = cli_tester.run_cli(
        "related",
        str(knowledge_id),
        "--format",
        "json",
    )

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["status"] == "degraded"
    assert payload["strategy"] == "vector_related"
    assert payload["total"] == 0
    assert payload["results"] == []
    assert payload["issues"]


def test_related_command_returns_fixed_offline_vector_neighbor_without_mutation(
    cli_tester: CLIBlackboxTester,
):
    """真实离线 related 应读取固定向量且不改写任何索引 artifact。"""
    from src.storage.vector_store import VectorStore

    seed_id = cli_tester.entry_ids["Python 装饰器详解"]
    nearest_id = cli_tester.entry_ids["Docker 容器化实践"]
    distant_id = cli_tester.entry_ids["React Hooks 使用指南"]
    vector_store = VectorStore(cli_tester.vector_dir, dim=3)
    vector_store.add_doc_vector(seed_id, np.array([1.0, 0.0, 0.0], dtype=np.float32))
    vector_store.add_doc_vector(
        nearest_id,
        np.array([0.99, 0.01, 0.0], dtype=np.float32),
    )
    vector_store.add_doc_vector(
        distant_id,
        np.array([0.0, 1.0, 0.0], dtype=np.float32),
    )
    del vector_store
    _remove_vector_writer_lock_sidecars(cli_tester.vector_dir)
    before_snapshot = _snapshot_vector_artifacts(cli_tester.vector_dir)
    assert before_snapshot
    assert not any(
        name.endswith(".lock") or name.endswith(".pair-transaction.json")
        for name in before_snapshot
    )

    result = cli_tester.run_cli(
        "related",
        str(seed_id),
        "--limit",
        "1",
        "--format",
        "json",
    )

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["status"] == "success"
    assert payload["strategy"] == "vector_related"
    assert payload["total"] == 1
    assert payload["issues"] == []
    assert len(payload["results"]) == 1
    neighbor = payload["results"][0]
    assert neighbor["knowledge_id"] == nearest_id
    assert neighbor["knowledge_id"] != seed_id
    assert neighbor["title"] == "Docker 容器化实践"
    assert 0.0 <= neighbor["score"] <= 1.0
    assert _snapshot_vector_artifacts(cli_tester.vector_dir) == before_snapshot


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
        "--format",
        "json",
    )
    assert search_result.returncode == 0
    search_payload = json.loads(search_result.stdout)
    assert search_payload["total"] == 1
    assert len(search_payload["results"]) == 1
    first_result = search_payload["results"][0]
    assert first_result["title"] == "Python 装饰器详解"
    knowledge_id = first_result["entry_id"]
    assert knowledge_id == cli_tester.entry_ids["Python 装饰器详解"]

    # 2. 使用搜索结果中的真实 ID 显示详情
    show_result = cli_tester.run_cli("show", str(knowledge_id))
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
