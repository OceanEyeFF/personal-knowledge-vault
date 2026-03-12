"""
Unit tests for CLI commands.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest
from click.testing import CliRunner
from rich.panel import Panel
from rich.table import Table

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import src.cli.commands as commands
from src.retrieval.result import SearchResult
from src.workflow.models import WorkflowResult


class DummyConfig:
    """Lightweight config stub for CLI tests."""

    def __init__(self, base_path: Path) -> None:
        self.data_dir = base_path / ".data"
        self.vault_dir = base_path / "vault"
        self.db_path = base_path / "db" / "vault.db"
        self.vector_index_dir = base_path / "vectors"
        self.log_dir = base_path / "logs"
        self.tmp_dir = base_path / "tmp"
        self.deepseek_api_key: Optional[str] = None
        self.openai_api_key: Optional[str] = None
        self.log_level = "INFO"
        self._values = {
            "ai.deepseek.model": "deepseek-chat",
            "ai.openai.embedding_model": "text-embedding-3-small",
            "storage.vault_dir": str(self.vault_dir),
        }
        self._env: Dict[str, str] = {}

    def get(self, key: str, default: Any = None) -> Any:
        """Return a configuration value."""
        return self._values.get(key, default)

    def get_env(self, key: str, default: Optional[str] = None) -> Optional[str]:
        """Return a mocked environment value."""
        return self._env.get(key, default)


class DummyStatus:
    """Minimal context manager for console.status."""

    def __enter__(self) -> "DummyStatus":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False


def _make_entry(
    title: str = "Sample",
    source_url: str = "https://example.com",
    tags: Optional[List[str]] = None,
    summary: str = "summary text",
) -> SimpleNamespace:
    return SimpleNamespace(
        title=title,
        source_url=source_url,
        tags=tags or ["tag-a"],
        summary_100_words=summary,
    )


def _make_search_results() -> List[SearchResult]:
    return [
        SearchResult(
            knowledge_id=1,
            title="Alpha",
            score=0.91,
            highlight="alpha",
            metadata={"tags": ["alpha"]},
        ),
        SearchResult(
            knowledge_id=2,
            title="Beta",
            score=0.72,
            highlight="beta",
            metadata={"tags": ["beta"]},
        ),
    ]


def _printed_strings(console_spy) -> List[str]:
    texts: List[str] = []
    for call in console_spy.call_args_list:
        if not call.args:
            continue
        for value in call.args:
            if isinstance(value, str):
                texts.append(value)
    return texts


@pytest.fixture
def runner() -> CliRunner:
    """Return a Click CLI runner."""
    return CliRunner()


@pytest.fixture
def mock_config(tmp_path: Path) -> DummyConfig:
    """Provide a dummy config instance."""
    return DummyConfig(tmp_path)


@pytest.fixture
def load_config_stub(mocker: pytest.MockFixture, mock_config: DummyConfig):
    """Patch CLI config loader to return the dummy config."""
    return mocker.patch.object(commands, "_load_config", return_value=mock_config)


@pytest.fixture
def console_spy(mocker: pytest.MockFixture):
    """Patch console.print and console.status for assertions."""
    mocker.patch.object(commands.console, "status", return_value=DummyStatus())
    return mocker.patch.object(commands.console, "print")


def test_archive_command_success(
    runner: CliRunner,
    mocker: pytest.MockFixture,
    load_config_stub,
    console_spy,
) -> None:
    """archive should run workflow and print a success panel."""
    url = "https://example.com/article"
    entry = _make_entry(title="CLI Test", source_url=url)
    result = WorkflowResult(
        success=True,
        data={"knowledge_id": 42, "file_path": "vault/test.md", "entry": entry},
        errors=[],
        logs=["ok"],
    )

    engine = mocker.MagicMock()
    mocker.patch.object(commands, "WorkflowEngine", return_value=engine)
    mocker.patch.object(commands.asyncio, "run", return_value=result)

    response = runner.invoke(commands.cli, ["archive", url])

    assert response.exit_code == 0
    engine.execute_async.assert_called_once()
    args, _ = engine.execute_async.call_args
    assert args[0] == "archive-url"
    assert args[1]["url"] == url
    assert args[1]["skip_sharpen"] is False
    assert "manual_tags" not in args[1]
    assert any(
        isinstance(call.args[0], Panel)
        for call in console_spy.call_args_list
        if call.args
    )


def test_archive_command_with_skip_sharpen(
    runner: CliRunner,
    mocker: pytest.MockFixture,
    load_config_stub,
    console_spy,
) -> None:
    """archive should pass skip_sharpen when flag is provided."""
    url = "https://example.com/skip"
    result = WorkflowResult(success=True, data={"knowledge_id": 7, "file_path": "x"})

    engine = mocker.MagicMock()
    mocker.patch.object(commands, "WorkflowEngine", return_value=engine)
    mocker.patch.object(commands.asyncio, "run", return_value=result)

    response = runner.invoke(commands.cli, ["archive", url, "--skip-sharpen"])

    assert response.exit_code == 0
    args, _ = engine.execute_async.call_args
    assert args[1]["skip_sharpen"] is True


def test_archive_command_with_manual_tags(
    runner: CliRunner,
    mocker: pytest.MockFixture,
    load_config_stub,
    console_spy,
) -> None:
    """archive should forward manual tags to workflow input."""
    url = "https://example.com/tags"
    result = WorkflowResult(success=True, data={"knowledge_id": 9, "file_path": "x"})

    engine = mocker.MagicMock()
    mocker.patch.object(commands, "WorkflowEngine", return_value=engine)
    mocker.patch.object(commands.asyncio, "run", return_value=result)

    response = runner.invoke(
        commands.cli, ["archive", url, "--tags", "alpha, beta, gamma"]
    )

    assert response.exit_code == 0
    args, _ = engine.execute_async.call_args
    assert args[1]["manual_tags"] == ["alpha", "beta", "gamma"]


def test_archive_command_failure(
    runner: CliRunner,
    mocker: pytest.MockFixture,
    load_config_stub,
    console_spy,
) -> None:
    """archive should exit non-zero on workflow failure."""
    url = "https://example.com/fail"
    result = WorkflowResult(success=False, data={}, errors=["boom"], logs=[])

    engine = mocker.MagicMock()
    mocker.patch.object(commands, "WorkflowEngine", return_value=engine)
    mocker.patch.object(commands.asyncio, "run", return_value=result)

    response = runner.invoke(commands.cli, ["archive", url])

    assert response.exit_code != 0
    engine.execute_async.assert_called_once()
    assert console_spy.call_count >= 1


def test_search_command_auto_strategy(
    runner: CliRunner,
    mocker: pytest.MockFixture,
    load_config_stub,
    mock_config: DummyConfig,
    console_spy,
) -> None:
    """search should use QueryRouter when strategy is auto."""
    results = _make_search_results()
    router = mocker.MagicMock()
    router.search.return_value = results
    mocker.patch.object(commands, "QueryRouter", return_value=router)
    mocker.patch.object(commands, "Embedder", return_value=mocker.MagicMock())
    mocker.patch.object(commands, "VectorRetriever", return_value=mocker.MagicMock())

    text_processor = mocker.MagicMock()
    text_processor.tokenize_chinese.return_value = "a b"
    mocker.patch.object(commands, "TextProcessor", return_value=text_processor)

    response = runner.invoke(commands.cli, ["search", "hello world", "--limit", "3"])

    assert response.exit_code == 0
    commands.QueryRouter.assert_called_once_with(
        db_path=mock_config.db_path,
        vector_index_dir=mock_config.vector_index_dir,
        embedder=mocker.ANY,
        token_threshold=10,
    )
    router.search.assert_called_once_with("hello world", 3)
    assert any("bm25" in text for text in _printed_strings(console_spy))


def test_search_command_bm25_strategy(
    runner: CliRunner,
    mocker: pytest.MockFixture,
    load_config_stub,
    mock_config: DummyConfig,
    console_spy,
) -> None:
    """search should use BM25 retriever when strategy is bm25."""
    results = _make_search_results()
    retriever = mocker.MagicMock()
    retriever.search.return_value = results
    mocker.patch.object(commands, "BM25Retriever", return_value=retriever)
    mocker.patch.object(commands, "QueryRouter", return_value=mocker.MagicMock())

    response = runner.invoke(
        commands.cli, ["search", "keyword", "--strategy", "bm25", "--limit", "2"]
    )

    assert response.exit_code == 0
    commands.BM25Retriever.assert_called_once_with(mock_config.db_path)
    retriever.search.assert_called_once_with("keyword", 2)


def test_search_command_vector_strategy(
    runner: CliRunner,
    mocker: pytest.MockFixture,
    load_config_stub,
    mock_config: DummyConfig,
    console_spy,
) -> None:
    """search should use vector retriever when strategy is vector."""
    results = _make_search_results()
    retriever = mocker.MagicMock()
    retriever.search.return_value = results
    mocker.patch.object(commands, "VectorRetriever", return_value=retriever)
    mocker.patch.object(commands, "Embedder", return_value=mocker.MagicMock())

    response = runner.invoke(
        commands.cli, ["search", "semantic", "--strategy", "vector", "--limit", "4"]
    )

    assert response.exit_code == 0
    commands.VectorRetriever.assert_called_once_with(
        mock_config.db_path, mock_config.vector_index_dir, mocker.ANY
    )
    retriever.search.assert_called_once_with("semantic", 4)


def test_search_command_json_output(
    runner: CliRunner,
    mocker: pytest.MockFixture,
    load_config_stub,
    mock_config: DummyConfig,
    console_spy,
) -> None:
    """search should emit JSON when output format is json."""
    results = _make_search_results()
    retriever = mocker.MagicMock()
    retriever.search.return_value = results
    mocker.patch.object(commands, "BM25Retriever", return_value=retriever)

    response = runner.invoke(
        commands.cli,
        ["search", "query", "--strategy", "bm25", "--format", "json", "--limit", "5"],
    )

    assert response.exit_code == 0
    payload_text = response.output
    payload = json.loads(payload_text)
    assert payload["query"] == "query"
    assert payload["strategy"] == "bm25"
    assert payload["total"] == len(results)
    assert payload["results"][0]["entry_id"] == 1


def test_show_command_by_id(
    runner: CliRunner,
    mocker: pytest.MockFixture,
    load_config_stub,
    console_spy,
) -> None:
    """show should look up an entry by ID."""
    entry = {"knowledge_id": 3, "title": "Entry", "file_path": "x"}
    store = mocker.MagicMock()
    store.query_by_id.return_value = entry
    mocker.patch.object(commands, "SQLiteStore", return_value=store)

    response = runner.invoke(commands.cli, ["show", "3"])

    assert response.exit_code == 0
    store.query_by_id.assert_called_once_with(3)
    assert any(
        isinstance(call.args[0], Panel)
        for call in console_spy.call_args_list
        if call.args
    )


def test_show_command_by_url(
    runner: CliRunner,
    mocker: pytest.MockFixture,
    load_config_stub,
    console_spy,
) -> None:
    """show should look up an entry by URL."""
    entry = {"knowledge_id": 4, "title": "Entry", "file_path": "x"}
    store = mocker.MagicMock()
    store.query_by_url.return_value = entry
    mocker.patch.object(commands, "SQLiteStore", return_value=store)

    response = runner.invoke(
        commands.cli, ["show", "--url", "https://example.com/post"]
    )

    assert response.exit_code == 0
    store.query_by_url.assert_called_once_with("https://example.com/post")


def test_show_command_raw(
    runner: CliRunner,
    mocker: pytest.MockFixture,
    load_config_stub,
    console_spy,
    tmp_path: Path,
) -> None:
    """show should emit raw markdown when --raw is used."""
    content = "# Raw Content\n\nBody"
    md_path = tmp_path / "entry.md"
    md_path.write_text(content, encoding="utf-8")

    entry = {"knowledge_id": 9, "file_path": str(md_path)}
    store = mocker.MagicMock()
    store.query_by_id.return_value = entry
    mocker.patch.object(commands, "SQLiteStore", return_value=store)

    response = runner.invoke(commands.cli, ["show", "9", "--raw"])

    assert response.exit_code == 0
    assert any(
        call.args and call.args[0] == content and call.kwargs.get("markup") is False
        for call in console_spy.call_args_list
    )


def test_list_command(
    runner: CliRunner,
    mocker: pytest.MockFixture,
    load_config_stub,
    console_spy,
) -> None:
    """list should render a table with entries."""
    rows = [
        {
            "knowledge_id": 1,
            "title": "First",
            "source_type": "webpage",
            "source_url": "https://example.com",
            "tags": "a,b",
            "archived_at": "2026-02-16",
        }
    ]

    store = mocker.MagicMock()
    store.list_entries.return_value = rows
    mocker.patch.object(commands, "SQLiteStore", return_value=store)

    response = runner.invoke(commands.cli, ["list"])

    assert response.exit_code == 0
    store.list_entries.assert_called_once_with(
        limit=20, sort_by="archived_at", sort_order="asc", tag=None
    )
    assert any(
        isinstance(call.args[0], Table)
        for call in console_spy.call_args_list
        if call.args
    )


def test_list_command_with_tag_filter(
    runner: CliRunner,
    mocker: pytest.MockFixture,
    load_config_stub,
    console_spy,
) -> None:
    """list should forward tag filters to the store."""
    rows = [
        {
            "knowledge_id": 2,
            "title": "Second",
            "source_type": "webpage",
            "source_url": "https://example.com",
            "tags": "ai",
            "archived_at": "2026-02-16",
        }
    ]

    store = mocker.MagicMock()
    store.list_entries.return_value = rows
    mocker.patch.object(commands, "SQLiteStore", return_value=store)

    response = runner.invoke(
        commands.cli,
        ["list", "--tag", "ai", "--sort", "title", "--desc", "--limit", "5"],
    )

    assert response.exit_code == 0
    store.list_entries.assert_called_once_with(
        limit=5, sort_by="title", sort_order="desc", tag="ai"
    )


def test_config_show(
    runner: CliRunner,
    load_config_stub,
    console_spy,
) -> None:
    """config show should print a table."""
    response = runner.invoke(commands.cli, ["config", "show"])

    assert response.exit_code == 0
    assert any(
        isinstance(call.args[0], Table)
        for call in console_spy.call_args_list
        if call.args
    )


def test_config_get(
    runner: CliRunner,
    load_config_stub,
    mock_config: DummyConfig,
    console_spy,
) -> None:
    """config get should print the requested value."""
    mock_config._values["storage.vault_dir"] = "/tmp/vault"

    response = runner.invoke(commands.cli, ["config", "get", "storage.vault_dir"])

    assert response.exit_code == 0
    assert any("/tmp/vault" in text for text in _printed_strings(console_spy))


def test_config_get_alias_key(
    runner: CliRunner,
    load_config_stub,
    mock_config: DummyConfig,
    console_spy,
) -> None:
    """config get should support legacy alias keys."""
    response = runner.invoke(commands.cli, ["config", "get", "db_path"])

    assert response.exit_code == 0
    assert any("vault.db" in text for text in _printed_strings(console_spy))


def test_config_set(
    runner: CliRunner,
    mocker: pytest.MockFixture,
    console_spy,
    tmp_path: Path,
) -> None:
    """config set should update the .env file."""
    mocker.patch.object(commands, "_project_root", return_value=tmp_path)

    response = runner.invoke(commands.cli, ["config", "set", "TEST_KEY", "test-value"])

    assert response.exit_code == 0
    env_path = tmp_path / ".env"
    assert env_path.exists()
    assert "TEST_KEY=test-value" in env_path.read_text(encoding="utf-8")


def test_stats_command(
    runner: CliRunner,
    mocker: pytest.MockFixture,
    load_config_stub,
    mock_config: DummyConfig,
    console_spy,
) -> None:
    """stats should render a panel when data exists."""
    mock_config.db_path.parent.mkdir(parents=True, exist_ok=True)
    mock_config.db_path.write_text("", encoding="utf-8")

    store = mocker.MagicMock()
    store.table_exists.return_value = True
    store.count_entries.return_value = 3
    store.count_entries_by_source_type.return_value = [("webpage", 2), ("test", 1)]
    store.get_all_tags_with_count.return_value = [
        {"name": "alpha", "count": 2},
        {"name": "beta", "count": 1},
    ]
    mocker.patch.object(commands, "SQLiteStore", return_value=store)
    mocker.patch.object(commands, "_dir_size", side_effect=[1024, 2048, 4096])

    response = runner.invoke(commands.cli, ["stats"])

    assert response.exit_code == 0
    assert any(
        isinstance(call.args[0], Panel)
        for call in console_spy.call_args_list
        if call.args
    )
