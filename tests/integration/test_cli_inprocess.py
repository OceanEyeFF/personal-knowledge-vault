"""
CLI in-process integration tests.

These tests use a temporary SQLite database and Click's CliRunner to exercise
command wiring without starting an OS subprocess. Subprocess exit codes, stdout,
stderr, and JSON boundaries are owned by tests/blackbox/test_cli_*.py.
"""

# ruff: noqa: E402 - 该集成测试需先将项目根目录加入 sys.path

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, Tuple

import pytest
import yaml
from click.testing import CliRunner

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import src.cli.commands as commands  # noqa: E402
from src.storage.markdown_store import Entry, MarkdownStore  # noqa: E402
from src.storage.sqlite_store import SQLiteStore  # noqa: E402
from src.utils import config as config_module  # noqa: E402
from src.utils.config import Config  # noqa: E402


ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences from CLI output."""
    return ANSI_ESCAPE_RE.sub("", text)


def _parse_json_payload(output: str) -> Dict[str, Any]:
    """Parse CLI output as one canonical JSON object."""
    text = output.strip()
    if not text:
        raise AssertionError("CLI output is empty; expected JSON payload")
    payload = json.loads(text)
    assert isinstance(payload, dict), (
        f"CLI JSON payload must be an object, got {type(payload).__name__}"
    )
    return payload


def _configure_temp_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    """Create a Config that points all storage paths to the temp directory."""
    db_path = tmp_path / "db" / "knowledge_vault.db"
    runtime_paths = {
        "DATA_DIR": tmp_path,
        "DB_PATH": db_path,
        "VAULT_DIR": tmp_path / "vault",
        "VECTOR_DIR": tmp_path / "vectors",
        "LOG_DIR": tmp_path / "logs",
        "TMP_DIR": tmp_path / "tmp",
    }
    for key, path in runtime_paths.items():
        monkeypatch.setenv(key, str(path))

    # Reset the global config singleton to avoid cross-test reuse.
    monkeypatch.setattr(config_module, "_config_instance", None)
    config = Config(str(PROJECT_ROOT / "config" / "config.yaml"))

    storage = config._config.setdefault("storage", {})
    storage["vault_dir"] = str(tmp_path / "vault")
    storage["db_path"] = str(db_path)
    storage["vector_index_dir"] = str(tmp_path / "vectors")
    storage["log_dir"] = str(tmp_path / "logs")
    storage["tmp_dir"] = str(tmp_path / "tmp")

    config.ensure_dirs()
    return config


def _patch_cli_config(monkeypatch: pytest.MonkeyPatch, config: Config) -> None:
    """Force CLI commands to use the provided Config instance."""
    monkeypatch.setattr(commands, "_load_config", lambda: config)
    monkeypatch.setattr(config_module, "get_config", lambda: config)


def _seed_entry(
    markdown_store: MarkdownStore,
    sqlite_store: SQLiteStore,
    *,
    title: str,
    summary: str,
    tags: Iterable[str],
    keywords: str,
    source_url: str,
    subdir: str = "test",
) -> Tuple[int, Path]:
    """Create a Markdown entry and insert it into SQLite."""
    entry = Entry(
        title=title,
        content=f"# {title}\n\nIntegration test content.",
        abstract=summary,
        summary_one_sentence=summary,
        summary_100_words=summary,
        tags=list(tags),
        keywords=keywords,
        source_type="generic",
        source_url=source_url,
        search_strategy="keyword",
    )

    file_path = markdown_store.save(entry, subdir=subdir)
    knowledge_id = sqlite_store.insert_entry(entry, str(file_path))
    return knowledge_id, file_path


@pytest.fixture
def runner() -> CliRunner:
    """Provide a Click CLI runner."""
    return CliRunner()


@pytest.fixture
def temp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Set up a temporary database and storage paths for CLI tests."""
    config = _configure_temp_config(tmp_path, monkeypatch)
    sqlite_store = SQLiteStore(config.db_path)
    sqlite_store.initialize()
    markdown_store = MarkdownStore(config.vault_dir)
    return config, sqlite_store, markdown_store, tmp_path


def test_search_inprocess(
    runner: CliRunner,
    temp_db,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CLI search should return results from the temporary database."""
    config, sqlite_store, markdown_store, _ = temp_db
    _patch_cli_config(monkeypatch, config)

    entry_id, _ = _seed_entry(
        markdown_store,
        sqlite_store,
        title="python async guide",
        summary="python async overview",
        tags=["python", "async"],
        keywords="python,async",
        source_url="https://example.com/python-async",
    )

    result = runner.invoke(
        commands.cli,
        [
            "search",
            "python",
            "--strategy",
            "bm25",
            "--limit",
            "5",
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = _parse_json_payload(result.output)
    assert set(payload) == {"query", "strategy", "total", "results"}
    assert payload["query"] == "python"
    assert payload["strategy"] == "bm25"
    assert payload["total"] == len(payload["results"])
    assert payload["total"] >= 1
    for item in payload["results"]:
        assert set(item) == {"entry_id", "title", "snippet", "score", "metadata"}
        assert isinstance(item["entry_id"], int)
        assert not isinstance(item["entry_id"], bool)
    assert entry_id in [item["entry_id"] for item in payload["results"]]


def test_show_list_inprocess(
    runner: CliRunner,
    temp_db,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CLI show and list should render entries from the temporary database."""
    config, sqlite_store, markdown_store, _ = temp_db
    _patch_cli_config(monkeypatch, config)

    alpha_id, _ = _seed_entry(
        markdown_store,
        sqlite_store,
        title="alpha record",
        summary="alpha summary",
        tags=["alpha", "test"],
        keywords="alpha,test",
        source_url="https://example.com/alpha",
    )
    _seed_entry(
        markdown_store,
        sqlite_store,
        title="beta record",
        summary="beta summary",
        tags=["beta", "test"],
        keywords="beta,test",
        source_url="https://example.com/beta",
    )

    list_result = runner.invoke(commands.cli, ["list", "--limit", "10"])
    assert list_result.exit_code == 0, list_result.output
    list_output = _strip_ansi(list_result.output)
    assert "alpha record" in list_output
    assert "beta record" in list_output

    show_result = runner.invoke(commands.cli, ["show", str(alpha_id)])
    assert show_result.exit_code == 0, show_result.output
    show_output = _strip_ansi(show_result.output)
    assert "alpha record" in show_output


def test_config_inprocess(
    runner: CliRunner,
    temp_db,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CLI config commands should read and update configuration values."""
    config, _, _, tmp_path = temp_db
    _patch_cli_config(monkeypatch, config)
    monkeypatch.setattr(commands, "_project_root", lambda: tmp_path, raising=False)

    get_result = runner.invoke(commands.cli, ["config", "get", "storage.vault_dir"])
    assert get_result.exit_code == 0, get_result.output
    get_output = _strip_ansi(get_result.output)
    # Windows 终端可能将长路径换行输出，去掉空白字符后再比较
    assert str(config.vault_dir).replace("\\", "/") in get_output.replace("\n", "").replace(" ", "").replace("\\", "/")

    set_result = runner.invoke(
        commands.cli,
        ["config", "set", "ai.llm.model", "test-model"],
    )
    assert set_result.exit_code == 0, set_result.output

    local_config_path = tmp_path / "config" / "local.yaml"
    assert local_config_path.exists()
    local_config = yaml.safe_load(local_config_path.read_text(encoding="utf-8"))
    assert local_config["ai"]["llm"]["model"] == "test-model"
