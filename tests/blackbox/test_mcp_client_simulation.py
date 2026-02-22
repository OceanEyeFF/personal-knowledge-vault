"""
Layer 2 MCP 客户端模拟器 + 6 场景测试

目标:
- 进程内调用 FastMCP (asyncio)
- 使用 .data-test/ 隔离环境
- 集成样本数据 (WECHAT_SAMPLES, ZHIHU_SAMPLES, TEXT_SAMPLES)
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import uuid
from pathlib import Path
from typing import Any, Dict, List, Tuple
from unittest.mock import patch

import numpy as np
import pytest

# 确保项目根目录在 Python path 中
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.mcp.server import mcp
from src.storage.markdown_store import Entry, MarkdownStore
from src.storage.sqlite_store import SQLiteStore
from src.storage.vector_store import VectorStore
from src.workflow.models import WorkflowResult
import src.mcp.server as mcp_server
import src.utils.config as config_module


# ============================================================
# 样本数据 (tests/fixtures)
# ============================================================

FIXTURES_DIR = PROJECT_ROOT / "tests" / "fixtures"


def _read_fixture(name: str) -> str:
    path = FIXTURES_DIR / name
    return path.read_text(encoding="utf-8")


WECHAT_SAMPLES: List[Tuple[str, str]] = [
    ("https://mp.weixin.qq.com/s/sample_ai_workflow", _read_fixture("wechat_sample.html")),
    ("https://mp.weixin.qq.com/s/sample_ai_toolkit", _read_fixture("wechat_sample.html")),
]

ZHIHU_SAMPLES: List[Tuple[str, str]] = [
    ("https://www.zhihu.com/question/sample_sky_blue", _read_fixture("zhihu_sample.html")),
]

TEXT_SAMPLES: List[str] = [
    _read_fixture("chat_sample.txt"),
    _read_fixture("wechat_chat_sample.txt")[:500],
]


# ============================================================
# MCP 调用结果解析辅助函数
# ============================================================

def parse_tool_result(result: Any) -> Dict[str, Any]:
    """将 FastMCP.call_tool() 的返回值解析为 dict。"""
    if isinstance(result, dict):
        return result
    if isinstance(result, (list, tuple)) and len(result) > 0:
        first = result[0]
        text = getattr(first, "text", None)
        if text:
            return json.loads(text)
    raise ValueError(f"无法解析 call_tool 结果: {type(result)}")


def extract_prompt_text(prompt_result: Any) -> str:
    """兼容多种 PromptResult 输出结构，提取文本。"""
    if hasattr(prompt_result, "messages") and prompt_result.messages:
        msg = prompt_result.messages[0]
        content = getattr(msg, "content", None)
        if isinstance(content, str) and content:
            return content
        if hasattr(content, "text"):
            return content.text
        return str(msg)
    return str(prompt_result)


def assert_search_schema(result: Dict[str, Any]) -> None:
    assert "total" in result
    assert "results" in result
    assert isinstance(result["results"], list)
    for item in result["results"]:
        for key in ["knowledge_id", "title", "abstract", "score", "tags", "source_type", "archived_at"]:
            assert key in item
        assert isinstance(item["tags"], list)
        assert isinstance(item["score"], (int, float))


def assert_entry_schema(entry: Dict[str, Any]) -> None:
    for key in ["knowledge_id", "title", "content", "tags", "source_type", "source_url"]:
        assert key in entry
    assert isinstance(entry["tags"], list)
    assert entry["content"]


def assert_related_schema(result: Dict[str, Any]) -> None:
    assert "results" in result
    assert isinstance(result["results"], list)
    for item in result["results"]:
        for key in ["knowledge_id", "title", "abstract", "score", "tags", "source_type"]:
            assert key in item


def build_reference_card(entry: Dict[str, Any]) -> str:
    """生成简单的引用卡片 HTML。"""
    tags = ", ".join(entry.get("tags") or [])
    source_url = entry.get("source_url") or ""
    return (
        f"<div class='ref-card' data-id='{entry['knowledge_id']}'>"
        f"<a href='{source_url}'>{entry['title']}</a>"
        f"<span class='meta'>{entry.get('source_type', '')}</span>"
        f"<span class='tags'>{tags}</span>"
        "</div>"
    )


# ============================================================
# MCP 客户端模拟器
# ============================================================

class MCPClientSimulator:
    """模拟 Claude Code MCP 客户端的行为。"""

    def __init__(self, mcp_instance):
        self.mcp = mcp_instance

    async def _call_tool(self, name: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        raw = await self.mcp.call_tool(name, payload)
        return parse_tool_result(raw)

    async def search_and_display(
        self,
        query: str,
        strategy: str = "bm25",
        top_k: int = 5,
        source_type: str | None = None,
        tag: str | None = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "query": query,
            "strategy": strategy,
            "top_k": top_k,
        }
        if source_type:
            payload["source_type"] = source_type
        if tag:
            payload["tag"] = tag
        return await self._call_tool("search_knowledge", payload)

    async def archive_and_verify(
        self,
        url: str,
        verify_query: str,
        delay: float = 0.0,
    ) -> Dict[str, Any]:
        archive_result = await self._call_tool("archive_url", {"url": url})
        search_result = None
        if archive_result.get("success"):
            if delay:
                await asyncio.sleep(delay)
            search_result = await self.search_and_display(verify_query, strategy="bm25")
        return {"archive": archive_result, "search": search_result}

    async def get_related_entries(self, entry_id: str, limit: int = 5) -> Dict[str, Any]:
        return await self._call_tool(
            "get_related", {"knowledge_id": str(entry_id), "limit": limit}
        )

    async def get_entry_detail(self, entry_id: str) -> Dict[str, Any]:
        return await self._call_tool("get_entry", {"knowledge_id": str(entry_id)})

    async def test_prompt_template(self, prompt_name: str, context: Dict[str, Any]) -> str:
        context = dict(context or {})
        injected = context.pop("injected_context", None)
        result = await self.mcp.get_prompt(prompt_name, context)
        text = extract_prompt_text(result)
        if injected:
            text = f"{text}\n\n[检索上下文]\n{injected}"
        return text


# ============================================================
# Fixtures: .data-test 环境 + 样本数据
# ============================================================

def _build_sample_entries() -> List[Entry]:
    wechat_html_a = WECHAT_SAMPLES[0][1]
    wechat_html_b = WECHAT_SAMPLES[1][1]
    zhihu_html = ZHIHU_SAMPLES[0][1]
    text_a = TEXT_SAMPLES[0]
    text_b = TEXT_SAMPLES[1]

    return [
        Entry(
            title="微信样本：AI 工作流实践",
            source_type="wechat",
            source_url=WECHAT_SAMPLES[0][0],
            tags=["AI", "工作流", "实践"],
            keywords=["AI", "工作流"],
            abstract="AI 工作流实践摘要",
            summary_one_sentence="AI 工作流实践要点",
            summary_100_words="介绍 AI 工作流 的关键步骤与最佳实践。",
            content=f"{wechat_html_a}\n\nAI 工作流 关键步骤：设计-实现-验证。",
            word_count=200,
        ),
        Entry(
            title="微信样本：AI 工具箱速览",
            source_type="wechat",
            source_url=WECHAT_SAMPLES[1][0],
            tags=["AI", "工具"],
            keywords=["AI", "工具箱"],
            abstract="AI 工具箱速览",
            summary_one_sentence="快速了解 AI 工具箱",
            summary_100_words="包含 embedding、prompt、检索组件的速览。",
            content=f"{wechat_html_b}\n\nAI 工具箱 组件清单与用途。",
            word_count=180,
        ),
        Entry(
            title="知乎样本：天空为什么是蓝色",
            source_type="zhihu",
            source_url=ZHIHU_SAMPLES[0][0],
            tags=["科普", "物理"],
            keywords=["天空", "蓝色"],
            abstract="天空颜色的物理解释",
            summary_one_sentence="散射导致天空呈蓝色",
            summary_100_words="从瑞利散射解释天空为何是蓝色。",
            content=f"{zhihu_html}\n\n天空 蓝色 散射 物理 原理。",
            word_count=160,
        ),
        Entry(
            title="文本样本：项目计划与工作流",
            source_type="text",
            source_url="local://text/plan",
            tags=["计划", "工作流"],
            keywords=["计划", "AI", "工作流"],
            abstract="项目计划与工作流讨论",
            summary_one_sentence="记录项目计划与工作流讨论",
            summary_100_words="围绕 AI 工作流 的计划讨论纪要。",
            content=f"{text_a}\n\nAI 工作流 计划 讨论 纪要。",
            word_count=120,
        ),
        Entry(
            title="文本样本：灵感速记",
            source_type="text",
            source_url="local://text/idea",
            tags=["灵感", "速记"],
            keywords=["灵感", "速记"],
            abstract="灵感速记内容",
            summary_one_sentence="关于灵感的快速记录",
            summary_100_words="灵感与想法的简短记录，用于后续磨砺。",
            content=f"{text_b}\n\n灵感 速记 片段。",
            word_count=140,
        ),
    ]


@pytest.fixture
def test_env(monkeypatch) -> Dict[str, Path]:
    run_id = f"mcp_client_sim_{uuid.uuid4().hex[:8]}"
    base_dir = PROJECT_ROOT / ".data-test" / "mcp_client_sim" / run_id
    db_path = base_dir / "db" / "knowledge_vault.db"
    vault_dir = base_dir / "vault"
    vector_dir = base_dir / "vectors"

    db_path.parent.mkdir(parents=True, exist_ok=True)
    vault_dir.mkdir(parents=True, exist_ok=True)
    vector_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("OPENAI_EMBEDDING_DIM", "4")
    monkeypatch.setenv("LOG_LEVEL", "WARNING")

    # 重置配置与 MCP 单例缓存
    config = config_module.Config()
    config._config.setdefault("storage", {})
    config._config["storage"]["vector_index_dir"] = str(vector_dir)
    config._config["storage"]["vault_dir"] = str(vault_dir)
    config_module._config_instance = config
    mcp_server._sqlite_store = None
    mcp_server._markdown_store = None
    mcp_server._query_router = None

    yield {
        "base_dir": base_dir,
        "db_path": db_path,
        "vault_dir": vault_dir,
        "vector_dir": vector_dir,
    }

    shutil.rmtree(base_dir, ignore_errors=True)


@pytest.fixture
def populated_env(test_env) -> Dict[str, Any]:
    store = SQLiteStore(test_env["db_path"])
    store.initialize()
    md_store = MarkdownStore(test_env["vault_dir"])

    entries = _build_sample_entries()
    entry_ids: List[int] = []
    for entry in entries:
        md_path = md_store.save(entry, subdir=entry.source_type)
        entry_id = store.insert_entry(entry, str(md_path))
        entry_ids.append(entry_id)

    # 构造向量索引，保证 get_related 可用
    dim = int(os.environ.get("OPENAI_EMBEDDING_DIM", "4"))
    vector_store = VectorStore(test_env["vector_dir"], dim=dim)

    vectors = [
        np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32),
        np.array([0.95, 0.05, 0.0, 0.0], dtype=np.float32),
        np.array([0.0, 1.0, 0.0, 0.0], dtype=np.float32),
        np.array([1.0, 0.0, 0.1, 0.0], dtype=np.float32),
        np.array([0.0, 0.0, 1.0, 0.0], dtype=np.float32),
    ]
    for kid, vec in zip(entry_ids, vectors):
        vector_store.add_doc_vector(kid, vec)

    return {
        "store": store,
        "md_store": md_store,
        "entry_ids": entry_ids,
        "entries": entries,
        "vector_store": vector_store,
    }


@pytest.fixture
def mcp_patches(populated_env):
    patches = [
        patch("src.mcp.tools.get_sqlite_store", return_value=populated_env["store"]),
        patch("src.mcp.resources.get_sqlite_store", return_value=populated_env["store"]),
        patch("src.mcp.tools.get_markdown_store", return_value=populated_env["md_store"]),
        patch("src.mcp.resources.get_markdown_store", return_value=populated_env["md_store"]),
    ]
    for p in patches:
        p.start()
    yield
    for p in reversed(patches):
        p.stop()


@pytest.fixture
def archive_stub(populated_env):
    store = populated_env["store"]
    md_store = populated_env["md_store"]

    async def _fake_execute_async(self, workflow_name: str, input_data: Dict[str, Any]) -> WorkflowResult:
        if workflow_name != "archive-url":
            return WorkflowResult(success=False, data={}, errors=["未知工作流"], logs=[])

        url = input_data.get("url", "")
        entry = Entry(
            title="快速归档：AI 工作流测试",
            source_type="generic",
            source_url=url,
            tags=["归档", "URL", "AI"],
            keywords=["归档", "AI", "工作流"],
            abstract="快速归档测试条目",
            summary_one_sentence="快速归档测试条目",
            summary_100_words="用于验证 MCP archive_url 的模拟结果。",
            content="快速归档内容示例：AI 工作流 测试。",
            word_count=80,
        )
        md_path = md_store.save(entry, subdir=entry.source_type)
        knowledge_id = store.insert_entry(entry, str(md_path))
        return WorkflowResult(
            success=True,
            data={
                "knowledge_id": knowledge_id,
                "title": entry.title,
                "file_path": str(md_path),
                "tags": entry.tags,
                "summary_one_sentence": entry.summary_one_sentence,
            },
            errors=[],
            logs=[],
        )

    with patch("src.workflow.engine.WorkflowEngine.execute_async", new=_fake_execute_async):
        yield


# ============================================================
# 6 个真实场景测试
# ============================================================

@pytest.mark.asyncio
async def test_scenario_1_search_flow(mcp_patches, populated_env):
    """场景 1: 知识库搜索 (准确性、排序、字段完整)。"""
    simulator = MCPClientSimulator(mcp)

    result = await simulator.search_and_display("AI 工作流", strategy="bm25", top_k=5)
    assert result.get("strategy_used") == "bm25"
    assert result["total"] == len(result["results"])
    assert result["total"] >= 2

    assert_search_schema(result)

    scores = [r["score"] for r in result["results"]]
    assert scores == sorted(scores, reverse=True)

    for item in result["results"]:
        text = f"{item['title']} {' '.join(item['tags'])}"
        assert ("AI" in text) or ("工作流" in text)

    # 搜索后获取第一条详情
    first_id = result["results"][0]["knowledge_id"]
    entry = await simulator.get_entry_detail(first_id)
    assert_entry_schema(entry)


@pytest.mark.asyncio
async def test_scenario_2_archive_and_search(mcp_patches, archive_stub):
    """场景 2: 快速归档 URL + 即刻搜索验证。"""
    simulator = MCPClientSimulator(mcp)

    outcome = await simulator.archive_and_verify(
        "https://example.com/fast-archive",
        "快速归档",
    )
    archive_result = outcome["archive"]
    search_result = outcome["search"]

    assert archive_result["success"] is True
    assert archive_result.get("knowledge_id")
    assert archive_result.get("file_path")
    assert Path(archive_result["file_path"]).exists()

    assert_search_schema(search_result)
    titles = [r["title"] for r in search_result["results"]]
    assert any("快速归档" in title for title in titles)


@pytest.mark.asyncio
async def test_scenario_2_archive_invalid_url(mcp_patches):
    """场景 2 错误路径: 无效 URL 应被拒绝。"""
    simulator = MCPClientSimulator(mcp)

    outcome = await simulator.archive_and_verify(
        "http://127.0.0.1/admin",
        "AI 工作流",
    )
    archive_result = outcome["archive"]

    assert archive_result["success"] is False
    assert "内网" in archive_result["error"]
    assert outcome["search"] is None


@pytest.mark.asyncio
async def test_scenario_3_related_and_reference_cards(mcp_patches, populated_env):
    """场景 3: 关联条目查询 + 引用卡片生成。"""
    simulator = MCPClientSimulator(mcp)
    entry_id = populated_env["entry_ids"][0]

    related = await simulator.get_related_entries(entry_id, limit=3)
    assert_related_schema(related)
    assert related["total"] >= 1

    cards = []
    for item in related["results"]:
        detail = await simulator.get_entry_detail(item["knowledge_id"])
        assert_entry_schema(detail)
        card = build_reference_card(detail)
        assert detail["title"] in card
        assert str(detail["knowledge_id"]) in card
        if detail.get("source_url"):
            assert detail["source_url"] in card
        cards.append(card)

    assert cards


@pytest.mark.asyncio
async def test_scenario_4_prompt_search_suggestion(mcp_patches):
    """场景 4: 搜索建议 Prompt 模板验证。"""
    simulator = MCPClientSimulator(mcp)

    prompt = await simulator.test_prompt_template(
        "search_and_summarize",
        {"query": "AI 工作流", "context": "准备写一份方案"},
    )
    assert "AI 工作流" in prompt
    assert "准备写一份方案" in prompt
    assert "search_knowledge" in prompt


@pytest.mark.asyncio
async def test_scenario_5_prompt_knowledge_qa_context(mcp_patches):
    """场景 5: 知识库 QA Prompt 上下文注入验证。"""
    simulator = MCPClientSimulator(mcp)

    search_result = await simulator.search_and_display("AI 工作流", strategy="bm25")
    context_lines = [
        f"- {item['title']} ({item['source_type']})"
        for item in search_result["results"][:3]
    ]
    injected_context = "\n".join(context_lines)

    prompt = await simulator.test_prompt_template(
        "knowledge_qa",
        {"question": "AI 工作流是什么？", "injected_context": injected_context},
    )
    assert "AI 工作流是什么" in prompt
    assert "search_knowledge" in prompt
    assert "get_entry" in prompt
    assert "[检索上下文]" in prompt
    for line in context_lines:
        assert line in prompt


@pytest.mark.asyncio
async def test_scenario_6_prompt_idea_sharpen_inspiration(mcp_patches, populated_env):
    """场景 6: 思想磨砺 Prompt 灵感补充验证。"""
    simulator = MCPClientSimulator(mcp)

    entry_id = populated_env["entry_ids"][0]
    entry = await simulator.get_entry_detail(entry_id)
    related = await simulator.get_related_entries(entry_id, limit=2)

    inspiration_lines = ["灵感补充:"] + [
        f"- {item['title']}" for item in related["results"]
    ]
    inspiration_text = "\n".join(inspiration_lines)

    prompt = await simulator.test_prompt_template(
        "idea_sharpen",
        {
            "content": entry["content"][:200],
            "entry_id": str(entry_id),
            "injected_context": inspiration_text,
        },
    )
    assert "核心价值" in prompt
    assert str(entry_id) in prompt
    assert "灵感补充" in prompt


@pytest.mark.asyncio
async def test_search_no_results_returns_empty(mcp_patches):
    """错误路径: 搜索无结果时应返回空列表。"""
    simulator = MCPClientSimulator(mcp)

    result = await simulator.search_and_display("不存在的关键词xyz", strategy="bm25")
    assert result["total"] == 0
    assert result["results"] == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
