# ruff: noqa: E402

"""
Wechat processor unit tests.
"""

import os
import sys
from pathlib import Path
from types import SimpleNamespace

# Add project root to Python path
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.processors.wechat_processor import WechatProcessor
from src.runtime.errors import ErrorCode, PKVRuntimeError
from src.runtime.layout import RuntimeLayout


@pytest.fixture
def wechat_html() -> str:
    """Load Wechat HTML fixture."""
    fixture_path = Path(__file__).parent.parent / "fixtures" / "wechat_sample.html"
    return fixture_path.read_text(encoding="utf-8")


def test_wechat_can_handle():
    """can_handle should recognize Wechat URLs."""
    assert WechatProcessor.can_handle("https://mp.weixin.qq.com/s/test")
    assert not WechatProcessor.can_handle("https://example.com")


@pytest.mark.asyncio
async def test_wechat_process(wechat_html: str):
    """Processor should extract Wechat metadata and content."""
    processor = WechatProcessor()

    with patch.object(processor, "_fetch_html", new=AsyncMock(return_value=wechat_html)):
        entry = await processor.process("https://mp.weixin.qq.com/s/test")

    assert entry.title == "Wechat Sample Title"
    assert entry.source_type == "wechat"
    assert entry.source_url == "https://mp.weixin.qq.com/s/test"
    assert "Wechat main content paragraph." in entry.content

    metadata = getattr(entry, "metadata", {})
    assert metadata.get("author") == "Wechat Author"
    assert metadata.get("published_time") == "2026-02-14 12:00:00"
    assert entry.published_at == "2026-02-14 12:00:00"


@pytest.mark.asyncio
async def test_wechat_process_without_publish_time_keeps_published_at_empty():
    processor = WechatProcessor()
    html = """
    <html>
      <head><title>Wechat No Time</title></head>
      <body>
        <div id="img-content">
          <p>Wechat main content paragraph.</p>
        </div>
      </body>
    </html>
    """

    with (
        patch.object(processor, "_fetch_html", new=AsyncMock(return_value=html)),
        patch.object(processor, "_download_images", new=AsyncMock(return_value=None)),
    ):
        entry = await processor.process("https://mp.weixin.qq.com/s/no-time")

    metadata = getattr(entry, "metadata", {})
    assert "published_time" not in metadata
    assert entry.published_at is None


# ============================================================
# 统一可写叶子合同：微信图片临时文件
# ============================================================


def _runtime_layout(tmp_path: Path) -> RuntimeLayout:
    """把数据根绑定到 tmp 目录的显式 layout（完整 containment 合同）。"""
    resources = tmp_path / "resources"
    resources.mkdir()
    return RuntimeLayout.resolve(
        resources_root=resources,
        user_data_root=tmp_path / "data",
        environment={},
    )


def _processor_with_layout(tmp_path: Path):
    """构造绑定显式 layout 的 processor（覆盖默认离线配置）。"""
    layout = _runtime_layout(tmp_path)
    processor = WechatProcessor()
    processor._config = SimpleNamespace(layout=layout)
    processor.tmp_dir = layout.tmp_dir
    return processor, layout


def test_wechat_image_write_publishes_atomically(tmp_path: Path):
    """微信图片写完整临时文件后原子发布，不留临时残留。"""
    processor, layout = _processor_with_layout(tmp_path)
    target = layout.tmp_dir / "wechat_img.jpg"

    processor._write_image_file(target, b"image-bytes")

    assert target.read_bytes() == b"image-bytes"
    assert list(layout.tmp_dir.glob(f".{target.name}.*.tmp")) == []


def test_wechat_image_write_rejects_hardlinked_target(tmp_path: Path):
    """目标叶子是硬链接时，写入前拒绝且根外文件不被覆盖。"""
    processor, layout = _processor_with_layout(tmp_path)
    layout.ensure_user_directories()
    target = layout.tmp_dir / "wechat_img.jpg"
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"attacker")
    target.write_bytes(b"original")
    target.unlink()
    os.link(outside, target)

    with pytest.raises(PKVRuntimeError) as exc_info:
        processor._write_image_file(target, b"new-content")

    assert exc_info.value.code is ErrorCode.DATA_ROOT_UNSAFE
    assert outside.read_bytes() == b"attacker"
    assert target.read_bytes() == b"attacker"


def test_wechat_image_write_rejects_symlinked_target(tmp_path: Path):
    """目标叶子是 symlink 时，写入前拒绝且根外文件不被改写。"""
    processor, layout = _processor_with_layout(tmp_path)
    layout.ensure_user_directories()
    target = layout.tmp_dir / "wechat_img.jpg"
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"attacker")
    try:
        target.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlink unavailable: {exc}")

    with pytest.raises(PKVRuntimeError) as exc_info:
        processor._write_image_file(target, b"new-content")

    assert exc_info.value.code is ErrorCode.DATA_ROOT_UNSAFE
    assert outside.read_bytes() == b"attacker"


def test_wechat_image_write_failure_preserves_target_and_cleans_temp(
    tmp_path: Path,
):
    """发布失败时原目标字节不变且临时文件被清理。"""
    processor, layout = _processor_with_layout(tmp_path)
    layout.ensure_user_directories()
    target = layout.tmp_dir / "wechat_img.jpg"
    target.write_bytes(b"original")

    def fail_replace(source, destination):
        raise OSError("injected replace failure")

    with patch("src.runtime.layout.os.replace", side_effect=fail_replace):
        with pytest.raises(OSError, match="injected replace failure"):
            processor._write_image_file(target, b"new-content")

    assert target.read_bytes() == b"original"
    assert list(layout.tmp_dir.glob(f".{target.name}.*.tmp")) == []


@pytest.mark.asyncio
async def test_wechat_download_image_uses_contract(tmp_path: Path):
    """_download_image 通过合同写入临时图片并返回 file URI。"""
    processor, layout = _processor_with_layout(tmp_path)
    layout.ensure_user_directories()
    client = AsyncMock()
    response = MagicMock()
    response.content = b"image-bytes"
    client.get = AsyncMock(return_value=response)

    uri = await processor._download_image(
        client,
        "https://mp.weixin.qq.com/s/article/image.png",
    )

    assert uri.startswith("file:")
    files = list(layout.tmp_dir.iterdir())
    assert len(files) == 1
    assert files[0].read_bytes() == b"image-bytes"
