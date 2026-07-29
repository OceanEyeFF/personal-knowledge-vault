"""Offline coverage contracts for MCP boundary and degradation paths."""

import logging
import runpy
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from src.mcp import resources, server, tools, utils


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


def test_server_main_runs_http_with_configured_log_level(
    preserve_root_logger,
):
    config = MagicMock()
    config.log_level = "WARNING"
    config.get.side_effect = (
        lambda key, default=None: (
            False if key == "logging.file.enabled" else default
        )
    )

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
        patch.object(server, "get_config", return_value=config),
        patch.object(server.mcp, "run") as run_server,
    ):
        server.main()

    run_server.assert_called_once_with(
        transport="streamable-http",
        port=4321,
    )


def test_server_main_degrades_when_file_logging_cannot_start(
    tmp_path,
    preserve_root_logger,
):
    config = MagicMock()
    config.log_level = "INFO"
    config.log_dir = tmp_path / "logs"
    config.get.side_effect = lambda _key, default=None: default

    with (
        patch.object(sys, "argv", ["pkv-mcp", "--log-level", "ERROR"]),
        patch.object(server, "get_config", return_value=config),
        patch(
            "logging.handlers.RotatingFileHandler",
            side_effect=OSError("read-only log target"),
        ),
        patch.object(server.mcp, "run") as run_server,
    ):
        server.main()

    run_server.assert_called_once_with(transport="stdio")


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
    retriever.search.return_value = []

    with (
        patch.object(tools, "get_config", return_value=config),
        patch(
            "src.ai.openai_client.OpenAIClient",
            return_value=embedder,
        ) as embedder_type,
        patch(retriever_path, return_value=retriever) as retriever_type,
    ):
        result = await tools.search_knowledge(
            query="offline",
            strategy=strategy,
            top_k=3,
        )

    assert result == {
        "total": 0,
        "strategy_used": strategy,
        "results": [],
    }
    embedder_type.assert_called_once_with(config)
    retriever_type.assert_called_once_with(
        config.db_path,
        config.vector_index_dir,
        embedder,
    )
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
async def test_archive_url_degrades_on_workflow_exception():
    with patch(
        "src.workflow.engine.WorkflowEngine",
        side_effect=RuntimeError("workflow unavailable"),
    ):
        result = await tools.archive_url("https://example.com/offline")

    assert result == {
        "success": False,
        "error": "归档异常",
    }


def _archive_entry():
    return SimpleNamespace(
        title="Offline title",
        content="Offline content",
        tags=["offline"],
    )


@pytest.mark.asyncio
async def test_archive_text_reports_workflow_failure():
    processor = MagicMock()
    processor.process = AsyncMock(return_value=_archive_entry())
    workflow = MagicMock()
    workflow.execute_async = AsyncMock(
        return_value=SimpleNamespace(
            success=False,
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

    assert result == {"success": False, "error": "归档失败"}


@pytest.mark.asyncio
async def test_archive_text_degrades_on_processor_exception():
    processor = MagicMock()
    processor.process = AsyncMock(side_effect=RuntimeError("parse failed"))

    with patch(
        "src.processors.text_fallback_processor.TextFallbackProcessor",
        return_value=processor,
    ):
        result = await tools.archive_text("Offline content")

    assert result == {
        "success": False,
        "error": "归档异常",
    }


@pytest.mark.asyncio
async def test_get_related_stops_after_requested_limit():
    sqlite_store = MagicMock()
    sqlite_store.query_by_id.side_effect = [
        {"knowledge_id": 1, "title": "Seed"},
        {
            "knowledge_id": 2,
            "title": "Related",
            "tags": "offline",
        },
    ]
    vector_store = MagicMock()
    vector_store.get_doc_vector.return_value = [0.1, 0.2]
    vector_store.search_doc.return_value = [
        (1, 0.0),
        (2, 0.1),
        (3, 0.2),
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
async def test_relation_tools_translate_service_value_errors(
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

    assert result == {"error": "invalid offline request"}


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
    result_object = MagicMock()
    result_object.to_dict.return_value = {
        "knowledge_id": 1,
        "edges": [
            {
                "evidence_payload": {
                    "source_file_path": r"C:\\Users\\audit\\secret.md",
                    "target_file_path": r"\\\\server\\private\\target.md",
                    "source_url": "file:///C:/Users/audit/source.md",
                }
            }
        ],
    }
    service = MagicMock()
    getattr(service, service_method).return_value = result_object

    with patch.object(tools, getter_name, return_value=service):
        result = await getattr(tools, handler_name)(**kwargs)

    payload = result["edges"][0]["evidence_payload"]
    assert "source_file_path" not in payload
    assert "target_file_path" not in payload
    assert payload["source_url"] == ""


@pytest.mark.asyncio
async def test_contrast_rejects_empty_first_topic():
    assert await tools.contrast(topic_a=" ", topic_b="B") == {
        "error": "topic_a 不能为空"
    }


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
        with pytest.raises(ValueError, match="条目内容不可用"):
            await resources.get_entry_content("1")

        markdown_store.load.side_effect = RuntimeError("decode failed")
        with pytest.raises(ValueError, match="条目内容不可用") as exc_info:
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
        "urlparse",
        side_effect=RuntimeError("parser failed"),
    ):
        assert utils.validate_url("https://example.com") == (
            False,
            "URL 解析失败: https://example.com",
        )


def test_python_module_entrypoint_delegates_to_server_main():
    with patch.object(server, "main") as main:
        runpy.run_module("src.mcp.__main__", run_name="__main__")

    main.assert_called_once_with()
