"""Cross-layer regression for WeChat image URL publication safety."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.mcp.resources import get_entry_content
from src.processors.safe_fetch import SafeResponse
from src.processors.wechat_processor import WechatProcessor
from src.storage.coordinator import StorageCoordinator
from src.storage.markdown_store import MarkdownStore
from src.storage.migration_manager import MigrationManager
from src.storage.sqlite_store import SQLiteStore


PROJECT_ROOT = Path(__file__).parent.parent.parent
MIGRATIONS_DIR = PROJECT_ROOT / "scripts" / "migrations"
IMAGE_SECRET = "IMG_SECRET"
PUBLIC_IMAGE_URL = "https://img.example/p.png?token=redacted&width=640"


def _assert_public_image_boundary(value: str, *, private_tmp: Path) -> None:
    """Assert one persisted/public surface contains only the safe public URL."""

    lowered = value.lower()
    assert PUBLIC_IMAGE_URL in value
    assert "file:" not in lowered
    assert IMAGE_SECRET not in value
    assert "private-fragment" not in value
    assert str(private_tmp) not in value
    assert private_tmp.as_uri() not in value
    assert private_tmp.name not in value


@pytest.mark.asyncio
async def test_wechat_image_url_stays_public_safe_through_mcp_resource(
    tmp_path: Path,
) -> None:
    """Temporary image paths and query secrets never cross the archive boundary."""

    data_root = tmp_path / "data"
    db_path = data_root / "db" / "vault.db"
    MigrationManager(
        db_path,
        MIGRATIONS_DIR,
        backup_dir=data_root / "backups",
    ).initialize_fresh()
    markdown_store = MarkdownStore(data_root / "vault")
    sqlite_store = SQLiteStore(db_path)
    coordinator = StorageCoordinator(
        markdown_store,
        sqlite_store,
        data_root / "runtime" / "operations",
    )

    private_tmp = tmp_path / "PRIVATE_TMP_CANARY"
    processor = WechatProcessor()
    processor._config = SimpleNamespace(layout=None)
    processor.tmp_dir = private_tmp
    image_url = (
        f"https://img.example/p.png?token={IMAGE_SECRET}"
        "&width=640#private-fragment"
    )
    html = (
        "<html><head><title>WeChat image boundary</title></head><body>"
        "<div id=\"js_content\"><p>archive boundary canary</p>"
        f"<img alt=\"fixture\" data-src=\"{image_url}\"></div>"
        "</body></html>"
    )
    image_fetcher = MagicMock()
    image_fetcher.fetch = AsyncMock(
        return_value=SafeResponse(
            url=image_url,
            status_code=200,
            headers={"content-type": "image/png"},
            content=b"synthetic-image-bytes",
        )
    )
    processor._safe_fetcher = image_fetcher

    with patch.object(processor, "_fetch_html", new=AsyncMock(return_value=html)):
        entry = await processor.process("https://mp.weixin.qq.com/s/canary")

    assert image_fetcher.fetch.await_count == 1
    assert private_tmp.is_dir()
    assert list(private_tmp.iterdir())
    _assert_public_image_boundary(entry.content, private_tmp=private_tmp)

    archive_result = coordinator.archive(entry)
    assert archive_result.successful
    assert archive_result.core_committed
    assert archive_result.knowledge_id is not None
    assert archive_result.file_path is not None
    assert archive_result.relative_file_path is not None

    knowledge_id = archive_result.knowledge_id
    markdown_entry = markdown_store.load(archive_result.relative_file_path)
    sqlite_entry = sqlite_store.query_by_id(knowledge_id)
    assert sqlite_entry is not None
    chunks = sqlite_store.get_chunks_by_knowledge_id(knowledge_id)
    assert chunks

    with (
        patch(
            "src.mcp.resources.get_sqlite_store",
            return_value=sqlite_store,
        ),
        patch(
            "src.mcp.resources.get_markdown_store",
            return_value=markdown_store,
        ),
    ):
        resource_content = await get_entry_content(str(knowledge_id))

    persisted_file = Path(archive_result.file_path).read_text(encoding="utf-8")
    surfaces = {
        "markdown": markdown_entry.content,
        "markdown_file": persisted_file,
        "sqlite": str(sqlite_entry["content"]),
        "sqlite_chunks": "\n".join(str(chunk["chunk_text"]) for chunk in chunks),
        "mcp_resource": resource_content,
    }
    for value in surfaces.values():
        _assert_public_image_boundary(value, private_tmp=private_tmp)
