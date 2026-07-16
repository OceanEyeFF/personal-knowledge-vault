# ruff: noqa: E402

"""E2E pytest fixtures for MCP search tests."""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from mcp import StdioServerParameters
from mcp.client.session import ClientSession
from mcp.client.stdio import stdio_client

from src.storage.markdown_store import Entry
from src.storage.sqlite_store import SQLiteStore


@dataclass(frozen=True)
class TestEnv:
    data_dir: Path
    db_path: Path
    vault_dir: Path
    vector_dir: Path
    env: Dict[str, str]


@dataclass(frozen=True)
class MCPTestClient:
    params: StdioServerParameters

    async def _with_session(self, operation: Callable[[ClientSession], Awaitable[Any]]) -> Any:
        async with stdio_client(self.params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await operation(session)

    async def call_tool(self, name: str, arguments: Dict[str, Any]):
        return await self._with_session(lambda session: session.call_tool(name, arguments))

    async def get_prompt(self, name: str, arguments: Dict[str, str] | None = None):
        return await self._with_session(lambda session: session.get_prompt(name, arguments))


def _clean_path(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
    elif path.exists():
        path.unlink(missing_ok=True)


def _write_markdown(entry: Entry, path: Path) -> None:
    path.write_text(
        "---\n"
        f"title: {entry.title}\n"
        f"source_type: {entry.source_type}\n"
        f"tags: [{', '.join(entry.tags)}]\n"
        "---\n"
        f"{entry.content}\n",
        encoding="utf-8",
    )


def _build_sample_entries() -> List[Entry]:
    base = [
        {
            "title": "AI 工作流深度实践",
            "source_type": "wechat",
            "tags": ["AI", "工作流"],
            "keywords": ["AI", "工作流", "工程化"],
            "summary": "AI 工作流从数据到部署的完整实践路径。",
            "archived_at": "2026-02-20 10:00:00",
        },
        {
            "title": "AI 工作流速查笔记",
            "source_type": "zhihu",
            "tags": ["AI", "工作流", "笔记"],
            "keywords": ["AI", "工作流"],
            "summary": "快速回顾 AI 工作流的关键环节与常见坑位。",
            "archived_at": "2026-02-18 09:00:00",
        },
        {
            "title": "AI 工具链入门",
            "source_type": "text",
            "tags": ["AI", "工具"],
            "keywords": ["工具链", "AI"],
            "summary": "从零搭建 AI 工具链，覆盖检索、向量化与评测。",
            "archived_at": "2026-02-10 08:00:00",
        },
        {
            "title": "NLP 入门路线",
            "source_type": "zhihu",
            "tags": ["NLP", "入门"],
            "keywords": ["自然语言处理", "NLP"],
            "summary": "自然语言处理入门路线与关键概念速览。",
            "archived_at": "2026-01-22 11:30:00",
        },
        {
            "title": "知识管理系统设计",
            "source_type": "generic",
            "tags": ["知识管理", "系统设计"],
            "keywords": ["知识库", "系统设计"],
            "summary": "知识管理系统的架构拆解与演进路径。",
            "archived_at": "2026-01-20 12:00:00",
        },
        {
            "title": "向量检索基础",
            "source_type": "text",
            "tags": ["向量检索", "搜索"],
            "keywords": ["向量", "检索"],
            "summary": "向量检索基础概念与常见索引结构。",
            "archived_at": "2026-01-18 09:15:00",
        },
        {
            "title": "BM25 检索原理",
            "source_type": "text",
            "tags": ["BM25", "搜索"],
            "keywords": ["BM25", "关键词"],
            "summary": "BM25 检索原理与 FTS5 实践要点。",
            "archived_at": "2026-01-17 14:05:00",
        },
        {
            "title": "RAG 实战案例",
            "source_type": "wechat",
            "tags": ["RAG", "AI"],
            "keywords": ["RAG", "检索增强"],
            "summary": "RAG 实战案例拆解：从检索到生成的闭环。",
            "archived_at": "2026-01-15 16:20:00",
        },
        {
            "title": "Python 高效编程",
            "source_type": "generic",
            "tags": ["Python", "编程"],
            "keywords": ["Python", "效率"],
            "summary": "Python 高效编程技巧与工具推荐。",
            "archived_at": "2026-01-12 10:45:00",
        },
        {
            "title": "数据库索引优化",
            "source_type": "zhihu",
            "tags": ["数据库", "性能"],
            "keywords": ["索引", "优化"],
            "summary": "数据库索引优化策略与常见误区。",
            "archived_at": "2026-01-10 15:30:00",
        },
        {
            "title": "分布式系统 CAP 定理",
            "source_type": "generic",
            "tags": ["分布式", "理论"],
            "keywords": ["CAP", "一致性"],
            "summary": "CAP 定理的核心约束与工程权衡。",
            "archived_at": "2026-01-08 09:00:00",
        },
        {
            "title": "大模型提示词工程",
            "source_type": "wechat",
            "tags": ["大模型", "Prompt"],
            "keywords": ["提示词", "大模型"],
            "summary": "提示词工程的结构化方法与最佳实践。",
            "archived_at": "2026-01-06 13:10:00",
        },
        {
            "title": "MLOps 自动化流水线",
            "source_type": "text",
            "tags": ["MLOps", "工程化"],
            "keywords": ["MLOps", "流水线"],
            "summary": "MLOps 自动化流水线的搭建要点。",
            "archived_at": "2026-01-05 18:40:00",
        },
        {
            "title": "ChatGPT 使用指南",
            "source_type": "generic",
            "tags": ["AI", "工具"],
            "keywords": ["ChatGPT", "AI"],
            "summary": "ChatGPT 使用指南与常见场景示例。",
            "archived_at": "2026-01-04 11:55:00",
        },
        {
            "title": "语义搜索评测",
            "source_type": "text",
            "tags": ["搜索", "评测"],
            "keywords": ["语义搜索", "评测"],
            "summary": "语义搜索评测指标与评测集构建。",
            "archived_at": "2026-01-03 10:05:00",
        },
        {
            "title": "知识图谱构建",
            "source_type": "zhihu",
            "tags": ["知识图谱", "图谱"],
            "keywords": ["知识图谱", "构建"],
            "summary": "知识图谱构建流程与工程实践细节。",
            "archived_at": "2026-01-02 09:20:00",
        },
        {
            "title": "AI 对话记录 2026-02",
            "source_type": "ai_chat",
            "tags": ["AI", "对话"],
            "keywords": ["对话", "总结"],
            "summary": "AI 对话记录整理与关键结论汇总。",
            "archived_at": "2026-02-01 21:10:00",
        },
        {
            "title": "前端性能优化",
            "source_type": "generic",
            "tags": ["前端", "性能"],
            "keywords": ["前端", "性能"],
            "summary": "前端性能优化路线与实战 checklist。",
            "archived_at": "2025-12-28 10:00:00",
        },
        {
            "title": "Linux 网络排障",
            "source_type": "text",
            "tags": ["Linux", "网络"],
            "keywords": ["网络", "排障"],
            "summary": "Linux 网络排障工具与常用命令速查。",
            "archived_at": "2025-12-25 16:00:00",
        },
        {
            "title": "数据可视化最佳实践",
            "source_type": "generic",
            "tags": ["数据可视化", "设计"],
            "keywords": ["可视化", "设计"],
            "summary": "数据可视化最佳实践与可读性设计原则。",
            "archived_at": "2025-12-20 09:30:00",
        },
    ]

    entries = []
    for idx, item in enumerate(base, start=1):
        summary = item["summary"]
        content = f"# {item['title']}\n\n{summary}\n\n{summary}"
        entries.append(
            Entry(
                title=item["title"],
                source_type=item["source_type"],
                source_url=f"https://example.com/e2e/{idx}",
                tags=item["tags"],
                keywords=item["keywords"],
                abstract=summary,
                summary_one_sentence=summary,
                summary_100_words=summary,
                content=content,
                search_strategy="keyword",
                archived_at=item["archived_at"],
            )
        )
    return entries


@pytest.fixture(scope="session")
def test_env() -> TestEnv:
    data_dir = PROJECT_ROOT / ".data-test"
    db_path = data_dir / "db" / "knowledge_vault_e2e.db"
    vault_dir = data_dir / "vault-e2e"
    vector_dir = data_dir / "vectors-e2e"

    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "db").mkdir(parents=True, exist_ok=True)

    _clean_path(db_path)
    _clean_path(vault_dir)
    _clean_path(vector_dir)

    vault_dir.mkdir(parents=True, exist_ok=True)
    vector_dir.mkdir(parents=True, exist_ok=True)

    old_env = {key: os.environ.get(key) for key in ["DB_PATH", "DATA_DIR", "VECTOR_DIR", "VAULT_DIR", "LOG_LEVEL"]}
    os.environ["DB_PATH"] = str(db_path)
    os.environ["DATA_DIR"] = str(data_dir)
    os.environ["VECTOR_DIR"] = str(vector_dir)
    os.environ["VAULT_DIR"] = str(vault_dir)
    os.environ["LOG_LEVEL"] = "WARNING"
    os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")

    try:
        import src.utils.config as config_module
        config_module._config_instance = None
    except Exception:
        pass

    store = SQLiteStore(db_path)
    store.initialize()

    env = os.environ.copy()
    env.update(
        {
            "DB_PATH": str(db_path),
            "DATA_DIR": str(data_dir),
            "VECTOR_DIR": str(vector_dir),
            "VAULT_DIR": str(vault_dir),
            "LOG_LEVEL": "WARNING",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )

    yield TestEnv(
        data_dir=data_dir,
        db_path=db_path,
        vault_dir=vault_dir,
        vector_dir=vector_dir,
        env=env,
    )

    for key, value in old_env.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


@pytest.fixture
def mcp_server(test_env: TestEnv, sample_knowledge_db) -> MCPTestClient:
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "src.mcp.server"],
        env=test_env.env,
        cwd=str(PROJECT_ROOT),
    )
    return MCPTestClient(params=params)


@pytest.fixture(scope="session")
def sample_knowledge_db(test_env: TestEnv) -> Dict[str, object]:
    store = SQLiteStore(test_env.db_path)
    store.initialize()

    entries = _build_sample_entries()
    entry_ids = []

    for idx, entry in enumerate(entries, start=1):
        md_dir = test_env.vault_dir / entry.source_type
        md_dir.mkdir(parents=True, exist_ok=True)
        safe_title = entry.title.replace(" ", "_")
        md_path = md_dir / f"{idx:02d}-{safe_title}.md"
        _write_markdown(entry, md_path)
        entry_ids.append(store.insert_entry(entry, str(md_path)))

    vector_enabled = False
    vector_error = ""
    from src.utils.config import get_config

    if os.getenv("PKV_RUN_LIVE") == "1" and get_config().embd_api_key:
        try:
            from src.ai.embedder import Embedder
            from src.storage.vector_store import VectorStore

            config_vector_dir = get_config().vector_index_dir
            if Path(config_vector_dir).resolve() == test_env.vector_dir.resolve():
                embedder = Embedder()
                vector_store = VectorStore(test_env.vector_dir)
                for entry, kid in zip(entries, entry_ids):
                    vector = embedder.embed_document(entry.content or entry.summary_100_words)
                    vector_store.add_doc_vector(kid, vector)
                vector_enabled = True
            else:
                vector_error = (
                    "VECTOR_DIR 环境变量未生效，向量索引目录不一致，跳过向量构建"
                )
        except Exception as exc:
            vector_error = str(exc)

    return {
        "db_path": test_env.db_path,
        "vault_dir": test_env.vault_dir,
        "vector_dir": test_env.vector_dir,
        "entries": entries,
        "entry_ids": entry_ids,
        "vector_enabled": vector_enabled,
        "vector_error": vector_error,
    }
