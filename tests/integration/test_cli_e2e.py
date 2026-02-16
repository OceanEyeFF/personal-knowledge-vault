"""
CLI end-to-end integration tests.

These tests use a temporary SQLite database and Click's CliRunner to exercise
the CLI as a user would. Network-dependent workflows are intentionally skipped
by default.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

import pytest
from click.testing import CliRunner

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import src.cli.commands as commands
from src.storage.markdown_store import Entry, MarkdownStore
from src.storage.sqlite_store import SQLiteStore
from src.utils import config as config_module
from src.utils.config import Config


ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences from CLI output."""
    return ANSI_ESCAPE_RE.sub("", text)


def _extract_json_payload(output: str) -> Dict[str, Any]:
    """Extract a JSON object from CLI output."""
    text = output.strip()
    if not text:
        raise AssertionError("CLI output is empty; expected JSON payload")
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise AssertionError(f"CLI output does not contain JSON: {output}")
    payload = text[start : end + 1]
    return json.loads(payload)


def _collect_result_ids(payload: Dict[str, Any]) -> List[int]:
    """Collect result IDs from a JSON payload in a tolerant way."""
    ids: List[int] = []
    results = payload.get("results", [])
    for item in results:
        if not isinstance(item, dict):
            continue
        for key in ("entry_id", "knowledge_id", "id"):
            if key in item and isinstance(item[key], int):
                ids.append(item[key])
    return ids


def _configure_temp_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    """Create a Config that points all storage paths to the temp directory."""
    db_path = tmp_path / "db" / "knowledge_vault.db"
    monkeypatch.setenv("DB_PATH", str(db_path))

    # Reset the global config singleton to avoid cross-test reuse.
    config_module._config_instance = None
    config = Config()

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


def test_search_e2e(
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
    payload = _extract_json_payload(result.output)
    assert payload.get("total", 0) >= 1
    assert entry_id in _collect_result_ids(payload)


def test_show_list_e2e(
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


def test_config_e2e(
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
    assert str(config.vault_dir) in get_output

    set_result = runner.invoke(
        commands.cli,
        ["config", "set", "TEST_KEY", "test-value"],
    )
    assert set_result.exit_code == 0, set_result.output

    env_path = tmp_path / ".env"
    assert env_path.exists()
    assert "TEST_KEY=test-value" in env_path.read_text(encoding="utf-8")


ARCHIVE_TEST_URL = os.getenv("PKV_E2E_ARCHIVE_URL")


@pytest.mark.skipif(
    not ARCHIVE_TEST_URL,
    reason="Set PKV_E2E_ARCHIVE_URL to run the live archive test",
)
def test_archive_url_e2e(
    runner: CliRunner,
    temp_db,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Optional: archive a real URL end-to-end (requires network/API keys)."""
    config, _, _, _ = temp_db
    _patch_cli_config(monkeypatch, config)

    result = runner.invoke(
        commands.cli,
        ["archive", ARCHIVE_TEST_URL, "--skip-sharpen"],
    )

    assert result.exit_code == 0, result.output
