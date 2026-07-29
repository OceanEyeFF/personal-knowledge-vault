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
from src.relations.citations import is_local_reference
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

CHUNK_FIXTURES = {
    "alpha_id": (
        101,
        0,
        "Alpha 通过 Gamma 的显式关系连接到 Delta，可用于回答证据链问题。",
    ),
    "beta_id": (
        201,
        0,
        "Beta 的独立片段，只用于验证 query 与 chunk 命中不可互换。",
    ),
    "gamma_id": (
        301,
        0,
        "Gamma 是 Alpha 与 Delta 之间的桥接节点。",
    ),
    "delta_id": (
        401,
        0,
        "Delta 是 Alpha 经由 Gamma 两跳关系路径的终点证据。",
    ),
}

LEGACY_TIMES = {
    "legacy_published_time_id": (
        "published_time",
        "2026-03-04 10:00:00",
    ),
    "legacy_publish_time_id": (
        "publish_time",
        "2026-03-05 10:00:00",
    ),
}


class OfflineQueryRouter:
    """Fixed retrieval results; never performs embedding or network access."""

    def __init__(self, aliases: dict[str, int], entries: dict[int, dict[str, Any]]) -> None:
        self.aliases = aliases
        self.entries = entries

    def search(self, query: str, limit: int = 10) -> list[SearchResult]:
        if query == "__contract_event_time__":
            keys = ["alpha_id"]
        elif query == "__contract_published_at__":
            keys = ["beta_id"]
        elif query == "__contract_published_time__":
            keys = ["legacy_published_time_id"]
        elif query == "__contract_publish_time__":
            keys = ["legacy_publish_time_id"]
        elif query == "__contract_archived_at__":
            keys = ["gamma_id"]
        elif query == "__contract_unavailable__":
            keys = ["no_time_id"]
        elif query in {
            "__contract_outside_entry__",
            "__contract_outside_chunk__",
        }:
            keys = ["outside_resource_id"]
        elif query == "Topic A":
            keys = ["alpha_id", "gamma_id"]
        elif query == "Topic B":
            keys = ["gamma_id", "delta_id"]
        elif query == "Alpha 时间线":
            keys = ["alpha_id", "beta_id"]
        elif "未连接" in query:
            keys = ["version_base_id"]
        else:
            keys = ["alpha_id", "delta_id", "gamma_id"]
        results = [
            self._result(self.aliases[key], score=max(0.60, 0.96 - index * 0.08))
            for index, key in enumerate(keys[:limit])
        ]
        for result in results:
            for alias, (field_name, value) in LEGACY_TIMES.items():
                if result.knowledge_id == self.aliases[alias]:
                    result.metadata[field_name] = value
        return results

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

        if query == "__contract_outside_chunk__":
            rows = [
                (
                    "outside_resource_id",
                    9901,
                    0,
                    "CHUNK_SECRET vault boundary bypass",
                    0.99,
                )
            ]
        elif "chunk-alpha-delta" in query:
            rows = [
                ("alpha_id", *CHUNK_FIXTURES["alpha_id"], 0.97),
                ("delta_id", *CHUNK_FIXTURES["delta_id"], 0.91),
                ("gamma_id", *CHUNK_FIXTURES["gamma_id"], 0.88),
            ]
        elif "chunk-beta-only" in query:
            rows = [
                ("beta_id", *CHUNK_FIXTURES["beta_id"], 0.95)
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
        self._patches.enter_context(
            patch("src.mcp.tools.get_sqlite_store", return_value=self.sqlite_store)
        )
        self._patches.enter_context(
            patch("src.mcp.tools.get_markdown_store", return_value=self.markdown_store)
        )
        self._patches.enter_context(
            patch("src.mcp.resources.get_sqlite_store", return_value=self.sqlite_store)
        )
        self._patches.enter_context(
            patch("src.mcp.resources.get_markdown_store", return_value=self.markdown_store)
        )
        self._patches.enter_context(
            patch(
                "src.mcp.resources.get_relation_query_service",
                return_value=self.query_service,
            )
        )
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self._patches.close()

    async def registered_tools(self) -> dict[str, Any]:
        tools = {tool.name: tool for tool in await mcp.list_tools()}
        await self._validate_timeline_field_preflight()
        await self._validate_local_source_preflight()
        await self._validate_resource_boundary_preflight()
        return tools

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        raw = await mcp.call_tool(tool_name, arguments)
        parsed = self._parse_tool_result(raw)
        await self._validate_public_tool_contract(tool_name, parsed)
        return parsed

    @staticmethod
    def _parse_tool_result(raw: Any) -> dict[str, Any]:
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, (list, tuple)) and raw:
            text = getattr(raw[0], "text", None)
            if text:
                parsed = json.loads(text)
                if isinstance(parsed, dict):
                    return parsed
        raise ValueError(f"无法解析 MCP Tool 返回值: {type(raw)}")

    async def read_resource(self, locator: str) -> Any:
        contents = list(await mcp.read_resource(locator))
        if not contents:
            raise ValueError(f"MCP Resource 返回空内容: {locator}")
        content = getattr(contents[0], "content", None)
        if content in (None, "", b""):
            raise ValueError(f"MCP Resource 内容为空: {locator}")
        if isinstance(content, bytes):
            content = content.decode("utf-8")
        if isinstance(content, str):
            stripped = content.strip()
            if not stripped:
                raise ValueError(f"MCP Resource 返回空内容: {locator}")
            if stripped.startswith(("# 错误", "# 未找到条目")) or any(
                marker in stripped
                for marker in ("(读取失败:", "(内容不可用)", "(文件路径缺失)")
            ):
                raise ValueError(f"MCP Resource 返回伪成功错误内容: {locator}")
            try:
                parsed = json.loads(content)
            except json.JSONDecodeError:
                return content
            if isinstance(parsed, dict) and parsed.get("error"):
                raise ValueError(f"MCP Resource 返回错误对象: {locator}")
            return parsed
        return content

    @staticmethod
    def _citation_locators(value: Any) -> list[str]:
        locators: list[str] = []
        if isinstance(value, dict):
            for key, item in value.items():
                if (
                    isinstance(item, str)
                    and (
                        key.endswith("_locator")
                        or item.startswith("pkv://")
                    )
                ):
                    locators.append(item)
                locators.extend(OfflineMcpScenario._citation_locators(item))
        elif isinstance(value, list):
            for item in value:
                locators.extend(OfflineMcpScenario._citation_locators(item))
        return locators

    @staticmethod
    def _assert_no_absolute_paths(value: Any, location: str = "$") -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"file_path", "source_file_path", "target_file_path"}:
                    raise AssertionError(f"{location}.{key} 泄漏本机路径字段")
                OfflineMcpScenario._assert_no_absolute_paths(
                    item,
                    f"{location}.{key}",
                )
        elif isinstance(value, list):
            for index, item in enumerate(value):
                OfflineMcpScenario._assert_no_absolute_paths(
                    item,
                    f"{location}[{index}]",
                )
        elif isinstance(value, str):
            if is_local_reference(value):
                raise AssertionError(f"{location} 泄漏本地引用: {value}")

    @staticmethod
    def _assert_source_fallbacks(value: Any, location: str = "$") -> None:
        if isinstance(value, dict):
            raw_id = value.get("knowledge_id")
            try:
                knowledge_id = int(raw_id)
            except (TypeError, ValueError):
                knowledge_id = 0
            if value.get("source_url") == "" and knowledge_id > 0:
                expected = f"pkv://entries/{knowledge_id}"
                for source_key in ("source", "citation_source"):
                    if source_key in value and value[source_key] != expected:
                        raise AssertionError(
                            f"{location}.{source_key} 未回退 entry Resource"
                        )
            for key, item in value.items():
                OfflineMcpScenario._assert_source_fallbacks(
                    item,
                    f"{location}.{key}",
                )
        elif isinstance(value, list):
            for index, item in enumerate(value):
                OfflineMcpScenario._assert_source_fallbacks(
                    item,
                    f"{location}[{index}]",
                )

    async def _validate_public_tool_contract(
        self,
        tool_name: str,
        payload: dict[str, Any],
    ) -> None:
        phase_b_tools = {
            "collect_evidence",
            "find_bridges",
            "timeline_of",
            "contrast",
        }
        if tool_name not in phase_b_tools:
            return

        self._assert_no_absolute_paths(payload)
        self._assert_source_fallbacks(payload)
        resource_payloads = {
            locator: await self.read_resource(locator)
            for locator in dict.fromkeys(self._citation_locators(payload))
        }
        for locator, resource_payload in resource_payloads.items():
            self._validate_resource_success(locator, resource_payload)
        if tool_name == "collect_evidence":
            self._validate_collect_contract(payload, resource_payloads)
        elif tool_name == "find_bridges":
            self._validate_bridge_contract(payload, resource_payloads)
        elif tool_name == "timeline_of":
            self._validate_timeline_contract(payload, resource_payloads)
        elif tool_name == "contrast":
            self._validate_contrast_contract(payload, resource_payloads)

    @staticmethod
    def _validate_resource_success(locator: str, payload: Any) -> None:
        """Validate resource identity and required fields, not merely non-empty data."""
        if locator.startswith("pkv://entries/"):
            suffix = locator.removeprefix("pkv://entries/")
            parts = suffix.split("/")
            try:
                knowledge_id = int(parts[0])
            except (TypeError, ValueError, IndexError):
                raise AssertionError(f"entry locator 无法解析: {locator}") from None
            if len(parts) == 1:
                if not isinstance(payload, str) or not payload.strip():
                    raise AssertionError(f"entry Resource 不是真实正文: {locator}")
                return
            if not isinstance(payload, dict):
                raise AssertionError(f"entry 子 Resource 非结构化成功: {locator}")
            if int(payload.get("knowledge_id", 0)) != knowledge_id:
                raise AssertionError(f"entry 子 Resource 端点不一致: {locator}")
            if parts[1] == "metadata":
                if len(parts) == 3:
                    if (
                        payload.get("field") != parts[2]
                        or payload.get("physical_source_field") != parts[2]
                        or payload.get("value") in (None, "")
                    ):
                        raise AssertionError(
                            f"metadata field Resource 与 locator 不一致: {locator}"
                        )
                return
            if parts[1] == "chunks":
                if (
                    int(payload.get("chunk_id", 0)) != int(parts[2])
                    or not str(payload.get("chunk_text", "")).strip()
                    or payload.get("citation_locator") != locator
                ):
                    raise AssertionError(f"chunk Resource 与 locator 不一致: {locator}")
                return
            if parts[1] == "chunk-index":
                if (
                    int(payload.get("chunk_index", -1)) != int(parts[2])
                    or not str(payload.get("chunk_text", "")).strip()
                    or payload.get("citation_locator") != locator
                ):
                    raise AssertionError(
                        f"chunk-index Resource 与 locator 不一致: {locator}"
                    )
                return
            raise AssertionError(f"未知 entry Resource 形态: {locator}")

        if locator.startswith("pkv://relations/"):
            if not isinstance(payload, dict):
                raise AssertionError(f"relation Resource 非结构化成功: {locator}")
            required = (
                "source_knowledge_id",
                "target_knowledge_id",
                "relation_type",
                "relation_source_type",
                "citation_locator",
            )
            if any(payload.get(field) in (None, "") for field in required):
                raise AssertionError(f"relation Resource 缺少必要字段: {locator}")
            if locator.startswith("pkv://relations/by-edge/"):
                parts = locator.removeprefix("pkv://relations/by-edge/").split("/")
                if (
                    len(parts) != 4
                    or int(payload["source_knowledge_id"]) != int(parts[0])
                    or int(payload["target_knowledge_id"]) != int(parts[1])
                    or payload["relation_type"] != parts[2]
                    or payload["relation_source_type"] != parts[3]
                    or not str(payload["citation_locator"]).startswith(
                        "pkv://relations/"
                    )
                ):
                    raise AssertionError(
                        f"by-edge Resource 与关系端点不一致: {locator}"
                    )
            elif payload.get("citation_locator") != locator:
                raise AssertionError(
                    f"relation Resource canonical locator 不一致: {locator}"
                )
            return

        raise AssertionError(f"公开引用不是受支持的 MCP Resource: {locator}")

    @staticmethod
    def _resource_for_locator(
        locator: str,
        resource_payloads: dict[str, Any],
    ) -> Any:
        if locator not in resource_payloads:
            raise AssertionError(f"Tool locator 未被读取: {locator}")
        return resource_payloads[locator]

    @classmethod
    def _assert_entry_locator_matches(
        cls,
        locator: str,
        knowledge_id: int,
        resource_payloads: dict[str, Any],
        *,
        suffix: str = "",
    ) -> None:
        expected = f"pkv://entries/{knowledge_id}{suffix}"
        if locator != expected:
            raise AssertionError(
                f"entry locator 未指向所在 Tool 条目: {locator} != {expected}"
            )
        resource = cls._resource_for_locator(locator, resource_payloads)
        if suffix and (
            not isinstance(resource, dict)
            or int(resource.get("knowledge_id", 0)) != knowledge_id
        ):
            raise AssertionError(f"entry Resource 与 Tool 条目不一致: {locator}")

    @classmethod
    def _assert_relation_locator_matches(
        cls,
        edge: dict[str, Any],
        resource_payloads: dict[str, Any],
    ) -> None:
        source_id = int(edge["source_knowledge_id"])
        target_id = int(edge["target_knowledge_id"])
        relation_type = str(edge["relation_type"])
        source_type = str(edge["relation_source_type"])
        relation_id = edge.get("relation_id")
        if relation_id is None:
            expected = (
                "pkv://relations/by-edge/"
                f"{source_id}/{target_id}/{relation_type}/{source_type}"
            )
        else:
            expected = f"pkv://relations/{int(relation_id)}"
        locator = str(edge.get("citation_locator") or "")
        if locator != expected:
            raise AssertionError(
                f"relation locator 未指向所在 Tool 关系边: {locator} != {expected}"
            )
        resource = cls._resource_for_locator(locator, resource_payloads)
        if not isinstance(resource, dict) or (
            int(resource.get("source_knowledge_id", 0)) != source_id
            or int(resource.get("target_knowledge_id", 0)) != target_id
            or resource.get("relation_type") != relation_type
            or resource.get("relation_source_type") != source_type
        ):
            raise AssertionError(f"relation Resource 与 Tool 关系边不一致: {locator}")
        if relation_id is not None and int(resource.get("relation_id", 0)) != int(
            relation_id
        ):
            raise AssertionError(f"relation_id 与 Tool 关系边不一致: {locator}")

    @classmethod
    def _validate_collect_contract(
        cls,
        payload: dict[str, Any],
        resource_payloads: dict[str, Any],
    ) -> None:
        for item in payload.get("evidence", []):
            knowledge_id = int(item["knowledge_id"])
            locator = str(item.get("citation_locator") or "")
            chunk_id = item.get("chunk_id")
            chunk_index = item.get("chunk_index")
            if chunk_id is not None:
                suffix = f"/chunks/{int(chunk_id)}"
            elif chunk_index is not None:
                suffix = f"/chunk-index/{int(chunk_index)}"
            else:
                cls._assert_entry_locator_matches(
                    locator,
                    knowledge_id,
                    resource_payloads,
                )
                continue

            cls._assert_entry_locator_matches(
                locator,
                knowledge_id,
                resource_payloads,
                suffix=suffix,
            )
            resource = cls._resource_for_locator(locator, resource_payloads)
            expected_chunk_field = "chunk_id" if chunk_id is not None else "chunk_index"
            expected_chunk_value = chunk_id if chunk_id is not None else chunk_index
            if (
                resource.get(expected_chunk_field) != expected_chunk_value
                or resource.get("chunk_text") != item.get("chunk_text")
            ):
                raise AssertionError(
                    f"chunk Resource 与 collect_evidence 条目不一致: {locator}"
                )

    @staticmethod
    def _validate_bridge_contract(
        payload: dict[str, Any],
        resource_payloads: dict[str, Any],
    ) -> None:
        seed_id = int(payload["seed_knowledge_id"])
        for candidate in payload.get("items", []):
            candidate_id = int(candidate["knowledge_id"])
            for edge in candidate.get("evidence_path", []):
                OfflineMcpScenario._assert_relation_locator_matches(
                    edge,
                    resource_payloads,
                )
            path_edges = [
                edge
                for edge in candidate.get("evidence_path", [])
                if "seed_path" in edge.get("evidence_roles", [])
            ]
            current_id = seed_id
            for hop_index, edge in enumerate(path_edges, start=1):
                if edge.get("hop_index") != hop_index:
                    raise AssertionError("bridge seed path hop_index 不连续")
                if edge.get("from_knowledge_id") != current_id:
                    raise AssertionError("bridge seed path 端点不连续")
                current_id = int(edge["to_knowledge_id"])
            if current_id != candidate_id:
                raise AssertionError("bridge seed path 未到达 candidate")

            adjacency_edges = [
                edge
                for edge in candidate.get("evidence_path", [])
                if "candidate_adjacency" in edge.get("evidence_roles", [])
            ]
            covered_neighbors: set[int] = set()
            for edge in adjacency_edges:
                source_id = int(edge["source_knowledge_id"])
                target_id = int(edge["target_knowledge_id"])
                if source_id == candidate_id:
                    covered_neighbors.add(target_id)
                elif target_id == candidate_id:
                    covered_neighbors.add(source_id)
            expected_neighbors = {
                int(item)
                for item in candidate.get("connected_knowledge_ids", [])
            }
            if covered_neighbors != expected_neighbors:
                raise AssertionError(
                    "bridge evidence_path 未覆盖全部 connected_knowledge_ids"
                )

            support = candidate.get("supporting_subgraph", {})
            if set(support.get("candidate_connected_knowledge_ids", [])) != expected_neighbors:
                raise AssertionError("bridge supporting_subgraph 邻接范围不一致")
            scoped_edges = support.get("edges", [])
            for edge in scoped_edges:
                OfflineMcpScenario._assert_relation_locator_matches(
                    edge,
                    resource_payloads,
                )
            edge_pairs = {
                frozenset(
                    (
                        int(edge["source_knowledge_id"]),
                        int(edge["target_knowledge_id"]),
                    )
                )
                for edge in scoped_edges
            }
            recomputed_disconnected = {
                frozenset((left_id, right_id))
                for left_id in expected_neighbors
                for right_id in expected_neighbors
                if left_id < right_id
                and frozenset((left_id, right_id)) not in edge_pairs
            }
            published_disconnected = {
                frozenset(
                    (
                        int(pair["left_knowledge_id"]),
                        int(pair["right_knowledge_id"]),
                    )
                )
                for pair in support.get("disconnected_neighbor_pairs", [])
            }
            if recomputed_disconnected != published_disconnected:
                raise AssertionError("bridge 断连邻居对无法从限定子图复算")
            semantic_inputs = support.get("semantic_score_inputs", {})
            if set(semantic_inputs.get("fields_used", [])) != {
                "title",
                "summary_one_sentence",
                "summary_100_words",
                "tags",
            }:
                raise AssertionError("bridge semantic score 未公开确定性输入字段")
            if round(
                float(semantic_inputs.get("semantic_score", -1)),
                4,
            ) != round(float(candidate["semantic_bridge_score"]), 4):
                raise AssertionError("bridge semantic score 无法从公开输入核对")
            if not semantic_inputs.get("candidate", {}).get("citation_locator"):
                raise AssertionError("bridge semantic candidate 缺少 entry locator")
            if not semantic_inputs.get("candidate", {}).get("metadata_locator"):
                raise AssertionError("bridge semantic candidate 缺少 metadata locator")
            semantic_candidate = semantic_inputs["candidate"]
            OfflineMcpScenario._assert_entry_locator_matches(
                semantic_candidate["citation_locator"],
                candidate_id,
                resource_payloads,
            )
            OfflineMcpScenario._assert_entry_locator_matches(
                semantic_candidate["metadata_locator"],
                candidate_id,
                resource_payloads,
                suffix="/metadata",
            )
            if not semantic_inputs.get("comparisons"):
                raise AssertionError("bridge semantic comparisons 缺少 provenance")
            if not all(
                item.get("citation_locator") and item.get("metadata_locator")
                for item in semantic_inputs["comparisons"]
            ):
                raise AssertionError(
                    "bridge semantic comparison 缺少 entry/metadata locator"
                )
            for comparison in semantic_inputs["comparisons"]:
                comparison_id = int(comparison["knowledge_id"])
                OfflineMcpScenario._assert_entry_locator_matches(
                    comparison["citation_locator"],
                    comparison_id,
                    resource_payloads,
                )
                OfflineMcpScenario._assert_entry_locator_matches(
                    comparison["metadata_locator"],
                    comparison_id,
                    resource_payloads,
                    suffix="/metadata",
                )

            structural_inputs = support.get("structural_score_inputs", {})
            candidate_depth = int(structural_inputs["candidate_depth"])
            neighbor_count = int(structural_inputs["neighbor_count"])
            max_depth = int(structural_inputs["max_depth"])
            depth_bonus = max(
                0.0,
                1 - ((candidate_depth - 1) / max(max_depth, 1)),
            )
            expected_structural = min(
                max(
                    0.7 * min(neighbor_count / 4.0, 1.0)
                    + 0.3 * depth_bonus,
                    0.0,
                ),
                1.0,
            )
            graph_inputs = support.get("graph_score_inputs", {})
            expected_graph = min(
                max(
                    float(graph_inputs["disconnected_pair_ratio"]) * 0.55
                    + min(
                        int(graph_inputs["depth_span"]) / max(max_depth, 1),
                        1.0,
                    )
                    * 0.25
                    + (1.0 if graph_inputs["seed_frontier"] else 0.0) * 0.2,
                    0.0,
                ),
                1.0,
            )
            if round(expected_structural, 4) != round(
                float(candidate["structural_bridge_score"]),
                4,
            ):
                raise AssertionError("bridge structural score 无法从公开输入复算")
            if round(expected_graph, 4) != round(
                float(candidate["graph_bridge_score"]),
                4,
            ):
                raise AssertionError("bridge graph score 无法从公开输入复算")
            expected_total = (
                expected_structural * 0.4
                + expected_graph * 0.4
                + float(semantic_inputs["semantic_score"]) * 0.2
            )
            if round(expected_total, 4) != round(
                float(candidate["bridge_score"]),
                4,
            ):
                raise AssertionError("bridge total score 无法从公开输入复算")

    @staticmethod
    def _validate_timeline_contract(
        payload: dict[str, Any],
        resource_payloads: dict[str, Any],
    ) -> None:
        unavailable_items = [
            item
            for item in payload.get("items", [])
            if item.get("time_source") == "unavailable"
            or item.get("time_precision") == "unavailable"
        ]
        if unavailable_items and not any(
            "不作为精确时间点" in note
            for note in payload.get("limitation_notes", [])
        ):
            raise AssertionError("timeline unavailable item 缺少公开 limitation")
        for item in payload.get("items", []):
            locator = item["citation_locator"]
            time_source = item.get("time_source")
            time_precision = item.get("time_precision")
            physical_field = item.get("time_source_field")
            if time_source == "unavailable" or time_precision == "unavailable":
                if time_source != "unavailable" or time_precision != "unavailable":
                    raise AssertionError("timeline unavailable 标记不一致")
                if item.get("time_value") or physical_field:
                    raise AssertionError("timeline unavailable item 伪装了精确时间字段")
                expected_locator = f"pkv://entries/{int(item['knowledge_id'])}"
                if locator != expected_locator:
                    raise AssertionError("timeline unavailable item 未回退 entry Resource")
                if not resource_payloads.get(locator):
                    raise AssertionError("timeline unavailable entry Resource 不可读")
                OfflineMcpScenario._assert_entry_locator_matches(
                    locator,
                    int(item["knowledge_id"]),
                    resource_payloads,
                )
                continue
            if time_precision != "structured_field":
                raise AssertionError("timeline 精确时间缺少 structured_field 标记")
            if not physical_field:
                raise AssertionError("timeline locator 未指向实际物理时间字段")
            OfflineMcpScenario._assert_entry_locator_matches(
                locator,
                int(item["knowledge_id"]),
                resource_payloads,
                suffix=f"/metadata/{physical_field}",
            )
            resource = resource_payloads[locator]
            if not isinstance(resource, dict):
                raise AssertionError("timeline metadata Resource 必须返回 JSON")
            if resource.get("field") != physical_field:
                raise AssertionError("timeline Resource 字段与 time_source_field 不一致")
            if str(resource.get("value") or "") != str(item.get("time_value") or ""):
                raise AssertionError("timeline Resource 时间值与 Tool 输出不一致")

    @classmethod
    def _assert_nested_entry_provenance_matches(
        cls,
        value: Any,
        resource_payloads: dict[str, Any],
    ) -> None:
        if isinstance(value, dict):
            knowledge_id = value.get("knowledge_id")
            locator = value.get("citation_locator")
            if (
                knowledge_id is not None
                and isinstance(locator, str)
                and locator.startswith("pkv://entries/")
            ):
                cls._assert_entry_locator_matches(
                    locator,
                    int(knowledge_id),
                    resource_payloads,
                )
            for nested in value.values():
                cls._assert_nested_entry_provenance_matches(
                    nested,
                    resource_payloads,
                )
        elif isinstance(value, list):
            for nested in value:
                cls._assert_nested_entry_provenance_matches(
                    nested,
                    resource_payloads,
                )

    @staticmethod
    def _validate_contrast_contract(
        payload: dict[str, Any],
        resource_payloads: dict[str, Any],
    ) -> None:
        dimensions = payload.get("comparison_dimensions", {})
        provenance = dimensions.get("provenance", {})
        for candidate in (
            payload.get("topic_a_candidates", [])
            + payload.get("topic_b_candidates", [])
        ):
            OfflineMcpScenario._assert_entry_locator_matches(
                candidate["citation_locator"],
                int(candidate["knowledge_id"]),
                resource_payloads,
            )
        OfflineMcpScenario._assert_nested_entry_provenance_matches(
            provenance,
            resource_payloads,
        )
        for dimension_name in ("shared_tags", "only_a_tags", "only_b_tags"):
            visible_values = payload.get(dimension_name, [])
            dimension_provenance = provenance.get(dimension_name, {})
            if set(dimension_provenance) != set(visible_values):
                raise AssertionError(
                    f"contrast {dimension_name} provenance 覆盖不完整"
                )
            for value in visible_values:
                mapped = dimension_provenance[value]
                candidate_lists = (
                    mapped.values()
                    if isinstance(mapped, dict)
                    else (mapped,)
                )
                for candidates in candidate_lists:
                    if not candidates:
                        raise AssertionError(
                            f"contrast {dimension_name}.{value} provenance 为空"
                        )
                    for candidate in candidates:
                        if not candidate.get("citation_locator") or not candidate.get(
                            "source"
                        ):
                            raise AssertionError(
                                "contrast candidate provenance 缺少可用来源"
                            )
        overlap = {
            str(knowledge_id)
            for knowledge_id in payload.get("overlap_knowledge_ids", [])
        }
        if set(provenance.get("overlap_knowledge_ids", {})) != overlap:
            raise AssertionError("contrast overlap provenance 覆盖不完整")
        for pair in provenance.get("relation_graph_signal", []):
            OfflineMcpScenario._assert_entry_locator_matches(
                pair["topic_a_citation_locator"],
                int(pair["topic_a_knowledge_id"]),
                resource_payloads,
            )
            OfflineMcpScenario._assert_entry_locator_matches(
                pair["topic_b_citation_locator"],
                int(pair["topic_b_knowledge_id"]),
                resource_payloads,
            )
            if not pair.get("evidence_path"):
                raise AssertionError("contrast relation signal 缺少 evidence_path")
            for edge in pair["evidence_path"]:
                OfflineMcpScenario._assert_relation_locator_matches(
                    edge,
                    resource_payloads,
                )

    async def _validate_timeline_field_preflight(self) -> None:
        expected = {
            "__contract_event_time__": "event_time",
            "__contract_published_at__": "published_at",
            "__contract_published_time__": "published_time",
            "__contract_publish_time__": "publish_time",
            "__contract_archived_at__": "archived_at",
        }
        for topic, physical_field in expected.items():
            result = await self.call_tool(
                "timeline_of",
                {"topic": topic, "top_k": 1, "sort_order": "asc"},
            )
            items = result.get("items", [])
            if len(items) != 1 or items[0].get("time_source_field") != physical_field:
                raise AssertionError(
                    f"timeline physical field preflight failed: {physical_field}"
                )
        unavailable = await self.call_tool(
            "timeline_of",
            {
                "topic": "__contract_unavailable__",
                "top_k": 1,
                "sort_order": "asc",
            },
        )
        items = unavailable.get("items", [])
        if len(items) != 1:
            raise AssertionError("timeline unavailable preflight 未返回唯一候选")
        item = items[0]
        expected_locator = f"pkv://entries/{self.aliases['no_time_id']}"
        if (
            item.get("time_source") != "unavailable"
            or item.get("time_precision") != "unavailable"
            or item.get("time_source_field")
            or item.get("time_value")
            or item.get("citation_locator") != expected_locator
            or unavailable.get("inferred_time_field") != "unavailable"
            or not any(
                "不作为精确时间点" in note
                for note in unavailable.get("limitation_notes", [])
            )
        ):
            raise AssertionError("timeline unavailable preflight 诚实降级失败")

    async def _validate_local_source_preflight(self) -> None:
        collect = await self.call_tool(
            "collect_evidence",
            {
                "question": "chunk-alpha-delta 本地 chat 来源安全检查",
                "top_k": 3,
                "relation_max_depth": 2,
                "include_chunks": True,
            },
        )
        timeline = await self.call_tool(
            "timeline_of",
            {
                "topic": "__contract_archived_at__",
                "top_k": 1,
                "sort_order": "asc",
            },
        )
        contrast = await self.call_tool(
            "contrast",
            {"topic_a": "Topic A", "topic_b": "Topic B", "top_k": 5},
        )
        for output in (collect, timeline, contrast):
            self._assert_no_absolute_paths(output)
            self._assert_source_fallbacks(output)
        for tool_name, output in (
            ("collect_evidence", collect),
            ("contrast", contrast),
        ):
            if "[redacted-local-reference]" not in json.dumps(
                output,
                ensure_ascii=False,
            ):
                raise AssertionError(
                    f"{tool_name} 未覆盖嵌套 relation evidence 本地引用负向 fixture"
                )

        for alias in ("gamma_id", "delta_id", "version_base_id", "no_time_id"):
            knowledge_id = self.aliases[alias]
            metadata = await self.read_resource(
                f"pkv://entries/{knowledge_id}/metadata"
            )
            self._assert_no_absolute_paths(metadata)
            if metadata.get("source_url") != "":
                raise AssertionError(f"{alias} metadata Resource 未清空本地 source_url")

        relation = next(
            (
                edge
                for edge in self.query_service.query_subgraph(
                    self.aliases["alpha_id"],
                    depth=2,
                ).edges
                if edge.relation_id is not None
            ),
            None,
        )
        if relation is None:
            raise AssertionError("local source preflight 缺少持久关系")
        resource = await self.read_resource(
            f"pkv://relations/{relation.relation_id}"
        )
        self._assert_no_absolute_paths(resource)

    async def _validate_resource_boundary_preflight(self) -> None:
        forbidden_paths = [
            self.resource_boundary_fixtures["outside_path"],
            self.resource_boundary_fixtures["missing_path"],
            self.vault_dir,
        ]
        for locator in self.resource_boundary_fixtures["rejected_locators"]:
            try:
                await self.read_resource(locator)
            except Exception as exc:
                message = str(exc)
                if any(str(path) in message for path in forbidden_paths):
                    raise AssertionError("Resource 错误泄漏本机路径") from None
            else:
                raise AssertionError(f"越界 Resource 被伪装为成功: {locator}")

        for knowledge_id in self.resource_boundary_fixtures["unsafe_entry_ids"]:
            result = await self.call_tool(
                "get_entry",
                {"knowledge_id": str(knowledge_id)},
            )
            self._assert_no_absolute_paths(result)
            if result.get("content") != "(内容不可用)":
                raise AssertionError("get_entry 读取了 vault 边界外正文")

        invalid_entry = await self.call_tool(
            "get_entry",
            {"knowledge_id": str(self.resource_boundary_fixtures["outside_path"])},
        )
        self._assert_no_absolute_paths(invalid_entry)
        if invalid_entry.get("error") != "无效的 knowledge_id，需要数字":
            raise AssertionError("get_entry invalid id 未受控拒绝")

        collected = await self.call_tool(
            "collect_evidence",
            {
                "question": "__contract_outside_entry__",
                "top_k": 1,
                "relation_max_depth": 1,
                "include_chunks": False,
            },
        )
        self._assert_no_absolute_paths(collected)
        if collected.get("found") or collected.get("evidence"):
            raise AssertionError("collect_evidence 暴露了 vault 边界外正文")
        if not any(
            "vault 文件边界校验" in note
            for note in collected.get("limitation_notes", [])
        ):
            raise AssertionError("collect_evidence 缺少 vault 边界降级说明")

        chunk_collected = await self.call_tool(
            "collect_evidence",
            {
                "question": "__contract_outside_chunk__",
                "top_k": 1,
                "relation_max_depth": 1,
                "include_chunks": True,
            },
        )
        self._assert_no_absolute_paths(chunk_collected)
        serialized_chunk_result = json.dumps(chunk_collected, ensure_ascii=False)
        if (
            chunk_collected.get("found")
            or chunk_collected.get("evidence")
            or "CHUNK_SECRET" in serialized_chunk_result
        ):
            raise AssertionError("collect_evidence 暴露了 vault 边界外 chunk")
        if not any(
            "chunk 检索候选未通过 vault 文件边界校验" in note
            for note in chunk_collected.get("limitation_notes", [])
        ):
            raise AssertionError("collect_evidence 缺少 chunk vault 边界降级说明")

        timeline = await self.call_tool(
            "timeline_of",
            {
                "topic": "__contract_outside_entry__",
                "top_k": 1,
                "sort_order": "asc",
            },
        )
        if timeline.get("found") or timeline.get("items"):
            raise AssertionError("timeline_of 返回了不可读取的 entry locator")
        if not any(
            "vault 文件边界校验" in note
            for note in timeline.get("limitation_notes", [])
        ):
            raise AssertionError("timeline_of 缺少 vault 边界降级说明")

        contrast = await self.call_tool(
            "contrast",
            {
                "topic_a": "__contract_outside_entry__",
                "topic_b": "__contract_outside_entry__",
                "top_k": 1,
            },
        )
        if (
            contrast.get("found")
            or contrast.get("topic_a_candidates")
            or contrast.get("topic_b_candidates")
        ):
            raise AssertionError("contrast 返回了不可读取的 entry locator")
        if not any(
            "vault 文件边界校验" in note
            for note in contrast.get("limitation_notes", [])
        ):
            raise AssertionError("contrast 缺少 vault 边界降级说明")

        alpha_id = self.aliases["alpha_id"]
        if not await self.read_resource(f"pkv://entries/{alpha_id}"):
            raise AssertionError("有效 vault 内 entry Resource 不可读")
        if not await self.read_resource(f"pkv://entries/{alpha_id}/chunks/101"):
            raise AssertionError("有效 chunk Resource 不可读")
        if not await self.read_resource(
            f"pkv://entries/{alpha_id}/metadata/event_time"
        ):
            raise AssertionError("有效 metadata field Resource 不可读")

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
                r"C:\Users\fixture\gamma-chat.html",
                source_type="ai_chat",
                archived_at="2026-03-12 09:00:00",
                tags="桥接,共同",
            ),
            "delta_id": self._insert_entry(
                files["delta_id"],
                "Delta",
                r"\\fixture-server\share\delta-chat.md",
                source_type="ai_chat",
                archived_at="2026-03-13 09:00:00",
                tags="终点,共同",
            ),
            "version_base_id": self._insert_entry(
                files["version_base_id"],
                "Version Base",
                "file://fixture-server/share/version-base-chat.md",
                source_type="ai_chat",
                archived_at="2026-03-16 09:00:00",
                tags="版本",
            ),
            "legacy_published_time_id": self._insert_entry(
                files["legacy_published_time_id"],
                "Legacy Published Time",
                "https://example.test/legacy-published-time",
                tags="旧字段",
            ),
            "legacy_publish_time_id": self._insert_entry(
                files["legacy_publish_time_id"],
                "Legacy Publish Time",
                "https://example.test/legacy-publish-time",
                tags="旧字段",
            ),
            "no_time_id": self._insert_entry(
                files["no_time_id"],
                "No Persistent Time",
                "file:///C:/Users/fixture/no-time-chat.md",
                source_type="ai_chat",
                tags="无时间,聊天",
            ),
        }
        self._insert_fixture_chunks()

        RelationBackfillService(
            db_path=self.db_path,
            vault_dir=self.vault_dir,
        ).backfill(apply=True)
        self._inject_local_relation_evidence()

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
            vault_dir=self.vault_dir,
        )
        self._build_resource_boundary_fixtures()

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
            "legacy_published_time_id": self.vault_dir / "legacy-published-time.md",
            "legacy_publish_time_id": self.vault_dir / "legacy-publish-time.md",
            "no_time_id": self.vault_dir / "no-time.md",
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
            "legacy_published_time_id": (
                "---\npublished_time: '2026-03-04 10:00:00'\n---\n"
                "# Legacy Published Time\n"
            ),
            "legacy_publish_time_id": (
                "---\npublish_time: '2026-03-05 10:00:00'\n---\n"
                "# Legacy Publish Time\n"
            ),
            "no_time_id": "# No Persistent Time\n\n没有持久时间字段的聊天记录。\n",
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
        source_type: str = "generic",
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
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    title,
                    source_type,
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

    def _inject_local_relation_evidence(self) -> None:
        connection = sqlite3.connect(str(self.db_path))
        try:
            rows = connection.execute(
                """
                SELECT relation_id, evidence_payload
                FROM knowledge_relations
                ORDER BY relation_id
                """
            ).fetchall()
            if not rows:
                raise AssertionError("离线评测未生成可注入安全 fixture 的关系")
            for relation_id, evidence_payload in rows:
                payload = json.loads(evidence_payload or "{}")
                payload["security_fixture"] = {
                    "source_url": r"C:\Users\fixture\relation-source.md",
                    "raw_target": r"\\fixture-server\share\relation-target.md",
                    "origin": "file:///C:/Users/fixture/relation-origin.md",
                    "nested": {
                        "raw_target": "file://fixture-server/share/nested-target.md",
                    },
                }
                connection.execute(
                    """
                    UPDATE knowledge_relations
                    SET evidence_payload = ?
                    WHERE relation_id = ?
                    """,
                    (json.dumps(payload, ensure_ascii=False), relation_id),
                )
            connection.commit()
        finally:
            connection.close()

    def _insert_fixture_chunks(self) -> None:
        connection = sqlite3.connect(str(self.db_path))
        try:
            for alias, (chunk_id, chunk_index, chunk_text) in CHUNK_FIXTURES.items():
                connection.execute(
                    """
                    INSERT INTO content_chunks (
                        chunk_id, knowledge_id, chunk_index, chunk_text
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        chunk_id,
                        self.aliases[alias],
                        chunk_index,
                        chunk_text,
                    ),
                )
            connection.commit()
        finally:
            connection.close()

    def _insert_path_only_entry(self, title: str, file_path: str) -> int:
        connection = sqlite3.connect(str(self.db_path))
        try:
            cursor = connection.execute(
                """
                INSERT INTO knowledge_items (
                    title, source_type, source_url, file_path, content,
                    summary_one_sentence, summary_100_words, tags, keywords
                ) VALUES (?, 'ai_chat', ?, ?, '# fixture', '', '', '', '')
                """,
                (title, f"https://fixture.test/{title}", file_path),
            )
            connection.commit()
            return int(cursor.lastrowid)
        finally:
            connection.close()

    def _build_resource_boundary_fixtures(self) -> None:
        outside_path = self.work_dir / "outside-resource.md"
        outside_path.write_text(
            "---\npublished_time: '2026-03-04 10:00:00'\n---\nsecret\n",
            encoding="utf-8",
        )
        missing_path = self.vault_dir / "missing-resource.md"
        outside_id = self._insert_path_only_entry("outside", str(outside_path))
        directory_id = self._insert_path_only_entry(
            "directory",
            str(self.vault_dir),
        )
        missing_id = self._insert_path_only_entry("missing", str(missing_path))
        unc_id = self._insert_path_only_entry(
            "unc",
            r"\\fixture-server\share\secret.md",
        )
        unsafe_chunk_ids = {}
        for offset, (label, knowledge_id) in enumerate(
            (
                ("outside", outside_id),
                ("directory", directory_id),
                ("missing", missing_id),
                ("unc", unc_id),
            ),
            start=1,
        ):
            chunk_id = 9900 + offset
            self._insert_unsafe_chunk(
                knowledge_id,
                chunk_id,
                f"CHUNK_SECRET {label}",
            )
            unsafe_chunk_ids[knowledge_id] = chunk_id
        rejected_locators = [
            f"pkv://entries/{outside_id}",
            f"pkv://entries/{outside_id}/metadata/published_time",
            f"pkv://entries/{directory_id}",
            f"pkv://entries/{missing_id}",
            f"pkv://entries/{unc_id}",
            "pkv://entries/999999",
            "pkv://entries/not-an-id",
            "pkv://entries/999999/chunks/999999",
            "pkv://entries/999999/chunk-index/0",
        ]
        for knowledge_id, chunk_id in unsafe_chunk_ids.items():
            rejected_locators.extend(
                [
                    f"pkv://entries/{knowledge_id}/chunks/{chunk_id}",
                    f"pkv://entries/{knowledge_id}/chunk-index/0",
                ]
            )

        symlink_path = self.vault_dir / "escape-resource.md"
        try:
            symlink_path.symlink_to(outside_path)
        except (OSError, NotImplementedError):
            symlink_path = None
        if symlink_path is not None:
            symlink_id = self._insert_path_only_entry(
                "symlink",
                str(symlink_path),
            )
            symlink_chunk_id = 9998
            self._insert_unsafe_chunk(
                symlink_id,
                symlink_chunk_id,
                "CHUNK_SECRET symlink",
            )
            rejected_locators.extend(
                [
                    f"pkv://entries/{symlink_id}",
                    f"pkv://entries/{symlink_id}/metadata/published_time",
                    f"pkv://entries/{symlink_id}/chunks/{symlink_chunk_id}",
                    f"pkv://entries/{symlink_id}/chunk-index/0",
                ]
            )

        self.resource_boundary_fixtures = {
            "outside_path": outside_path,
            "missing_path": missing_path,
            "rejected_locators": rejected_locators,
            "unsafe_entry_ids": [
                outside_id,
                directory_id,
                missing_id,
                unc_id,
            ],
        }
        if symlink_path is not None:
            self.resource_boundary_fixtures["unsafe_entry_ids"].append(
                symlink_id
            )
        self.aliases["outside_resource_id"] = outside_id
        outside_entry = self.sqlite_store.query_by_id(outside_id) or {}
        self.entries[outside_id] = outside_entry

    def _insert_unsafe_chunk(
        self,
        knowledge_id: int,
        chunk_id: int,
        chunk_text: str,
    ) -> None:
        connection = sqlite3.connect(str(self.db_path))
        try:
            connection.execute(
                """
                INSERT INTO content_chunks (
                    chunk_id, knowledge_id, chunk_index, chunk_text
                ) VALUES (?, ?, 0, ?)
                """,
                (chunk_id, knowledge_id, chunk_text),
            )
            connection.commit()
        finally:
            connection.close()
