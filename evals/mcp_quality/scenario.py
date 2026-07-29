"""Deterministic offline scenario used by the MCP quality evaluation."""

from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3
from typing import Any
from unittest.mock import patch

from src.mcp.server import mcp
from src.relations.evidence_service import EvidenceCollectionService
from src.relations.exploration_service import ExplorationService
from src.relations.extractors import RelationBackfillService
from src.relations.query_service import RelationQueryService
from src.retrieval.result import SearchResult
from src.storage.markdown_store import MarkdownStore
from src.storage.relation_store import RelationStore
from src.storage.sqlite_store import SQLiteStore

from .safety import PROJECT_ROOT, reject_production_path

BASE_SQL = PROJECT_ROOT / "scripts/migrations/001_initial_schema.sql"
RELATION_SQL = PROJECT_ROOT / "scripts/migrations/006_add_relations_foundation.sql"


class OfflineQueryRouter:
    """Fixed retrieval results; never performs embedding or network access."""

    def __init__(self, aliases: dict[str, int], entries: dict[int, dict[str, Any]]) -> None:
        self.aliases = aliases
        self.entries = entries

    def search(self, query: str, limit: int = 10) -> list[SearchResult]:
        if query == "Topic A":
            keys = ["alpha_id", "gamma_id"]
        elif query == "Topic B":
            keys = ["gamma_id", "delta_id"]
        elif query == "Alpha 时间线":
            keys = ["alpha_id", "beta_id"]
        elif "未连接" in query:
            keys = ["version_base_id"]
        else:
            keys = ["alpha_id", "delta_id", "gamma_id"]
        return [
            self._result(self.aliases[key], score=max(0.60, 0.96 - index * 0.08))
            for index, key in enumerate(keys[:limit])
        ]

    def _result(self, knowledge_id: int, score: float) -> SearchResult:
        entry = self.entries[knowledge_id]
        return SearchResult(
            knowledge_id=knowledge_id,
            title=str(entry["title"]),
            score=score,
            highlight=str(entry["summary_one_sentence"]),
            metadata={
                "source_type": entry["source_type"],
                "source_url": entry["source_url"],
                "file_path": entry["file_path"],
                "archived_at": entry["archived_at"],
                "tags": entry["tags"],
            },
        )


class OfflineChunkSearcher:
    """Fixed chunk search with explicit no-hit and failure modes."""

    def __init__(self, aliases: dict[str, int], entries: dict[int, dict[str, Any]]) -> None:
        self.aliases = aliases
        self.entries = entries

    def search_chunks(self, query: str, limit: int = 10) -> list[SearchResult]:
        if "chunk-search-error" in query:
            raise RuntimeError("offline injected chunk failure")
        if "chunk-no-hits" in query:
            return []

        if "chunk-alpha-delta" in query:
            rows = [
                (
                    "alpha_id",
                    101,
                    0,
                    "Alpha 通过 Gamma 的显式关系连接到 Delta，可用于回答证据链问题。",
                    0.97,
                ),
                (
                    "delta_id",
                    401,
                    0,
                    "Delta 是 Alpha 经由 Gamma 两跳关系路径的终点证据。",
                    0.91,
                ),
                (
                    "gamma_id",
                    301,
                    0,
                    "Gamma 是 Alpha 与 Delta 之间的桥接节点。",
                    0.88,
                ),
            ]
        elif "chunk-beta-only" in query:
            rows = [
                (
                    "beta_id",
                    201,
                    0,
                    "Beta 的独立片段，只用于验证 query 与 chunk 命中不可互换。",
                    0.95,
                )
            ]
        else:
            return []

        results: list[SearchResult] = []
        for key, chunk_id, chunk_index, chunk_text, score in rows[:limit]:
            knowledge_id = self.aliases[key]
            entry = self.entries[knowledge_id]
            results.append(
                SearchResult(
                    knowledge_id=knowledge_id,
                    title=str(entry["title"]),
                    score=score,
                    highlight=chunk_text,
                    metadata={
                        "chunk_id": chunk_id,
                        "chunk_index": chunk_index,
                        "chunk_text": chunk_text,
                        "source_type": entry["source_type"],
                        "source_url": entry["source_url"],
                        "file_path": entry["file_path"],
                        "archived_at": entry["archived_at"],
                        "tags": entry["tags"],
                    },
                )
            )
        return results


class EvidenceFacade:
    """Select the success or degraded evidence path from the fixed question."""

    def __init__(
        self,
        normal: EvidenceCollectionService,
        path_unavailable: EvidenceCollectionService,
    ) -> None:
        self.normal = normal
        self.path_unavailable = path_unavailable

    def collect_evidence(self, question: str, **kwargs: Any):
        service = (
            self.path_unavailable
            if "chunk-path-unavailable" in question
            else self.normal
        )
        return service.collect_evidence(question=question, **kwargs)


@dataclass
class OfflineMcpScenario:
    """Owns isolated fixture data and patches MCP dependency accessors."""

    work_dir: Path

    def __post_init__(self) -> None:
        self.work_dir = reject_production_path(
            self.work_dir,
            purpose="离线 MCP 场景工作目录",
        )
        self.db_path = self.work_dir / "db" / "quality-eval.db"
        self.vault_dir = self.work_dir / "vault"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.vault_dir.mkdir(parents=True, exist_ok=True)
        self.aliases: dict[str, int] = {}
        self._patches = ExitStack()
        self._build()

    def __enter__(self) -> "OfflineMcpScenario":
        self._patches.enter_context(
            patch("src.mcp.tools.get_relation_query_service", return_value=self.query_service)
        )
        self._patches.enter_context(
            patch("src.mcp.tools.get_evidence_collection_service", return_value=self.evidence_service)
        )
        self._patches.enter_context(
            patch("src.mcp.tools.get_exploration_service", return_value=self.exploration_service)
        )
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self._patches.close()

    async def registered_tools(self) -> dict[str, Any]:
        return {tool.name: tool for tool in await mcp.list_tools()}

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        raw = await mcp.call_tool(tool_name, arguments)
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, (list, tuple)) and raw:
            text = getattr(raw[0], "text", None)
            if text:
                parsed = json.loads(text)
                if isinstance(parsed, dict):
                    return parsed
        raise ValueError(f"无法解析 MCP Tool 返回值: {type(raw)}")

    def resolve_aliases(self, value: Any) -> Any:
        if isinstance(value, str) and value.startswith("$"):
            alias = value[1:]
            if alias not in self.aliases:
                raise KeyError(f"unknown scenario alias: {value}")
            return self.aliases[alias]
        if isinstance(value, list):
            return [self.resolve_aliases(item) for item in value]
        if isinstance(value, dict):
            return {key: self.resolve_aliases(item) for key, item in value.items()}
        return value

    def _build(self) -> None:
        self._apply_sql(BASE_SQL)
        self._apply_sql(RELATION_SQL)
        files = self._write_vault_files()

        self.aliases = {
            "alpha_id": self._insert_entry(
                files["alpha_id"],
                "Alpha",
                "https://example.test/alpha",
                event_time="2026-03-01 08:00:00",
                archived_at="2026-03-10 09:00:00",
                tags="测试,共同",
            ),
            "beta_id": self._insert_entry(
                files["beta_id"],
                "Beta",
                "https://example.test/beta",
                published_at="2026-03-02 09:00:00",
                archived_at="2026-03-11 09:00:00",
                tags="测试",
            ),
            "gamma_id": self._insert_entry(
                files["gamma_id"],
                "Gamma",
                "https://example.test/gamma",
                archived_at="2026-03-12 09:00:00",
                tags="桥接,共同",
            ),
            "delta_id": self._insert_entry(
                files["delta_id"],
                "Delta",
                "https://example.test/delta",
                archived_at="2026-03-13 09:00:00",
                tags="终点,共同",
            ),
            "version_base_id": self._insert_entry(
                files["version_base_id"],
                "Version Base",
                "https://example.test/version-base",
                archived_at="2026-03-16 09:00:00",
                tags="版本",
            ),
        }

        RelationBackfillService(
            db_path=self.db_path,
            vault_dir=self.vault_dir,
        ).backfill(apply=True)

        self.sqlite_store = SQLiteStore(self.db_path)
        self.markdown_store = MarkdownStore(self.vault_dir)
        self.relation_store = RelationStore(self.db_path)
        self.query_service = RelationQueryService(self.relation_store)
        self.entries = {
            knowledge_id: self.sqlite_store.query_by_id(knowledge_id) or {}
            for knowledge_id in self.aliases.values()
        }
        self.query_router = OfflineQueryRouter(self.aliases, self.entries)
        self.chunk_searcher = OfflineChunkSearcher(self.aliases, self.entries)
        normal_evidence = EvidenceCollectionService(
            query_router=self.query_router,
            sqlite_store=self.sqlite_store,
            markdown_store=self.markdown_store,
            relation_query_service=self.query_service,
            chunk_searcher=self.chunk_searcher,
        )
        unavailable_evidence = EvidenceCollectionService(
            query_router=self.query_router,
            sqlite_store=self.sqlite_store,
            markdown_store=self.markdown_store,
            relation_query_service=self.query_service,
            chunk_searcher=None,
        )
        self.evidence_service = EvidenceFacade(normal_evidence, unavailable_evidence)
        self.exploration_service = ExplorationService(
            query_router=self.query_router,
            sqlite_store=self.sqlite_store,
            relation_query_service=self.query_service,
        )

    def _apply_sql(self, path: Path) -> None:
        connection = sqlite3.connect(str(self.db_path))
        try:
            connection.executescript(path.read_text(encoding="utf-8"))
        finally:
            connection.close()

    def _write_vault_files(self) -> dict[str, Path]:
        paths = {
            "alpha_id": self.vault_dir / "alpha.md",
            "beta_id": self.vault_dir / "beta.md",
            "gamma_id": self.vault_dir / "gamma.md",
            "delta_id": self.vault_dir / "delta.md",
            "version_base_id": self.vault_dir / "version-base.md",
        }
        contents = {
            "alpha_id": (
                "---\ntitle: Alpha\nrelated_docs:\n  - gamma.md\n---\n"
                "# Alpha\n\n请参考 [Beta](./beta.md)。\n"
            ),
            "beta_id": "# Beta\n\n回链到 [Alpha](./alpha.md)。\n",
            "gamma_id": "# Gamma\n\n继续参考 [Delta](./delta.md)。\n",
            "delta_id": "# Delta\n\nAlpha 经由 Gamma 两跳关系到达的终点证据。\n",
            "version_base_id": "# Version Base\n\n与 Alpha 图不连通的版本文档。\n",
        }
        for key, path in paths.items():
            path.write_text(contents[key], encoding="utf-8")
        return paths

    def _insert_entry(
        self,
        file_path: Path,
        title: str,
        source_url: str,
        *,
        event_time: str = "",
        published_at: str = "",
        archived_at: str = "",
        tags: str = "",
    ) -> int:
        connection = sqlite3.connect(str(self.db_path))
        try:
            cursor = connection.execute(
                """
                INSERT INTO knowledge_items (
                    title, source_type, source_url, file_path, content,
                    summary_one_sentence, summary_100_words, tags, keywords,
                    event_time, published_at, archived_at
                ) VALUES (?, 'generic', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    title,
                    source_url,
                    str(file_path),
                    file_path.read_text(encoding="utf-8"),
                    f"{title} 摘要",
                    f"{title} 详细摘要",
                    tags,
                    "关系,证据",
                    event_time or None,
                    published_at or None,
                    archived_at or None,
                ),
            )
            connection.commit()
            return int(cursor.lastrowid)
        finally:
            connection.close()
