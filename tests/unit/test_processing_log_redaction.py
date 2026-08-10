"""Regression tests for processing and storage log redaction boundaries."""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.processors.ai_chat_processor import AIChatProcessor
from src.processors.chat_processor import ChatProcessor
from src.processors.generic_processor import GenericProcessor
from src.processors.text_fallback_processor import TextFallbackProcessor
from src.processors.wechat_processor import WechatProcessor
from src.processors.zhihu_processor import ZhihuProcessor
from src.storage.markdown_store import Entry, MarkdownStore
from src.storage.migration_manager import MigrationManager
from src.storage.review_manager import ReviewItem, ReviewManager
from src.storage.sqlite_store import SQLiteStore
from src.storage.vector_store import VectorStore, _PairTransaction


PROJECT_ROOT = Path(__file__).resolve().parents[2]
LOG_SECRET = "LOG-SECRET-7f4e2"
PRIVATE_PATH_COMPONENT = (
    f"PKV_DATA_ROOT-PRIVATE-PATH-FORGED-api_key={LOG_SECRET}"
)
SENSITIVE_TITLE = f"PRIVATE-TITLE-{LOG_SECRET}\r\nFORGED api_key={LOG_SECRET}"
SENSITIVE_CONTENT = f"PRIVATE-CONTENT-{LOG_SECRET}\r\nFORGED api_key={LOG_SECRET}"
SENSITIVE_ERROR = f"upstream failed\r\nFORGED api_key={LOG_SECRET}"
SENSITIVE_PATH_NAME = f"{PRIVATE_PATH_COMPONENT}.txt"
SENSITIVE_FILE_TITLE = f"PRIVATE-TITLE-{PRIVATE_PATH_COMPONENT}"


def _captured_messages(caplog: pytest.LogCaptureFixture) -> str:
    return "\n".join(record.getMessage() for record in caplog.records)


def _assert_sensitive_values_are_absent(
    caplog: pytest.LogCaptureFixture,
    *raw_values: str,
) -> None:
    messages = _captured_messages(caplog)
    assert messages
    assert LOG_SECRET not in messages
    assert "PKV_DATA_ROOT" not in messages
    assert "PRIVATE-PATH" not in messages
    assert "api_key" not in messages
    assert "FORGED" not in messages
    assert "\r" not in messages
    assert all("\r" not in record.getMessage() and "\n" not in record.getMessage()
               for record in caplog.records)
    for raw_value in raw_values:
        assert raw_value not in messages


@pytest.mark.asyncio
async def test_generic_processor_logs_neither_remote_payload_nor_failure_detail(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="src.processors.generic_processor")
    processor = GenericProcessor()
    url = f"https://example.com/private/{LOG_SECRET}?api_key={LOG_SECRET}#FORGED"
    html = (
        f"<html><head><title>{SENSITIVE_TITLE}</title></head>"
        f"<body><article><p>{SENSITIVE_CONTENT}</p></article></body></html>"
    )

    with patch.object(processor, "_fetch_html", new=AsyncMock(return_value=html)):
        await processor.process(url)

    messages = _captured_messages(caplog)
    assert "GenericProcessor completed target=https://example.com" in messages
    _assert_sensitive_values_are_absent(
        caplog,
        url,
        SENSITIVE_TITLE,
        SENSITIVE_CONTENT,
    )

    caplog.clear()
    with patch.object(
        processor,
        "_fetch_html",
        new=AsyncMock(side_effect=RuntimeError(SENSITIVE_ERROR)),
    ):
        with pytest.raises(RuntimeError, match="upstream failed"):
            await processor.process(url)

    assert "GenericProcessor processing target=https://example.com" in _captured_messages(
        caplog
    )
    _assert_sensitive_values_are_absent(caplog, url, SENSITIVE_ERROR)


@pytest.mark.asyncio
async def test_wechat_processor_logs_neither_remote_payload_nor_failure_detail(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="src.processors.wechat_processor")
    processor = WechatProcessor()
    url = (
        f"https://mp.weixin.qq.com/s/private-{LOG_SECRET}"
        f"?api_key={LOG_SECRET}#FORGED"
    )
    html = (
        f"<html><head><title>{SENSITIVE_TITLE}</title></head><body>"
        f'<div id="js_content"><p>{SENSITIVE_CONTENT}</p></div>'
        "</body></html>"
    )

    with (
        patch.object(processor, "_fetch_html", new=AsyncMock(return_value=html)),
        patch.object(processor, "_download_images", new=AsyncMock(return_value=[])),
    ):
        await processor.process(url)

    messages = _captured_messages(caplog)
    assert "WechatProcessor completed target=https://mp.weixin.qq.com" in messages
    _assert_sensitive_values_are_absent(
        caplog,
        url,
        SENSITIVE_TITLE,
        SENSITIVE_CONTENT,
    )

    caplog.clear()
    with patch.object(
        processor,
        "_fetch_html",
        new=AsyncMock(side_effect=RuntimeError(SENSITIVE_ERROR)),
    ):
        with pytest.raises(RuntimeError, match="upstream failed"):
            await processor.process(url)

    assert "WechatProcessor processing target=https://mp.weixin.qq.com" in (
        _captured_messages(caplog)
    )
    _assert_sensitive_values_are_absent(caplog, url, SENSITIVE_ERROR)


@pytest.mark.asyncio
async def test_zhihu_processor_logs_neither_remote_payload_nor_failure_detail(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="src.processors.zhihu_processor")
    processor = ZhihuProcessor()
    url = f"https://www.zhihu.com/question/{LOG_SECRET}?api_key={LOG_SECRET}#FORGED"
    html = (
        "<html><body>"
        f'<h1 class="QuestionHeader-title">{SENSITIVE_TITLE}</h1>'
        f'<div class="RichContent-inner"><p>{SENSITIVE_CONTENT}</p></div>'
        "</body></html>"
    )

    with patch.object(processor, "_fetch_html", new=AsyncMock(return_value=html)):
        await processor.process(url)

    messages = _captured_messages(caplog)
    assert "ZhihuProcessor completed target=https://www.zhihu.com" in messages
    _assert_sensitive_values_are_absent(
        caplog,
        url,
        SENSITIVE_TITLE,
        SENSITIVE_CONTENT,
    )

    caplog.clear()
    with patch.object(
        processor,
        "_fetch_html",
        new=AsyncMock(side_effect=RuntimeError(SENSITIVE_ERROR)),
    ):
        with pytest.raises(RuntimeError, match="upstream failed"):
            await processor.process(url)

    assert "ZhihuProcessor processing target=https://www.zhihu.com" in (
        _captured_messages(caplog)
    )
    _assert_sensitive_values_are_absent(caplog, url, SENSITIVE_ERROR)


@pytest.mark.asyncio
async def test_ai_chat_processor_logs_only_shape_and_failure_type(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="src.processors.ai_chat_processor")
    client = MagicMock()
    client.summarize.side_effect = RuntimeError(SENSITIVE_ERROR)
    processor = AIChatProcessor(deepseek_client=client)
    raw_chat = (
        f"**User**: {SENSITIVE_TITLE}\r\n"
        f"{SENSITIVE_CONTENT}\r\n"
        "**Assistant**: synthetic response"
    )

    await processor.process(raw_chat)

    messages = _captured_messages(caplog)
    assert "AIChatProcessor processing input_length=" in messages
    assert "AI summary generation failed: error_type=RuntimeError" in messages
    assert "AIChatProcessor completed format=markdown platform=chatgpt messages=2" in messages
    _assert_sensitive_values_are_absent(
        caplog,
        raw_chat,
        SENSITIVE_TITLE,
        SENSITIVE_CONTENT,
        SENSITIVE_ERROR,
    )


@pytest.mark.asyncio
async def test_chat_processor_logs_neither_file_path_nor_content(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    chat_path = tmp_path / SENSITIVE_PATH_NAME
    raw_chat = (
        f"Alice 10:00\r\n{SENSITIVE_CONTENT}\r\n\r\n"
        "Bob 10:01\r\nsynthetic response"
    )
    chat_path.write_bytes(raw_chat.encode("utf-8"))
    client = MagicMock()
    client.summarize.side_effect = RuntimeError(SENSITIVE_ERROR)
    processor = ChatProcessor(deepseek_client=client)
    caplog.set_level(logging.INFO, logger="src.processors.chat_processor")

    await processor.process_file(chat_path)

    messages = _captured_messages(caplog)
    assert "ChatProcessor processing input_type=file" in messages
    assert "AI summary generation failed: error_type=RuntimeError" in messages
    assert "ChatProcessor completed messages=2" in messages
    _assert_sensitive_values_are_absent(
        caplog,
        str(chat_path),
        chat_path.name,
        raw_chat,
        SENSITIVE_CONTENT,
        SENSITIVE_ERROR,
    )


@pytest.mark.asyncio
async def test_text_fallback_processor_logs_only_type_and_lengths(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="src.processors.text_fallback_processor")
    client = MagicMock()
    client.summarize.side_effect = RuntimeError(SENSITIVE_ERROR)
    processor = TextFallbackProcessor(deepseek_client=client)
    raw_text = f"{SENSITIVE_TITLE}\r\n{SENSITIVE_CONTENT}\r\nsynthetic body"

    await processor.process_text(raw_text)

    messages = _captured_messages(caplog)
    assert "TextFallbackProcessor processing input length=" in messages
    assert "AI summary generation failed: error_type=RuntimeError" in messages
    assert "TextFallbackProcessor completed type=article content_length=" in messages
    _assert_sensitive_values_are_absent(
        caplog,
        raw_text,
        SENSITIVE_TITLE,
        SENSITIVE_CONTENT,
        SENSITIVE_ERROR,
    )


def test_sqlite_insert_logs_neither_entry_values_nor_failure_detail(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = SQLiteStore(tmp_path / "neutral.db")
    store.initialize()
    private_file_path = tmp_path / SENSITIVE_PATH_NAME
    entry = Entry(
        title=SENSITIVE_TITLE,
        source_type="generic",
        source_url=f"https://example.com/?api_key={LOG_SECRET}#FORGED",
        content=SENSITIVE_CONTENT,
    )
    caplog.set_level(logging.INFO, logger="src.storage.sqlite_store")
    caplog.clear()

    knowledge_id = store.insert_entry(entry, str(private_file_path))

    messages = _captured_messages(caplog)
    assert f"插入知识条目: knowledge_id={knowledge_id}" in messages
    assert "source_type=generic" in messages
    assert f"content_length={len(SENSITIVE_CONTENT)}" in messages
    _assert_sensitive_values_are_absent(
        caplog,
        entry.title,
        entry.content,
        str(private_file_path),
        private_file_path.name,
    )

    caplog.clear()
    with patch.object(store, "_insert_entry", side_effect=RuntimeError(SENSITIVE_ERROR)):
        with pytest.raises(RuntimeError, match="upstream failed"):
            store.insert_entry(entry, str(private_file_path))

    messages = _captured_messages(caplog)
    assert "数据库操作失败" in messages
    _assert_sensitive_values_are_absent(
        caplog,
        entry.title,
        entry.content,
        str(private_file_path),
        private_file_path.name,
        SENSITIVE_ERROR,
    )

    caplog.clear()
    assert store.delete_entry(SENSITIVE_ERROR) is False  # type: ignore[arg-type]
    store.update_session(
        "missing-session",
        [],
        SENSITIVE_ERROR,  # type: ignore[arg-type]
        SENSITIVE_ERROR,  # type: ignore[arg-type]
    )
    messages = _captured_messages(caplog)
    assert "知识条目不存在，无法删除" in messages
    assert "更新会话成功" in messages
    _assert_sensitive_values_are_absent(caplog, SENSITIVE_ERROR)


def test_sqlite_initialization_and_integrity_logs_hide_paths_and_payloads(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    private_root = tmp_path / PRIVATE_PATH_COMPONENT
    caplog.set_level(logging.INFO, logger="src.storage.sqlite_store")

    store = SQLiteStore(private_root / "db" / "vault.db")

    foreign_key_cursor = MagicMock()
    foreign_key_cursor.fetchall.return_value = [
        (SENSITIVE_CONTENT, SENSITIVE_PATH_NAME, LOG_SECRET)
    ]
    integrity_cursor = MagicMock()
    integrity_cursor.fetchone.return_value = (SENSITIVE_ERROR,)
    connection = MagicMock()
    connection.execute.side_effect = [foreign_key_cursor, integrity_cursor]
    store._verify_integrity(connection)

    messages = _captured_messages(caplog)
    assert "SQLite 存储初始化完成" in messages
    assert "发现外键约束违规: count=1" in messages
    assert "数据库完整性检查失败: status=invalid" in messages
    _assert_sensitive_values_are_absent(
        caplog,
        str(private_root),
        SENSITIVE_CONTENT,
        SENSITIVE_ERROR,
    )


def test_markdown_store_logs_hide_vault_and_file_paths(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    private_root = tmp_path / PRIVATE_PATH_COMPONENT
    caplog.set_level(logging.INFO, logger="src.storage.markdown_store")
    store = MarkdownStore(private_root / "vault")
    entry = Entry(
        title=SENSITIVE_FILE_TITLE,
        source_type="generic",
        content=SENSITIVE_CONTENT,
    )

    saved_path = store.save(entry)
    loaded = store.load(saved_path)
    store.delete(saved_path)
    missing_path = store.vault_dir / f"missing-{SENSITIVE_PATH_NAME}.md"
    store.delete(missing_path)

    assert LOG_SECRET in loaded.content
    assert f"FORGED api_key={LOG_SECRET}" in loaded.content
    messages = _captured_messages(caplog)
    assert "Markdown 存储初始化完成" in messages
    assert "加载 Markdown 文件完成" in messages
    assert "删除 Markdown 文件完成" in messages
    assert "Markdown 文件不存在，无法删除" in messages
    _assert_sensitive_values_are_absent(
        caplog,
        str(private_root),
        str(saved_path),
        str(missing_path),
        SENSITIVE_FILE_TITLE,
        SENSITIVE_CONTENT,
    )


def test_migration_logs_hide_runtime_and_backup_paths(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    private_root = tmp_path / PRIVATE_PATH_COMPONENT
    db_path = private_root / "db" / "vault.db"
    migrations_dir = PROJECT_ROOT / "scripts" / "migrations"
    caplog.set_level(logging.INFO, logger="src.storage.migration_manager")

    MigrationManager(
        db_path,
        private_root / "missing-migrations",
        backup_dir=private_root / "backups",
    )
    SQLiteStore(db_path).initialize()
    manager = MigrationManager(
        db_path,
        migrations_dir,
        backup_dir=private_root / "backups",
    )
    backup_path = manager._backup_database(SENSITIVE_PATH_NAME)
    manager._publication_lock_identity = (1, 2)
    with patch(
        "src.storage.migration_manager.os.lstat",
        side_effect=OSError(SENSITIVE_ERROR),
    ):
        manager._release_publication_lock()

    assert backup_path.is_file()
    assert PRIVATE_PATH_COMPONENT in str(backup_path)
    messages = _captured_messages(caplog)
    assert "迁移目录不存在" in messages
    assert "自动备份数据库开始" in messages
    assert "无法核对迁移发布锁身份" in messages
    assert "error_type=OSError" in messages
    _assert_sensitive_values_are_absent(
        caplog,
        str(private_root),
        str(db_path),
        str(backup_path),
        SENSITIVE_PATH_NAME,
    )


def test_vector_store_logs_component_names_without_index_paths(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    private_root = tmp_path / PRIVATE_PATH_COMPONENT
    vector_dir = private_root / "vectors"
    config = SimpleNamespace(
        layout=None,
        embd_base_url="https://embedding.example/v1",
        embd_model="synthetic-model",
        embedding_index_fingerprint=lambda dim: {
            "base_url": "https://embedding.example/v1",
            "embedding_model": "synthetic-model",
            "embedding_dim": str(dim),
        },
    )
    caplog.set_level(logging.INFO, logger="src.storage.vector_store")

    with patch("src.storage.vector_store.get_config", return_value=config):
        VectorStore(vector_dir, dim=4)
        loaded_store = VectorStore(vector_dir, dim=4)
    with (
        patch.object(
            loaded_store,
            "_reload_index_for_update_locked",
            return_value=MagicMock(),
        ),
        patch.object(
            loaded_store,
            "_load_metadata",
            return_value={"id_mapping": {"123\r\n": [1, 0]}},
        ),
        patch.object(
            loaded_store,
            "_chunk_vector_exists",
            return_value=False,
        ),
    ):
        assert loaded_store.get_chunk_indices_for_entry(1) == []

    messages = _captured_messages(caplog)
    assert "创建新索引: component=doc_vectors" in messages
    assert "创建新索引: component=chunk_vectors" in messages
    assert "加载已有索引: component=doc_vectors" in messages
    assert "加载已有索引: component=chunk_vectors" in messages
    assert "hnswlib_id=123" in messages
    assert "向量存储初始化完成" in messages
    _assert_sensitive_values_are_absent(caplog, str(private_root), str(vector_dir))


def test_vector_auxiliary_cleanup_warning_hides_path_and_failure_detail(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    private_root = tmp_path / PRIVATE_PATH_COMPONENT
    private_root.mkdir()
    marker_path = private_root / "marker.json"
    marker_path.write_text("{}", encoding="utf-8")
    auxiliary_path = private_root / SENSITIVE_PATH_NAME
    auxiliary_path.write_text(SENSITIVE_CONTENT, encoding="utf-8")
    transaction = _PairTransaction(
        name="doc_vectors",
        payload={
            "originals": {
                "index": {
                    "rollback": {
                        "file_name": auxiliary_path.name,
                    }
                }
            },
            "outputs": {},
            "recovery_outputs": {},
        },
        marker_identity=(1, 2),
        marker_sha256="synthetic-sha256",
    )
    store = object.__new__(VectorStore)
    store.index_dir = private_root
    store._pair_transaction_path = MagicMock(return_value=marker_path)
    store._read_small_file_snapshot = MagicMock(
        return_value=(
            b"{}",
            {
                "identity": [1, 2],
                "sha256": "synthetic-sha256",
            },
        )
    )
    store._unlink_exact_snapshot = MagicMock(
        side_effect=RuntimeError(SENSITIVE_ERROR)
    )
    store._fsync_index_directory = MagicMock()
    caplog.set_level(logging.WARNING, logger="src.storage.vector_store")

    store._clear_pair_transaction(transaction)

    messages = _captured_messages(caplog)
    assert "component=doc_vectors" in messages
    _assert_sensitive_values_are_absent(
        caplog,
        str(private_root),
        str(auxiliary_path),
        SENSITIVE_CONTENT,
        SENSITIVE_ERROR,
    )


def test_review_manager_logs_tag_count_without_database_path_or_tags(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    private_root = tmp_path / PRIVATE_PATH_COMPONENT
    db_path = private_root / "db" / "vault.db"
    SQLiteStore(db_path).initialize()
    caplog.set_level(logging.DEBUG, logger="src.storage.review_manager")
    caplog.clear()
    manager = ReviewManager(db_path)
    review_id = manager.create_review(
        ReviewItem(
            ai_generated_summary="synthetic summary",
            ai_generated_tags="synthetic",
            source_type="generic",
        )
    )
    private_tags = [
        PRIVATE_PATH_COMPONENT,
        f"tag\r\nFORGED api_key={LOG_SECRET}",
    ]

    assert manager.update_user_tags(review_id, private_tags) is True
    assert manager.update_user_tags(  # type: ignore[arg-type]
        SENSITIVE_ERROR,
        private_tags,
    ) is False
    assert manager.get_review(SENSITIVE_ERROR) is None  # type: ignore[arg-type]

    messages = _captured_messages(caplog)
    assert "ReviewManager 初始化完成" in messages
    assert f"用户标签已更新: review_id={review_id}, tag_count=2" in messages
    assert "review_id=invalid" in messages
    _assert_sensitive_values_are_absent(
        caplog,
        str(private_root),
        str(db_path),
        *private_tags,
    )
