"""
MCP 协议级 stdio 黑盒测试 (Layer 3)

通过 MCP SDK 的 stdio_client + ClientSession 启动真实子进程，
经由 JSON-RPC over stdio 进行端到端协议级测试：

    [pytest] ──stdio_client──> [tests/offline_entrypoint.py mcp] (子进程)
              <──JSON-RPC───

验证内容：
1. 服务启动与协议初始化（MCP 握手）
2. 功能发现（list_tools / list_prompts / list_resources）
3. 只读 Tool 端到端调用
4. 写入 Tool 安全验证
5. Prompt 端到端调用
6. Resource 端到端读取

测试隔离：
- 全部运行路径环境变量指向临时目录
- 子进程 cwd 设置为项目根目录
- 每个测试 session 使用独立的临时目录

对比 Layer 2 (进程内 FastMCP):
    Layer 3 更真实 — 跨进程通信、协议序列化/反序列化、子进程生命周期管理。
    但更慢、更难调试。两层互补。
"""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Optional

import pytest
import yaml
from mcp import StdioServerParameters
from mcp.client.session import ClientSession
from mcp.client.stdio import stdio_client
from mcp.shared.exceptions import McpError

# 确保项目根目录在 Python path 中
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
from tests.offline_runtime import prepare_offline_child_env

W2_MCP_FIXTURE_PATH = (
    PROJECT_ROOT / "tests" / "fixtures" / "w2" / "mcp" / "v1" / "matrix.yaml"
)
W2_MCP_FIXTURE = yaml.safe_load(
    W2_MCP_FIXTURE_PATH.read_text(encoding="utf-8")
)
READONLY_TOOL_MATRIX = W2_MCP_FIXTURE["readonly_tools"]


# ============================================================
# 辅助函数
# ============================================================


def get_server_params(
    db_path: str,
    log_level: str = "WARNING",
    extra_env: Optional[Dict[str, str]] = None,
    *,
    ready_fixture: bool = True,
    absent_data_root: bool = False,
) -> StdioServerParameters:
    """构建 StdioServerParameters 用于启动 MCP Server 子进程。

    Args:
        db_path: 临时数据库路径
        log_level: 日志级别（默认 WARNING 减少 stderr 干扰）
        extra_env: 额外环境变量
        ready_fixture: 为已初始化的离线 SQLite fixture 写入匹配的无密钥
            runtime snapshot。设为 False 可验证 fresh/unready 的 status-only
            MCP 启动路径。
        absent_data_root: 保持 selected data root 完全不存在，以验证 L3
            status-only 行为不会由测试 harness 的 ``ensure_dirs`` 掩盖。

    Returns:
        StdioServerParameters 实例
    """
    data_dir = Path(db_path).resolve().parent.parent
    runtime_overrides: Dict[str, str] = {
        "DATA_DIR": str(data_dir),
        "DB_PATH": str(Path(db_path).resolve()),
        "VAULT_DIR": str(data_dir / "vault"),
        "VECTOR_DIR": str(data_dir / "vectors"),
        "LOG_DIR": str(data_dir / "logs"),
        "TMP_DIR": str(data_dir / "tmp"),
        "LOG_LEVEL": log_level,
    }
    if extra_env:
        runtime_overrides.update(extra_env)
    env = prepare_offline_child_env(
        project_root=PROJECT_ROOT,
        runtime_overrides=runtime_overrides,
    )
    if absent_data_root and ready_fixture:
        raise ValueError("an absent data-root fixture cannot be synthetic-ready")
    if absent_data_root:
        env.pop("PKV_TEST_SYNTHETIC_RUNTIME_READY", None)
        env["PKV_TEST_ABSENT_DATA_ROOT"] = "1"
    elif ready_fixture:
        env["PKV_TEST_SYNTHETIC_RUNTIME_READY"] = "1"

    return StdioServerParameters(
        command=sys.executable,
        args=[str(PROJECT_ROOT / "tests" / "offline_entrypoint.py"), "mcp"],
        env=env,
        cwd=str(PROJECT_ROOT),
    )


def parse_tool_content(result) -> Dict[str, Any]:
    """解析 ClientSession.call_tool() 返回的 CallToolResult。

    ClientSession.call_tool() 返回 CallToolResult 对象，
    包含 content: list[TextContent|ImageContent|EmbeddedResource]
    和 isError: bool。

    成功结果必须只含一个 JSON TextContent，禁止忽略额外块或 isError。
    """
    if getattr(result, "isError", False):
        raise ValueError(f"call_tool 返回 MCP error: {result}")
    content = getattr(result, "content", None)
    if not isinstance(content, list) or len(content) != 1:
        raise ValueError(f"call_tool 必须返回单一 TextContent: {result}")
    text = getattr(content[0], "text", None)
    if not isinstance(text, str) or not text:
        raise ValueError(f"call_tool TextContent 缺少 JSON 文本: {result}")
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ValueError(f"call_tool JSON 必须为 object: {type(payload).__name__}")
    return payload


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
lease = VaultWriteLease(layout)
try:
    lease.acquire()
    print("LEASE_HELD", flush=True)
    sys.stdin.readline()
finally:
    lease.release()
"""


def _start_external_write_lease_holder(data_root: Path) -> subprocess.Popen[str]:
    """Hold the real OS advisory lease from a process outside the MCP server."""

    environment = os.environ.copy()
    existing_pythonpath = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = str(PROJECT_ROOT) + (
        os.pathsep + existing_pythonpath if existing_pythonpath else ""
    )
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            _WRITE_LEASE_HOLDER_SCRIPT,
            str(PROJECT_ROOT),
            str(data_root),
        ],
        cwd=PROJECT_ROOT,
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
    )
    assert holder.stdout is not None
    readiness = holder.stdout.readline().strip()
    if readiness != "LEASE_HELD":
        output, _ = holder.communicate(timeout=10)
        raise AssertionError(
            f"external write lease holder did not become ready: {readiness!r}\\n{output}"
        )
    return holder


def _release_external_write_lease_holder(holder: subprocess.Popen[str]) -> None:
    if holder.poll() is None:
        assert holder.stdin is not None
        holder.stdin.write("\\n")
        holder.stdin.flush()
    output, _ = holder.communicate(timeout=10)
    assert holder.returncode == 0, output


def _persistent_file_state(root: Path) -> Dict[str, str]:
    """Return a stable isolated-root snapshot, excluding the expected lease anchor."""

    state: Dict[str, str] = {}
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if relative == "runtime/write.lease":
            continue
        state[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return state


def assert_stats_payload(
    payload: Dict[str, Any],
    *,
    expect_tool_envelope: bool = False,
) -> None:
    """Assert the canonical statistics schema after stdio serialization."""

    expected_keys = {"total_entries", "by_source_type", "top_tags"}
    if expect_tool_envelope:
        expected_keys |= {"status", "issues"}
    assert set(payload) == expected_keys
    if expect_tool_envelope:
        assert payload["status"] == "success"
        assert payload["issues"] == []
    assert isinstance(payload["total_entries"], int)
    assert not isinstance(payload["total_entries"], bool)
    assert payload["total_entries"] >= 0
    assert isinstance(payload["by_source_type"], list)
    assert all(
        isinstance(item, list)
        and len(item) == 2
        and isinstance(item[0], str)
        and isinstance(item[1], int)
        and not isinstance(item[1], bool)
        for item in payload["by_source_type"]
    )
    assert isinstance(payload["top_tags"], list)
    assert all(
        set(item) == {"name", "count"}
        and isinstance(item["name"], str)
        and isinstance(item["count"], int)
        and not isinstance(item["count"], bool)
        for item in payload["top_tags"]
    )


# ============================================================
# Fixtures
# ============================================================

@pytest.fixture
def tmp_db(tmp_path: Path) -> Path:
    """创建临时数据库目录并返回 db 路径。"""
    db_dir = tmp_path / "db"
    db_dir.mkdir()
    return db_dir / "test_blackbox.db"


@pytest.fixture
def populated_db_path(tmp_db: Path) -> str:
    """填充测试数据到临时数据库并返回路径字符串。"""
    sys.path.insert(0, str(PROJECT_ROOT))
    from src.storage.sqlite_store import SQLiteStore
    from src.storage.markdown_store import Entry

    store = SQLiteStore(tmp_db)
    store.initialize()

    entries = [
        Entry(
            title="微信文章：AI 工程化实践",
            source_type="wechat",
            source_url="https://mp.weixin.qq.com/s/test_article_1",
            tags=["AI", "工程化"],
            keywords=["人工智能", "MLOps"],
            abstract="AI 工程化实践总结",
            summary_one_sentence="AI 工程化的最佳实践指南",
            summary_100_words="本文总结了 AI 工程化的关键实践...",
            content="# AI 工程化\n\n这是关于 AI 工程化的详细内容。",
            word_count=200,
        ),
        Entry(
            title="知乎回答：NLP 入门路线",
            source_type="zhihu",
            source_url="https://www.zhihu.com/answer/test_456",
            tags=["NLP", "入门"],
            keywords=["自然语言处理"],
            abstract="NLP 入门路线图",
            summary_one_sentence="NLP 学习路线推荐",
            summary_100_words="自然语言处理入门学习路线...",
            content="# NLP 入门\n\n自然语言处理入门指南。",
            word_count=150,
        ),
        Entry(
            title="通用网页：Python 编程教程",
            source_type="generic",
            source_url="https://example.com/python_test",
            tags=["Python", "教程"],
            keywords=["编程"],
            abstract="Python 基础教程",
            summary_one_sentence="Python 编程入门教程",
            summary_100_words="Python 从入门到实践教程。",
            content="# Python 教程\n\nPython 从入门到实践。",
            word_count=300,
        ),
    ]

    # 创建临时 vault 目录用于 file_path（数据库需要 file_path 字段）
    vault_dir = tmp_db.parent.parent / "vault"
    vault_dir.mkdir(parents=True, exist_ok=True)

    for entry in entries:
        md_dir = vault_dir / entry.source_type
        md_dir.mkdir(parents=True, exist_ok=True)
        safe_title = entry.title[:10].replace("：", "_")
        md_path = md_dir / f"{safe_title}.md"
        md_path.write_text(
            f"---\ntitle: {entry.title}\nsource_type: {entry.source_type}\n"
            f"tags: [{', '.join(entry.tags)}]\n---\n{entry.content}",
            encoding="utf-8",
        )
        store.insert_entry(entry, str(md_path))

    return str(tmp_db)


@pytest.fixture
def empty_db_path(tmp_db: Path) -> str:
    """创建空的临时数据库并返回路径字符串。"""
    sys.path.insert(0, str(PROJECT_ROOT))
    from src.storage.sqlite_store import SQLiteStore

    store = SQLiteStore(tmp_db)
    store.initialize()
    return str(tmp_db)


# ============================================================
# 1. 服务启动与协议发现
# ============================================================

class TestServerStartup:
    """MCP Server 启动与协议初始化测试。"""

    @pytest.mark.asyncio
    async def test_server_initializes_successfully(self, empty_db_path):
        """Server 应能通过 stdio 成功启动并完成 MCP 初始化握手。"""
        params = get_server_params(empty_db_path)

        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                result = await session.initialize()
                # 初始化成功后应返回 server info
                assert result is not None

    @pytest.mark.asyncio
    async def test_server_capabilities(self, empty_db_path):
        """Server 应声明 tools/prompts/resources 能力。"""
        params = get_server_params(empty_db_path)

        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                init_result = await session.initialize()
                # InitializeResult.capabilities 包含 server 能力声明
                caps = init_result.capabilities
                assert caps is not None

    @pytest.mark.asyncio
    async def test_list_tools_returns_15(self, empty_db_path):
        """list_tools 应返回 15 个 Tool。"""
        params = get_server_params(empty_db_path)

        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools_result = await session.list_tools()
                tool_names = {t.name for t in tools_result.tools}

                assert len(tool_names) == 15, f"期望 15 个 Tool，实际: {tool_names}"
                expected = {
                    "get_runtime_status", "search_knowledge", "get_entry", "list_tags", "list_entries",
                    "get_stats", "archive_url", "archive_text", "get_related",
                    "query_subgraph", "explain_relation", "collect_evidence",
                    "find_bridges", "timeline_of", "contrast",
                }
                assert tool_names == expected

    @pytest.mark.asyncio
    async def test_unready_runtime_starts_status_only_without_mutation(self, tmp_db):
        """Fresh roots publish status but cannot lazily create a product runtime."""
        data_root = tmp_db.parent.parent
        params = get_server_params(str(tmp_db), ready_fixture=False)

        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools_result = await session.list_tools()
                status_result = await session.call_tool("get_runtime_status", {})
                stats_result = await session.call_tool("get_stats", {})

        tool_names = {tool.name for tool in tools_result.tools}
        status = parse_tool_content(status_result)
        stats = parse_tool_content(stats_result)
        assert len(tool_names) == 15
        assert "get_runtime_status" in tool_names
        assert status["status"] == "success"
        # The generic offline child creates declared directories before it
        # imports the MCP adapter.  That makes this intentionally partial root
        # repair-required rather than a completely absent setup-required root;
        # either state must still stay status-only at the real stdio boundary.
        assert status["readiness"] == "repair_required"
        assert status["inspection"]["readiness"] == "repair_required"
        assert status["plan"] is not None
        assert stats["status"] == "error"
        assert stats["issues"][0]["code"] == "repair_required"
        # ``offline_entrypoint`` creates declared directories before importing
        # the server. The MCP adapter itself must not create a database,
        # runtime snapshot, or file logger while only status is available.
        assert not tmp_db.exists()
        assert not (data_root / "config" / "local.yaml").exists()
        assert not (data_root / "logs" / "pkv.log").exists()

    @pytest.mark.asyncio
    async def test_absent_data_root_starts_status_only_without_mutation(self, tmp_path):
        """A real stdio child preserves a wholly absent root while serving status."""

        data_root = tmp_path / "absent-data-root"
        db_path = data_root / "db" / "knowledge_vault.db"
        assert not os.path.lexists(data_root)
        params = get_server_params(
            str(db_path),
            ready_fixture=False,
            absent_data_root=True,
        )
        assert not os.path.lexists(data_root)

        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                status_result = await session.call_tool("get_runtime_status", {})
                stats_result = await session.call_tool("get_stats", {})

        status = parse_tool_content(status_result)
        stats = parse_tool_content(stats_result)
        assert status["status"] == "success"
        assert status["readiness"] == "setup_required"
        assert status["inspection"]["readiness"] == "setup_required"
        assert status["plan"] is not None
        assert stats["status"] == "error"
        assert stats["issues"][0]["code"] == "setup_required"
        assert not os.path.lexists(data_root)

    @pytest.mark.asyncio
    async def test_list_prompts_returns_3(self, empty_db_path):
        """list_prompts 应返回 3 个 Prompt。"""
        params = get_server_params(empty_db_path)

        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                prompts_result = await session.list_prompts()
                prompt_names = {p.name for p in prompts_result.prompts}

                assert len(prompt_names) == 3
                expected = {"search_and_summarize", "knowledge_qa", "idea_sharpen"}
                assert prompt_names == expected

    @pytest.mark.asyncio
    async def test_list_resources(self, empty_db_path):
        """list_resources 应返回静态 Resource（tags, stats）。"""
        params = get_server_params(empty_db_path)

        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                resources_result = await session.list_resources()
                uris = {str(r.uri) for r in resources_result.resources}

                # 静态 Resource 应在列表中
                assert any("tags" in uri for uri in uris), f"缺少 tags Resource: {uris}"
                assert any("stats" in uri for uri in uris), f"缺少 stats Resource: {uris}"

    @pytest.mark.asyncio
    async def test_list_resource_templates(self, empty_db_path):
        """list_resource_templates 应返回带参数的 Resource 模板。"""
        params = get_server_params(empty_db_path)

        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                templates_result = await session.list_resource_templates()
                template_uris = {t.uriTemplate for t in templates_result.resourceTemplates}

                assert "pkv://entries/{knowledge_id}" in template_uris
                assert "pkv://entries/{knowledge_id}/metadata" in template_uris


# ============================================================
# 2. 只读 Tool 端到端调用
# ============================================================

class TestReadonlyTools:
    """只读 Tool 端到端调用（经 stdio JSON-RPC 协议）。"""

    @pytest.mark.asyncio
    async def test_get_runtime_status(self, empty_db_path):
        """The lifecycle status Tool is read-only and exposes no config secrets."""
        params = get_server_params(empty_db_path)

        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool("get_runtime_status", {})

        data = parse_tool_content(result)
        assert data["status"] == "success"
        assert data["readiness"] == "ready"
        assert data["inspection"]["runtime_snapshot"] == "valid"
        serialized = json.dumps(data, ensure_ascii=False).lower()
        assert "api_key" not in serialized
        assert "offline-test-placeholder" not in serialized

    @pytest.mark.asyncio
    async def test_list_entries(self, populated_db_path):
        """list_entries 应返回填充的测试数据。"""
        params = get_server_params(populated_db_path)

        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool("list_entries", {
                    "page": 1, "per_page": 10,
                })

        data = parse_tool_content(result)
        assert data["total"] == 3
        assert len(data["entries"]) == 3

    @pytest.mark.asyncio
    async def test_list_entries_with_source_filter(self, populated_db_path):
        """list_entries 按 source_type 过滤。"""
        params = get_server_params(populated_db_path)

        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool("list_entries", {
                    "source_type": "wechat",
                })

        data = parse_tool_content(result)
        assert data["total"] == 1
        assert data["entries"][0]["source_type"] == "wechat"

    @pytest.mark.asyncio
    async def test_list_entries_with_tag_filter(self, populated_db_path):
        """list_entries 按 tag 过滤。"""
        params = get_server_params(populated_db_path)

        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool("list_entries", {
                    "tag": "AI",
                })

        data = parse_tool_content(result)
        assert data["total"] == 1
        assert "AI" in data["entries"][0]["tags"]

    @pytest.mark.asyncio
    async def test_get_entry(self, populated_db_path):
        """get_entry 应返回完整条目。"""
        params = get_server_params(populated_db_path)

        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                # 先获取条目列表确定 ID
                list_result = await session.call_tool("list_entries", {"per_page": 1})
                entries = parse_tool_content(list_result)
                first_id = str(entries["entries"][0]["knowledge_id"])

                # 获取详情
                result = await session.call_tool("get_entry", {
                    "knowledge_id": first_id,
                })

        data = parse_tool_content(result)
        assert "title" in data
        assert "tags" in data
        assert isinstance(data["tags"], list)

    @pytest.mark.asyncio
    async def test_get_entry_not_found(self, populated_db_path):
        """get_entry 对不存在的 ID 应返回错误。"""
        params = get_server_params(populated_db_path)

        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool("get_entry", {
                    "knowledge_id": "99999",
                })

        data = parse_tool_content(result)
        assert "error" in data

    @pytest.mark.asyncio
    async def test_list_tags(self, populated_db_path):
        """list_tags 应返回所有标签及计数。"""
        params = get_server_params(populated_db_path)

        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool("list_tags", {})

        data = parse_tool_content(result)
        assert data["status"] == "success"
        assert data["issues"] == []
        assert "total_tags" in data
        assert data["total_tags"] > 0
        tag_names = [t["name"] for t in data["tags"]]
        assert "AI" in tag_names

    @pytest.mark.asyncio
    async def test_get_stats(self, populated_db_path):
        """get_stats 应返回统计数据。"""
        params = get_server_params(populated_db_path)

        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool("get_stats", {})

        data = parse_tool_content(result)
        assert data["status"] == "success"
        assert data["issues"] == []
        assert data["total_entries"] == 3
        assert dict(data["by_source_type"]) == {
            "generic": 1,
            "wechat": 1,
            "zhihu": 1,
        }
        assert {
            item["name"]: item["count"] for item in data["top_tags"]
        }["AI"] == 1
        assert_stats_payload(data, expect_tool_envelope=True)

    @pytest.mark.asyncio
    async def test_list_entries_pagination(self, populated_db_path):
        """list_entries 分页应正确工作。"""
        params = get_server_params(populated_db_path)

        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                # 第一页 2 条
                result1 = await session.call_tool("list_entries", {
                    "page": 1, "per_page": 2,
                })
                data1 = parse_tool_content(result1)
                assert data1["status"] == "success"
                assert data1["issues"] == []
                assert data1["total"] == 3
                assert len(data1["entries"]) == 2

                # 第二页 1 条
                result2 = await session.call_tool("list_entries", {
                    "page": 2, "per_page": 2,
                })
                data2 = parse_tool_content(result2)
                assert data2["status"] == "success"
                assert data2["issues"] == []
                assert len(data2["entries"]) == 1

    @pytest.mark.asyncio
    async def test_list_entries_empty_db(self, empty_db_path):
        """list_entries 对空数据库应返回空列表。"""
        params = get_server_params(empty_db_path)

        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool("list_entries", {})

        data = parse_tool_content(result)
        assert data["status"] == "no_hits"
        assert data["issues"] == []
        assert data["total"] == 0
        assert len(data["entries"]) == 0


# ============================================================
# 3. 写入 Tool 安全验证（经 stdio 协议）
# ============================================================

class TestWriteToolSecurity:
    """写入 Tool 安全拦截验证（经真实 stdio 协议）。"""

    @pytest.mark.asyncio
    async def test_archive_url_rejects_private_ip_127(self, empty_db_path):
        """archive_url 应拒绝 127.0.0.1 (SSRF 防护)。"""
        params = get_server_params(empty_db_path)

        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool("archive_url", {
                    "url": "http://127.0.0.1/admin",
                })

        data = parse_tool_content(result)
        assert data["success"] is False
        assert "内网" in data["error"]

    @pytest.mark.asyncio
    async def test_archive_url_rejects_private_ip_192(self, empty_db_path):
        """archive_url 应拒绝 192.168.x.x。"""
        params = get_server_params(empty_db_path)

        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool("archive_url", {
                    "url": "http://192.168.1.100/data",
                })

        data = parse_tool_content(result)
        assert data["success"] is False
        assert "内网" in data["error"]

    @pytest.mark.asyncio
    async def test_archive_url_rejects_private_ip_10(self, empty_db_path):
        """archive_url 应拒绝 10.x.x.x。"""
        params = get_server_params(empty_db_path)

        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool("archive_url", {
                    "url": "http://10.0.0.1/internal",
                })

        data = parse_tool_content(result)
        assert data["success"] is False

    @pytest.mark.asyncio
    async def test_archive_url_rejects_private_ip_172(self, empty_db_path):
        """archive_url 应拒绝 172.16-31.x.x。"""
        params = get_server_params(empty_db_path)

        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool("archive_url", {
                    "url": "http://172.16.0.1/secret",
                })

        data = parse_tool_content(result)
        assert data["success"] is False

    @pytest.mark.asyncio
    async def test_archive_url_rejects_localhost(self, empty_db_path):
        """archive_url 应拒绝 localhost。"""
        params = get_server_params(empty_db_path)

        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool("archive_url", {
                    "url": "http://localhost:8080/",
                })

        data = parse_tool_content(result)
        assert data["success"] is False

    @pytest.mark.asyncio
    async def test_archive_url_rejects_ftp(self, empty_db_path):
        """archive_url 应拒绝非 http/https 协议。"""
        params = get_server_params(empty_db_path)

        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool("archive_url", {
                    "url": "ftp://files.example.com/data",
                })

        data = parse_tool_content(result)
        assert data["success"] is False

    @pytest.mark.asyncio
    async def test_archive_url_rejects_empty(self, empty_db_path):
        """archive_url 应拒绝空 URL。"""
        params = get_server_params(empty_db_path)

        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool("archive_url", {
                    "url": "",
                })

        data = parse_tool_content(result)
        assert data["success"] is False

    @pytest.mark.asyncio
    async def test_archive_text_rejects_empty(self, empty_db_path):
        """archive_text 应拒绝空文本。"""
        params = get_server_params(empty_db_path)

        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool("archive_text", {
                    "text": "",
                })

        data = parse_tool_content(result)
        assert data["success"] is False
        assert data["terminal"] == "error"
        assert data["error"] == "工作流配置无效"
        assert data["issues"][0]["code"] == "workflow_config_invalid"
        assert data["issues"][0]["stage"] == "text_validation"

    @pytest.mark.asyncio
    async def test_archive_text_rejects_whitespace(self, empty_db_path):
        """archive_text 应拒绝纯空白文本。"""
        params = get_server_params(empty_db_path)

        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool("archive_text", {
                    "text": "   \n\t  ",
                })

        data = parse_tool_content(result)
        assert data["success"] is False

    @pytest.mark.asyncio
    async def test_archive_text_rejects_too_long(self, empty_db_path):
        """archive_text 应拒绝超长文本 (>100000 字符)。"""
        params = get_server_params(empty_db_path)

        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool("archive_text", {
                    "text": "x" * 100001,
                })

        data = parse_tool_content(result)
        assert data["success"] is False
        assert data["terminal"] == "error"
        assert data["error"] == "工作流配置无效"
        assert data["issues"][0]["code"] == "workflow_config_invalid"
        assert data["issues"][0]["stage"] == "text_validation"

    @pytest.mark.asyncio
    async def test_archive_text_returns_write_busy_from_external_holder_without_side_effects(
        self,
        populated_db_path,
    ):
        """Real stdio archive returns normal busy JSON while reads remain available."""

        data_root = Path(populated_db_path).resolve().parent.parent
        params = get_server_params(populated_db_path)

        # The synthetic READY child publishes its fixture snapshot before the
        # independent holder is started.  A later server can therefore inspect
        # the same ready root without trying to initialize it under the holder.
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                seeded_status = parse_tool_content(
                    await session.call_tool("get_runtime_status", {})
                )
        assert seeded_status["readiness"] == "ready"
        assert (data_root / "config" / "local.yaml").is_file()

        holder = _start_external_write_lease_holder(data_root)
        try:
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()

                    # A normal database read is allowed while a different
                    # application owns the writer lease.  Run it before the
                    # snapshot so harmless reader setup cannot mask a write.
                    stats = parse_tool_content(await session.call_tool("get_stats", {}))
                    assert stats["status"] == "success"
                    assert stats["total_entries"] == 3
                    before = _persistent_file_state(data_root)

                    result = await session.call_tool(
                        "archive_text",
                        {
                            "text": "busy archive must not invoke a Provider; "
                            "api_key=blackbox-write-busy-secret",
                            "title": "must not persist",
                        },
                    )
                    payload = parse_tool_content(result)
                    after = _persistent_file_state(data_root)
        finally:
            _release_external_write_lease_holder(holder)

        # ``parse_tool_content`` also asserts ``isError is False`` and the
        # single TextContent envelope, so this is protocol-level—not a direct
        # Python handler—evidence of the public retry contract.
        assert payload["success"] is False
        assert payload["terminal"] == "error"
        assert payload["error_code"] == "write_busy"
        assert payload["retryable"] is True
        assert payload["issues"] == [
            {
                "code": "write_busy",
                "message": "另一个应用正在写入知识库，请稍后重试",
                "stage": "write_lease",
                "recoverable": True,
                "severity": "error",
            }
        ]
        assert "blackbox-write-busy-secret" not in json.dumps(payload, ensure_ascii=False)
        # The only permitted holder mutation is its stable lease anchor.  The
        # busy archive itself cannot construct a Processor/Provider, audit
        # trace, journal, Markdown, SQLite or vector write.
        assert after == before
        assert not (data_root / "logs" / "audit.jsonl").exists()
        assert not (data_root / "runtime" / "operations").exists()

    @pytest.mark.asyncio
    async def test_get_related_invalid_id(self, empty_db_path):
        """get_related 应拒绝非数字 ID。"""
        params = get_server_params(empty_db_path)

        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool("get_related", {
                    "knowledge_id": "not_a_number",
                })

        data = parse_tool_content(result)
        assert "error" in data
        assert "无效" in data["error"]


# ============================================================
# 4. Prompt 端到端调用
# ============================================================

class TestPrompts:
    """Prompt 端到端调用（经 stdio JSON-RPC 协议）。"""

    @pytest.mark.asyncio
    async def test_search_and_summarize(self, empty_db_path):
        """search_and_summarize Prompt 应生成搜索引导文本。"""
        params = get_server_params(empty_db_path)

        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.get_prompt("search_and_summarize", {
                    "query": "AI 工程化",
                })

        assert result.messages, "Prompt 应返回非空 messages"
        text = result.messages[0].content.text
        assert "AI 工程化" in text
        assert "search_knowledge" in text

    @pytest.mark.asyncio
    async def test_search_and_summarize_with_context(self, empty_db_path):
        """search_and_summarize 带 context 应包含背景信息。"""
        params = get_server_params(empty_db_path)

        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.get_prompt("search_and_summarize", {
                    "query": "NLP",
                    "context": "准备写一篇综述",
                })

        text = result.messages[0].content.text
        assert "NLP" in text
        assert "准备写一篇综述" in text

    @pytest.mark.asyncio
    async def test_knowledge_qa(self, empty_db_path):
        """knowledge_qa Prompt 应生成问答引导文本。"""
        params = get_server_params(empty_db_path)

        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.get_prompt("knowledge_qa", {
                    "question": "什么是 RAG?",
                })

        text = result.messages[0].content.text
        assert "什么是 RAG?" in text
        assert "search_knowledge" in text

    @pytest.mark.asyncio
    async def test_idea_sharpen(self, empty_db_path):
        """idea_sharpen Prompt 应生成思想磨砺引导文本。"""
        params = get_server_params(empty_db_path)

        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.get_prompt("idea_sharpen", {
                    "content": "这是一篇关于知识管理的文章",
                })

        text = result.messages[0].content.text
        assert "知识管理" in text

    @pytest.mark.asyncio
    async def test_idea_sharpen_with_entry_id(self, empty_db_path):
        """idea_sharpen 带 entry_id 应包含条目引用。"""
        params = get_server_params(empty_db_path)

        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.get_prompt("idea_sharpen", {
                    "content": "测试内容",
                    "entry_id": "42",
                })

        text = result.messages[0].content.text
        assert "42" in text
        assert "get_entry" in text


# ============================================================
# 5. Resource 端到端读取
# ============================================================

class TestResources:
    """Resource 端到端读取（经 stdio JSON-RPC 协议）。"""

    @pytest.mark.asyncio
    async def test_read_tags_resource(self, populated_db_path):
        """pkv://tags 应返回 JSON 标签列表。"""
        params = get_server_params(populated_db_path)

        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.read_resource("pkv://tags")

        assert result.contents, "Resource 应返回非空内容"
        data = json.loads(result.contents[0].text)
        assert "total_tags" in data
        assert data["total_tags"] > 0

    @pytest.mark.asyncio
    async def test_read_stats_resource(self, populated_db_path):
        """pkv://stats 应返回 JSON 统计数据。"""
        params = get_server_params(populated_db_path)

        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.read_resource("pkv://stats")

        data = json.loads(result.contents[0].text)
        assert data["total_entries"] == 3
        assert dict(data["by_source_type"]) == {
            "generic": 1,
            "wechat": 1,
            "zhihu": 1,
        }
        assert {
            item["name"]: item["count"] for item in data["top_tags"]
        }["AI"] == 1
        assert_stats_payload(data)

    @pytest.mark.asyncio
    async def test_read_entry_metadata(self, populated_db_path):
        """pkv://entries/{id}/metadata 应返回 JSON 元数据。"""
        params = get_server_params(populated_db_path)

        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                # 先获取第一条条目的 ID
                list_result = await session.call_tool("list_entries", {"per_page": 1})
                entries = parse_tool_content(list_result)
                first_id = str(entries["entries"][0]["knowledge_id"])

                # 读取元数据 Resource
                result = await session.read_resource(
                    f"pkv://entries/{first_id}/metadata"
                )

        data = json.loads(result.contents[0].text)
        assert "title" in data
        assert isinstance(data["tags"], list)

    @pytest.mark.asyncio
    async def test_read_entry_content(self, populated_db_path):
        """pkv://entries/{id} 应返回 Markdown 内容。"""
        params = get_server_params(populated_db_path)

        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                # 获取第一条条目 ID
                list_result = await session.call_tool("list_entries", {"per_page": 1})
                entries = parse_tool_content(list_result)
                first_id = str(entries["entries"][0]["knowledge_id"])

                # 读取全文 Resource
                result = await session.read_resource(
                    f"pkv://entries/{first_id}"
                )

        text = result.contents[0].text
        assert isinstance(text, str)
        assert len(text) > 0

    @pytest.mark.asyncio
    async def test_read_entry_not_found(self, empty_db_path):
        """pkv://entries/99999 应返回真实 MCP 错误，而非伪成功正文。"""
        params = get_server_params(empty_db_path)

        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                with pytest.raises(McpError, match="未找到条目"):
                    await session.read_resource("pkv://entries/99999")


# ============================================================
# 6. 跨功能端到端场景
# ============================================================

class TestEndToEnd:
    """跨功能端到端场景（单次 session 内多步骤操作）。"""

    @pytest.mark.asyncio
    async def test_full_readonly_workflow(self, populated_db_path):
        """完整只读工作流：list → get_entry → read_resource → stats。"""
        params = get_server_params(populated_db_path)

        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                # Step 1: 列出条目
                list_result = await session.call_tool("list_entries", {
                    "page": 1, "per_page": 10,
                })
                entries = parse_tool_content(list_result)
                assert entries["total"] == 3

                # Step 2: 获取第一条详情
                first_id = str(entries["entries"][0]["knowledge_id"])
                detail_result = await session.call_tool("get_entry", {
                    "knowledge_id": first_id,
                })
                detail = parse_tool_content(detail_result)
                assert "title" in detail

                # Step 3: 读取元数据 Resource
                meta_result = await session.read_resource(
                    f"pkv://entries/{first_id}/metadata"
                )
                meta = json.loads(meta_result.contents[0].text)
                assert meta["title"] == detail["title"]

                # Step 4: 查看统计
                stats_result = await session.call_tool("get_stats", {})
                stats = parse_tool_content(stats_result)
                assert_stats_payload(stats, expect_tool_envelope=True)

                # Step 5: 列出标签
                tags_result = await session.call_tool("list_tags", {})
                tags = parse_tool_content(tags_result)
                assert tags["total_tags"] > 0

    @pytest.mark.asyncio
    async def test_discovery_then_invoke(self, populated_db_path):
        """发现 → 调用 工作流：先 list_tools, 再 call 每个只读 tool。"""
        params = get_server_params(populated_db_path)

        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                # 发现所有 Tool
                tools_result = await session.list_tools()
                tool_names = {t.name for t in tools_result.tools}

                readonly_names = {case["name"] for case in READONLY_TOOL_MATRIX}
                assert len(readonly_names) == 13
                assert readonly_names == tool_names - {"archive_url", "archive_text"}

                # 版本化 fixture 为每个只读 Tool 提供有效、离线且确定性的参数。
                invoked = []
                for case in READONLY_TOOL_MATRIX:
                    name = case["name"]
                    assert name in tool_names, f"Tool {name} 未注册"
                    result = await session.call_tool(name, case["arguments"])
                    data = parse_tool_content(result)
                    assert isinstance(data, dict), f"{name} 返回值不是 dict"
                    if case.get("forbid_error", False):
                        assert "error" not in data, f"{name} 意外错误: {data}"
                    invoked.append(name)

                assert invoked == [case["name"] for case in READONLY_TOOL_MATRIX]

    @pytest.mark.asyncio
    async def test_prompt_then_tool(self, populated_db_path):
        """Prompt 引导 → Tool 调用 工作流。"""
        params = get_server_params(populated_db_path)

        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                # Step 1: 获取 Prompt 生成的引导文本
                prompt_result = await session.get_prompt("knowledge_qa", {
                    "question": "有哪些 AI 相关的知识?",
                })
                prompt_text = prompt_result.messages[0].content.text
                assert "search_knowledge" in prompt_text

                # Step 2: 按引导调用 Tool
                list_result = await session.call_tool("list_entries", {
                    "tag": "AI",
                })
                data = parse_tool_content(list_result)
                assert data["total"] >= 1
