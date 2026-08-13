"""
Unit tests for CLI commands.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, List, Optional

import pytest
import yaml
from click.testing import CliRunner
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

import src.cli.commands as commands
import src.workflow.steps as workflow_steps
from src.retrieval.result import RetrievalIssue, SearchResponse, SearchResult
from src.runtime.errors import ErrorCode, PKVRuntimeError
from src.storage.markdown_store import Entry
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
        self.llm_provider = "openai_compatible"
        self.embd_provider = "openai_compatible"
        self.log_level = "INFO"
        self.llm_base_url = "https://api.deepseek.com/v1"
        self.llm_model = "deepseek-chat"
        self.embd_base_url = "https://api.openai.com/v1"
        self.embd_model = "text-embedding-3-small"
        self.embedding_dim = 1536
        self.embedding_dim_is_auto = False
        self.embd_timeout_seconds = 30.0
        self.embd_max_retries = 2
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
) -> Entry:
    return Entry(
        title=title,
        source_type="generic",
        source_url=source_url,
        tags=tags or ["tag-a"],
        summary_100_words=summary,
        content="body",
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


def _completed_search(strategy: str = "bm25") -> SearchResponse:
    return SearchResponse.completed(_make_search_results(), strategy=strategy)


def _printed_strings(console_spy) -> List[str]:
    texts: List[str] = []
    for call in console_spy.call_args_list:
        if not call.args:
            continue
        for value in call.args:
            if isinstance(value, str):
                texts.append(value)
    return texts


def _public_cli_output(response, sink: io.StringIO) -> str:
    stderr = getattr(response, "stderr", "")
    return f"{sink.getvalue()}\n{response.output}\n{stderr}"


@pytest.mark.parametrize(
    ("args", "expected_code"),
    [
        (["show", "1"], "cli_show_failed"),
        (["list"], "cli_list_failed"),
        (["config", "show"], "cli_config_read_failed"),
        (["config", "get", "ai.llm.api_key"], "cli_config_get_failed"),
        (
            ["config", "set", "ai.llm.model", "safe-model"],
            "cli_config_set_failed",
        ),
        (["stats"], "cli_stats_failed"),
    ],
)
def test_cli_command_failures_are_stable_and_redacted(
    runner: CliRunner,
    mocker: pytest.MockFixture,
    console_spy,
    caplog,
    args: list[str],
    expected_code: str,
) -> None:
    """Public command failures must not echo paths, credentials, or exception text."""

    canary = (
        r"C:\private\vault.db"
        " https://user:pass@example.test/path?access_token=CLI-SECRET-CANARY"
    )
    mocker.patch.object(commands, "_load_config", side_effect=RuntimeError(canary))
    caplog.set_level("ERROR", logger="pkv.cli")

    response = runner.invoke(commands.cli, args)

    assert response.exit_code != 0
    public_output = "\n".join(_printed_strings(console_spy))
    combined = f"{public_output}\n{response.output}\n{caplog.text}"
    assert expected_code in combined
    assert "RuntimeError" in caplog.text
    assert "CLI-SECRET-CANARY" not in combined
    assert "user:pass" not in combined
    assert "private" not in combined


def _make_db_entry(**overrides: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "knowledge_id": 3,
        "title": "Entry",
        "source_type": "webpage",
        "source_url": "https://example.com/post",
        "file_path": "vault/entry.md",
        "archived_at": "2026-08-07 12:00:00",
        "tags": "",
        "keywords": "",
        "summary_100_words": "",
    }
    entry.update(overrides)
    return entry


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
        data={
            "knowledge_id": 42,
            "status": "ready",
            "core_committed": True,
            "file_path": "vault/test.md",
            "entry": entry,
        },
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


@pytest.mark.parametrize("use_absolute", [False, True], ids=["relative", "absolute"])
def test_archive_existing_local_file_receives_one_shot_capability(
    use_absolute: bool,
    runner: CliRunner,
    mocker: pytest.MockFixture,
    load_config_stub,
    console_spy,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    note = tmp_path / "note.md"
    note.write_text("# Imported note\nBody", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    source = str(note) if use_absolute else note.name
    result = WorkflowResult(
        success=True,
        data={
            "knowledge_id": 71,
            "status": "ready",
            "core_committed": True,
            "file_path": "note.md",
            "entry": _make_entry(source_url=source),
        },
    )
    engine = mocker.MagicMock()
    mocker.patch.object(commands, "WorkflowEngine", return_value=engine)
    mocker.patch.object(commands.asyncio, "run", return_value=result)

    response = runner.invoke(commands.cli, ["archive", source, "--quiet"])

    assert response.exit_code == 0
    payload = engine.execute_async.call_args.args[1]
    capability = payload[workflow_steps._CLI_LOCAL_FILE_IMPORT_KEY]
    assert payload["url"] == source
    assert type(capability) is tuple
    assert capability[0] is workflow_steps._CLI_LOCAL_FILE_IMPORT_TOKEN
    assert capability[1] == source
    assert capability is not True


@pytest.mark.parametrize(
    "source",
    [
        "https://example.com/note.md",
        "HTTPS://example.com/note.md",
        "file:///C:/private/note.md",
        r"\\server\share\note.md",
        "//server/share/note.md",
        "smb://server/share/note.md",
    ],
)
def test_archive_network_shapes_never_receive_local_file_capability_or_probe(
    source: str,
    runner: CliRunner,
    mocker: pytest.MockFixture,
    load_config_stub,
    console_spy,
) -> None:
    result = WorkflowResult(
        success=True,
        data={
            "knowledge_id": 72,
            "status": "ready",
            "core_committed": True,
            "file_path": "note.md",
            "entry": _make_entry(source_url=source),
        },
    )
    engine = mocker.MagicMock()
    mocker.patch.object(commands, "WorkflowEngine", return_value=engine)
    mocker.patch.object(commands.asyncio, "run", return_value=result)
    validate = mocker.patch.object(
        commands,
        "validate_path_components",
        side_effect=AssertionError("network-shaped input probed as local path"),
    )
    lstat = mocker.patch.object(
        commands.os,
        "lstat",
        side_effect=AssertionError("network-shaped input lstat'ed"),
    )

    response = runner.invoke(commands.cli, ["archive", source, "--quiet"])

    assert response.exit_code == 0
    payload = engine.execute_async.call_args.args[1]
    assert workflow_steps._CLI_LOCAL_FILE_IMPORT_KEY not in payload
    validate.assert_not_called()
    lstat.assert_not_called()


def test_archive_unsafe_local_file_state_fails_without_publishing_path(
    runner: CliRunner,
    mocker: pytest.MockFixture,
    load_config_stub,
    console_spy,
) -> None:
    source = r"C:\private\CLI-FILE-SECRET-CANARY\note.md"
    engine = mocker.MagicMock()
    mocker.patch.object(commands, "WorkflowEngine", return_value=engine)
    mocker.patch.object(
        commands,
        "validate_path_components",
        side_effect=PKVRuntimeError(
            ErrorCode.DATA_ROOT_UNSAFE,
            source,
        ),
    )

    response = runner.invoke(commands.cli, ["archive", source])

    assert response.exit_code != 0
    engine.execute_async.assert_not_called()
    published = "\n".join(_printed_strings(console_spy)) + response.output
    assert "cli_archive_local_file_unsafe" in published
    assert "CLI-FILE-SECRET-CANARY" not in published
    assert source not in published


def test_archive_command_with_skip_sharpen(
    runner: CliRunner,
    mocker: pytest.MockFixture,
    load_config_stub,
    console_spy,
) -> None:
    """archive should pass skip_sharpen when flag is provided."""
    url = "https://example.com/skip"
    result = WorkflowResult(
        success=True,
        data={
            "knowledge_id": 7,
            "status": "ready",
            "core_committed": True,
            "file_path": "x",
            "entry": _make_entry(source_url=url),
        },
    )

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
    result = WorkflowResult(
        success=True,
        data={
            "knowledge_id": 9,
            "status": "ready",
            "core_committed": True,
            "file_path": "x",
            "entry": _make_entry(source_url=url),
        },
    )

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
    printed = "\n".join(_printed_strings(console_spy))
    assert "boom" not in printed
    assert "workflow_step_failed" in printed
    assert "归档步骤未能完成" in printed


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
            "operation_id": "9" * 32,
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
    assert "9" * 32 in printed
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
            "operation_id": "d" * 32,
            "repair_actions": ["rebuild_vectors_for_entry"],
            "core_committed": True,
            "do_not_retry": True,
        },
        errors=[],
        logs=[],
        warnings=["向量索引写入失败，核心归档已提交"],
        issues=[
            {
                "code": "workflow_step_failed",
                "message": "向量索引不可用",
                "severity": "warning",
                "recoverable": True,
                "stage": "index",
            }
        ],
        terminal="degraded",
    )

    engine = mocker.MagicMock()
    mocker.patch.object(commands, "WorkflowEngine", return_value=engine)
    mocker.patch.object(commands.asyncio, "run", return_value=result)

    response = runner.invoke(commands.cli, ["archive", url])

    assert response.exit_code == 0
    printed = "\n".join(_printed_strings(console_spy))
    assert "d" * 32 in printed
    assert "degraded" in printed
    assert "归档步骤已降级" in printed
    assert "workflow_step_failed" in printed
    assert "stage=index" in printed
    assert "recoverable=true" in printed
    assert "向量索引不可用" not in printed
    assert "核心归档已提交" not in printed
    assert "rebuild_vectors_for_entry" in printed
    assert "请勿盲目重试" in printed


@pytest.mark.parametrize(
    "operation_id",
    [
        "op-sk-OPERATION-ID-SECRET-CANARY",
        "api_key_OPERATION-ID-SECRET-CANARY",
        "A" * 32,
        "f" * 31,
        True,
    ],
)
def test_archive_operation_id_only_publishes_lowercase_uuid_hex(
    operation_id: object,
    runner: CliRunner,
    mocker: pytest.MockFixture,
    load_config_stub,
    console_spy,
) -> None:
    result = WorkflowResult(
        success=False,
        data={
            "knowledge_id": 7,
            "status": "repair_required",
            "operation_id": operation_id,
            "core_committed": True,
            "do_not_retry": True,
            "repair_actions": ["repair_operation_journal"],
        },
        errors=["backend detail"],
    )
    engine = mocker.MagicMock()
    mocker.patch.object(commands, "WorkflowEngine", return_value=engine)
    mocker.patch.object(commands.asyncio, "run", return_value=result)

    response = runner.invoke(
        commands.cli,
        ["archive", "https://example.com/operation"],
    )

    assert response.exit_code != 0
    published = "\n".join(_printed_strings(console_spy)) + response.output
    assert "OPERATION-ID-SECRET-CANARY" not in published
    assert "api_key_" not in published
    assert "已隐藏" in published


def test_archive_quiet_degraded_still_exposes_warning(
    runner: CliRunner,
    mocker: pytest.MockFixture,
    load_config_stub,
    console_spy,
) -> None:
    result = WorkflowResult(
        success=True,
        data={
            "knowledge_id": 8,
            "status": "degraded",
            "core_committed": True,
            "entry": _make_entry(source_url="https://example.com/quiet"),
        },
        warnings=["辅助索引待修复"],
        issues=[
            {
                "code": "workflow_step_failed",
                "message": "辅助索引失败",
                "severity": "warning",
                "recoverable": True,
            }
        ],
        terminal="degraded",
    )
    engine = mocker.MagicMock()
    mocker.patch.object(commands, "WorkflowEngine", return_value=engine)
    mocker.patch.object(commands.asyncio, "run", return_value=result)

    response = runner.invoke(
        commands.cli,
        ["archive", "https://example.com/quiet", "--quiet"],
    )

    assert response.exit_code == 0
    input_data = engine.execute_async.call_args.args[1]
    assert input_data["skip_sharpen"] is True
    assert input_data["skip_review"] is True
    printed = "\n".join(_printed_strings(console_spy))
    assert "degraded" in printed
    assert "归档步骤已降级" in printed
    assert "workflow_step_failed" in printed
    assert "辅助索引待修复" not in printed
    assert "辅助索引失败" not in printed


@pytest.mark.parametrize(
    "variant",
    (
        "success_with_degraded_storage",
        "core_not_committed",
        "missing_core_committed",
        "missing_entry",
        "duck_typed_entry",
        "invalid_entry_tags",
    ),
)
def test_archive_rejects_incoherent_or_malformed_completed_projection_before_success(
    variant: str,
    runner: CliRunner,
    mocker: pytest.MockFixture,
    load_config_stub,
    console_spy,
) -> None:
    entry: object = _make_entry()
    terminal = "success"
    status = "ready"
    core_committed = True
    include_core_committed = True
    warnings: list[str] = []
    issues: list[dict[str, object]] = []

    if variant == "success_with_degraded_storage":
        status = "degraded"
    elif variant == "core_not_committed":
        core_committed = False
    elif variant == "missing_core_committed":
        include_core_committed = False
    elif variant == "missing_entry":
        entry = {}
    elif variant == "duck_typed_entry":
        entry = SimpleNamespace(
            title="title",
            source_url=None,
            tags=["tag"],
            summary_100_words="summary",
        )
    else:
        assert type(entry) is Entry
        entry.tags = [object()]

    data: dict[str, object] = {
        "knowledge_id": 42,
        "status": status,
        "entry": entry,
    }
    if include_core_committed:
        data["core_committed"] = core_committed
    result = WorkflowResult(
        success=True,
        terminal=terminal,
        data=data,
        warnings=warnings,
        issues=issues,
    )
    engine = mocker.MagicMock()
    mocker.patch.object(commands, "WorkflowEngine", return_value=engine)
    mocker.patch.object(commands.asyncio, "run", return_value=result)

    response = runner.invoke(commands.cli, ["archive", "https://example.com/item"])

    assert response.exit_code != 0
    published = "\n".join(_printed_strings(console_spy)) + response.output
    assert "成功: 归档完成" not in published
    assert "工作流终态: error" in published


def test_archive_accepts_non_storage_degradation_with_ready_committed_storage(
    runner: CliRunner,
    mocker: pytest.MockFixture,
    load_config_stub,
    console_spy,
) -> None:
    result = WorkflowResult(
        success=True,
        terminal="degraded",
        data={
            "knowledge_id": 42,
            "status": "ready",
            "core_committed": True,
            "entry": _make_entry(),
        },
        warnings=["stable provider warning"],
        issues=[
            {
                "code": "provider_unavailable",
                "message": "stable provider warning",
                "severity": "warning",
                "recoverable": True,
            }
        ],
    )
    engine = mocker.MagicMock()
    mocker.patch.object(commands, "WorkflowEngine", return_value=engine)
    mocker.patch.object(commands.asyncio, "run", return_value=result)

    response = runner.invoke(commands.cli, ["archive", "https://example.com/item"])

    assert response.exit_code == 0
    published = "\n".join(_printed_strings(console_spy)) + response.output
    assert "归档以 degraded 终态完成" in published
    assert "归档失败" not in published


def test_archive_unknown_exception_does_not_echo_canary_or_path(
    runner: CliRunner,
    mocker: pytest.MockFixture,
    load_config_stub,
    console_spy,
) -> None:
    logger_spy = mocker.patch.object(commands.logger, "error")
    engine = mocker.MagicMock()
    mocker.patch.object(commands, "WorkflowEngine", return_value=engine)
    mocker.patch.object(
        commands.asyncio,
        "run",
        side_effect=RuntimeError("CANARY C:\\private\\vault\\secret.txt"),
    )

    response = runner.invoke(
        commands.cli,
        ["archive", "https://example.com/failure"],
    )

    assert response.exit_code != 0
    printed = "\n".join(_printed_strings(console_spy))
    assert "发生内部异常" in printed
    assert "CANARY" not in printed
    assert "C:\\private" not in printed
    assert "secret.txt" not in printed
    logged = repr(logger_spy.call_args_list)
    assert "CANARY" not in logged
    assert "C:\\private" not in logged
    assert "secret.txt" not in logged


@pytest.mark.parametrize(
    ("terminal", "code", "stage", "recoverable", "expected_exit"),
    [
        ("success", ErrorCode.STORAGE_INDEX_FAILED, "sqlite_index", True, 0),
        ("degraded", ErrorCode.STORAGE_VECTOR_FAILED, "vector_index", True, 0),
        ("error", ErrorCode.STORAGE_REPAIR_REQUIRED, "storage_finalize", False, 1),
    ],
)
def test_archive_terminal_never_publishes_upstream_canaries(
    terminal: str,
    code: ErrorCode,
    stage: str,
    recoverable: bool,
    expected_exit: int,
    runner: CliRunner,
    mocker: pytest.MockFixture,
    load_config_stub,
) -> None:
    path_canary = r"C:\private\ARCHIVE_PATH_CANARY\vault.db"
    query_canary = "ARCHIVE_QUERY_SECRET_CANARY"
    secret_url = f"https://example.com/article?api_key={query_canary}"
    api_key_canary = "sk-ARCHIVE-API-KEY-CANARY"
    raw_diagnostic = f"{path_canary} {secret_url} {api_key_canary}"
    severity = "error" if terminal == "error" else "warning"
    data = {
        "knowledge_id": 41,
        "entry": _make_entry(title="Safe title", source_url=secret_url),
        "file_path": path_canary,
        "status": (
            "repair_required"
            if terminal == "error"
            else "degraded"
            if terminal == "degraded"
            else "ready"
        ),
        "operation_id": {
            "success": "a" * 32,
            "degraded": "b" * 32,
            "error": "c" * 32,
        }[terminal],
        "core_committed": True,
        "do_not_retry": terminal in {"degraded", "error"},
        "repair_actions": (
            ["repair_operation_journal"]
            if terminal == "error"
            else ["rebuild_vectors_for_entry"]
            if terminal == "degraded"
            else []
        ),
    }
    result = WorkflowResult(
        success=terminal != "error",
        terminal=terminal,
        data=data,
        errors=[raw_diagnostic] if terminal == "error" else [],
        warnings=[raw_diagnostic] if terminal == "degraded" else [],
        issues=(
            [
                {
                    "code": code,
                    "message": raw_diagnostic,
                    "severity": severity,
                    "stage": stage,
                    "recoverable": recoverable,
                }
            ]
            if terminal != "success"
            else []
        ),
    )
    engine = mocker.MagicMock()
    mocker.patch.object(commands, "WorkflowEngine", return_value=engine)
    mocker.patch.object(commands.asyncio, "run", return_value=result)
    sink = io.StringIO()
    mocker.patch.object(
        commands,
        "console",
        Console(file=sink, force_terminal=False, color_system=None, width=160),
    )

    response = runner.invoke(commands.cli, ["archive", secret_url])

    assert response.exit_code == expected_exit
    published = _public_cli_output(response, sink)
    assert path_canary not in published
    assert "ARCHIVE_PATH_CANARY" not in published
    assert query_canary not in published
    assert api_key_canary not in published
    if terminal != "success":
        assert code.value in published
        assert f"stage={stage}" in published
        assert f"recoverable={str(recoverable).lower()}" in published
    assert data["status"] in published
    assert data["operation_id"] in published
    for action in data["repair_actions"]:
        assert action in published
    if terminal != "error":
        assert "已隐藏" in published


def test_archive_issue_stage_step_and_cause_are_fixed_public_projections() -> None:
    canary = "ARCHIVE-DIAGNOSTIC-SECRET-CANARY"

    projected = commands._normalise_archive_issue(
        {
            "code": ErrorCode.WORKFLOW_STEP_FAILED,
            "message": f"message-{canary}",
            "severity": "warning",
            "stage": f"stage-{canary}",
            "step_id": f"step-{canary}",
            "recoverable": "false",
            "cause_type": f"cause-{canary}",
        },
        default_severity="warning",
    )

    assert projected == {
        "code": ErrorCode.WORKFLOW_STEP_FAILED.value,
        "message": "归档步骤已降级",
        "severity": "warning",
        "stage": "workflow",
        "recoverable": False,
        "step_id": "unknown_step",
    }
    assert canary not in repr(projected)

    safe = commands._normalise_archive_issue(
        {
            "code": ErrorCode.WORKFLOW_STEP_FAILED,
            "severity": "error",
            "stage": "workflow_fetch",
            "step_id": "fetch_content",
            "recoverable": True,
        },
        default_severity="error",
    )
    assert safe["stage"] == "workflow_fetch"
    assert safe["step_id"] == "fetch_content"
    assert safe["recoverable"] is True


@pytest.mark.parametrize(
    "terminal_case",
    ["missing", "none", "unknown", "non_string"],
)
def test_archive_invalid_or_missing_terminal_fails_closed(
    terminal_case: str,
    runner: CliRunner,
    mocker: pytest.MockFixture,
    load_config_stub,
) -> None:
    terminal_canary = "TERMINAL-CONTRACT-SECRET-CANARY"
    diagnostic_canary = r"C:\private\WORKFLOW-CONTRACT-CANARY\secret.key"
    result = SimpleNamespace(
        success=True,
        data={
            "status": "error",
            "operation_id": "e" * 32,
            "repair_actions": [],
        },
        errors=[diagnostic_canary],
        warnings=[],
        issues=[
            {
                "code": ErrorCode.WORKFLOW_STEP_FAILED,
                "message": diagnostic_canary,
                "severity": "error",
                "stage": "upstream",
                "recoverable": True,
            }
        ],
    )
    if terminal_case == "missing":
        del result.data
    elif terminal_case == "none":
        result.terminal = None
    elif terminal_case == "unknown":
        result.terminal = f"success-{terminal_canary}"
    elif terminal_case == "non_string":
        result.terminal = {"value": terminal_canary}

    engine = mocker.MagicMock()
    mocker.patch.object(commands, "WorkflowEngine", return_value=engine)
    mocker.patch.object(commands.asyncio, "run", return_value=result)
    logger_spy = mocker.patch.object(commands.logger, "error")
    sink = io.StringIO()
    mocker.patch.object(
        commands,
        "console",
        Console(file=sink, force_terminal=False, color_system=None, width=160),
    )

    response = runner.invoke(
        commands.cli,
        ["archive", "https://example.com/contract"],
    )

    assert response.exit_code != 0
    published = _public_cli_output(response, sink)
    assert "成功: 归档完成" not in published
    assert "归档结果" not in published
    assert "工作流终态: error" in published
    assert "workflow_step_failed" in published
    assert "stage=workflow_contract" in published
    assert "recoverable=false" in published
    assert terminal_canary not in published
    assert diagnostic_canary not in published
    logged = repr(logger_spy.call_args_list)
    assert terminal_canary not in logged
    assert diagnostic_canary not in logged


@pytest.mark.parametrize(
    "result",
    [
        SimpleNamespace(
            terminal="success",
            success=False,
            data={"knowledge_id": 1, "status": "ready"},
            issues=(),
            errors=(),
            warnings=(),
        ),
        SimpleNamespace(
            terminal="error",
            success=True,
            data={"status": "ready"},
            issues=(),
            errors=(),
            warnings=(),
        ),
        SimpleNamespace(
            terminal="success",
            success=True,
            data=None,
            issues=(),
            errors=(),
            warnings=(),
        ),
        SimpleNamespace(
            terminal="success",
            success=True,
            data={},
            issues=(),
            errors=(),
            warnings=(),
        ),
        SimpleNamespace(
            terminal="success",
            success=True,
            data={"knowledge_id": 0, "status": "ready"},
            issues=(),
            errors=(),
            warnings=(),
        ),
        SimpleNamespace(
            terminal="degraded",
            success=True,
            data={"knowledge_id": True, "status": "degraded"},
            issues=(),
            errors=(),
            warnings=(),
        ),
    ],
    ids=[
        "success-terminal-false-bool",
        "error-terminal-true-bool",
        "non-dict-data",
        "missing-knowledge-id",
        "zero-knowledge-id",
        "bool-knowledge-id",
    ],
)
def test_archive_inconsistent_or_uncommitted_success_fails_closed(
    result: SimpleNamespace,
    runner: CliRunner,
    mocker: pytest.MockFixture,
    load_config_stub,
) -> None:
    """A malformed terminal or missing committed identity never becomes success."""

    sink = io.StringIO()
    mocker.patch.object(
        commands,
        "console",
        Console(file=sink, force_terminal=False, color_system=None),
    )
    mocker.patch.object(commands, "WorkflowEngine", return_value=mocker.MagicMock())
    mocker.patch.object(commands.asyncio, "run", return_value=result)

    response = runner.invoke(commands.cli, ["archive", "https://example.com/item"])

    output = _public_cli_output(response, sink)
    assert response.exit_code != 0
    assert "workflow_step_failed" in output
    assert "归档完成" not in output


@pytest.mark.parametrize("terminal", ["success", "degraded"])
@pytest.mark.parametrize(
    "storage_status",
    [pytest.param(None, id="missing"), "repair_required", "rejected", "unknown", True],
)
def test_archive_non_completed_storage_status_fails_closed(
    terminal: str,
    storage_status: object,
    runner: CliRunner,
    mocker: pytest.MockFixture,
    load_config_stub,
) -> None:
    """Fatal, missing, unknown, and non-string storage states cannot look complete."""

    data: dict[str, object] = {"knowledge_id": 17}
    if storage_status is not None:
        data["status"] = storage_status
    result = SimpleNamespace(
        terminal=terminal,
        success=True,
        data=data,
        issues=(),
        errors=(),
        warnings=(),
    )
    sink = io.StringIO()
    mocker.patch.object(
        commands,
        "console",
        Console(file=sink, force_terminal=False, color_system=None),
    )
    mocker.patch.object(commands, "WorkflowEngine", return_value=mocker.MagicMock())
    mocker.patch.object(commands.asyncio, "run", return_value=result)

    response = runner.invoke(commands.cli, ["archive", "https://example.com/item"])

    output = _public_cli_output(response, sink)
    assert response.exit_code != 0
    assert "workflow_step_failed" in output
    assert "归档完成" not in output


@pytest.mark.parametrize(
    ("terminal", "errors", "warnings", "issues"),
    [
        ("success", ["BACKEND-ERROR"], [], []),
        ("success", [], ["hidden warning"], []),
        (
            "success",
            [],
            [],
            [{"severity": "warning", "message": "hidden warning"}],
        ),
        ("degraded", [], [], []),
        (
            "degraded",
            [],
            ["warning"],
            [{"severity": "error", "message": "hidden error"}],
        ),
        ("degraded", (), ["warning"], []),
    ],
    ids=[
        "success-hides-errors",
        "success-hides-warnings",
        "success-hides-issues",
        "degraded-without-diagnostic",
        "degraded-hides-error-issue",
        "diagnostics-must-be-exact-lists",
    ],
)
def test_archive_inconsistent_completion_diagnostics_fail_closed(
    terminal: str,
    errors: object,
    warnings: object,
    issues: object,
    runner: CliRunner,
    mocker: pytest.MockFixture,
    load_config_stub,
) -> None:
    """A completed terminal cannot erase or contradict workflow diagnostics."""

    result = SimpleNamespace(
        terminal=terminal,
        success=True,
        data={"knowledge_id": 17, "status": "ready"},
        errors=errors,
        warnings=warnings,
        issues=issues,
    )
    sink = io.StringIO()
    mocker.patch.object(
        commands,
        "console",
        Console(file=sink, force_terminal=False, color_system=None),
    )
    mocker.patch.object(commands, "WorkflowEngine", return_value=mocker.MagicMock())
    mocker.patch.object(commands.asyncio, "run", return_value=result)

    response = runner.invoke(commands.cli, ["archive", "https://example.com/item"])

    output = _public_cli_output(response, sink)
    assert response.exit_code != 0
    assert "workflow_step_failed" in output
    assert "归档完成" not in output
    assert "BACKEND-ERROR" not in output


def test_issue_text_never_falls_back_to_unknown_object_repr() -> None:
    class UnsafeIssue:
        def __str__(self) -> str:
            return "CANARY C:\\private\\secret.txt"

    rendered = commands._issue_text(UnsafeIssue())

    assert rendered == "retrieval_backend_failed: 检索服务暂不可用 (retrieval)"
    assert "CANARY" not in rendered
    assert "C:\\private" not in rendered


def test_search_command_auto_strategy(
    runner: CliRunner,
    mocker: pytest.MockFixture,
    load_config_stub,
    mock_config: DummyConfig,
    console_spy,
) -> None:
    """search should use QueryRouter when strategy is auto."""
    router = mocker.MagicMock()
    router.search.return_value = _completed_search("bm25")
    mocker.patch.object(commands, "QueryRouter", return_value=router)
    provider_factory = mocker.patch.object(commands, "create_embedder")

    response = runner.invoke(commands.cli, ["search", "hello world", "--limit", "3"])

    assert response.exit_code == 0
    commands.QueryRouter.assert_called_once_with(
        db_path=mock_config.db_path,
        vector_index_dir=mock_config.vector_index_dir,
        token_threshold=10,
        embedder_factory=mocker.ANY,
    )
    router.search.assert_called_once_with("hello world", 3)
    provider_factory.assert_not_called()
    assert any("bm25" in text for text in _printed_strings(console_spy))


def test_search_command_bm25_strategy(
    runner: CliRunner,
    mocker: pytest.MockFixture,
    load_config_stub,
    mock_config: DummyConfig,
    console_spy,
) -> None:
    """search should use BM25 retriever when strategy is bm25."""
    retriever = mocker.MagicMock()
    retriever.search.return_value = _completed_search("bm25")
    mocker.patch.object(
        commands,
        "BM25Retriever",
        return_value=retriever,
    )
    mocker.patch.object(commands, "QueryRouter", return_value=mocker.MagicMock())
    provider_factory = mocker.patch.object(commands, "create_embedder")

    response = runner.invoke(
        commands.cli, ["search", "keyword", "--strategy", "bm25", "--limit", "2"]
    )

    assert response.exit_code == 0
    commands.BM25Retriever.assert_called_once_with(mock_config.db_path)
    retriever.search.assert_called_once_with("keyword", 2)
    provider_factory.assert_not_called()


def test_search_command_vector_strategy(
    runner: CliRunner,
    mocker: pytest.MockFixture,
    load_config_stub,
    mock_config: DummyConfig,
    console_spy,
) -> None:
    """search should use vector retriever when strategy is vector."""
    retriever = mocker.MagicMock()
    retriever.search.return_value = _completed_search("vector")
    vector_type = mocker.patch.object(commands, "VectorRetriever", return_value=retriever)
    embedder = mocker.MagicMock()
    provider_factory = mocker.patch.object(
        commands, "create_embedder", return_value=embedder
    )

    response = runner.invoke(
        commands.cli, ["search", "semantic", "--strategy", "vector", "--limit", "4"]
    )

    assert response.exit_code == 0
    provider_factory.assert_not_called()
    factory = vector_type.call_args.kwargs["embedder_factory"]
    assert factory() is embedder
    provider_factory.assert_called_once_with(mock_config)
    vector_type.assert_called_once_with(
        mock_config.db_path,
        mock_config.vector_index_dir,
        embedder_factory=mocker.ANY,
    )
    retriever.search.assert_called_once_with("semantic", 4)


def test_search_hybrid_defers_provider_to_retriever(
    runner: CliRunner,
    mocker: pytest.MockFixture,
    load_config_stub,
    mock_config: DummyConfig,
    console_spy,
) -> None:
    retriever = mocker.MagicMock()
    retriever.search.return_value = SearchResponse(
        status="no_hits",
        strategy="hybrid",
    )
    hybrid_type = mocker.patch.object(
        commands,
        "HybridRetriever",
        return_value=retriever,
    )
    embedder = mocker.MagicMock()
    provider_factory = mocker.patch.object(
        commands,
        "create_embedder",
        return_value=embedder,
    )

    response = runner.invoke(
        commands.cli,
        ["search", "combined", "--strategy", "hybrid"],
    )

    assert response.exit_code == 0
    provider_factory.assert_not_called()
    factory = hybrid_type.call_args.kwargs["embedder_factory"]
    assert factory() is embedder
    provider_factory.assert_called_once_with(mock_config)
    hybrid_type.assert_called_once_with(
        mock_config.db_path,
        mock_config.vector_index_dir,
        embedder_factory=mocker.ANY,
    )


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
    retriever.search.return_value = SearchResponse.completed(results, strategy="bm25")
    mocker.patch.object(commands, "BM25Retriever", return_value=retriever)

    response = runner.invoke(
        commands.cli,
        ["search", "query", "--strategy", "bm25", "--format", "json", "--limit", "5"],
    )

    assert response.exit_code == 0
    payload_text = response.output
    payload = json.loads(payload_text)
    assert payload["query"] == "query"
    assert payload["status"] == "success"
    assert payload["strategy"] == "bm25"
    assert payload["total"] == len(results)
    assert payload["issues"] == []
    assert payload["results"][0]["entry_id"] == 1


def test_search_degraded_is_successful_but_warns_in_table_output(
    runner: CliRunner,
    mocker: pytest.MockFixture,
    load_config_stub,
    console_spy,
) -> None:
    issue = RetrievalIssue(
        code=ErrorCode.RETRIEVAL_METADATA_INCONSISTENT,
        message="一条结果的元数据缺失",
        stage="metadata_hydration",
        recoverable=True,
    )
    retriever = mocker.MagicMock()
    retriever.search.return_value = SearchResponse.degraded_response(
        _make_search_results()[:1],
        [issue],
        strategy="bm25",
    )
    mocker.patch.object(
        commands,
        "BM25Retriever",
        return_value=retriever,
    )

    response = runner.invoke(
        commands.cli,
        ["search", "query", "--strategy", "bm25"],
    )

    assert response.exit_code == 0
    printed = "\n".join(_printed_strings(console_spy))
    assert "状态: degraded" in printed
    assert "警告" in printed
    assert "retrieval_metadata_inconsistent" in printed
    assert "检索结果元数据不一致" in printed
    assert "一条结果的元数据缺失" not in printed


def test_search_invalid_json_is_structured_and_nonzero(
    runner: CliRunner,
    mocker: pytest.MockFixture,
    load_config_stub,
) -> None:
    retriever = mocker.MagicMock()
    retriever.search.return_value = SearchResponse.invalid(
        "查询不能为空",
        strategy="bm25",
    )
    retriever_type = mocker.patch.object(
        commands,
        "BM25Retriever",
        return_value=retriever,
    )

    response = runner.invoke(
        commands.cli,
        ["search", "   ", "--strategy", "bm25", "--format", "json"],
    )

    assert response.exit_code != 0
    payload = json.loads(response.output)
    assert payload["status"] == "invalid"
    assert payload["total"] == 0
    assert payload["results"] == []
    assert payload["issues"][0]["code"] == "retrieval_invalid_query"
    assert payload["issues"][0]["message"] == "查询条件无效"
    retriever_type.assert_not_called()


def test_search_provider_failure_is_error_not_empty_results(
    runner: CliRunner,
    mocker: pytest.MockFixture,
    load_config_stub,
) -> None:
    provider_factory = mocker.patch.object(
        commands,
        "create_embedder",
        side_effect=PKVRuntimeError(
            ErrorCode.PROVIDER_UNAVAILABLE,
            "Embedding Provider 不可用",
            stage="provider_connect",
            recoverable=True,
        ),
    )

    def vector_factory(db_path, vector_index_dir, *, embedder_factory):
        retriever = mocker.MagicMock()
        retriever.search.side_effect = lambda query, limit: embedder_factory()
        return retriever

    vector_retriever = mocker.patch.object(
        commands,
        "VectorRetriever",
        side_effect=vector_factory,
    )

    response = runner.invoke(
        commands.cli,
        ["search", "semantic", "--strategy", "vector", "--format", "json"],
    )

    assert response.exit_code != 0
    payload = json.loads(response.output)
    assert payload["status"] == "error"
    assert payload["total"] == 0
    assert payload["results"] == []
    assert payload["issues"][0]["code"] == "provider_unavailable"
    assert payload["issues"][0]["message"] == "检索 Provider 暂不可用"
    assert payload["issues"][0]["stage"] == "provider_connect"
    assert "Embedding Provider 不可用" not in response.output
    vector_retriever.assert_called_once()
    provider_factory.assert_called_once()


def test_search_invalid_limit_does_not_construct_provider_or_store(
    runner: CliRunner,
    mocker: pytest.MockFixture,
    load_config_stub,
) -> None:
    provider_factory = mocker.patch.object(commands, "create_embedder")
    vector_type = mocker.patch.object(commands, "VectorRetriever")
    sqlite_store = mocker.patch.object(commands, "SQLiteStore")

    response = runner.invoke(
        commands.cli,
        ["search", "semantic", "--strategy", "vector", "--limit", "0", "--format", "json"],
    )

    assert response.exit_code != 0
    payload = json.loads(response.output)
    assert payload["status"] == "invalid"
    assert payload["issues"][0]["stage"] == "limit_validation"
    provider_factory.assert_not_called()
    vector_type.assert_not_called()
    sqlite_store.assert_not_called()


def test_search_config_failure_is_structured_json_error(
    runner: CliRunner,
    mocker: pytest.MockFixture,
) -> None:
    mocker.patch.object(
        commands,
        "_load_config",
        side_effect=RuntimeError("private path and secret must not leak"),
    )

    response = runner.invoke(
        commands.cli,
        ["search", "query", "--strategy", "bm25", "--format", "json"],
    )

    assert response.exit_code != 0
    payload = json.loads(response.output)
    assert payload["status"] == "error"
    assert payload["issues"][0]["code"] == "retrieval_backend_failed"
    assert payload["issues"][0]["message"] == "检索服务暂不可用"
    assert "private path" not in response.output
    assert "secret" not in response.output


def test_search_invalid_backend_response_is_structured_error(
    runner: CliRunner,
    mocker: pytest.MockFixture,
    load_config_stub,
) -> None:
    retriever = mocker.MagicMock()
    retriever.search.return_value = []
    mocker.patch.object(commands, "BM25Retriever", return_value=retriever)

    response = runner.invoke(
        commands.cli,
        ["search", "query", "--strategy", "bm25", "--format", "json"],
    )

    assert response.exit_code != 0
    payload = json.loads(response.output)
    assert payload["status"] == "error"
    assert payload["issues"] == [
        {
            "code": "retrieval_backend_failed",
            "message": "检索服务暂不可用",
            "stage": "cli_search_protocol",
            "recoverable": False,
        }
    ]


@pytest.mark.parametrize(
    ("requested", "reported"),
    [
        ("bm25", "vector"),
        ("vector", "hybrid"),
        ("hybrid", "bm25"),
        ("auto", "vector"),
    ],
)
def test_search_response_strategy_must_match_requested_contract(
    requested: str,
    reported: str,
) -> None:
    response = SearchResponse.completed([], strategy=reported)

    projected = commands._ensure_search_response(response, strategy=requested)

    assert projected.status == "error"
    assert projected.strategy == requested
    assert projected.issues[0].code is ErrorCode.RETRIEVAL_BACKEND_FAILED
    assert projected.issues[0].stage == "cli_search_protocol"
    assert projected.issues[0].cause_type == "SearchStrategyMismatch"


@pytest.mark.parametrize("reported", ["router", "bm25", "hybrid"])
def test_auto_search_accepts_only_published_router_strategies(reported: str) -> None:
    response = SearchResponse.completed([], strategy=reported)

    assert commands._ensure_search_response(response, strategy="auto") is response


def test_search_frozen_corruption_and_strategy_canary_fail_closed(
    runner: CliRunner,
    mocker: pytest.MockFixture,
    load_config_stub,
) -> None:
    canary = "frozen_response_secret_canary"
    response = SearchResponse.completed(_make_search_results()[:1], strategy="bm25")
    object.__setattr__(response, "results", (canary,))
    retriever = mocker.MagicMock()
    retriever.search.return_value = response
    mocker.patch.object(commands, "BM25Retriever", return_value=retriever)

    result = runner.invoke(
        commands.cli,
        ["search", "query", "--strategy", "bm25", "--format", "json"],
    )

    assert result.exit_code != 0
    payload = json.loads(result.output)
    assert payload["status"] == "error"
    assert payload["strategy"] == "bm25"
    assert payload["issues"] == [
        {
            "code": ErrorCode.RETRIEVAL_BACKEND_FAILED.value,
            "message": "检索服务暂不可用",
            "stage": "cli_search_protocol",
            "recoverable": False,
        }
    ]
    assert canary not in result.output


def test_search_error_markdown_exposes_status_and_issue(
    runner: CliRunner,
    mocker: pytest.MockFixture,
    load_config_stub,
    console_spy,
) -> None:
    issue = RetrievalIssue(
        code=ErrorCode.RETRIEVAL_BACKEND_FAILED,
        message="数据库查询失败",
        stage="bm25_query",
    )
    retriever = mocker.MagicMock()
    retriever.search.return_value = SearchResponse.failed_response(
        issue,
        strategy="bm25",
    )
    mocker.patch.object(commands, "BM25Retriever", return_value=retriever)

    response = runner.invoke(
        commands.cli,
        ["search", "query", "--strategy", "bm25", "--format", "markdown"],
    )

    assert response.exit_code != 0
    printed = "\n".join(_printed_strings(console_spy))
    assert "状态: error" in printed
    assert "retrieval_backend_failed" in printed
    assert "检索服务暂不可用" in printed
    assert "数据库查询失败" not in printed


def test_search_issue_message_stage_and_cause_never_publish_canaries(
    runner: CliRunner,
    mocker: pytest.MockFixture,
    load_config_stub,
) -> None:
    canary = "RETRIEVAL_DIAGNOSTIC_SECRET_CANARY"
    issue = RetrievalIssue(
        code=ErrorCode.PROVIDER_UNAVAILABLE,
        message=f"message_{canary}",
        stage=f"stage_{canary}",
        recoverable=True,
        cause_type=f"cause_{canary}",
    )
    retriever = mocker.MagicMock()
    retriever.search.return_value = SearchResponse.failed_response(
        issue,
        strategy="bm25",
    )
    mocker.patch.object(commands, "BM25Retriever", return_value=retriever)

    response = runner.invoke(
        commands.cli,
        ["search", "query", "--strategy", "bm25", "--format", "json"],
    )

    assert response.exit_code != 0
    payload = json.loads(response.output)
    assert payload["issues"] == [
        {
            "code": ErrorCode.PROVIDER_UNAVAILABLE.value,
            "message": "检索 Provider 暂不可用",
            "stage": "retrieval",
            "recoverable": True,
        }
    ]
    assert canary not in response.output


def test_show_command_by_id(
    runner: CliRunner,
    mocker: pytest.MockFixture,
    load_config_stub,
    console_spy,
) -> None:
    """show should look up an entry by ID."""
    entry = _make_db_entry()
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
    entry = _make_db_entry(knowledge_id=4)
    store = mocker.MagicMock()
    store.query_by_url.return_value = entry
    mocker.patch.object(commands, "SQLiteStore", return_value=store)

    response = runner.invoke(
        commands.cli, ["show", "--url", "https://example.com/post"]
    )

    assert response.exit_code == 0
    store.query_by_url.assert_called_once_with("https://example.com/post")


def test_show_non_raw_sanitizes_source_and_file_projection(
    runner: CliRunner,
    mocker: pytest.MockFixture,
    load_config_stub,
    console_spy,
) -> None:
    credential_canary = "SHOW-PASSWORD-SECRET-CANARY"
    query_canary = "SHOW-API-KEY-SECRET-CANARY"
    fragment_canary = "SHOW-FRAGMENT-SECRET-CANARY"
    path_canary = "SHOW-ABSOLUTE-PATH-SECRET-CANARY"
    entry = _make_db_entry(
        source_url=(
            f"https://user:{credential_canary}@example.com/article;password="
            f"{credential_canary}?api_key={query_canary}#{fragment_canary}"
        ),
        file_path=rf"C:\private\{path_canary}\entry.md",
    )
    store = mocker.MagicMock()
    store.query_by_id.return_value = entry
    mocker.patch.object(commands, "SQLiteStore", return_value=store)

    response = runner.invoke(commands.cli, ["show", "3"])

    assert response.exit_code == 0
    panel = next(
        call.args[0]
        for call in console_spy.call_args_list
        if call.args and isinstance(call.args[0], Panel)
    )
    published = str(panel.renderable)
    assert credential_canary not in published
    assert query_canary not in published
    assert fragment_canary not in published
    assert path_canary not in published
    assert "user:" not in published
    assert "https://example.com/" in published
    assert "api_key=redacted" in published
    assert "路径已隐藏" in published


def test_show_non_raw_preserves_safe_public_url_and_vault_relative_path(
    runner: CliRunner,
    mocker: pytest.MockFixture,
    load_config_stub,
    console_spy,
) -> None:
    entry = _make_db_entry(
        source_url="https://example.com/public/article?lang=zh#unstable",
        file_path="articles/2026/entry.md",
    )
    store = mocker.MagicMock()
    store.query_by_id.return_value = entry
    mocker.patch.object(commands, "SQLiteStore", return_value=store)

    response = runner.invoke(commands.cli, ["show", "3"])

    assert response.exit_code == 0
    panel = next(
        call.args[0]
        for call in console_spy.call_args_list
        if call.args and isinstance(call.args[0], Panel)
    )
    published = str(panel.renderable)
    assert "https://example.com/public/article?lang=zh" in published
    assert "unstable" not in published
    assert "articles/2026/entry.md" in published


@pytest.mark.parametrize(
    "projection",
    [
        {},
        [],
        _make_db_entry(knowledge_id=0),
        _make_db_entry(knowledge_id=True),
        _make_db_entry(title=""),
        _make_db_entry(source_url={"secret": "SHOW-READ-SECRET-CANARY"}),
        {key: value for key, value in _make_db_entry().items() if key != "file_path"},
    ],
    ids=[
        "empty-dict",
        "list",
        "zero-id",
        "bool-id",
        "empty-title",
        "source-url-wrong-type",
        "missing-required-field",
    ],
)
def test_show_corrupt_projection_is_not_reported_as_not_found(
    projection: object,
    runner: CliRunner,
    mocker: pytest.MockFixture,
    load_config_stub,
    console_spy,
) -> None:
    store = mocker.MagicMock()
    store.query_by_id.return_value = projection
    mocker.patch.object(commands, "SQLiteStore", return_value=store)

    response = runner.invoke(commands.cli, ["show", "3"])

    assert response.exit_code != 0
    published = "\n".join(_printed_strings(console_spy)) + response.output
    assert "cli_show_failed" in published
    assert "未找到对应条目" not in published
    assert "SHOW-READ-SECRET-CANARY" not in published


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
    entry = _make_db_entry(knowledge_id=9, file_path="entry.md")
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


@pytest.mark.parametrize(
    "projection",
    [
        "LIST-READ-SECRET-CANARY",
        (),
        [{}],
        [_make_db_entry(knowledge_id=0)],
        [_make_db_entry(knowledge_id=True)],
        [{key: value for key, value in _make_db_entry().items() if key != "tags"}],
        [_make_db_entry(tags=["not", "sqlite-text"])],
    ],
    ids=[
        "string",
        "tuple",
        "empty-row",
        "zero-id",
        "bool-id",
        "missing-tags",
        "tags-wrong-type",
    ],
)
def test_list_corrupt_projection_fails_closed(
    projection: object,
    runner: CliRunner,
    mocker: pytest.MockFixture,
    load_config_stub,
    console_spy,
) -> None:
    store = mocker.MagicMock()
    store.list_entries.return_value = projection
    mocker.patch.object(commands, "SQLiteStore", return_value=store)

    response = runner.invoke(commands.cli, ["list"])

    assert response.exit_code != 0
    published = "\n".join(_printed_strings(console_spy)) + response.output
    assert "cli_list_failed" in published
    assert "未找到条目" not in published
    assert "LIST-READ-SECRET-CANARY" not in published


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


@pytest.mark.parametrize(
    ("total", "source_rows", "tag_rows"),
    [
        (True, [], []),
        (-1, [], []),
        ("STATS-READ-SECRET-CANARY", [], []),
        (0, "STATS-READ-SECRET-CANARY", []),
        (0, [["webpage", 0]], []),
        (0, [("webpage", True)], []),
        (0, [("webpage", -1)], []),
        (0, [], "STATS-READ-SECRET-CANARY"),
        (0, [], [{}]),
        (0, [], [{"name": "tag", "count": True}]),
        (0, [], [{"name": "", "count": 0}]),
    ],
    ids=[
        "bool-total",
        "negative-total",
        "string-total",
        "string-source-rows",
        "list-source-row",
        "bool-source-count",
        "negative-source-count",
        "string-tag-rows",
        "empty-tag-row",
        "bool-tag-count",
        "empty-tag-name",
    ],
)
def test_stats_corrupt_projection_fails_closed(
    total: object,
    source_rows: object,
    tag_rows: object,
    runner: CliRunner,
    mocker: pytest.MockFixture,
    load_config_stub,
    mock_config: DummyConfig,
    console_spy,
) -> None:
    mock_config.db_path.parent.mkdir(parents=True, exist_ok=True)
    mock_config.db_path.write_text("", encoding="utf-8")
    store = mocker.MagicMock()
    store.table_exists.return_value = True
    store.count_entries.return_value = total
    store.count_entries_by_source_type.return_value = source_rows
    store.get_all_tags_with_count.return_value = tag_rows
    mocker.patch.object(commands, "SQLiteStore", return_value=store)

    response = runner.invoke(commands.cli, ["stats"])

    assert response.exit_code != 0
    published = "\n".join(_printed_strings(console_spy)) + response.output
    assert "cli_stats_failed" in published
    assert "STATS-READ-SECRET-CANARY" not in published


def test_stats_malformed_table_exists_fails_closed(
    runner: CliRunner,
    mocker: pytest.MockFixture,
    load_config_stub,
    mock_config: DummyConfig,
    console_spy,
) -> None:
    mock_config.db_path.parent.mkdir(parents=True, exist_ok=True)
    mock_config.db_path.write_text("", encoding="utf-8")
    store = mocker.MagicMock()
    store.table_exists.return_value = "STATS-TABLE-SECRET-CANARY"
    mocker.patch.object(commands, "SQLiteStore", return_value=store)

    response = runner.invoke(commands.cli, ["stats"])

    assert response.exit_code != 0
    published = "\n".join(_printed_strings(console_spy)) + response.output
    assert "cli_stats_failed" in published
    assert "STATS-TABLE-SECRET-CANARY" not in published


# ---------------------------------------------------------------------------
# archive-text / tags / related command contracts
# ---------------------------------------------------------------------------


def test_archive_text_json_uses_literal_processor_seam_and_workflow(
    runner: CliRunner,
    mocker: pytest.MockFixture,
    load_config_stub,
    tmp_path: Path,
) -> None:
    """archive-text must treat path-shaped input as text and inject its Entry."""

    path_shaped_text = str(tmp_path / "PRIVATE-TEXT-SOURCE-CANARY.txt")
    entry = _make_entry(title="自动标题", source_url=None, tags=["text", "note"])
    entry.content = path_shaped_text
    processor = mocker.MagicMock()
    processor.process_text = mocker.AsyncMock(return_value=entry)
    engine = mocker.MagicMock()
    engine.execute_async = mocker.AsyncMock(
        return_value=WorkflowResult(
            success=True,
            terminal="success",
            data={
                "knowledge_id": 71,
                "status": "ready",
                "core_committed": True,
                "file_path": "vault/text-note.md",
                "entry": entry,
            },
            errors=[],
            warnings=[],
            issues=[],
        )
    )
    mocker.patch.object(commands, "TextFallbackProcessor", return_value=processor)
    mocker.patch.object(commands, "WorkflowEngine", return_value=engine)
    path_exists = mocker.patch.object(
        Path,
        "exists",
        side_effect=AssertionError("archive-text must not probe path-shaped text"),
    )
    path_read = mocker.patch.object(
        Path,
        "read_text",
        side_effect=AssertionError("archive-text must not read path-shaped text"),
    )

    response = runner.invoke(
        commands.cli,
        [
            "archive-text",
            path_shaped_text,
            "--title",
            "人工标题",
            "--format",
            "json",
        ],
    )

    assert response.exit_code == 0
    processor.process_text.assert_awaited_once_with(path_shaped_text)
    path_exists.assert_not_called()
    path_read.assert_not_called()
    engine.execute_async.assert_awaited_once()
    workflow_name, workflow_input = engine.execute_async.await_args.args
    assert workflow_name == "archive-text"
    assert workflow_input["text"] == path_shaped_text
    assert workflow_input["title"] == "人工标题"
    assert workflow_input["entry"] is entry
    assert workflow_input["content"] == path_shaped_text
    assert workflow_input["skip_review"] is True
    assert workflow_input["skip_sharpen"] is True
    assert entry.title == "人工标题"

    payload = json.loads(response.output)
    assert payload == {
        "terminal": "success",
        "status": "ready",
        "knowledge_id": 71,
        "title": "人工标题",
        "tags": ["text", "note"],
        "file_path": "vault/text-note.md",
        "issues": [],
    }


def test_archive_text_invalid_completed_terminal_fails_closed_in_json(
    runner: CliRunner,
    mocker: pytest.MockFixture,
    load_config_stub,
) -> None:
    """A contradictory workflow terminal must never be rendered as archive success."""

    entry = _make_entry(title="安全标题", source_url=None)
    processor = mocker.MagicMock()
    processor.process_text = mocker.AsyncMock(return_value=entry)
    engine = mocker.MagicMock()
    engine.execute_async = mocker.AsyncMock(
        return_value=SimpleNamespace(
            terminal="success",
            success=False,
            data={
                "knowledge_id": 72,
                "status": "ready",
                "core_committed": True,
                "entry": entry,
            },
            errors=[],
            warnings=[],
            issues=[],
        )
    )
    mocker.patch.object(commands, "TextFallbackProcessor", return_value=processor)
    mocker.patch.object(commands, "WorkflowEngine", return_value=engine)

    response = runner.invoke(
        commands.cli,
        ["archive-text", "普通文本", "--format", "json"],
    )

    assert response.exit_code != 0
    payload = json.loads(response.output)
    assert payload["terminal"] == "error"
    assert payload["status"] == "error"
    assert payload["knowledge_id"] is None
    assert payload["issues"][0]["code"] == ErrorCode.WORKFLOW_STEP_FAILED.value
    assert "安全标题" not in response.output


@pytest.mark.parametrize("unsafe_title", ["../other", r"..\\other", "bad\nterminal"])
def test_archive_text_rejects_path_shaped_or_control_titles_before_processing(
    unsafe_title: str,
    runner: CliRunner,
    mocker: pytest.MockFixture,
    load_config_stub,
) -> None:
    """A display title must not become a filename/path or terminal-control channel."""

    processor_type = mocker.patch.object(commands, "TextFallbackProcessor")
    engine_type = mocker.patch.object(commands, "WorkflowEngine")

    response = runner.invoke(
        commands.cli,
        ["archive-text", "safe literal text", "--title", unsafe_title, "--format", "json"],
    )

    assert response.exit_code != 0
    assert json.loads(response.output)["status"] == "error"
    processor_type.assert_not_called()
    engine_type.assert_not_called()


def test_tags_json_projects_store_rows(
    runner: CliRunner,
    mocker: pytest.MockFixture,
    load_config_stub,
    mock_config: DummyConfig,
) -> None:
    """tags should expose the validated SQLite tag/count projection in JSON."""

    store = mocker.MagicMock()
    store.get_all_tags_with_count.return_value = [
        {"name": "python", "count": 4},
        {"name": "workflow", "count": 2},
    ]
    store_type = mocker.patch.object(commands, "SQLiteStore", return_value=store)

    response = runner.invoke(
        commands.cli,
        ["tags", "--limit", "7", "--format", "json"],
    )

    assert response.exit_code == 0
    store_type.assert_called_once_with(mock_config.db_path)
    store.get_all_tags_with_count.assert_called_once_with(limit=7)
    assert json.loads(response.output) == {
        "status": "success",
        "total": 2,
        "tags": [
            {"name": "python", "count": 4},
            {"name": "workflow", "count": 2},
        ],
    }


def test_tags_rejects_unbounded_limit_before_opening_storage(
    runner: CliRunner,
    mocker: pytest.MockFixture,
    load_config_stub,
) -> None:
    """tags must not turn an arbitrary CLI integer into an unbounded DB read."""

    store_type = mocker.patch.object(commands, "SQLiteStore")

    response = runner.invoke(
        commands.cli,
        [
            "tags",
            "--limit",
            str(commands._MAX_TAG_LIST_LIMIT + 1),
            "--format",
            "json",
        ],
    )

    assert response.exit_code != 0
    payload = json.loads(response.output)
    assert payload == {
        "status": "invalid",
        "total": 0,
        "tags": [],
        "message": f"limit 必须是 1 到 {commands._MAX_TAG_LIST_LIMIT} 的整数",
    }
    store_type.assert_not_called()


def test_tags_malformed_store_projection_fails_closed(
    runner: CliRunner,
    mocker: pytest.MockFixture,
    load_config_stub,
) -> None:
    """A malformed tag row is an error rather than a successful empty list."""

    canary = "TAGS-STORE-PROJECTION-CANARY"
    store = mocker.MagicMock()
    store.get_all_tags_with_count.return_value = [
        {"name": canary, "count": True},
    ]
    mocker.patch.object(commands, "SQLiteStore", return_value=store)

    response = runner.invoke(
        commands.cli,
        ["tags", "--format", "json"],
    )

    assert response.exit_code != 0
    payload = json.loads(response.output)
    assert payload["status"] == "error"
    assert payload["total"] == 0
    assert payload["tags"] == []
    assert canary not in response.output


def test_related_json_returns_vector_neighbour_and_excludes_seed(
    runner: CliRunner,
    mocker: pytest.MockFixture,
    load_config_stub,
    mock_config: DummyConfig,
) -> None:
    """related should hydrate a vector neighbour locally without a Provider."""

    seed = _make_db_entry(
        knowledge_id=7,
        title="Seed",
        source_type="text",
        source_url=None,
        tags="seed",
        summary_one_sentence="seed abstract",
    )
    neighbour = _make_db_entry(
        knowledge_id=8,
        title="Neighbour",
        source_type="text",
        source_url=None,
        tags="python, vector",
        summary_one_sentence="neighbour abstract",
    )
    store = mocker.MagicMock()
    store.query_by_id.side_effect = [seed, neighbour]
    store_type = mocker.patch.object(commands, "SQLiteStore", return_value=store)
    vector_store = mocker.MagicMock()
    vector_store.get_doc_vector.return_value = [0.25, 0.75]
    vector_store.search_doc.return_value = [(7, 0.0), (8, 0.125)]
    vector_type = mocker.patch.object(
        commands,
        "VectorStore",
    )
    vector_type.has_index_artifacts.return_value = True
    vector_type.open_readonly.return_value = vector_store
    provider_factory = mocker.patch.object(commands, "create_embedder")

    response = runner.invoke(
        commands.cli,
        ["related", "7", "--limit", "2", "--format", "json"],
    )

    assert response.exit_code == 0
    store_type.assert_called_once_with(mock_config.db_path)
    vector_type.assert_not_called()
    vector_type.open_readonly.assert_called_once_with(
        index_dir=mock_config.vector_index_dir,
        dim=None,
    )
    vector_store.get_doc_vector.assert_called_once_with(7)
    vector_store.search_doc.assert_called_once_with([0.25, 0.75], k=3)
    provider_factory.assert_not_called()
    assert json.loads(response.output) == {
        "status": "success",
        "strategy": "vector_related",
        "total": 1,
        "results": [
            {
                "knowledge_id": 8,
                "title": "Neighbour",
                "abstract": "neighbour abstract",
                "tags": ["python", "vector"],
                "source_type": "text",
                "score": 0.875,
            }
        ],
        "issues": [],
    }


def test_related_missing_vector_index_is_explicit_degraded_json(
    runner: CliRunner,
    mocker: pytest.MockFixture,
    load_config_stub,
) -> None:
    """A missing local index is observable degradation, not a false no-hit."""

    store = mocker.MagicMock()
    store.query_by_id.return_value = _make_db_entry(
        knowledge_id=4,
        source_url=None,
        summary_one_sentence="seed abstract",
    )
    mocker.patch.object(commands, "SQLiteStore", return_value=store)
    vector_type = mocker.patch.object(commands, "VectorStore")
    vector_type.has_index_artifacts.return_value = False

    response = runner.invoke(
        commands.cli,
        ["related", "4", "--format", "json"],
    )

    assert response.exit_code == 0
    vector_type.assert_not_called()
    payload = json.loads(response.output)
    assert payload["status"] == "degraded"
    assert payload["strategy"] == "vector_related"
    assert payload["total"] == 0
    assert payload["results"] == []
    assert payload["issues"] == [
        {
            "code": ErrorCode.RETRIEVAL_INDEX_UNAVAILABLE.value,
            "message": "检索索引不可用",
            "stage": "vector_index",
            "recoverable": True,
        }
    ]


@pytest.mark.parametrize(
    ("args", "expected_stage"),
    [
        (["related", "not-an-id", "--format", "json"], "related_entry_lookup"),
        (["related", "1", "--limit", "0", "--format", "json"], "limit_validation"),
    ],
)
def test_related_invalid_inputs_are_structured_and_do_not_open_storage(
    args: list[str],
    expected_stage: str,
    runner: CliRunner,
    mocker: pytest.MockFixture,
    load_config_stub,
) -> None:
    """Input rejection must occur before any database or vector access."""

    store_type = mocker.patch.object(commands, "SQLiteStore")
    vector_type = mocker.patch.object(commands, "VectorStore")

    response = runner.invoke(commands.cli, args)

    assert response.exit_code != 0
    payload = json.loads(response.output)
    assert payload["status"] == "invalid"
    assert payload["strategy"] == "vector_related"
    assert payload["total"] == 0
    assert payload["results"] == []
    assert payload["issues"][0]["code"] == ErrorCode.RETRIEVAL_INVALID_QUERY.value
    assert payload["issues"][0]["stage"] == expected_stage
    store_type.assert_not_called()
    vector_type.assert_not_called()
