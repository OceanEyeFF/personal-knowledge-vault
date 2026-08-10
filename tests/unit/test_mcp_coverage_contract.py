"""Offline coverage contracts for MCP boundary and degradation paths."""

import json
import logging
import runpy
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from src.mcp import resources, server, tools, utils
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
from src.relations.citations import build_entry_locator
from src.retrieval.result import RetrievalIssue, SearchResponse, SearchResult
from src.runtime.errors import ErrorCode
from src.runtime.errors import PKVRuntimeError
from src.workflow.models import WorkflowResult


def _parse_fastmcp_tool_result(result):
    if isinstance(result, dict):
        return result
    if not isinstance(result, (list, tuple)) or len(result) != 1:
        raise AssertionError(f"unexpected FastMCP result shape: {type(result).__name__}")
    text = getattr(result[0], "text", None)
    if type(text) is not str:
        raise AssertionError("FastMCP result did not contain JSON text")
    payload = json.loads(text)
    if type(payload) is not dict:
        raise AssertionError("FastMCP result was not a JSON object")
    return payload


def _strict_entry_row(knowledge_id: int, title: str) -> dict[str, object]:
    return {
        "knowledge_id": knowledge_id,
        "title": title,
        "summary_one_sentence": "",
        "summary_100_words": "",
        "tags": "offline",
        "keywords": "offline",
        "source_type": "text",
        "source_url": "",
        "archived_at": "2026-08-07",
        "word_count": 1,
        "file_path": "offline.md",
    }


@pytest.fixture
def reset_server_singletons():
    """Keep lazy-service tests independent from the process-wide cache."""
    names = (
        "_relation_query_service",
        "_evidence_collection_service",
        "_exploration_service",
    )
    previous = {name: getattr(server, name) for name in names}
    for name in names:
        setattr(server, name, None)
    try:
        yield
    finally:
        for name, value in previous.items():
            setattr(server, name, value)


@pytest.fixture
def preserve_root_logger():
    """Restore handlers cleared by the server CLI setup."""
    root_logger = logging.getLogger()
    previous_handlers = list(root_logger.handlers)
    previous_level = root_logger.level
    try:
        yield root_logger
    finally:
        for handler in root_logger.handlers:
            if handler not in previous_handlers:
                handler.close()
        root_logger.handlers[:] = previous_handlers
        root_logger.setLevel(previous_level)


def test_relation_query_service_is_built_once(reset_server_singletons):
    config = SimpleNamespace(db_path=Path("isolated") / "knowledge.db")
    relation_store = object()
    relation_service = object()

    with (
        patch.object(server, "get_config", return_value=config),
        patch(
            "src.storage.relation_store.RelationStore",
            return_value=relation_store,
        ) as store_type,
        patch(
            "src.relations.query_service.RelationQueryService",
            return_value=relation_service,
        ) as service_type,
    ):
        assert server.get_relation_query_service() is relation_service
        assert server.get_relation_query_service() is relation_service

    store_type.assert_called_once_with(config.db_path)
    service_type.assert_called_once_with(relation_store)


def test_evidence_service_wires_lazy_dependencies_once(
    reset_server_singletons,
):
    dependencies = {
        "query_router": object(),
        "sqlite_store": object(),
        "markdown_store": object(),
        "relation_query_service": object(),
    }
    evidence_service = object()

    with (
        patch.object(
            server,
            "get_query_router",
            return_value=dependencies["query_router"],
        ),
        patch.object(
            server,
            "get_sqlite_store",
            return_value=dependencies["sqlite_store"],
        ),
        patch.object(
            server,
            "get_markdown_store",
            return_value=dependencies["markdown_store"],
        ),
        patch.object(
            server,
            "get_relation_query_service",
            return_value=dependencies["relation_query_service"],
        ),
        patch(
            "src.relations.evidence_service.EvidenceCollectionService",
            return_value=evidence_service,
        ) as service_type,
    ):
        assert server.get_evidence_collection_service() is evidence_service
        assert server.get_evidence_collection_service() is evidence_service

    service_type.assert_called_once_with(**dependencies)


def test_exploration_service_wires_lazy_dependencies_once(
    reset_server_singletons,
):
    dependencies = {
        "query_router": object(),
        "sqlite_store": object(),
        "relation_query_service": object(),
        "vault_dir": Path(".data-test") / "coverage-contract-vault",
    }
    markdown_store = SimpleNamespace(vault_dir=dependencies["vault_dir"])
    exploration_service = object()

    with (
        patch.object(
            server,
            "get_query_router",
            return_value=dependencies["query_router"],
        ),
        patch.object(
            server,
            "get_sqlite_store",
            return_value=dependencies["sqlite_store"],
        ),
        patch.object(
            server,
            "get_relation_query_service",
            return_value=dependencies["relation_query_service"],
        ),
        patch.object(
            server,
            "get_markdown_store",
            return_value=markdown_store,
        ),
        patch(
            "src.relations.exploration_service.ExplorationService",
            return_value=exploration_service,
        ) as service_type,
    ):
        assert server.get_exploration_service() is exploration_service
        assert server.get_exploration_service() is exploration_service

    service_type.assert_called_once_with(**dependencies)


def test_server_main_rejects_unpublished_http_before_bootstrap(capsys):
    with (
        patch.object(
            sys,
            "argv",
            [
                "pkv-mcp",
                "--transport",
                "streamable-http",
                "--port",
                "4321",
            ],
        ),
        patch.object(server, "get_config") as get_config,
        patch.object(server, "bootstrap_runtime") as bootstrap,
        patch.object(server.mcp, "run") as run_server,
    ):
        with pytest.raises(SystemExit) as exc_info:
            server.main()

    assert exc_info.value.code == 2
    assert "transport_unsupported" in capsys.readouterr().err
    get_config.assert_not_called()
    bootstrap.assert_not_called()
    run_server.assert_not_called()


def test_server_main_degrades_when_file_logging_cannot_start(
    tmp_path,
    preserve_root_logger,
    capsys,
):
    secret = "pkv-log-secret"
    config = MagicMock()
    config.log_level = "INFO"
    config.log_dir = tmp_path / "logs"
    config.get.side_effect = lambda _key, default=None: default

    with (
        patch.object(sys, "argv", ["pkv-mcp", "--log-level", "WARNING"]),
        patch.object(server, "get_config", return_value=config),
        patch.object(server, "bootstrap_runtime") as bootstrap,
        patch(
            "src.utils.logger.LoggerSetup.add_file_handler",
            side_effect=OSError(f"{secret}: {tmp_path}"),
        ),
        patch.object(server.mcp, "run") as run_server,
    ):
        server.main()

    bootstrap.assert_called_once_with(config)
    run_server.assert_called_once_with(transport="stdio")
    stderr = capsys.readouterr().err
    assert "cause_type=OSError" in stderr
    assert secret not in stderr
    assert str(tmp_path) not in stderr


def test_server_main_registers_validated_file_logger(
    tmp_path,
    preserve_root_logger,
):
    """MCP 文件日志通过统一可写叶子合同注册（validator 来自 runtime layout）。"""
    config = MagicMock()
    config.log_level = "INFO"
    config.log_dir = tmp_path / "logs"
    config.get.side_effect = lambda _key, default=None: default

    with (
        patch.object(sys, "argv", ["pkv-mcp", "--log-level", "ERROR"]),
        patch.object(server, "get_config", return_value=config),
        patch.object(server, "bootstrap_runtime"),
        patch(
            "src.utils.logger.LoggerSetup.add_file_handler"
        ) as add_file_handler,
        patch.object(server.mcp, "run"),
    ):
        server.main()

    add_file_handler.assert_called_once()
    assert add_file_handler.call_args.args[0] == config.log_dir / "pkv.log"
    assert (
        add_file_handler.call_args.kwargs["path_validator"]
        is config.layout.writable_user_path
    )
    assert add_file_handler.call_args.kwargs["level"] == logging.ERROR


@pytest.mark.parametrize(
    ("strategy", "retriever_path"),
    [
        ("vector", "src.retrieval.vector_retriever.VectorRetriever"),
        ("hybrid", "src.retrieval.hybrid_retriever.HybridRetriever"),
    ],
)
@pytest.mark.asyncio
async def test_search_semantic_strategies_are_constructed_offline(
    strategy,
    retriever_path,
):
    config = SimpleNamespace(
        db_path=Path("isolated") / "knowledge.db",
        vector_index_dir=Path("isolated") / "vectors",
    )
    embedder = object()
    retriever = MagicMock()
    retriever.search.return_value = SearchResponse.completed((), strategy=strategy)

    with (
        patch.object(tools, "get_config", return_value=config),
        patch(
            "src.ai.provider_factory.create_embedder",
            return_value=embedder,
        ) as embedder_factory,
        patch(retriever_path, return_value=retriever) as retriever_type,
    ):
        result = await tools.search_knowledge(
            query="offline",
            strategy=strategy,
            top_k=3,
        )

    assert result == {
        "status": "no_hits",
        "strategy": strategy,
        "total": 0,
        "results": [],
        "issues": [],
    }
    retriever_type.assert_called_once_with(
        config.db_path,
        config.vector_index_dir,
        None,
        embedder_factory=retriever_type.call_args.kwargs["embedder_factory"],
    )
    embedder_factory.assert_not_called()
    assert retriever_type.call_args.kwargs["embedder_factory"]() is embedder
    embedder_factory.assert_called_once_with(config)
    retriever.search.assert_called_once_with("offline", limit=3)


@pytest.mark.asyncio
async def test_get_entry_degrades_on_unexpected_markdown_error(tmp_path: Path):
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    entry_path = vault_dir / "offline.md"
    entry_path.write_text("# Offline", encoding="utf-8")
    sqlite_store = MagicMock()
    sqlite_store.query_by_id.return_value = {
        "knowledge_id": 1,
        "title": "Offline entry",
        "summary_one_sentence": None,
        "summary_100_words": None,
        "tags": None,
        "keywords": None,
        "source_type": "text",
        "source_url": None,
        "archived_at": "2026-08-07",
        "word_count": 1,
        "file_path": str(entry_path),
    }
    markdown_store = MagicMock()
    markdown_store.vault_dir = vault_dir
    markdown_store.load.side_effect = RuntimeError("decode failed")

    with (
        patch.object(
            tools,
            "get_sqlite_store",
            return_value=sqlite_store,
        ),
        patch.object(
            tools,
            "get_markdown_store",
            return_value=markdown_store,
        ),
    ):
        result = await tools.get_entry("1")

    assert result["content"] == "(内容不可用)"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "get_entry_empty",
        "get_entry_dict_subclass",
        "get_entry_wrong_id",
        "list_tags_empty_string",
        "list_tags_list_subclass",
        "list_tags_boolean_count",
        "list_entries_empty_string",
        "list_entries_boolean_total",
        "list_entries_boolean_id",
        "get_stats_empty",
        "get_stats_dict_subclass",
        "get_stats_bad_source_count",
        "get_stats_duplicate_source",
        "get_stats_source_sum_mismatch",
        "get_stats_duplicate_tag",
        "get_stats_unsorted_tags",
        "get_stats_too_many_tags",
        "get_stats_tag_count_exceeds_total",
    ],
)
async def test_read_projection_malformed_backend_is_stable_direct_and_fastmcp(case):
    secret = "api_key_READ_PROJECTION_CANARY"

    class DictSubclass(dict):
        pass

    class ListSubclass(list):
        pass

    def entry_row(**updates):
        row = {
            "knowledge_id": 1,
            "title": "safe",
            "summary_one_sentence": None,
            "summary_100_words": None,
            "tags": None,
            "keywords": None,
            "source_type": "text",
            "source_url": None,
            "archived_at": "2026-08-07",
            "word_count": 1,
            "file_path": "",
        }
        row.update(updates)
        return row

    store = MagicMock()
    if case == "get_entry_empty":
        tool_name, arguments = "get_entry", {"knowledge_id": "1"}
        store.query_by_id.return_value = {}
    elif case == "get_entry_dict_subclass":
        tool_name, arguments = "get_entry", {"knowledge_id": "1"}
        store.query_by_id.return_value = DictSubclass(entry_row(title=secret))
    elif case == "get_entry_wrong_id":
        tool_name, arguments = "get_entry", {"knowledge_id": "1"}
        store.query_by_id.return_value = entry_row(knowledge_id=2, title=secret)
    elif case == "list_tags_empty_string":
        tool_name, arguments = "list_tags", {}
        store.get_all_tags_with_count.return_value = ""
    elif case == "list_tags_list_subclass":
        tool_name, arguments = "list_tags", {}
        store.get_all_tags_with_count.return_value = ListSubclass(
            [{"name": secret, "count": 1}]
        )
    elif case == "list_tags_boolean_count":
        tool_name, arguments = "list_tags", {}
        store.get_all_tags_with_count.return_value = [
            {"name": secret, "count": True}
        ]
    elif case == "list_entries_empty_string":
        tool_name, arguments = "list_entries", {}
        store.list_entries.return_value = ""
        store.count_entries.return_value = 0
    elif case == "list_entries_boolean_total":
        tool_name, arguments = "list_entries", {}
        store.list_entries.return_value = []
        store.count_entries.return_value = True
    elif case == "list_entries_boolean_id":
        tool_name, arguments = "list_entries", {}
        store.list_entries.return_value = [entry_row(knowledge_id=True, title=secret)]
        store.count_entries.return_value = 1
    elif case == "get_stats_empty":
        tool_name, arguments = "get_stats", {}
        store.get_statistics.return_value = {}
    elif case == "get_stats_dict_subclass":
        tool_name, arguments = "get_stats", {}
        store.get_statistics.return_value = DictSubclass(
            total_entries=0,
            by_source_type=[],
            top_tags=[{"name": secret, "count": 0}],
        )
    elif case == "get_stats_bad_source_count":
        tool_name, arguments = "get_stats", {}
        store.get_statistics.return_value = {
            "total_entries": 1,
            "by_source_type": [(secret, True)],
            "top_tags": [],
        }
    elif case == "get_stats_duplicate_source":
        tool_name, arguments = "get_stats", {}
        store.get_statistics.return_value = {
            "total_entries": 2,
            "by_source_type": [("text", 1), ("text", 1)],
            "top_tags": [],
        }
    elif case == "get_stats_source_sum_mismatch":
        tool_name, arguments = "get_stats", {}
        store.get_statistics.return_value = {
            "total_entries": 2,
            "by_source_type": [("text", 1)],
            "top_tags": [],
        }
    elif case == "get_stats_duplicate_tag":
        tool_name, arguments = "get_stats", {}
        store.get_statistics.return_value = {
            "total_entries": 2,
            "by_source_type": [("text", 2)],
            "top_tags": [
                {"name": "AI", "count": 1},
                {"name": "AI", "count": 1},
            ],
        }
    elif case == "get_stats_unsorted_tags":
        tool_name, arguments = "get_stats", {}
        store.get_statistics.return_value = {
            "total_entries": 2,
            "by_source_type": [("text", 2)],
            "top_tags": [
                {"name": "low", "count": 1},
                {"name": "high", "count": 2},
            ],
        }
    elif case == "get_stats_too_many_tags":
        tool_name, arguments = "get_stats", {}
        store.get_statistics.return_value = {
            "total_entries": 21,
            "by_source_type": [("text", 21)],
            "top_tags": [
                {"name": f"tag-{index}", "count": 1}
                for index in range(21)
            ],
        }
    else:
        tool_name, arguments = "get_stats", {}
        store.get_statistics.return_value = {
            "total_entries": 1,
            "by_source_type": [("text", 1)],
            "top_tags": [{"name": "AI", "count": 2}],
        }

    with patch.object(tools, "get_sqlite_store", return_value=store):
        direct = await getattr(tools, tool_name)(**arguments)
        fastmcp_raw = await server.mcp.call_tool(tool_name, arguments)

    fastmcp = _parse_fastmcp_tool_result(fastmcp_raw)
    for payload in (direct, fastmcp):
        assert payload["status"] == "error"
        assert payload["status"] not in {"success", "no_hits"}
        assert payload["issues"][0]["code"] == ErrorCode.RETRIEVAL_BACKEND_FAILED.value
        assert secret not in repr(payload)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "success_without_results",
        "no_hits_with_result",
        "invalid_with_wrong_issue",
        "error_with_result",
        "degraded_without_issue",
        "results_not_tuple",
        "strategy_not_public",
        "auto_wrong_label",
        "explicit_wrong_label",
        "corrupt_result_id",
        "corrupt_metadata_tags",
    ],
)
async def test_search_corrupt_frozen_response_fails_closed_direct_and_fastmcp(case):
    secret = "api_key_search_response_canary"
    item = SearchResult(
        knowledge_id=1,
        title="safe",
        score=0.5,
        highlight="safe",
        metadata={},
    )
    backend_issue = RetrievalIssue(
        code=ErrorCode.RETRIEVAL_BACKEND_FAILED,
        message=secret,
        stage="bm25_search",
        recoverable=True,
    )
    requested_strategy = "auto"
    if case == "success_without_results":
        response = SearchResponse.completed((item,), strategy="bm25")
        object.__setattr__(response, "results", ())
    elif case == "no_hits_with_result":
        response = SearchResponse.completed((), strategy="bm25")
        object.__setattr__(response, "results", (item,))
    elif case == "invalid_with_wrong_issue":
        response = SearchResponse.invalid("safe", strategy="bm25")
        object.__setattr__(response, "issues", (backend_issue,))
    elif case == "error_with_result":
        response = SearchResponse.failed_response(backend_issue, strategy="bm25")
        object.__setattr__(response, "results", (item,))
    elif case == "degraded_without_issue":
        response = SearchResponse.degraded_response(
            (item,),
            (backend_issue,),
            strategy="bm25",
        )
        object.__setattr__(response, "issues", ())
    elif case == "results_not_tuple":
        response = SearchResponse.completed((item,), strategy="bm25")
        object.__setattr__(response, "results", [item])
    elif case == "strategy_not_public":
        response = SearchResponse.completed((item,), strategy="bm25")
        object.__setattr__(response, "strategy", secret)
    elif case == "auto_wrong_label":
        response = SearchResponse.completed((item,), strategy="vector")
    elif case == "explicit_wrong_label":
        requested_strategy = "bm25"
        response = SearchResponse.completed((item,), strategy="hybrid")
    elif case == "corrupt_result_id":
        response = SearchResponse.completed((item,), strategy="bm25")
        object.__setattr__(item, "knowledge_id", True)
    else:
        response = SearchResponse.completed((item,), strategy="bm25")
        item.metadata["tags"] = [secret, 7]

    router = MagicMock()
    router.search.return_value = response
    with patch.object(
        tools,
        "get_query_router",
        return_value=router,
    ), patch(
        "src.retrieval.bm25_retriever.BM25Retriever",
        return_value=router,
    ):
        direct = await tools.search_knowledge("safe", strategy=requested_strategy)
        fastmcp_raw = await server.mcp.call_tool(
            "search_knowledge",
            {"query": "safe", "strategy": requested_strategy},
        )

    fastmcp = _parse_fastmcp_tool_result(fastmcp_raw)
    for payload in (direct, fastmcp):
        assert payload["status"] == "error"
        assert payload["results"] == []
        assert payload["issues"][0]["code"] == ErrorCode.RETRIEVAL_BACKEND_FAILED.value
        assert secret not in repr(payload)


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["query", "source_type", "tag", "top_k"])
async def test_search_rejects_scalar_subclasses_before_backend(field):
    secret = "api_key_SEARCH_SCALAR_SUBCLASS_CANARY"

    class StringSubclass(str):
        def strip(self, *args, **kwargs):
            raise AssertionError(secret)

    class IntSubclass(int):
        pass

    kwargs = {"query": "safe"}
    kwargs[field] = IntSubclass(5) if field == "top_k" else StringSubclass(secret)
    with patch.object(tools, "get_query_router") as factory:
        payload = await tools.search_knowledge(**kwargs)

    assert payload["status"] == "invalid"
    assert secret not in repr(payload)
    factory.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", ["error", "degraded"])
async def test_search_issue_fields_are_allowlisted_direct_and_fastmcp(terminal):
    message_secret = "api_key_SEARCH_ISSUE_MESSAGE_CANARY"
    stage_secret = "api_key_SEARCH_ISSUE_STAGE_CANARY"
    cause_secret = "api_key_SEARCH_ISSUE_CAUSE_CANARY"
    code = (
        ErrorCode.RETRIEVAL_BACKEND_FAILED
        if terminal == "error"
        else ErrorCode.RETRIEVAL_INDEX_UNAVAILABLE
    )
    issue = RetrievalIssue(
        code=code,
        message=message_secret,
        stage=stage_secret,
        recoverable=True,
        cause_type=cause_secret,
    )
    item = SearchResult(
        knowledge_id=1,
        title="safe",
        score=0.5,
        highlight="safe",
        metadata={},
    )
    response = (
        SearchResponse.failed_response(issue, strategy="bm25")
        if terminal == "error"
        else SearchResponse.degraded_response(
            (item,),
            (issue,),
            strategy="bm25",
        )
    )
    router = MagicMock()
    router.search.return_value = response

    with patch.object(tools, "get_query_router", return_value=router):
        direct = await tools.search_knowledge("safe", strategy="auto")
        fastmcp_raw = await server.mcp.call_tool(
            "search_knowledge",
            {"query": "safe", "strategy": "auto"},
        )

    fastmcp = _parse_fastmcp_tool_result(fastmcp_raw)
    for payload in (direct, fastmcp):
        public_issue = payload["issues"][0]
        assert public_issue["code"] == code.value
        assert public_issue["message"] == tools._PUBLIC_RUNTIME_MESSAGES[code]
        assert public_issue["stage"] == "retrieval"
        assert "cause_type" not in public_issue
        rendered = repr(payload)
        assert message_secret not in rendered
        assert stage_secret not in rendered
        assert cause_secret not in rendered

    safe_issue = tools._public_issue(
        RetrievalIssue(
            code=code,
            message="ignored",
            stage="bm25_search",
            recoverable=True,
            cause_type="RuntimeError",
        ),
        fallback_code=ErrorCode.RETRIEVAL_BACKEND_FAILED,
        fallback_message="检索后端不可用",
        fallback_stage="retrieval",
        fallback_severity="",
    )
    assert safe_issue["stage"] == "bm25_search"
    assert safe_issue["cause_type"] == "RuntimeError"


@pytest.mark.asyncio
async def test_archive_issue_identifiers_are_allowlisted_direct_and_fastmcp():
    message_secret = "api_key_ARCHIVE_ISSUE_MESSAGE_CANARY"
    stage_secret = "api_key_ARCHIVE_ISSUE_STAGE_CANARY"
    step_secret = "api_key_ARCHIVE_ISSUE_STEP_CANARY"
    result = WorkflowResult(
        success=False,
        terminal="error",
        errors=[message_secret],
        issues=[
            {
                "code": ErrorCode.STORAGE_VECTOR_FAILED.value,
                "message": message_secret,
                "stage": stage_secret,
                "step_id": step_secret,
                "cause_type": "api_key_ARCHIVE_ISSUE_CAUSE_CANARY",
                "severity": "error",
                "recoverable": False,
            }
        ],
    )
    engine = MagicMock()
    engine.execute_async = AsyncMock(return_value=result)
    with patch("src.workflow.engine.WorkflowEngine", return_value=engine):
        direct = await tools.archive_url("https://example.com/article")
        fastmcp_raw = await server.mcp.call_tool(
            "archive_url",
            {"url": "https://example.com/article"},
        )

    fastmcp = _parse_fastmcp_tool_result(fastmcp_raw)
    for payload in (direct, fastmcp):
        public_issue = payload["issues"][0]
        assert public_issue["code"] == ErrorCode.STORAGE_VECTOR_FAILED.value
        assert public_issue["message"] == "向量索引写入失败"
        assert public_issue["stage"] == "workflow"
        assert "step_id" not in public_issue
        assert "cause_type" not in public_issue
        rendered = repr(payload)
        assert message_secret not in rendered
        assert stage_secret not in rendered
        assert step_secret not in rendered

    safe_issue = tools._workflow_public_issue(
        {
            "code": ErrorCode.STORAGE_VECTOR_FAILED.value,
            "stage": "store_entry",
            "step_id": "store_entry",
            "severity": "error",
        },
        terminal="error",
    )
    assert safe_issue["stage"] == "store_entry"
    assert safe_issue["step_id"] == "store_entry"


@pytest.mark.asyncio
async def test_archive_url_degrades_on_workflow_exception():
    with patch(
        "src.workflow.engine.WorkflowEngine",
        side_effect=RuntimeError("workflow unavailable"),
    ):
        result = await tools.archive_url("https://example.com/offline")

    assert result == {
        "success": False,
        "terminal": "error",
        "error": "归档异常",
        "warnings": [],
        "issues": [
            {
                    "code": ErrorCode.WORKFLOW_STEP_FAILED.value,
                    "message": "工作流步骤执行失败",
                "stage": "archive_url",
                "recoverable": False,
                "severity": "error",
            }
        ],
    }


def _archive_entry():
    return SimpleNamespace(
        title="Offline title",
        content="Offline content",
        tags=["offline"],
    )


def test_public_storage_terminal_exposes_codes_but_not_local_messages():
    payload = tools._public_storage_terminal(
        {
            "status": "degraded",
            "repair_actions": ["rebuild_vectors_for_entry"],
            "storage_errors": [
                {
                    "code": "storage_vector_failed",
                    "message": r"C:\\Users\\private\\vectors.idx",
                }
            ],
        }
    )

    assert payload == {
        "storage_status": "degraded",
        "repair_actions": ["rebuild_vectors_for_entry"],
        "storage_error_codes": ["storage_vector_failed"],
    }


def test_public_storage_terminal_exposes_committed_needs_repair_semantics():
    """operation_id / core_committed / do_not_retry must reach MCP clients."""
    operation_id = "0123456789abcdef0123456789abcdef"
    payload = tools._public_storage_terminal(
        {
            "status": "repair_required",
            "operation_id": operation_id,
            "repair_actions": ["repair_operation_journal"],
            "storage_errors": [
                {"code": "storage_repair_required", "message": "core committed"}
            ],
            "core_committed": True,
            "do_not_retry": True,
        }
    )

    assert payload["storage_status"] == "repair_required"
    assert payload["operation_id"] == operation_id
    assert payload["core_committed"] is True
    assert payload["do_not_retry"] is True
    assert "请勿盲目重试" in payload["storage_warning"]
    assert payload["storage_error_codes"] == ["storage_repair_required"]


def test_public_storage_terminal_rejects_identifier_shaped_secrets():
    secret_operation_id = "sk-SECRET-CANARY"
    secret_repair_action = "api_key_CANARY"
    payload = tools._public_storage_terminal(
        {
            "status": "private_status_CANARY",
            "operation_id": secret_operation_id,
            "repair_actions": [
                secret_repair_action,
                "repair_operation_journal",
                secret_repair_action,
                "repair_operation_journal",
            ],
        }
    )

    assert payload == {"repair_actions": ["repair_operation_journal"]}
    assert secret_operation_id not in repr(payload)
    assert secret_repair_action not in repr(payload)


def test_public_storage_terminal_does_not_label_ready_as_needing_repair():
    payload = tools._public_storage_terminal(
        {
            "status": "ready",
            "core_committed": True,
            "do_not_retry": True,
            "repair_actions": [],
        }
    )

    assert payload["storage_status"] == "ready"
    assert payload["do_not_retry"] is True
    assert "storage_warning" not in payload


def test_tool_contract_helpers_fail_closed_and_cover_legacy_inputs():
    arbitrary_issue = tools._public_issue(
        object(),
        fallback_code=ErrorCode.RETRIEVAL_BACKEND_FAILED,
        fallback_message="检索失败",
        fallback_stage="test_stage",
    )
    assert arbitrary_issue["code"] == ErrorCode.RETRIEVAL_BACKEND_FAILED.value
    assert arbitrary_issue["message"] == "检索后端不可用"

    enum_issue = tools._public_issue(
        {
            "code": ErrorCode.RETRIEVAL_INDEX_UNAVAILABLE,
            "message": "索引不可用",
            "stage": "vector_index",
            "recoverable": True,
        },
        fallback_code=ErrorCode.RETRIEVAL_BACKEND_FAILED,
        fallback_message="检索失败",
        fallback_stage="test_stage",
        fallback_severity="",
    )
    assert enum_issue["code"] == ErrorCode.RETRIEVAL_INDEX_UNAVAILABLE.value

    invalid_code = tools._public_issue(
        {"code": r"C:\\private\\secret-code", "message": "safe"},
        fallback_code=ErrorCode.RETRIEVAL_BACKEND_FAILED,
        fallback_message="检索失败",
        fallback_stage="test_stage",
        fallback_severity="",
    )
    assert invalid_code["code"] == ErrorCode.RETRIEVAL_BACKEND_FAILED.value

    with_extra = tools._readonly_error_payload(
        status="degraded",
        code=ErrorCode.RESOURCE_NOT_READABLE,
        message="正文不可读取",
        stage="entry_read",
        recoverable=True,
        extra={"content": "(内容不可用)"},
    )
    assert with_extra["content"] == "(内容不可用)"

    missing_terminal = tools._workflow_result_payload(
        SimpleNamespace(success=True, data={}, warnings=[], issues=[])
    )
    assert missing_terminal["success"] is False
    assert missing_terminal["terminal"] == "error"
    assert missing_terminal["issues"][0]["code"] == ErrorCode.WORKFLOW_STEP_FAILED.value
    assert missing_terminal["issues"][0]["stage"] == "workflow_terminal"
    inconsistent = tools._workflow_result_payload(
        SimpleNamespace(
            success=False,
            terminal="success",
            data={},
            warnings=[],
            issues=[],
        )
    )
    assert inconsistent["terminal"] == "error"
    assert inconsistent["issues"][0]["code"] == ErrorCode.WORKFLOW_STEP_FAILED.value

    assert tools._public_entry_locator("not-an-id") == ""
    assert tools._public_storage_terminal(None) == {}


def test_public_issue_preserves_only_allowlisted_string_fields():
    payload = tools._public_issue(
        {
            "code": ErrorCode.RETRIEVAL_INDEX_UNAVAILABLE.value,
            "stage": "vector_index",
            "recoverable": True,
            "severity": "warning",
            "step_id": "store_entry",
        },
        fallback_code=ErrorCode.RETRIEVAL_BACKEND_FAILED,
        fallback_message="检索失败",
        fallback_stage="retrieval",
    )

    assert payload == {
        "code": ErrorCode.RETRIEVAL_INDEX_UNAVAILABLE.value,
        "message": "检索索引不可用",
        "stage": "vector_index",
        "recoverable": True,
        "severity": "warning",
        "step_id": "store_entry",
    }


def test_relation_projection_helpers_reject_incoherent_nested_and_path_shapes():
    assert tools._is_strict_json_tree(object()) is False
    assert tools._has_unsafe_public_url(
        ["https://user:password@example.com/private"]
    ) is True
    assert tools._is_relation_evidence_payload(
        {"declared_in_knowledge_id": 0}
    ) is False
    assert tools._is_relation_evidence_payload({"stub": "false"}) is False
    assert tools._is_relation_evidence_payload({"note": 1}) is False
    assert tools._is_canonical_public_source(1, 1) is False

    reverse_edge = {
        "source_knowledge_id": 2,
        "target_knowledge_id": 1,
        "relation_type": "supports",
    }
    assert tools._walk_relation_path(
        [reverse_edge],
        source_knowledge_id=1,
    ) == [1, 2]
    assert tools._expected_path_summary(1, [reverse_edge]) == "1 <-[supports]- 2"

    disconnected_edge = {
        "source_knowledge_id": 2,
        "target_knowledge_id": 3,
        "relation_type": "supports",
    }
    assert tools._walk_relation_path(
        [disconnected_edge],
        source_knowledge_id=1,
    ) is None
    assert tools._expected_path_summary(1, [disconnected_edge]) == ""

    cycle_path = [
        {
            "source_knowledge_id": 1,
            "target_knowledge_id": 2,
            "relation_type": "supports",
        },
        reverse_edge,
    ]
    assert tools._walk_relation_path(cycle_path, source_knowledge_id=1) is None
    assert tools._is_relation_edge_payload({}) is False


def test_bridge_and_timeline_projection_edge_branches_are_explicit():
    assert tools._is_bridge_edge_evidence(None) is False
    assert tools._is_bridge_edge_evidence({}) is False

    relation_fields_only = {
        field: None for field in tools._RELATION_EDGE_FIELDS
    }
    assert tools._is_bridge_edge_evidence(relation_fields_only) is False
    assert tools._is_bridge_edge_evidence(
        {
            **relation_fields_only,
            "evidence_roles": [],
            "citation_locator": "",
            "unexpected": "private",
        }
    ) is False

    missing = tools._timeline_sort_key(
        {"time_value": "", "knowledge_id": 3},
        "desc",
    )
    parsed = tools._timeline_sort_key(
        {"time_value": "2026-01-02", "knowledge_id": 2},
        "desc",
    )
    raw = tools._timeline_sort_key(
        {"time_value": "not-a-date", "knowledge_id": 1},
        "desc",
    )
    assert missing == (1, 1, 0.0, "", 3)
    assert parsed[:2] == (0, 0)
    assert parsed[2] < 0
    assert raw == (0, 1, "not-a-date", 1)


@pytest.mark.parametrize(
    ("terminal", "success"),
    [
        (None, True),
        ("unknown", True),
        (1, True),
        (True, True),
        ("success", 1),
    ],
)
def test_workflow_result_payload_requires_explicit_strict_terminal(
    terminal,
    success,
):
    secret = "pkv-workflow-terminal-canary"
    result = tools._workflow_result_payload(
        SimpleNamespace(
            success=success,
            terminal=terminal,
            data={"title": secret},
            warnings=[secret],
            issues=[{"message": secret}],
        )
    )

    assert result["success"] is False
    assert result["terminal"] == "error"
    assert result["issues"][0]["code"] == ErrorCode.WORKFLOW_STEP_FAILED.value
    assert result["issues"][0]["stage"] == "workflow_terminal"
    assert secret not in repr(result)


@pytest.mark.parametrize("terminal", ["success", "degraded"])
@pytest.mark.parametrize(
    "data",
    [
        None,
        [],
        {},
        {"knowledge_id": None},
        {"knowledge_id": 0},
        {"knowledge_id": -1},
        {"knowledge_id": True},
        {"knowledge_id": "1"},
    ],
)
def test_workflow_result_payload_requires_exact_committed_knowledge_id(
    terminal,
    data,
):
    secret = "pkv-workflow-result-canary"
    if isinstance(data, dict):
        data = {**data, "title": secret}
    result = tools._workflow_result_payload(
        SimpleNamespace(
            success=True,
            terminal=terminal,
            data=data,
            warnings=[secret],
            issues=[{"message": secret}],
        )
    )

    assert result["success"] is False
    assert result["terminal"] == "error"
    assert result["issues"][0]["code"] == ErrorCode.WORKFLOW_STEP_FAILED.value
    assert result["issues"][0]["stage"] == "workflow_result"
    assert "knowledge_id" not in result
    assert secret not in repr(result)


def test_invalid_workflow_result_data_does_not_read_diagnostics():
    class DiagnosticAccessBombResult:
        success = True
        terminal = "degraded"
        data = {"knowledge_id": True}

        @property
        def warnings(self):
            raise AssertionError("must not read workflow warnings")

        @property
        def issues(self):
            raise AssertionError("must not read workflow issues")

    result = tools._workflow_result_payload(DiagnosticAccessBombResult())

    assert result["success"] is False
    assert result["terminal"] == "error"
    assert result["issues"][0]["code"] == ErrorCode.WORKFLOW_STEP_FAILED.value
    assert result["issues"][0]["stage"] == "workflow_result"


@pytest.mark.parametrize("status", [None, "", "unknown", 1, True])
def test_completed_workflow_result_requires_exact_public_storage_status(status):
    result = tools._workflow_result_payload(
        SimpleNamespace(
            success=True,
            terminal="success",
            data={"knowledge_id": 1, "status": status},
            errors=[],
            warnings=[],
            issues=[],
        )
    )

    assert result["success"] is False
    assert result["terminal"] == "error"
    assert result["issues"][0]["code"] == ErrorCode.WORKFLOW_STEP_FAILED.value
    assert result["issues"][0]["stage"] == "workflow_result"


@pytest.mark.parametrize(
    ("terminal", "status", "core_committed", "warnings"),
    [
        ("success", "degraded", True, []),
        ("success", "ready", False, []),
        ("success", "ready", 1, []),
        ("degraded", "degraded", False, ["safe warning"]),
        ("degraded", "degraded", 1, ["safe warning"]),
    ],
)
def test_completed_workflow_result_requires_coherent_storage_terminal(
    terminal,
    status,
    core_committed,
    warnings,
):
    secret = "pkv-workflow-coherence-canary"
    result = tools._workflow_result_payload(
        SimpleNamespace(
            success=True,
            terminal=terminal,
            data={
                "knowledge_id": 1,
                "status": status,
                "core_committed": core_committed,
                "title": secret,
            },
            errors=[],
            warnings=warnings,
            issues=[],
        )
    )

    assert result["success"] is False
    assert result["terminal"] == "error"
    assert result["issues"][0]["code"] == ErrorCode.WORKFLOW_STEP_FAILED.value
    assert result["issues"][0]["stage"] == "workflow_result"
    assert "knowledge_id" not in result
    assert secret not in repr(result)


def test_completed_workflow_result_requires_explicit_core_commit_marker():
    result = tools._workflow_result_payload(
        SimpleNamespace(
            success=True,
            terminal="success",
            data={"knowledge_id": 1, "status": "ready"},
            errors=[],
            warnings=[],
            issues=[],
        )
    )

    assert result["success"] is False
    assert result["terminal"] == "error"
    assert result["issues"][0]["stage"] == "workflow_result"


def test_degraded_workflow_terminal_accepts_committed_ready_storage():
    result = tools._workflow_result_payload(
        SimpleNamespace(
            success=True,
            terminal="degraded",
            data={
                "knowledge_id": 1,
                "status": "ready",
                "core_committed": True,
            },
            errors=[],
            warnings=["analysis fallback was used"],
            issues=[],
        )
    )

    assert result["success"] is True
    assert result["terminal"] == "degraded"
    assert result["storage_status"] == "ready"
    assert result["core_committed"] is True
    assert len(result["issues"]) == 1


def test_completed_workflow_result_rejects_malformed_diagnostics_without_leak():
    secret = "pkv-completed-diagnostics-canary"

    class ListSubclass(list):
        pass

    class DictSubclass(dict):
        pass

    class MaliciousIterable:
        def __iter__(self):
            raise AssertionError(secret)

    cases = [
        ("success", [secret], [], []),
        ("success", [], [secret], []),
        ("success", [], [], [{"severity": "warning", "message": secret}]),
        ("degraded", [secret], ["warning"], []),
        ("degraded", [], [], []),
        ("degraded", [], [1], []),
        ("degraded", [], ListSubclass([secret]), []),
        ("degraded", [], MaliciousIterable(), []),
        ("degraded", [], [], [secret]),
        ("degraded", [], [], [{"severity": "error", "message": secret}]),
        (
            "degraded",
            [],
            [],
            [DictSubclass(severity="warning", message=secret)],
        ),
    ]

    for terminal, errors, warnings, issues in cases:
        result = tools._workflow_result_payload(
            SimpleNamespace(
                success=True,
                terminal=terminal,
                data={
                    "knowledge_id": 1,
                    "status": "ready" if terminal == "success" else "degraded",
                    "core_committed": True,
                    "title": secret,
                },
                errors=errors,
                warnings=warnings,
                issues=issues,
            )
        )

        assert result["success"] is False
        assert result["terminal"] == "error"
        assert result["issues"][0]["code"] == ErrorCode.WORKFLOW_STEP_FAILED.value
        assert result["issues"][0]["stage"] == "workflow_result"
        assert secret not in repr(result)


@pytest.mark.parametrize("bomb_field", ["errors", "warnings", "issues"])
def test_completed_workflow_result_diagnostic_property_failure_is_stable(bomb_field):
    secret = "pkv-diagnostic-property-canary"

    class DiagnosticPropertyBomb:
        success = True
        terminal = "degraded"
        data = {
            "knowledge_id": 1,
            "status": "degraded",
            "core_committed": True,
            "title": secret,
        }

        @property
        def errors(self):
            if bomb_field == "errors":
                raise RuntimeError(secret)
            return []

        @property
        def warnings(self):
            if bomb_field == "warnings":
                raise RuntimeError(secret)
            return ["safe warning"]

        @property
        def issues(self):
            if bomb_field == "issues":
                raise RuntimeError(secret)
            return []

    result = tools._workflow_result_payload(DiagnosticPropertyBomb())

    assert result["success"] is False
    assert result["terminal"] == "error"
    assert result["issues"][0]["code"] == ErrorCode.WORKFLOW_STEP_FAILED.value
    assert result["issues"][0]["stage"] == "workflow_result"
    assert secret not in repr(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("bomb_field", ["warnings", "issues"])
async def test_archive_url_diagnostic_property_failure_is_stable_direct_and_fastmcp(
    bomb_field,
):
    secret = "pkv-archive-diagnostic-handler-canary"

    class DiagnosticPropertyBomb:
        success = True
        terminal = "degraded"
        data = {
            "knowledge_id": 1,
            "status": "degraded",
            "core_committed": True,
        }
        errors = []

        @property
        def warnings(self):
            if bomb_field == "warnings":
                raise RuntimeError(secret)
            return ["safe warning"]

        @property
        def issues(self):
            if bomb_field == "issues":
                raise RuntimeError(secret)
            return []

    engine = MagicMock()
    engine.execute_async = AsyncMock(return_value=DiagnosticPropertyBomb())
    with (
        patch.object(tools, "validate_url_security_result", return_value=None),
        patch("src.workflow.engine.WorkflowEngine", return_value=engine),
    ):
        direct = await tools.archive_url("https://example.com/article")
        fastmcp_raw = await server.mcp.call_tool(
            "archive_url",
            {"url": "https://example.com/article"},
        )

    for payload in (direct, _parse_fastmcp_tool_result(fastmcp_raw)):
        assert payload["success"] is False
        assert payload["terminal"] == "error"
        assert payload["issues"][0]["code"] == ErrorCode.WORKFLOW_STEP_FAILED.value
        assert payload["issues"][0]["stage"] == "workflow_result"
        assert secret not in repr(payload)


@pytest.mark.parametrize(
    ("status", "core_committed", "explicit_do_not_retry", "expected"),
    [
        ("repair_required", False, False, True),
        ("rejected", False, False, False),
        ("rejected", False, True, True),
        ("rejected", True, False, True),
    ],
)
def test_fatal_completed_storage_result_has_exact_retry_oracle(
    status,
    core_committed,
    explicit_do_not_retry,
    expected,
):
    terminal = "success" if status == "repair_required" else "degraded"
    result = tools._workflow_result_payload(
        SimpleNamespace(
            success=True,
            terminal=terminal,
            data={
                "knowledge_id": 7,
                "status": status,
                "core_committed": core_committed,
                "do_not_retry": explicit_do_not_retry,
            },
            errors=[],
            warnings=[] if terminal == "success" else ["safe warning"],
            issues=[],
        )
    )

    assert result["success"] is False
    assert result["terminal"] == "error"
    assert result["storage_status"] == status
    assert result["knowledge_id"] == 7
    assert result["do_not_retry"] is expected
    expected_code = (
        ErrorCode.STORAGE_REPAIR_REQUIRED.value
        if status == "repair_required"
        else ErrorCode.WORKFLOW_STEP_FAILED.value
    )
    assert result["issues"][0]["code"] == expected_code


def test_new_readonly_and_workflow_helpers_fail_closed():
    assert tools._normalize_relation_types(
        ["references", "references", "related_document", "references"]
    ) == ["references", "related_document"]
    assert tools._retrieval_degraded_issue_from_notes(
        {"limitation_notes": "not-a-list"},
        operations=("timeline",),
    ) is None
    assert tools._retrieval_degraded_issue_from_notes(
        {"limitation_notes": [None, "ordinary partial limitation"]},
        operations=("timeline",),
    ) is None
    invalid_marker = tools._retrieval_degraded_issue_from_notes(
        {"limitation_notes": ["timeline_retrieval_degraded[unknown_code"]},
        operations=("timeline",),
    )
    assert invalid_marker["code"] == ErrorCode.RETRIEVAL_BACKEND_FAILED.value

    runtime_issue = tools._workflow_public_issue(
        PKVRuntimeError(
            ErrorCode.STORAGE_VECTOR_FAILED,
            "secret=pkv-workflow-helper C:\\Users\\private",
            stage="store_entry",
            recoverable=True,
        ),
        terminal="degraded",
    )
    assert runtime_issue["message"] == "向量索引写入失败"
    assert runtime_issue["recoverable"] is True

    enum_issue = tools._workflow_public_issue(
        {
            "code": ErrorCode.STORAGE_INDEX_FAILED,
            "message": "secret=pkv-workflow-helper",
            "stage": "unsafe C:\\Users\\private",
        },
        terminal="degraded",
    )
    assert enum_issue["code"] == ErrorCode.STORAGE_INDEX_FAILED.value
    assert enum_issue["stage"] == "workflow"

    unknown_issue = tools._workflow_public_issue(
        object(),
        terminal="error",
    )
    assert unknown_issue["code"] == ErrorCode.WORKFLOW_STEP_FAILED.value
    assert unknown_issue["message"] == "工作流步骤执行失败"

    warnings_only = tools._workflow_result_payload(
        SimpleNamespace(
            success=True,
            terminal="degraded",
            data={
                "knowledge_id": 1,
                "status": "degraded",
                "core_committed": True,
            },
            errors=[],
            warnings=["secret=pkv-workflow-helper C:\\Users\\private"],
            issues=[],
        )
    )
    assert warnings_only["terminal"] == "degraded"
    assert warnings_only["issues"]
    assert "pkv-workflow-helper" not in repr(warnings_only)


def test_readonly_service_failures_never_echo_exception_details(caplog):
    secret = "pkv-service-secret-f51c"
    runtime_error = PKVRuntimeError(
        ErrorCode.RETRIEVAL_INDEX_UNAVAILABLE,
        f"index failure {secret} C:\\Users\\private",
        stage="vector_index",
        recoverable=True,
    )
    runtime_payload = tools._readonly_service_failure(
        runtime_error,
        operation="关联查询",
        stage="relation_query",
    )
    assert runtime_payload["status"] == "error"
    assert runtime_payload["issues"][0]["code"] == ErrorCode.RETRIEVAL_INDEX_UNAVAILABLE.value
    assert runtime_payload["issues"][0]["message"] == "检索索引不可用"

    generic_payload = tools._readonly_service_failure(
        RuntimeError(f"boom {secret} C:\\Users\\private"),
        operation="关联查询",
        stage="relation_query",
    )
    assert generic_payload["status"] == "error"
    assert generic_payload["issues"][0]["code"] == ErrorCode.RETRIEVAL_BACKEND_FAILED.value
    rendered = repr((runtime_payload, generic_payload))
    assert secret not in rendered
    assert "Users" not in rendered
    assert secret not in caplog.text


@pytest.mark.asyncio
async def test_archive_text_committed_repair_failure_exposes_id_and_do_not_retry():
    """A committed-needs-repair workflow failure still exposes ID/locator/status."""
    operation_id = "1234567890abcdef1234567890abcdef"
    processor = MagicMock()
    processor.process_text = AsyncMock(return_value=_archive_entry())
    workflow = MagicMock()
    workflow.execute_async = AsyncMock(
        return_value=WorkflowResult(
            success=False,
            terminal="error",
            errors=["storage_repair_required: 核心存储已提交但操作日志更新失败"],
            issues=[
                {
                    "code": ErrorCode.STORAGE_REPAIR_REQUIRED.value,
                    "message": "核心存储已提交但操作日志更新失败",
                    "severity": "error",
                    "stage": "store_entry",
                    "recoverable": False,
                }
            ],
            data={
                "knowledge_id": 42,
                "status": "repair_required",
                "operation_id": operation_id,
                "core_committed": True,
                "do_not_retry": True,
                "repair_actions": ["repair_operation_journal"],
                "storage_errors": [
                    {"code": "storage_repair_required", "message": "x"}
                ],
            },
        )
    )

    with (
        patch(
            "src.processors.text_fallback_processor.TextFallbackProcessor",
            return_value=processor,
        ),
        patch(
            "src.workflow.engine.WorkflowEngine",
            return_value=workflow,
        ),
    ):
        result = await tools.archive_text("Offline content")

    assert result["success"] is False
    assert result["knowledge_id"] == 42
    assert result["entry_locator"] == "pkv://entries/42"
    assert result["storage_status"] == "repair_required"
    assert result["operation_id"] == operation_id
    assert result["core_committed"] is True
    assert result["do_not_retry"] is True
    assert result["storage_error_codes"] == ["storage_repair_required"]


@pytest.mark.asyncio
async def test_archive_text_reports_workflow_failure():
    processor = MagicMock()
    processor.process_text = AsyncMock(return_value=_archive_entry())
    workflow = MagicMock()
    workflow.execute_async = AsyncMock(
        return_value=WorkflowResult(
            success=False,
            terminal="error",
            errors=[],
        )
    )

    with (
        patch(
            "src.processors.text_fallback_processor.TextFallbackProcessor",
            return_value=processor,
        ),
        patch(
            "src.workflow.engine.WorkflowEngine",
            return_value=workflow,
        ),
    ):
        result = await tools.archive_text("Offline content")

    assert result == {
        "success": False,
        "terminal": "error",
        "warnings": [],
        "issues": [
            {
                "code": ErrorCode.WORKFLOW_STEP_FAILED.value,
                    "message": "工作流步骤执行失败",
                "stage": "workflow",
                "recoverable": False,
                "severity": "error",
            }
        ],
        "error": "归档失败",
    }


@pytest.mark.asyncio
async def test_archive_text_degrades_on_processor_exception():
    processor = MagicMock()
    processor.process_text = AsyncMock(side_effect=RuntimeError("parse failed"))

    with patch(
        "src.processors.text_fallback_processor.TextFallbackProcessor",
        return_value=processor,
    ):
        result = await tools.archive_text("Offline content")

    assert result == {
        "success": False,
        "terminal": "error",
        "error": "归档异常",
        "warnings": [],
        "issues": [
            {
                    "code": ErrorCode.WORKFLOW_STEP_FAILED.value,
                    "message": "工作流步骤执行失败",
                "stage": "archive_text",
                "recoverable": False,
                "severity": "error",
            }
        ],
    }


@pytest.mark.asyncio
async def test_get_related_stops_after_requested_limit():
    sqlite_store = MagicMock()
    sqlite_store.query_by_id.side_effect = [
        _strict_entry_row(1, "Seed"),
        _strict_entry_row(2, "Related"),
    ]
    vector_store = MagicMock()
    vector_store.get_doc_vector.return_value = [0.1, 0.2]
    vector_store.search_doc.return_value = [
        (1, 0.0),
        (2, 0.1),
    ]
    config = SimpleNamespace(
        vector_index_dir=Path("isolated") / "vectors",
    )

    with (
        patch.object(tools, "get_sqlite_store", return_value=sqlite_store),
        patch.object(tools, "get_config", return_value=config),
        patch("src.storage.vector_store.VectorStore") as vector_type,
    ):
        vector_type.has_index_artifacts.return_value = True
        vector_type.return_value = vector_store
        result = await tools.get_related("1", limit=1)

    assert result["total"] == 1
    assert result["results"][0]["knowledge_id"] == 2
    assert sqlite_store.query_by_id.call_args_list == [call(1), call(2)]


@pytest.mark.parametrize(
    "case",
    [
        "malformed_seed",
        "malformed_hit_list",
        "nonfinite_distance",
        "missing_related_metadata",
        "malformed_related_metadata",
    ],
)
@pytest.mark.asyncio
async def test_get_related_backend_shape_is_strict_direct_and_fastmcp(
    case,
    caplog,
):
    secret = "api_key_RELATED_SHAPE_CANARY"
    sqlite_store = MagicMock()

    def query_by_id(knowledge_id):
        if case == "malformed_seed" and knowledge_id == 1:
            return {"title": secret}
        if knowledge_id == 1:
            return _strict_entry_row(1, "Seed")
        if case == "missing_related_metadata":
            return None
        if case == "malformed_related_metadata":
            return {"knowledge_id": 2, "title": secret}
        return _strict_entry_row(2, "Related")

    sqlite_store.query_by_id.side_effect = query_by_id
    vector_store = MagicMock()
    vector_store.get_doc_vector.return_value = [0.1, 0.2]
    if case == "malformed_hit_list":
        vector_store.search_doc.return_value = secret
    elif case == "nonfinite_distance":
        vector_store.search_doc.return_value = [(2, float("nan"))]
    else:
        vector_store.search_doc.return_value = [(2, 0.1)]
    config = SimpleNamespace(vector_index_dir=Path("isolated") / "vectors")

    with (
        patch.object(tools, "get_sqlite_store", return_value=sqlite_store),
        patch.object(tools, "get_config", return_value=config),
        patch("src.storage.vector_store.VectorStore") as vector_type,
    ):
        vector_type.has_index_artifacts.return_value = True
        vector_type.return_value = vector_store
        direct = await tools.get_related("1")
        fastmcp_raw = await server.mcp.call_tool(
            "get_related",
            {"knowledge_id": "1"},
        )

    fastmcp = _parse_fastmcp_tool_result(fastmcp_raw)
    for payload in (direct, fastmcp):
        assert payload["status"] == "error"
        assert payload["issues"][0]["code"] in {
            ErrorCode.RETRIEVAL_BACKEND_FAILED.value,
            ErrorCode.RETRIEVAL_METADATA_INCONSISTENT.value,
        }
        assert secret not in repr(payload)
    assert secret not in caplog.text


@pytest.mark.parametrize("legacy_vector_case", ["zero", "float32_norm_overflow"])
@pytest.mark.asyncio
async def test_get_related_rejects_unsafe_legacy_vector_direct_and_fastmcp(
    legacy_vector_case,
    caplog,
):
    """A malformed stored vector cannot become a related-search query."""

    secret = f"api_key_LEGACY_VECTOR_CANARY_{legacy_vector_case}"
    sqlite_store = MagicMock()
    sqlite_store.query_by_id.return_value = _strict_entry_row(1, "Seed")
    vector_store = MagicMock()

    def reject_legacy_vector(_knowledge_id):
        raise PKVRuntimeError(
            ErrorCode.RETRIEVAL_METADATA_INCONSISTENT,
            f"{secret} C:\\Users\\private\\vectors.idx",
            stage="document_vector_read",
            recoverable=True,
        )

    vector_store.get_doc_vector.side_effect = reject_legacy_vector
    config = SimpleNamespace(vector_index_dir=Path("isolated") / "vectors")

    with (
        patch.object(tools, "get_sqlite_store", return_value=sqlite_store),
        patch.object(tools, "get_config", return_value=config),
        patch("src.storage.vector_store.VectorStore") as vector_type,
    ):
        vector_type.has_index_artifacts.return_value = True
        vector_type.return_value = vector_store
        direct = await tools.get_related("1")
        fastmcp_raw = await server.mcp.call_tool(
            "get_related",
            {"knowledge_id": "1"},
        )

    fastmcp = _parse_fastmcp_tool_result(fastmcp_raw)
    expected = {
        "status": "error",
        "strategy": "vector_related",
        "total": 0,
        "results": [],
        "message": "向量搜索不可用",
        "error": "向量搜索不可用",
        "issues": [
            {
                "code": ErrorCode.RETRIEVAL_METADATA_INCONSISTENT.value,
                "message": "检索元数据不一致",
                "stage": "document_vector_read",
                "recoverable": True,
            }
        ],
    }
    assert direct == expected
    assert fastmcp == expected
    assert vector_store.get_doc_vector.call_args_list == [call(1), call(1)]
    vector_store.search_doc.assert_not_called()
    assert secret not in repr(direct)
    assert secret not in repr(fastmcp)
    assert secret not in caplog.text


@pytest.mark.parametrize(
    (
        "handler_name",
        "getter_name",
        "service_method",
        "kwargs",
    ),
    [
        (
            "query_subgraph",
            "get_relation_query_service",
            "query_subgraph",
            {"knowledge_id": "1"},
        ),
        (
            "explain_relation",
            "get_relation_query_service",
            "explain_relation",
            {
                "source_knowledge_id": "1",
                "target_knowledge_id": "2",
            },
        ),
        (
            "collect_evidence",
            "get_evidence_collection_service",
            "collect_evidence",
            {"question": "offline question"},
        ),
        (
            "find_bridges",
            "get_exploration_service",
            "find_bridges",
            {"seed_knowledge_id": "1"},
        ),
        (
            "timeline_of",
            "get_exploration_service",
            "timeline_of",
            {"topic": "offline topic"},
        ),
        (
            "contrast",
            "get_exploration_service",
            "contrast",
            {
                "topic_a": "offline A",
                "topic_b": "offline B",
            },
        ),
    ],
)
@pytest.mark.asyncio
async def test_relation_tools_treat_service_value_errors_as_backend_failures(
    handler_name,
    getter_name,
    service_method,
    kwargs,
):
    service = MagicMock()
    getattr(service, service_method).side_effect = ValueError(
        "invalid offline request"
    )

    with patch.object(tools, getter_name, return_value=service):
        result = await getattr(tools, handler_name)(**kwargs)

    assert result["status"] == "error"
    assert result["issues"][0]["code"] == ErrorCode.RETRIEVAL_BACKEND_FAILED.value
    assert result["issues"][0]["recoverable"] is True
    assert "invalid offline request" not in repr(result)


@pytest.mark.parametrize(
    ("handler_name", "getter_name", "service_method", "kwargs"),
    [
        (
            "query_subgraph",
            "get_relation_query_service",
            "query_subgraph",
            {"knowledge_id": "1"},
        ),
        (
            "explain_relation",
            "get_relation_query_service",
            "explain_relation",
            {"source_knowledge_id": "1", "target_knowledge_id": "2"},
        ),
        (
            "collect_evidence",
            "get_evidence_collection_service",
            "collect_evidence",
            {"question": "offline question"},
        ),
        (
            "find_bridges",
            "get_exploration_service",
            "find_bridges",
            {"seed_knowledge_id": "1"},
        ),
        (
            "timeline_of",
            "get_exploration_service",
            "timeline_of",
            {"topic": "offline topic"},
        ),
        (
            "contrast",
            "get_exploration_service",
            "contrast",
            {"topic_a": "offline A", "topic_b": "offline B"},
        ),
    ],
)
@pytest.mark.asyncio
async def test_relation_tools_reject_wrong_domain_type_direct_and_fastmcp(
    handler_name,
    getter_name,
    service_method,
    kwargs,
    caplog,
):
    secret = "api_key_RELATION_DOMAIN_TYPE_CANARY"
    bad_result = MagicMock()
    bad_result.to_dict.side_effect = AssertionError(secret)
    service = MagicMock()
    getattr(service, service_method).return_value = bad_result

    with patch.object(tools, getter_name, return_value=service):
        direct = await getattr(tools, handler_name)(**kwargs)
        fastmcp_raw = await server.mcp.call_tool(handler_name, kwargs)

    fastmcp = _parse_fastmcp_tool_result(fastmcp_raw)
    for payload in (direct, fastmcp):
        assert payload["status"] == "error"
        assert payload["issues"][0]["code"] == (
            ErrorCode.RETRIEVAL_BACKEND_FAILED.value
        )
        assert secret not in repr(payload)
    bad_result.to_dict.assert_not_called()
    assert secret not in caplog.text


def _malformed_exact_relation_result(case):
    edge = RelationRecord(
        relation_id=7,
        source_knowledge_id=1,
        target_knowledge_id=3,
        relation_type=RelationType.REFERENCES,
        relation_source_type=RelationSourceType.MANUAL,
        evidence_payload={"raw_target": "three.md"},
    )
    if case == "subgraph_missing_endpoint":
        return RelationSubgraphResult(
            seed_knowledge_id=1,
            max_depth=2,
            nodes=[
                RelationSubgraphNode(knowledge_id=1, depth=0),
                RelationSubgraphNode(knowledge_id=2, depth=1),
            ],
            edges=[edge],
            grouped_edges={"references": [edge]},
        )
    if case == "explanation_wrong_target":
        return RelationExplanationResult(
            source_knowledge_id=1,
            target_knowledge_id=2,
            found=True,
            explanation_type="direct",
            hops=1,
            path=[edge],
            supporting_relations=[edge],
            summary="1 -[references]-> 3",
            evidence_items=[
                {
                    "step_index": 0,
                    "relation_type": "references",
                    "relation_source_type": "manual",
                    "direction": "directed",
                    "weight": 1.0,
                    "source_knowledge_id": 1,
                    "target_knowledge_id": 3,
                    "evidence_payload": {"raw_target": "three.md"},
                }
            ],
        )
    if case == "evidence_wrong_seed":
        return CollectedEvidenceResult(
            question="fixture",
            found=True,
            seed_knowledge_id=1,
            seed_title="one",
            evidence=[
                CollectedEvidenceItem(
                    knowledge_id=2,
                    title="two",
                    retrieval_rank=1,
                    retrieval_score=0.5,
                    is_seed=True,
                    citation_locator=build_entry_locator(2),
                )
            ],
        )
    if case in {"evidence_rank_gap", "evidence_rank_reordered"}:
        seed = CollectedEvidenceItem(
            knowledge_id=1,
            title="one",
            retrieval_rank=1,
            retrieval_score=0.5,
            is_seed=True,
            citation_locator=build_entry_locator(1),
        )
        candidate = CollectedEvidenceItem(
            knowledge_id=2,
            title="two",
            retrieval_rank=99 if case == "evidence_rank_gap" else 2,
            retrieval_score=0.5,
            citation_locator=build_entry_locator(2),
        )
        return CollectedEvidenceResult(
            question="fixture",
            found=True,
            seed_knowledge_id=1,
            seed_title="one",
            evidence=(
                [seed, candidate]
                if case == "evidence_rank_gap"
                else [candidate, seed]
            ),
        )
    if case == "bridge_seed_candidate":
        return BridgeDiscoveryResult(
            seed_knowledge_id=1,
            found=True,
            max_depth=2,
            items=[
                BridgeCandidate(
                    knowledge_id=1,
                    title="one",
                    depth=1,
                    bridge_score=0.5,
                    connected_knowledge_ids=[2, 3],
                    relation_types=["references"],
                )
            ],
            evidence_sources=[
                "relation_subgraph",
                "graph_bridge_signal",
                "entry_tags",
                "entry_title_summary",
            ],
            limitation_notes=["partial"],
        )
    if case == "timeline_wrong_order":
        return TimelineResult(
            topic="fixture",
            found=True,
            inferred_time_field="unavailable",
            time_source_priority=["event_time", "published_at", "archived_at"],
            items=[
                TimelinePoint(
                    knowledge_id=2,
                    title="two",
                    source=build_entry_locator(2),
                    citation_locator=build_entry_locator(2),
                    retrieval_score=0.5,
                ),
                TimelinePoint(
                    knowledge_id=1,
                    title="one",
                    source=build_entry_locator(1),
                    citation_locator=build_entry_locator(1),
                    retrieval_score=0.5,
                ),
            ],
            evidence_sources=[
                "query_results",
                "entry_metadata",
                "structured_time_fields",
            ],
            limitation_notes=["partial"],
        )
    candidate = ContrastCandidateItem(
        knowledge_id=1,
        title="one",
        tags=["A"],
        retrieval_score=0.5,
        source=build_entry_locator(1),
        citation_locator=build_entry_locator(1),
    )
    return ContrastResult(
        topic_a="A",
        topic_b="B",
        found=True,
        topic_a_candidates=[candidate],
        shared_tags=[],
        only_a_tags=[],
        only_b_tags=[],
        comparison_dimensions={},
        evidence_sources=[
            "query_results",
            "relation_graph",
            "entry_tags",
            "entry_summary",
        ],
        limitation_notes=["partial"],
    )


@pytest.mark.parametrize(
    ("case", "handler_name", "getter_name", "service_method", "arguments", "stage"),
    [
        (
            "subgraph_missing_endpoint",
            "query_subgraph",
            "get_relation_query_service",
            "query_subgraph",
            {"knowledge_id": "1"},
            "query_subgraph",
        ),
        (
            "explanation_wrong_target",
            "explain_relation",
            "get_relation_query_service",
            "explain_relation",
            {"source_knowledge_id": "1", "target_knowledge_id": "2"},
            "explain_relation",
        ),
        (
            "evidence_wrong_seed",
            "collect_evidence",
            "get_evidence_collection_service",
            "collect_evidence",
            {"question": "fixture"},
            "collect_evidence",
        ),
        (
            "evidence_rank_gap",
            "collect_evidence",
            "get_evidence_collection_service",
            "collect_evidence",
            {"question": "fixture"},
            "collect_evidence",
        ),
        (
            "evidence_rank_reordered",
            "collect_evidence",
            "get_evidence_collection_service",
            "collect_evidence",
            {"question": "fixture"},
            "collect_evidence",
        ),
        (
            "bridge_seed_candidate",
            "find_bridges",
            "get_exploration_service",
            "find_bridges",
            {"seed_knowledge_id": "1"},
            "find_bridges",
        ),
        (
            "timeline_wrong_order",
            "timeline_of",
            "get_exploration_service",
            "timeline_of",
            {"topic": "fixture"},
            "timeline_of",
        ),
        (
            "contrast_tag_mismatch",
            "contrast",
            "get_exploration_service",
            "contrast",
            {"topic_a": "A", "topic_b": "B"},
            "contrast",
        ),
    ],
)
@pytest.mark.asyncio
async def test_exact_relation_domain_cross_field_corruption_fails_closed_dual_channel(
    case,
    handler_name,
    getter_name,
    service_method,
    arguments,
    stage,
):
    service = MagicMock()
    getattr(service, service_method).return_value = _malformed_exact_relation_result(case)
    with patch.object(tools, getter_name, return_value=service):
        direct = await getattr(tools, handler_name)(**arguments)
        fastmcp_raw = await server.mcp.call_tool(handler_name, arguments)

    fastmcp = _parse_fastmcp_tool_result(fastmcp_raw)
    for payload in (direct, fastmcp):
        assert payload["status"] == "error"
        assert payload["issues"][0]["code"] == (
            ErrorCode.RETRIEVAL_BACKEND_FAILED.value
        )
        assert payload["issues"][0]["stage"] == stage


@pytest.mark.parametrize(
    ("handler_name", "getter_name", "service_method", "kwargs"),
    [
        (
            "query_subgraph",
            "get_relation_query_service",
            "query_subgraph",
            {"knowledge_id": "1"},
        ),
        (
            "explain_relation",
            "get_relation_query_service",
            "explain_relation",
            {"source_knowledge_id": "1", "target_knowledge_id": "2"},
        ),
    ],
)
@pytest.mark.asyncio
async def test_relation_tools_redact_local_evidence_payloads(
    handler_name,
    getter_name,
    service_method,
    kwargs,
):
    record = RelationRecord(
        source_knowledge_id=1,
        target_knowledge_id=2,
        relation_type=RelationType.REFERENCES,
        relation_source_type=RelationSourceType.MANUAL,
        evidence_payload={
            "source_file_path": r"C:\\Users\\audit\\secret.md",
            "target_file_path": r"\\\\server\\private\\target.md",
            "source_url": "file:///C:/Users/audit/source.md",
        },
    )
    if handler_name == "query_subgraph":
        result_object = RelationSubgraphResult(
            seed_knowledge_id=1,
            max_depth=2,
            nodes=[
                RelationSubgraphNode(knowledge_id=1, depth=0),
                RelationSubgraphNode(knowledge_id=2, depth=1),
            ],
            edges=[record],
            grouped_edges={RelationType.REFERENCES.value: [record]},
        )
        public_edge_key = "edges"
    else:
        result_object = RelationExplanationResult(
            source_knowledge_id=1,
            target_knowledge_id=2,
            found=True,
            explanation_type="direct",
            hops=1,
            path=[record],
            supporting_relations=[record],
            summary="1 -[references]-> 2",
            evidence_items=[
                {
                    "step_index": 0,
                    "relation_type": "references",
                    "relation_source_type": "manual",
                    "direction": "directed",
                    "weight": 1.0,
                    "source_knowledge_id": 1,
                    "target_knowledge_id": 2,
                    "evidence_payload": dict(record.evidence_payload),
                }
            ],
        )
        public_edge_key = "path"
    service = MagicMock()
    getattr(service, service_method).return_value = result_object

    with patch.object(tools, getter_name, return_value=service):
        result = await getattr(tools, handler_name)(**kwargs)

    payload = result[public_edge_key][0]["evidence_payload"]
    assert "source_file_path" not in payload
    assert "target_file_path" not in payload
    assert payload["source_url"] == ""


@pytest.mark.asyncio
async def test_contrast_rejects_empty_first_topic():
    result = await tools.contrast(topic_a=" ", topic_b="B")
    assert result["status"] == "invalid"
    assert result["error"] == "检索参数无效"
    assert result["issues"][0]["code"] == ErrorCode.RETRIEVAL_INVALID_QUERY.value


@pytest.mark.asyncio
async def test_entry_resource_handles_empty_and_failed_markdown(tmp_path: Path):
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    entry_path = vault_dir / "offline.md"
    entry_path.write_text("# Offline", encoding="utf-8")
    sqlite_store = MagicMock()
    sqlite_store.query_by_id.return_value = {
        "knowledge_id": 1,
        "title": "Offline entry",
        "source_type": "text",
        "file_path": str(entry_path),
    }
    markdown_store = MagicMock()
    markdown_store.vault_dir = vault_dir

    with (
        patch.object(
            resources,
            "get_sqlite_store",
            return_value=sqlite_store,
        ),
        patch.object(
            resources,
            "get_markdown_store",
            return_value=markdown_store,
        ),
    ):
        markdown_store.load.return_value = SimpleNamespace(content="")
        with pytest.raises(ValueError, match="resource_not_readable"):
            await resources.get_entry_content("1")

        markdown_store.load.side_effect = RuntimeError("decode failed")
        with pytest.raises(ValueError, match="resource_not_readable") as exc_info:
            await resources.get_entry_content("1")
        assert "decode failed" not in str(exc_info.value)


def test_utils_cover_summary_and_url_parse_degradation():
    assert utils.serialize_entry_summary(
        {
            "knowledge_id": 7,
            "title": "Offline entry",
            "summary_one_sentence": "Summary",
            "tags": "offline,contract",
        }
    ) == {
        "knowledge_id": 7,
        "title": "Offline entry",
        "abstract": "Summary",
        "tags": ["offline", "contract"],
        "source_type": "",
        "word_count": 0,
        "archived_at": "",
    }

    with patch.object(
        utils,
        "parse_http_target",
        side_effect=RuntimeError("parser failed"),
    ):
        assert utils.validate_url("https://example.com") == (
            False,
            "URL 解析失败",
        )


def test_python_module_entrypoint_delegates_to_server_main():
    with patch.object(server, "main") as main:
        runpy.run_module("src.mcp.__main__", run_name="__main__")

    main.assert_called_once_with()
