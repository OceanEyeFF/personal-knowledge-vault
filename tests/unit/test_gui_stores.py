"""GUI storage accessors must not create vector indexes from read/delete paths."""

from pathlib import Path
from types import SimpleNamespace
import logging
from unittest.mock import patch

from src.gui import stores


def setup_function() -> None:
    stores.reset_stores()


def teardown_function() -> None:
    stores.reset_stores()


def test_delete_accessor_does_not_create_missing_vector_pair(tmp_path: Path) -> None:
    config = SimpleNamespace(vector_index_dir=tmp_path / "vectors")

    with (
        patch("src.utils.config.get_config", return_value=config),
        patch("src.storage.vector_store.VectorStore") as vector_type,
    ):
        vector_type.has_index_artifacts.return_value = False

        result = stores.get_vector_store()

    assert result is None
    vector_type.assert_not_called()


def test_delete_accessor_opens_existing_vector_pair_read_safe(tmp_path: Path) -> None:
    config = SimpleNamespace(vector_index_dir=tmp_path / "vectors")
    sentinel = object()

    with (
        patch("src.utils.config.get_config", return_value=config),
        patch("src.storage.vector_store.VectorStore") as vector_type,
    ):
        vector_type.has_index_artifacts.return_value = True
        vector_type.return_value = sentinel

        result = stores.get_vector_store()

    assert result is sentinel
    vector_type.assert_called_once_with(
        index_dir=config.vector_index_dir,
        dim=None,
        allow_index_creation=False,
    )


def test_store_initialization_logs_do_not_expose_data_root(
    tmp_path: Path,
    caplog,
) -> None:
    """GUI accessor logs expose only fixed component state, never local paths."""
    path_canary = "PKV_DATA_ROOT-PATH-CANARY-api_key=gui-store-secret"
    data_root = tmp_path / path_canary
    config = SimpleNamespace(
        db_path=data_root / "index" / "knowledge.db",
        vault_dir=data_root / "vault",
        vector_index_dir=data_root / "vector",
    )

    with (
        patch("src.utils.config.get_config", return_value=config),
        patch("src.storage.sqlite_store.SQLiteStore"),
        patch("src.storage.markdown_store.MarkdownStore"),
        patch("src.storage.vector_store.VectorStore") as vector_type,
        patch("src.retrieval.bm25_retriever.BM25Retriever"),
        caplog.at_level(logging.INFO, logger="pkv.gui.stores"),
    ):
        vector_type.has_index_artifacts.return_value = True
        stores.get_sqlite_store()
        stores.get_markdown_store()
        stores.get_vector_store()
        stores.get_bm25_retriever()

    assert [record.getMessage() for record in caplog.records] == [
        "store_initialized component=sqlite status=ready",
        "store_initialized component=markdown status=ready",
        "store_initialized component=vector status=ready",
        "retriever_initialized component=bm25 status=ready",
    ]
    assert path_canary not in caplog.text
    assert str(data_root) not in caplog.text
