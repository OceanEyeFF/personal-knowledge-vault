"""
MCP 进程内功能测试 (Layer 2)

通过 FastMCP 内建方法 (list_tools/call_tool/list_prompts/get_prompt/read_resource)
在同一进程内直接调用 MCP handler，验证:
- 注册机制: 装饰器 → handler 映射
- Schema 正确性: 参数名/类型/描述
- 返回值: 序列化格式、字段完整性
- 安全验证: 写入 Tool 的前置拦截在 MCP 层生效

相比 Layer 1 (直接调用 handler 函数):
    更接近真实调用链 — 经过 FastMCP 的参数解析、类型转换、序列化。

相比 Layer 3 (stdio 黑盒):
    无子进程开销，执行更快，调试更方便。
"""

import json
import sys
from pathlib import Path
from typing import Any, Dict, Sequence
from unittest.mock import patch, MagicMock

import pytest

# 确保项目根目录在 Python path 中
PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.storage.sqlite_store import SQLiteStore
from src.storage.markdown_store import Entry, MarkdownStore
from src.relations.models import (
    BridgeCandidate,
    BridgeDiscoveryResult,
    CollectedEvidenceItem,
    CollectedEvidenceResult,
    ContrastCandidateItem,
    ContrastResult,
    RelationExplanationResult,
    RelationRecord,
    RelationSourceType,
    RelationSubgraphNode,
    RelationSubgraphResult,
    RelationType,
    TimelinePoint,
    TimelineResult,
)

# 导入 MCP 实例（会触发 tools/resources/prompts 注册）
from src.mcp.server import mcp


# ============================================================
# call_tool 结果解析辅助函数
# ============================================================

def parse_tool_result(result: Any) -> Dict[str, Any]:
    """将 FastMCP.call_tool() 的返回值解析为 dict。

    call_tool() 返回 Sequence[ContentBlock] | dict：
    - 如果 handler 返回 dict → FastMCP 序列化为 JSON 字符串放入 TextContent
    - 返回值形如 [TextContent(type='text', text='{...}')]

    本函数统一提取为 Python dict。
    """
    if isinstance(result, dict):
        return result
    # Sequence[ContentBlock] — 取第一个 TextContent 的 text 字段
    if isinstance(result, (list, tuple)) and len(result) > 0:
        first = result[0]
        text = getattr(first, "text", None)
        if text:
            return json.loads(text)
    raise ValueError(f"无法解析 call_tool 结果: {type(result)}")


# ============================================================
# Fixtures
# ============================================================

@pytest.fixture
def test_db(tmp_path: Path) -> SQLiteStore:
    """创建临时测试数据库。"""
    db_path = tmp_path / "test.db"
    store = SQLiteStore(db_path)
    store.initialize()
    return store


@pytest.fixture
def test_vault(tmp_path: Path) -> Path:
    """创建临时 Markdown vault 目录。"""
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    return vault_dir


@pytest.fixture
def populated_db(test_db: SQLiteStore, test_vault: Path) -> tuple:
    """填充测试数据的数据库和 vault。

    Returns:
        (SQLiteStore, MarkdownStore, vault_dir, list_of_entry_ids)
    """
    entries = [
        Entry(
            title="微信文章：AI 工程化实践",
            source_type="wechat",
            source_url="https://mp.weixin.qq.com/s/article1",
            tags=["AI", "工程化"],
            keywords=["人工智能", "MLOps"],
            abstract="AI 工程化实践总结",
            summary_one_sentence="AI 工程化的最佳实践指南",
            summary_100_words="本文总结了 AI 工程化的关键实践...",
            content="# AI 工程化\n\n这是关于 AI 工程化的详细内容。包含 MLOps 实践。",
            word_count=200,
        ),
        Entry(
            title="知乎回答：NLP 入门路线",
            source_type="zhihu",
            source_url="https://www.zhihu.com/answer/456",
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
            source_url="https://example.com/python",
            tags=["Python", "教程"],
            keywords=["编程"],
            abstract="Python 基础教程",
            summary_one_sentence="Python 编程入门教程",
            summary_100_words="Python 从入门到实践教程。",
            content="# Python 教程\n\nPython 从入门到实践。",
            word_count=300,
        ),
    ]

    md_store = MarkdownStore(test_vault)
    entry_ids = []
    for entry in entries:
        # 创建 Markdown 文件
        md_dir = test_vault / entry.source_type
        md_dir.mkdir(parents=True, exist_ok=True)
        safe_title = entry.title[:10].replace("：", "_")
        md_path = md_dir / f"{safe_title}.md"
        md_path.write_text(
            f"---\ntitle: {entry.title}\nsource_type: {entry.source_type}\n"
            f"tags: [{', '.join(entry.tags)}]\n---\n{entry.content}",
            encoding="utf-8",
        )
        # 插入数据库
        kid = test_db.insert_entry(entry, str(md_path))
        entry_ids.append(kid)

    return test_db, md_store, test_vault, entry_ids


def _patch_stores(store, md_store=None):
    """返回用于 mock MCP 服务单例的上下文管理器列表。"""
    patches = [
        patch("src.mcp.tools.get_sqlite_store", return_value=store),
        patch("src.mcp.resources.get_sqlite_store", return_value=store),
    ]
    if md_store:
        patches.extend([
            patch("src.mcp.tools.get_markdown_store", return_value=md_store),
            patch("src.mcp.resources.get_markdown_store", return_value=md_store),
        ])
    return patches


# ============================================================
# 1. Tool 注册验证
# ============================================================

class TestToolRegistration:
    """验证 Tool 注册机制是否正确。"""

    @pytest.mark.asyncio
    async def test_list_tools_returns_all_14(self):
        """list_tools() 应返回 14 个已注册的 Tool。"""
        tools = await mcp.list_tools()
        assert len(tools) == 14, f"期望 14 个 Tool，实际: {len(tools)}"

    @pytest.mark.asyncio
    async def test_tool_names(self):
        """14 个 Tool 名称应正确。"""
        tools = await mcp.list_tools()
        names = {t.name for t in tools}
        expected = {
            "search_knowledge", "get_entry", "list_tags", "list_entries",
            "get_stats", "archive_url", "archive_text", "get_related",
            "query_subgraph", "explain_relation", "collect_evidence",
            "find_bridges", "timeline_of", "contrast",
        }
        assert names == expected, f"Tool 名称不匹配: 多了 {names - expected}, 缺少 {expected - names}"

    @pytest.mark.asyncio
    async def test_readonly_tools_have_annotation(self):
        """只读 Tool 应标注 readOnlyHint=True。"""
        tools = await mcp.list_tools()
        readonly_names = {
            "search_knowledge", "get_entry", "list_tags", "list_entries",
            "get_stats", "get_related", "query_subgraph", "explain_relation",
            "collect_evidence", "find_bridges", "timeline_of", "contrast",
        }
        for tool in tools:
            if tool.name in readonly_names:
                assert tool.annotations is not None, f"{tool.name} 缺少 annotations"
                assert tool.annotations.readOnlyHint is True, f"{tool.name} readOnlyHint 应为 True"

    @pytest.mark.asyncio
    async def test_write_tools_no_readonly_annotation(self):
        """2 个写入 Tool 不应有 readOnlyHint=True。"""
        tools = await mcp.list_tools()
        write_names = {"archive_url", "archive_text"}
        for tool in tools:
            if tool.name in write_names:
                # 写入 Tool 要么无 annotations，要么 readOnlyHint 不为 True
                if tool.annotations:
                    assert tool.annotations.readOnlyHint is not True, \
                        f"{tool.name} 不应标注 readOnlyHint=True"

    @pytest.mark.asyncio
    async def test_tools_have_descriptions(self):
        """所有 Tool 应有非空描述。"""
        tools = await mcp.list_tools()
        for tool in tools:
            assert tool.description, f"{tool.name} 缺少描述"
            assert len(tool.description) > 10, f"{tool.name} 描述太短: {tool.description}"

    @pytest.mark.asyncio
    async def test_tool_input_schemas(self):
        """关键 Tool 的参数 Schema 应正确。"""
        tools = await mcp.list_tools()
        tool_map = {t.name: t for t in tools}

        # search_knowledge 应有 query 必填参数
        sk = tool_map["search_knowledge"]
        assert "query" in sk.inputSchema.get("properties", {}), "search_knowledge 缺少 query 参数"
        assert "query" in sk.inputSchema.get("required", []), "search_knowledge 的 query 应为必填"

        # archive_url 应有 url 必填参数
        au = tool_map["archive_url"]
        assert "url" in au.inputSchema.get("properties", {}), "archive_url 缺少 url 参数"

        # archive_text 应有 text 必填参数和可选 title
        at = tool_map["archive_text"]
        props = at.inputSchema.get("properties", {})
        assert "text" in props, "archive_text 缺少 text 参数"
        assert "title" in props, "archive_text 缺少 title 参数"

        # get_related 应有 knowledge_id 必填参数
        gr = tool_map["get_related"]
        assert "knowledge_id" in gr.inputSchema.get("properties", {}), "get_related 缺少 knowledge_id 参数"

        qs = tool_map["query_subgraph"]
        assert "knowledge_id" in qs.inputSchema.get("properties", {}), "query_subgraph 缺少 knowledge_id 参数"
        assert "depth" in qs.inputSchema.get("properties", {}), "query_subgraph 缺少 depth 参数"

        er = tool_map["explain_relation"]
        er_props = er.inputSchema.get("properties", {})
        assert "source_knowledge_id" in er_props, "explain_relation 缺少 source_knowledge_id 参数"
        assert "target_knowledge_id" in er_props, "explain_relation 缺少 target_knowledge_id 参数"

        ce = tool_map["collect_evidence"]
        ce_props = ce.inputSchema.get("properties", {})
        assert "question" in ce_props, "collect_evidence 缺少 question 参数"
        assert "top_k" in ce_props, "collect_evidence 缺少 top_k 参数"

        fb = tool_map["find_bridges"]
        fb_props = fb.inputSchema.get("properties", {})
        assert "seed_knowledge_id" in fb_props, "find_bridges 缺少 seed_knowledge_id 参数"

        tl = tool_map["timeline_of"]
        tl_props = tl.inputSchema.get("properties", {})
        assert "topic" in tl_props, "timeline_of 缺少 topic 参数"

        ct = tool_map["contrast"]
        ct_props = ct.inputSchema.get("properties", {})
        assert "topic_a" in ct_props, "contrast 缺少 topic_a 参数"
        assert "topic_b" in ct_props, "contrast 缺少 topic_b 参数"


# ============================================================
# 2. Prompt 注册验证
# ============================================================

class TestPromptRegistration:
    """验证 Prompt 注册机制是否正确。"""

    @pytest.mark.asyncio
    async def test_list_prompts_returns_all_3(self):
        """list_prompts() 应返回 3 个已注册的 Prompt。"""
        prompts = await mcp.list_prompts()
        assert len(prompts) == 3, f"期望 3 个 Prompt，实际: {len(prompts)}"

    @pytest.mark.asyncio
    async def test_prompt_names(self):
        """3 个 Prompt 名称应正确。"""
        prompts = await mcp.list_prompts()
        names = {p.name for p in prompts}
        expected = {"search_and_summarize", "knowledge_qa", "idea_sharpen"}
        assert names == expected

    @pytest.mark.asyncio
    async def test_prompts_have_descriptions(self):
        """所有 Prompt 应有非空描述。"""
        prompts = await mcp.list_prompts()
        for prompt in prompts:
            assert prompt.description, f"{prompt.name} 缺少描述"

    @pytest.mark.asyncio
    async def test_prompt_arguments(self):
        """关键 Prompt 的参数应正确。"""
        prompts = await mcp.list_prompts()
        prompt_map = {p.name: p for p in prompts}

        # search_and_summarize 应有 query (必填) 和 context (可选)
        sas = prompt_map["search_and_summarize"]
        arg_names = {a.name for a in (sas.arguments or [])}
        assert "query" in arg_names

        # knowledge_qa 应有 question (必填)
        kqa = prompt_map["knowledge_qa"]
        arg_names = {a.name for a in (kqa.arguments or [])}
        assert "question" in arg_names

        # idea_sharpen 应有 content (必填) 和 entry_id (可选)
        ids = prompt_map["idea_sharpen"]
        arg_names = {a.name for a in (ids.arguments or [])}
        assert "content" in arg_names
        assert "entry_id" in arg_names


# ============================================================
# 3. Prompt 调用验证
# ============================================================

class TestPromptExecution:
    """验证 Prompt 模板生成的输出。"""

    @pytest.mark.asyncio
    async def test_search_and_summarize_output(self):
        """search_and_summarize 应生成包含搜索引导的文本。"""
        result = await mcp.get_prompt("search_and_summarize", {"query": "AI 知识管理"})
        assert result.messages, "Prompt 应返回非空 messages"
        # 取第一条消息的文本内容
        text = str(result.messages[0])
        assert "AI 知识管理" in text
        assert "search_knowledge" in text

    @pytest.mark.asyncio
    async def test_search_and_summarize_with_context(self):
        """search_and_summarize 带 context 应包含背景信息。"""
        result = await mcp.get_prompt(
            "search_and_summarize",
            {"query": "NLP", "context": "准备写一篇综述"}
        )
        text = str(result.messages[0])
        assert "NLP" in text
        assert "准备写一篇综述" in text

    @pytest.mark.asyncio
    async def test_knowledge_qa_output(self):
        """knowledge_qa 应生成问答引导文本。"""
        result = await mcp.get_prompt("knowledge_qa", {"question": "什么是 RAG?"})
        text = str(result.messages[0])
        assert "什么是 RAG?" in text
        assert "search_knowledge" in text

    @pytest.mark.asyncio
    async def test_idea_sharpen_output(self):
        """idea_sharpen 应生成思想磨砺引导文本。"""
        result = await mcp.get_prompt(
            "idea_sharpen",
            {"content": "这是一篇关于知识管理的文章，讨论了个人知识库的构建方法。"}
        )
        text = str(result.messages[0])
        assert "知识管理" in text
        assert "核心价值" in text

    @pytest.mark.asyncio
    async def test_idea_sharpen_with_entry_id(self):
        """idea_sharpen 带 entry_id 应包含条目引用。"""
        result = await mcp.get_prompt(
            "idea_sharpen",
            {"content": "测试内容", "entry_id": "42"}
        )
        text = str(result.messages[0])
        assert "42" in text
        assert "get_entry" in text

    @pytest.mark.asyncio
    async def test_idea_sharpen_truncation(self):
        """idea_sharpen 应截取超长内容到 2000 字符。"""
        long_content = "长" * 5000
        result = await mcp.get_prompt(
            "idea_sharpen",
            {"content": long_content}
        )
        text = str(result.messages[0])
        # 截取后不应包含完整 5000 字
        assert text.count("长") <= 2000


# ============================================================
# 4. Resource 注册验证
# ============================================================

class TestResourceRegistration:
    """验证 Resource 注册机制是否正确。"""

    @pytest.mark.asyncio
    async def test_list_resource_templates(self):
        """应返回 Resource URI 模板（带参数的 Resource）。"""
        templates = await mcp.list_resource_templates()
        uris = {t.uriTemplate for t in templates}
        # 带参数的 Resource 是模板
        assert "pkv://entries/{knowledge_id}" in uris
        assert "pkv://entries/{knowledge_id}/metadata" in uris

    @pytest.mark.asyncio
    async def test_list_static_resources(self):
        """应返回静态 Resource（pkv://tags, pkv://stats）。"""
        resources = await mcp.list_resources()
        uris = {str(r.uri) for r in resources}
        assert "pkv://tags" in uris or "pkv://tags/" in uris
        assert "pkv://stats" in uris or "pkv://stats/" in uris


# ============================================================
# 5. Tool 调用 — 只读 (通过 call_tool)
# ============================================================

class TestToolCallReadonly:
    """通过 FastMCP.call_tool() 测试只读 Tool 调用。"""

    @pytest.mark.asyncio
    async def test_call_list_tags(self, populated_db):
        """call_tool('list_tags') 应返回标签数据。"""
        store, md_store, vault_dir, entry_ids = populated_db
        patches = _patch_stores(store, md_store)

        with patches[0], patches[1], patches[2], patches[3]:
            raw = await mcp.call_tool("list_tags", {})

        result = parse_tool_result(raw)
        assert "total_tags" in result
        assert result["total_tags"] > 0
        tag_names = [t["name"] for t in result["tags"]]
        assert "AI" in tag_names

    @pytest.mark.asyncio
    async def test_call_list_entries(self, populated_db):
        """call_tool('list_entries') 应返回条目列表。"""
        store, md_store, vault_dir, entry_ids = populated_db
        patches = _patch_stores(store, md_store)

        with patches[0], patches[1], patches[2], patches[3]:
            raw = await mcp.call_tool("list_entries", {"page": 1, "per_page": 10})

        result = parse_tool_result(raw)
        assert result["total"] == 3
        assert len(result["entries"]) == 3

    @pytest.mark.asyncio
    async def test_call_list_entries_filter(self, populated_db):
        """call_tool('list_entries') 按 source_type 过滤。"""
        store, md_store, vault_dir, entry_ids = populated_db
        patches = _patch_stores(store, md_store)

        with patches[0], patches[1], patches[2], patches[3]:
            raw = await mcp.call_tool(
                "list_entries", {"source_type": "wechat"}
            )

        result = parse_tool_result(raw)
        assert result["total"] == 1
        assert result["entries"][0]["source_type"] == "wechat"

    @pytest.mark.asyncio
    async def test_call_get_entry(self, populated_db):
        """call_tool('get_entry') 应返回完整条目。"""
        store, md_store, vault_dir, entry_ids = populated_db
        patches = _patch_stores(store, md_store)

        with patches[0], patches[1], patches[2], patches[3]:
            raw = await mcp.call_tool(
                "get_entry", {"knowledge_id": str(entry_ids[0])}
            )

        result = parse_tool_result(raw)
        assert result["title"] == "微信文章：AI 工程化实践"
        assert isinstance(result["tags"], list)
        assert "AI" in result["tags"]

    @pytest.mark.asyncio
    async def test_call_get_entry_not_found(self, populated_db):
        """call_tool('get_entry') 对不存在的 ID 返回 error。"""
        store, md_store, vault_dir, entry_ids = populated_db
        patches = _patch_stores(store, md_store)

        with patches[0], patches[1], patches[2], patches[3]:
            raw = await mcp.call_tool(
                "get_entry", {"knowledge_id": "99999"}
            )

        result = parse_tool_result(raw)
        assert "error" in result

    @pytest.mark.asyncio
    async def test_call_get_stats(self, populated_db):
        """call_tool('get_stats') 应返回统计数据。"""
        store, md_store, vault_dir, entry_ids = populated_db
        patches = _patch_stores(store, md_store)

        with patches[0], patches[1], patches[2], patches[3]:
            raw = await mcp.call_tool("get_stats", {})

        result = parse_tool_result(raw)
        # get_statistics 返回的字典应包含条目信息
        assert "total_entries" in result or "total" in result


# ============================================================
# 6. Tool 调用 — 写入 Tool 安全验证 (通过 call_tool)
# ============================================================

class TestToolCallWriteSecurity:
    """通过 call_tool() 验证写入 Tool 的安全拦截。"""

    @pytest.mark.asyncio
    async def test_archive_url_rejects_private_ip_127(self):
        """archive_url 应拒绝 127.0.0.1。"""
        raw = await mcp.call_tool(
            "archive_url", {"url": "http://127.0.0.1/admin"}
        )
        result = parse_tool_result(raw)
        assert result["success"] is False
        assert "内网" in result["error"]

    @pytest.mark.asyncio
    async def test_archive_url_rejects_private_ip_192(self):
        """archive_url 应拒绝 192.168.x.x。"""
        raw = await mcp.call_tool(
            "archive_url", {"url": "http://192.168.1.1/"}
        )
        result = parse_tool_result(raw)
        assert result["success"] is False
        assert "内网" in result["error"]

    @pytest.mark.asyncio
    async def test_archive_url_rejects_private_ip_10(self):
        """archive_url 应拒绝 10.x.x.x。"""
        raw = await mcp.call_tool(
            "archive_url", {"url": "http://10.0.0.1/path"}
        )
        result = parse_tool_result(raw)
        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_archive_url_rejects_localhost(self):
        """archive_url 应拒绝 localhost。"""
        raw = await mcp.call_tool(
            "archive_url", {"url": "http://localhost:8080/"}
        )
        result = parse_tool_result(raw)
        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_archive_url_rejects_ftp_scheme(self):
        """archive_url 应拒绝非 http/https 协议。"""
        raw = await mcp.call_tool(
            "archive_url", {"url": "ftp://files.example.com/data"}
        )
        result = parse_tool_result(raw)
        assert result["success"] is False
        assert "scheme" in result["error"] or "http" in result["error"]

    @pytest.mark.asyncio
    async def test_archive_url_rejects_empty(self):
        """archive_url 应拒绝空 URL。"""
        raw = await mcp.call_tool(
            "archive_url", {"url": ""}
        )
        result = parse_tool_result(raw)
        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_archive_text_rejects_empty(self):
        """archive_text 应拒绝空文本。"""
        raw = await mcp.call_tool(
            "archive_text", {"text": ""}
        )
        result = parse_tool_result(raw)
        assert result["success"] is False
        assert "空" in result["error"]

    @pytest.mark.asyncio
    async def test_archive_text_rejects_whitespace_only(self):
        """archive_text 应拒绝纯空白文本。"""
        raw = await mcp.call_tool(
            "archive_text", {"text": "   \n\t  "}
        )
        result = parse_tool_result(raw)
        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_archive_text_rejects_too_long(self):
        """archive_text 应拒绝超长文本 (>100000 字符)。"""
        raw = await mcp.call_tool(
            "archive_text", {"text": "x" * 100001}
        )
        result = parse_tool_result(raw)
        assert result["success"] is False
        assert "超过" in result["error"] or "限制" in result["error"]

    @pytest.mark.asyncio
    async def test_get_related_invalid_id(self):
        """get_related 应拒绝无效 ID。"""
        raw = await mcp.call_tool(
            "get_related", {"knowledge_id": "abc"}
        )
        result = parse_tool_result(raw)
        assert "error" in result
        assert "无效" in result["error"]

    @pytest.mark.asyncio
    async def test_get_related_not_found(self, populated_db):
        """get_related 应处理不存在的 ID。"""
        store, md_store, vault_dir, entry_ids = populated_db
        patches = _patch_stores(store, md_store)

        with patches[0], patches[1], patches[2], patches[3]:
            raw = await mcp.call_tool(
                "get_related", {"knowledge_id": "99999"}
            )

        result = parse_tool_result(raw)
        assert "error" in result
        assert "未找到" in result["error"]

    @pytest.mark.asyncio
    async def test_query_subgraph_success(self):
        """query_subgraph 应返回结构化子图。"""
        record = RelationRecord(
            source_knowledge_id=1,
            target_knowledge_id=2,
            relation_type=RelationType.REFERENCES,
            relation_source_type=RelationSourceType.MARKDOWN_LINK,
            evidence_payload={"href": "./beta.md"},
        )
        mock_service = MagicMock()
        mock_service.query_subgraph.return_value = RelationSubgraphResult(
            seed_knowledge_id=1,
            max_depth=2,
            nodes=[
                RelationSubgraphNode(knowledge_id=1, depth=0),
                RelationSubgraphNode(knowledge_id=2, depth=1),
            ],
            edges=[record],
            grouped_edges={RelationType.REFERENCES.value: [record]},
        )

        with patch("src.mcp.tools.get_relation_query_service", return_value=mock_service):
            raw = await mcp.call_tool(
                "query_subgraph",
                {"knowledge_id": "1", "depth": 2, "relation_types": ["references"]},
            )

        result = parse_tool_result(raw)
        assert result["seed_knowledge_id"] == 1
        assert result["total_nodes"] == 2
        assert result["grouped_edges"][RelationType.REFERENCES.value][0]["target_knowledge_id"] == 2

    @pytest.mark.asyncio
    async def test_explain_relation_success(self):
        """explain_relation 应返回结构化关系解释。"""
        record = RelationRecord(
            source_knowledge_id=1,
            target_knowledge_id=2,
            relation_type=RelationType.RELATED_DOCUMENT,
            relation_source_type=RelationSourceType.FRONTMATTER_RELATED_DOCS,
            evidence_payload={"field": "related_docs"},
        )
        mock_service = MagicMock()
        mock_service.explain_relation.return_value = RelationExplanationResult(
            source_knowledge_id=1,
            target_knowledge_id=2,
            found=True,
            explanation_type="direct",
            hops=1,
            path=[record],
            supporting_relations=[record],
            summary="1 -[related_document]-> 2",
            evidence_items=[
                {
                    "step_index": 0,
                    "relation_type": RelationType.RELATED_DOCUMENT.value,
                    "relation_source_type": RelationSourceType.FRONTMATTER_RELATED_DOCS.value,
                    "direction": record.direction.value,
                    "weight": record.weight,
                    "source_knowledge_id": 1,
                    "target_knowledge_id": 2,
                    "evidence_payload": {"field": "related_docs"},
                }
            ],
        )

        with patch("src.mcp.tools.get_relation_query_service", return_value=mock_service):
            raw = await mcp.call_tool(
                "explain_relation",
                {"source_knowledge_id": "1", "target_knowledge_id": "2"},
            )

        result = parse_tool_result(raw)
        assert result["found"] is True
        assert result["explanation_type"] == "direct"
        assert result["summary"] == "1 -[related_document]-> 2"

    @pytest.mark.asyncio
    async def test_collect_evidence_success(self):
        """collect_evidence 应返回结构化证据包。"""
        mock_service = MagicMock()
        mock_service.collect_evidence.return_value = CollectedEvidenceResult(
            question="Alpha 和 Beta 有什么关系？",
            found=True,
            seed_knowledge_id=1,
            seed_title="Alpha",
            evidence=[
                CollectedEvidenceItem(
                    knowledge_id=1,
                    title="Alpha",
                    abstract="Alpha 摘要",
                    source_type="generic",
                    archived_at="2026-03-10 10:00:00",
                    tags=["AI"],
                    retrieval_rank=1,
                    retrieval_score=0.95,
                    is_seed=True,
                ),
                CollectedEvidenceItem(
                    knowledge_id=2,
                    title="Beta",
                    abstract="Beta 摘要",
                    source_type="generic",
                    archived_at="2026-03-10 10:10:00",
                    tags=["知识图谱"],
                    retrieval_rank=2,
                    retrieval_score=0.82,
                    relation_found=True,
                    relation_explanation_type="direct",
                    relation_hops=1,
                    relation_summary="1 -[related_document]-> 2",
                ),
            ],
            summary="围绕问题共聚合 2 条证据",
        )

        with patch("src.mcp.tools.get_evidence_collection_service", return_value=mock_service):
            raw = await mcp.call_tool(
                "collect_evidence",
                {"question": "Alpha 和 Beta 有什么关系？", "top_k": 5},
            )

        result = parse_tool_result(raw)
        assert result["found"] is True
        assert result["seed_knowledge_id"] == 1
        assert result["total_evidence"] == 2
        assert result["related_evidence_count"] == 1

    @pytest.mark.asyncio
    async def test_find_bridges_success(self):
        """find_bridges 应返回桥接候选。"""
        mock_service = MagicMock()
        mock_service.find_bridges.return_value = BridgeDiscoveryResult(
            seed_knowledge_id=1,
            found=True,
            max_depth=2,
            items=[
                BridgeCandidate(
                    knowledge_id=3,
                    title="Gamma",
                    depth=1,
                    bridge_score=2.25,
                    connected_knowledge_ids=[1, 4],
                    relation_types=["references", "related_document"],
                    summary="Gamma 是桥接候选",
                )
            ],
            summary="找到 1 个桥接候选",
            limitation_notes=["partial"],
        )

        with patch("src.mcp.tools.get_exploration_service", return_value=mock_service):
            raw = await mcp.call_tool(
                "find_bridges",
                {"seed_knowledge_id": "1", "top_k": 5, "max_depth": 2},
            )

        result = parse_tool_result(raw)
        assert result["found"] is True
        assert result["total_bridges"] == 1
        assert result["implementation_level"] == "partial"

    @pytest.mark.asyncio
    async def test_timeline_of_success(self):
        """timeline_of 应返回弱时间线。"""
        mock_service = MagicMock()
        mock_service.timeline_of.return_value = TimelineResult(
            topic="AI Timeline",
            found=True,
            items=[
                TimelinePoint(
                    knowledge_id=1,
                    title="Alpha",
                    archived_at="2026-03-10 10:00:00",
                    source_type="generic",
                    abstract="Alpha 摘要",
                    tags=["AI"],
                    retrieval_score=0.91,
                )
            ],
            summary="时间线已生成",
            limitation_notes=["partial"],
        )

        with patch("src.mcp.tools.get_exploration_service", return_value=mock_service):
            raw = await mcp.call_tool(
                "timeline_of",
                {"topic": "AI Timeline", "top_k": 5, "sort_order": "asc"},
            )

        result = parse_tool_result(raw)
        assert result["found"] is True
        assert result["total_points"] == 1
        assert result["implementation_level"] == "partial"

    @pytest.mark.asyncio
    async def test_contrast_success(self):
        """contrast 应返回主题对比结构。"""
        mock_service = MagicMock()
        mock_service.contrast.return_value = ContrastResult(
            topic_a="Topic A",
            topic_b="Topic B",
            found=True,
            topic_a_candidates=[
                ContrastCandidateItem(
                    knowledge_id=1,
                    title="Alpha",
                    abstract="Alpha 摘要",
                    archived_at="2026-03-10 10:00:00",
                    source_type="generic",
                    tags=["AI", "共同"],
                    retrieval_score=0.93,
                )
            ],
            topic_b_candidates=[
                ContrastCandidateItem(
                    knowledge_id=2,
                    title="Beta",
                    abstract="Beta 摘要",
                    archived_at="2026-03-11 10:00:00",
                    source_type="generic",
                    tags=["时间线", "共同"],
                    retrieval_score=0.87,
                )
            ],
            shared_tags=["共同"],
            only_a_tags=["AI"],
            only_b_tags=["时间线"],
            overlap_knowledge_ids=[],
            summary="对比完成",
            limitation_notes=["partial"],
        )

        with patch("src.mcp.tools.get_exploration_service", return_value=mock_service):
            raw = await mcp.call_tool(
                "contrast",
                {"topic_a": "Topic A", "topic_b": "Topic B", "top_k": 5},
            )

        result = parse_tool_result(raw)
        assert result["found"] is True
        assert result["implementation_level"] == "partial"
        assert result["shared_tags"] == ["共同"]


# ============================================================
# 7. Resource 调用验证 (通过 read_resource)
# ============================================================

class TestResourceRead:
    """通过 FastMCP.read_resource() 测试 Resource 读取。"""

    @pytest.mark.asyncio
    async def test_read_tags_resource(self, populated_db):
        """read_resource('pkv://tags') 应返回 JSON 标签列表。"""
        store, md_store, vault_dir, entry_ids = populated_db
        patches = _patch_stores(store, md_store)

        with patches[0], patches[1], patches[2], patches[3]:
            contents = await mcp.read_resource("pkv://tags")

        assert contents, "Resource 应返回非空内容"
        content = list(contents)[0]
        data = json.loads(content.content)
        assert "total_tags" in data
        assert data["total_tags"] > 0

    @pytest.mark.asyncio
    async def test_read_stats_resource(self, populated_db):
        """read_resource('pkv://stats') 应返回 JSON 统计数据。"""
        store, md_store, vault_dir, entry_ids = populated_db
        patches = _patch_stores(store, md_store)

        with patches[0], patches[1], patches[2], patches[3]:
            contents = await mcp.read_resource("pkv://stats")

        content = list(contents)[0]
        data = json.loads(content.content)
        assert "total_entries" in data or "total" in data

    @pytest.mark.asyncio
    async def test_read_entry_content_resource(self, populated_db):
        """read_resource('pkv://entries/{id}') 应返回 Markdown 内容。"""
        store, md_store, vault_dir, entry_ids = populated_db
        patches = _patch_stores(store, md_store)

        with patches[0], patches[1], patches[2], patches[3]:
            contents = await mcp.read_resource(
                f"pkv://entries/{entry_ids[0]}"
            )

        content = list(contents)[0]
        text = content.content
        assert isinstance(text, str)
        # 内容应来自 Markdown 文件
        assert "AI 工程化" in text or "title" in text.lower()

    @pytest.mark.asyncio
    async def test_read_entry_metadata_resource(self, populated_db):
        """read_resource('pkv://entries/{id}/metadata') 应返回 JSON 元数据。"""
        store, md_store, vault_dir, entry_ids = populated_db
        patches = _patch_stores(store, md_store)

        with patches[0], patches[1], patches[2], patches[3]:
            contents = await mcp.read_resource(
                f"pkv://entries/{entry_ids[0]}/metadata"
            )

        content = list(contents)[0]
        data = json.loads(content.content)
        assert data["title"] == "微信文章：AI 工程化实践"
        assert isinstance(data["tags"], list)
