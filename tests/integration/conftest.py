"""Shared fixtures for in-process integration tests."""

from pathlib import Path

import pytest

from src.storage.sqlite_store import SQLiteStore


@pytest.fixture
def mcp_test_db(tmp_path: Path) -> SQLiteStore:
    """Return a freshly initialized SQLite store for one MCP test."""
    store = SQLiteStore(tmp_path / "test.db")
    store.initialize()
    return store


@pytest.fixture
def mcp_test_vault(tmp_path: Path) -> Path:
    """Return an empty Vault owned by one MCP test."""
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    return vault_dir
