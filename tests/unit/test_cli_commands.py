"""
Unit tests for CLI commands.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, List, Optional

import pytest
import yaml
from click.testing import CliRunner
from rich.panel import Panel
from rich.table import Table

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
        self.local_config_path = base_path / "config" / "local.yaml"
        self.llm_api_key: Optional[str] = None
        self.embd_api_key: Optional[str] = None
        self.log_level = "INFO"
        self.llm_base_url = "https://api.deepseek.com/v1"
        self.llm_model = "deepseek-chat"
        self.embd_base_url = "https://api.openai.com/v1"
        self.embd_model = "text-embedding-3-small"
        self.embedding_dim = 1536
        self.embedding_dim_is_auto = False
        self._values = {
            "storage.vault_dir": str(self.vault_dir),
        }

    def get(self, key: str, default: Any = None) -> Any:
        """Return a configuration value."""
        return self._values.get(key, default)

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


def test_archive_command_committed_failure_warns_do_not_retry(
    runner: CliRunner,
    mocker: pytest.MockFixture,
    load_config_stub,
    console_spy,
) -> None:
    """Committed-needs-repair failure exposes status/operation_id/repair/do-not-retry."""
    url = "https://example.com/committed"
    result = WorkflowResult(
        success=False,
        data={
            "knowledge_id": 7,
            "status": "repair_required",
            "operation_id": "op-9",
            "core_committed": True,
            "do_not_retry": True,
            "repair_actions": ["repair_operation_journal"],
        },
        errors=["storage_repair_required: 核心存储已提交但操作日志更新失败"],
        logs=[],
    )

    engine = mocker.MagicMock()
    mocker.patch.object(commands, "WorkflowEngine", return_value=engine)
    mocker.patch.object(commands.asyncio, "run", return_value=result)

    response = runner.invoke(commands.cli, ["archive", url])

    assert response.exit_code != 0
    printed = "\n".join(_printed_strings(console_spy))
    assert "op-9" in printed
    assert "repair_required" in printed
    assert "repair_operation_journal" in printed
    assert "请勿盲目重试" in printed


def test_archive_command_success_degraded_warns_repair(
    runner: CliRunner,
    mocker: pytest.MockFixture,
    load_config_stub,
    console_spy,
) -> None:
    """DEGRADED success exposes operation_id and visible repair guidance."""
    url = "https://example.com/degraded"
    entry = _make_entry(title="Degraded", source_url=url)
    result = WorkflowResult(
        success=True,
        data={
            "knowledge_id": 3,
            "entry": entry,
            "status": "degraded",
            "operation_id": "op-deg-1",
            "repair_actions": ["rebuild_vectors_for_entry"],
            "core_committed": True,
            "do_not_retry": True,
        },
        errors=[],
        logs=[],
    )

    engine = mocker.MagicMock()
    mocker.patch.object(commands, "WorkflowEngine", return_value=engine)
    mocker.patch.object(commands.asyncio, "run", return_value=result)

    response = runner.invoke(commands.cli, ["archive", url])

    assert response.exit_code == 0
    printed = "\n".join(_printed_strings(console_spy))
    assert "op-deg-1" in printed
    assert "辅助索引需要修复" in printed
    assert "rebuild_vectors_for_entry" in printed
    assert "请勿盲目重试" in printed


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
    md_path = tmp_path / "vault" / "entry.md"
    md_path.parent.mkdir(parents=True)
    md_path.write_text(content, encoding="utf-8")

    # SQLite persists a Vault-relative path; the gateway resolves and validates it.
    entry = {"knowledge_id": 9, "file_path": "entry.md"}
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


def test_config_show_reports_unset_api_keys(
    runner: CliRunner,
    load_config_stub,
    mock_config: DummyConfig,
    console_spy,
) -> None:
    """config show should not turn an unset key into an "already set" marker."""
    mock_config.llm_api_key = ""
    mock_config.embd_api_key = None

    response = runner.invoke(commands.cli, ["config", "show"])

    assert response.exit_code == 0
    table = next(
        call.args[0]
        for call in console_spy.call_args_list
        if call.args and isinstance(call.args[0], Table)
    )
    displayed = dict(zip(table.columns[0].cells, table.columns[1].cells))
    assert displayed["ai.llm.api_key"] == "未设置"
    assert displayed["ai.embedding.api_key"] == "未设置"


def test_config_show_redacts_endpoint_credentials(
    runner: CliRunner,
    load_config_stub,
    mock_config: DummyConfig,
    console_spy,
) -> None:
    """config show 不得显示 endpoint 中的 userinfo 或认证参数。"""
    mock_config.llm_base_url = (
        "https://display-user:display-password@llm.example/v1"
        "?access_token=display-query#callback?auth=display-fragment"
    )

    response = runner.invoke(commands.cli, ["config", "show"])

    assert response.exit_code == 0
    table = next(
        call.args[0]
        for call in console_spy.call_args_list
        if call.args and isinstance(call.args[0], Table)
    )
    displayed_values = "\n".join(str(value) for value in table.columns[1].cells)
    assert "llm.example" in displayed_values
    assert "已隐藏" in displayed_values
    assert "display-user" not in displayed_values
    assert "display-password" not in displayed_values
    assert "display-query" not in displayed_values
    assert "display-fragment" not in displayed_values


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


def test_config_get_redacts_credentials_from_arbitrary_url_value(
    runner: CliRunner,
    load_config_stub,
    mock_config: DummyConfig,
    console_spy,
) -> None:
    """任意配置叶子中的 URL 凭据都应遮罩，普通参数应保留。"""
    mock_config._values["service.endpoint"] = (
        "https://get-user:get-password@example.com/v1"
        "?region=cn&basicAuth=get-query"
        "#callback?code=get-fragment&signal=visible-signal&design=visible-design"
    )

    response = runner.invoke(commands.cli, ["config", "get", "service.endpoint"])

    assert response.exit_code == 0
    printed = "\n".join(_printed_strings(console_spy))
    assert "example.com" in printed
    assert "visible-signal" in printed
    assert "visible-design" in printed
    assert "get-user" not in printed
    assert "get-password" not in printed
    assert "get-query" not in printed
    assert "get-fragment" not in printed


def test_config_get_redacts_sensitive_values_in_parent_mapping(
    runner: CliRunner,
    load_config_stub,
    mock_config: DummyConfig,
    console_spy,
) -> None:
    """查询父级配置树时也不得打印嵌套密钥。"""
    mock_config._values["ai"] = {
        "llm": {"api_key": "test-secret", "model": "test-model"},
        "embedding": {"api_key": "", "dim": "auto"},
    }

    response = runner.invoke(commands.cli, ["config", "get", "ai"])

    assert response.exit_code == 0
    printed = "\n".join(_printed_strings(console_spy))
    assert "test-secret" not in printed
    assert "'api_key': '已设置'" in printed
    assert "'api_key': '未设置'" in printed
    assert "test-model" in printed


def test_config_get_redacts_descendants_of_sensitive_path(
    runner: CliRunner,
    load_config_stub,
    mock_config: DummyConfig,
    console_spy,
) -> None:
    """异常嵌套在敏感路径下的值也不得被直接打印。"""
    mock_config._values["ai.llm.api_key.value"] = "nested-secret"

    response = runner.invoke(
        commands.cli, ["config", "get", "ai.llm.api_key.value"]
    )

    assert response.exit_code == 0
    printed = "\n".join(_printed_strings(console_spy))
    assert "nested-secret" not in printed
    assert "已设置" in printed


@pytest.mark.parametrize(
    "key",
    [
        "service.openai_api_key",
        "service.clientSecret",
        "service.accessToken",
        "service.refreshToken",
        "service.APIKey",
        "service.clientCredentials",
        "service.JSESSIONID",
        "service.jwt",
        "service.pass",
        "service.passcode",
        "service.passphrase",
        "service.passwd",
        "service.pwd",
        "service.sessionId",
        "service.signature",
        "service.subscriptionKey",
        "service.requestSig",
    ],
)
def test_config_get_redacts_generic_secret_leaf(
    key: str,
    runner: CliRunner,
    load_config_stub,
    mock_config: DummyConfig,
    console_spy,
) -> None:
    """未来新增的常见敏感叶名默认按敏感值处理。"""
    mock_config._values[key] = "future-secret"

    response = runner.invoke(commands.cli, ["config", "get", key])

    assert response.exit_code == 0
    printed = "\n".join(_printed_strings(console_spy))
    assert "future-secret" not in printed
    assert "已设置" in printed


def test_sensitive_short_sig_marker_requires_identifier_boundary() -> None:
    """短标记 sig 只匹配独立键片段，不误伤 signal/design 等普通字段。"""
    assert commands._config_key_touches_sensitive_value("service.requestSig")
    assert commands._config_key_touches_sensitive_value("service.sigV4")
    assert not commands._config_key_touches_sensitive_value("service.signal")
    assert not commands._config_key_touches_sensitive_value("service.design")


@pytest.mark.parametrize(
    "key",
    [
        "service.pass",
        "service.dbPass",
        "service.passcode",
        "service.passwd",
        "service.userPwd",
    ],
)
def test_password_aliases_are_sensitive_identifier_parts(key: str) -> None:
    assert commands._config_key_touches_sensitive_value(key)


@pytest.mark.parametrize(
    "key",
    ["service.compass", "service.bypass", "service.compassMode", "service.bypassMode"],
)
def test_password_aliases_require_identifier_boundaries(key: str) -> None:
    assert not commands._config_key_touches_sensitive_value(key)


@pytest.mark.parametrize(
    "key",
    [
        "service.auth",
        "service.proxyAuthHeader",
        "service.basicAuth",
        "service.httpBasicAuthMode",
        "service.bearer",
        "service.bearerHeader",
    ],
)
def test_auth_markers_are_sensitive_identifier_parts(key: str) -> None:
    """auth/basicAuth/bearer 按标识符边界识别为敏感键。"""
    assert commands._config_key_touches_sensitive_value(key)


@pytest.mark.parametrize("key", ["service.signal", "service.design"])
def test_sensitive_markers_do_not_match_unrelated_words(key: str) -> None:
    assert not commands._config_key_touches_sensitive_value(key)


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
    load_config_stub,
    mock_config: DummyConfig,
    console_spy,
) -> None:
    """config set should update the machine-local YAML file."""
    response = runner.invoke(
        commands.cli, ["config", "set", "ai.llm.model", "test-model"]
    )

    assert response.exit_code == 0
    local_path = mock_config.local_config_path
    assert local_path.exists()
    data = yaml.safe_load(local_path.read_text(encoding="utf-8"))
    assert data["ai"]["llm"]["model"] == "test-model"


@pytest.mark.parametrize("subcommand", ["get", "set"])
@pytest.mark.parametrize(
    ("legacy_key", "replacement"),
    [
        ("PKV_LLM_MODEL", "ai.llm.model"),
        ("DEEPSEEK_API_KEY", "ai.llm.api_key"),
        ("OPENAI_API_KEY", "ai.embedding.api_key"),
    ],
)
def test_config_commands_reject_legacy_provider_keys(
    subcommand: str,
    legacy_key: str,
    replacement: str,
    runner: CliRunner,
    mocker: pytest.MockFixture,
    load_config_stub,
    console_spy,
    tmp_path: Path,
) -> None:
    """旧 Provider 环境变量式键应失败并提示 YAML 点号键。"""
    mocker.patch.object(commands, "_project_root", return_value=tmp_path)
    args = ["config", subcommand, legacy_key]
    if subcommand == "set":
        args.append("legacy-model")

    response = runner.invoke(commands.cli, args)

    assert response.exit_code != 0
    printed = "\n".join(_printed_strings(console_spy))
    assert f"旧配置键 {legacy_key} 已移除" in printed
    assert replacement in printed
    assert not (tmp_path / "config" / "local.yaml").exists()


def test_config_set_rejects_non_dotted_key(
    runner: CliRunner,
    mocker: pytest.MockFixture,
    console_spy,
    tmp_path: Path,
) -> None:
    """YAML 配置写入必须使用点号路径，避免制造无效顶层键。"""
    mocker.patch.object(commands, "_project_root", return_value=tmp_path)

    response = runner.invoke(commands.cli, ["config", "set", "TEST_KEY", "value"])

    assert response.exit_code != 0
    printed = "\n".join(_printed_strings(console_spy))
    assert "配置键必须使用 YAML 点号路径" in printed
    assert not (tmp_path / "config" / "local.yaml").exists()


def test_config_set_invalid_yaml_does_not_echo_value(
    runner: CliRunner,
    mocker: pytest.MockFixture,
    console_spy,
    tmp_path: Path,
) -> None:
    """YAML 解析错误不得把命令行原值带入错误输出。"""
    mocker.patch.object(commands, "_project_root", return_value=tmp_path)
    sentinel = "do-not-echo-this-value"

    response = runner.invoke(
        commands.cli,
        ["config", "set", "service.endpoint", f"[{sentinel}"],
    )

    assert response.exit_code != 0
    printed = "\n".join(_printed_strings(console_spy))
    assert "配置值不是有效的 YAML" in printed
    assert sentinel not in printed
    assert not (tmp_path / "config" / "local.yaml").exists()


@pytest.mark.parametrize(
    "structured_value",
    [
        "{api_key: do-not-persist-this-secret}",
        "{nested: {password: do-not-persist-this-secret}}",
        "[safe-value, do-not-persist-this-secret]",
    ],
)
def test_config_set_rejects_structured_yaml_values(
    structured_value: str,
    runner: CliRunner,
    mocker: pytest.MockFixture,
    console_spy,
    tmp_path: Path,
) -> None:
    """结构化 YAML 不得绕过路径检查，把嵌套敏感值写入本机配置。"""
    mocker.patch.object(commands, "_project_root", return_value=tmp_path)

    response = runner.invoke(
        commands.cli,
        ["config", "set", "service.settings", structured_value],
    )

    assert response.exit_code != 0
    printed = "\n".join(_printed_strings(console_spy))
    assert "config set 仅支持标量值" in printed
    assert "do-not-persist-this-secret" not in printed
    assert not (tmp_path / "config" / "local.yaml").exists()


@pytest.mark.parametrize(
    "key",
    [
        "ai",
        "ai.llm",
        "ai.llm.api_key",
        "ai.embedding",
        "ai.embedding.api_key",
        "processors",
        "processors.zhihu",
        "processors.zhihu.cookie",
        "service.client_secret",
        "service.auth_token",
        "service.openai_api_key",
        "service.password",
        "service.clientSecret",
        "service.accessToken",
        "service.refreshToken",
        "service.APIKey",
        "service.clientCredentials",
        "service.JSESSIONID",
        "service.jwt",
        "service.pass",
        "service.passcode",
        "service.passphrase",
        "service.passwd",
        "service.pwd",
        "service.sessionId",
        "service.signature",
        "service.subscriptionKey",
        "service.requestSig",
    ],
)
def test_config_set_rejects_sensitive_values_on_command_line(
    key: str,
    runner: CliRunner,
    mocker: pytest.MockFixture,
    console_spy,
    tmp_path: Path,
) -> None:
    """敏感值只能直接编辑 local.yaml，避免进入终端历史和进程参数。"""
    mocker.patch.object(commands, "_project_root", return_value=tmp_path)

    response = runner.invoke(commands.cli, ["config", "set", key, "test-secret"])

    assert response.exit_code != 0
    printed = "\n".join(_printed_strings(console_spy))
    assert "配置路径包含敏感值" in printed
    assert "test-secret" not in printed
    assert not (tmp_path / "config" / "local.yaml").exists()


@pytest.mark.parametrize(
    ("key", "endpoint"),
    [
        ("ai.llm.base_url", "https://set-user:set-password@example.com/v1"),
        ("ai.embedding.base_url", "https://example.com/v1?api_key=set-query"),
        ("service.baseUrl", "https://example.com/v1#callback?auth=set-fragment"),
        ("service.base_url", "https://example.com/callback#code=set-fragment"),
        ("service.base_url", "https://example.com/v1;pass=set-matrix"),
        ("service.base_url", "https://example.com/v1?passwd=set-passwd"),
        ("service.base_url", "https://example.com/v1#pwd=set-pwd"),
        ("service.base_url", "https://example.com/v1;JSESSIONID=set-session"),
        ("service.base_url", "https://example.com/v1?jwt=set-jwt"),
        ("service.base_url", "https://example.com/v1#session-id=set-session-id"),
        (
            "service.base_url",
            "https://example.com/v1?subscription-key=set-subscription",
        ),
    ],
)
def test_config_set_rejects_credentials_embedded_in_base_url(
    key: str,
    endpoint: str,
    runner: CliRunner,
    mocker: pytest.MockFixture,
    console_spy,
    tmp_path: Path,
) -> None:
    """Base URL 内嵌凭据时必须改为直接编辑私有 YAML。"""
    mocker.patch.object(commands, "_project_root", return_value=tmp_path)

    response = runner.invoke(commands.cli, ["config", "set", key, endpoint])

    assert response.exit_code != 0
    printed = "\n".join(_printed_strings(console_spy))
    assert "Base URL 不得通过命令行传入认证信息" in printed
    for sentinel in (
        "set-user",
        "set-password",
        "set-query",
        "set-fragment",
        "set-matrix",
        "set-passwd",
        "set-pwd",
        "set-session",
        "set-jwt",
        "set-session-id",
        "set-subscription",
    ):
        assert sentinel not in printed
    assert not (tmp_path / "config" / "local.yaml").exists()


def test_config_set_allows_non_sensitive_base_url_parameters(
    runner: CliRunner,
    load_config_stub,
    mock_config: DummyConfig,
) -> None:
    """signal/design 等普通 query 参数不应误报为凭据。"""
    endpoint = (
        "https://example.com/v1;compass=north"
        "?signal=enabled&design=compact&bypass=fast"
        "&region_code=north&routing_key=primary"
    )

    response = runner.invoke(
        commands.cli, ["config", "set", "service.base_url", endpoint]
    )

    assert response.exit_code == 0
    data = yaml.safe_load(
        mock_config.local_config_path.read_text(encoding="utf-8")
    )
    assert data["service"]["base_url"] == endpoint


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
