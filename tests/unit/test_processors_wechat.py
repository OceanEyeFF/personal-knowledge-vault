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
from bs4 import BeautifulSoup

from src.processors.wechat_processor import WechatProcessor
from src.processors.safe_fetch import SafeResponse
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
    assert not WechatProcessor.can_handle(
        "https://evil.example/?next=mp.weixin.qq.com"
    )


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


@pytest.mark.asyncio
async def test_wechat_empty_response_error_does_not_echo_url_secret():
    processor = WechatProcessor()
    secret = "pkv-url-secret"

    with patch.object(processor, "_fetch_html", new=AsyncMock(return_value="")):
        with pytest.raises(ValueError) as exc_info:
            await processor.process(f"https://mp.weixin.qq.com/s/test?token={secret}")

    assert secret not in str(exc_info.value)


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
    """内部下载 helper 通过合同写入临时图片；URI 不得进入公开正文。"""
    processor, layout = _processor_with_layout(tmp_path)
    layout.ensure_user_directories()
    fetcher = MagicMock()
    fetcher.fetch = AsyncMock(return_value=SafeResponse(
        url="https://mp.weixin.qq.com/s/article/image.png",
        status_code=200,
        headers={"content-type": "image/png"},
        content=b"image-bytes",
    ))
    processor._safe_fetcher = fetcher

    uri = await processor._download_image("https://mp.weixin.qq.com/s/article/image.png")

    assert uri.startswith("file:")
    assert fetcher.fetch.await_args.kwargs["max_response_bytes"] == 20 * 1024 * 1024
    files = list(layout.tmp_dir.iterdir())
    assert len(files) == 1
    assert files[0].read_bytes() == b"image-bytes"


@pytest.mark.asyncio
async def test_wechat_entry_content_never_publishes_tmp_file_uri(tmp_path: Path):
    private_root = tmp_path / "PRIVATE_TMP_CANARY"
    private_root.mkdir()
    processor, layout = _processor_with_layout(private_root)
    secret = "WECHAT-IMAGE-QUERY-SECRET"
    image_url = (
        f"https://img.example/article.png?token={secret}&width=640#private-fragment"
    )
    html = (
        "<html><head><title>Wechat image</title></head><body>"
        f'<div id="js_content"><img alt="fixture" data-src="{image_url}"/></div>'
        "</body></html>"
    )
    fetcher = MagicMock()
    fetcher.fetch = AsyncMock(
        return_value=SafeResponse(image_url, 200, {}, b"image-bytes")
    )
    processor._safe_fetcher = fetcher

    with patch.object(processor, "_fetch_html", new=AsyncMock(return_value=html)):
        entry = await processor.process("https://mp.weixin.qq.com/s/article")

    assert (
        "https://img.example/article.png?token=redacted&width=640"
        in entry.content
    )
    rendered = entry.content
    assert "file:" not in rendered.lower()
    assert secret not in rendered
    assert "private-fragment" not in rendered
    assert str(layout.tmp_dir) not in rendered
    assert layout.tmp_dir.as_uri() not in rendered
    assert list(layout.tmp_dir.iterdir())


@pytest.mark.asyncio
async def test_wechat_image_ssrf_denial_becomes_stable_processing_issue(
    tmp_path: Path,
    caplog,
):
    processor, layout = _processor_with_layout(tmp_path)
    canary = "WECHAT-SSRF-PRIVATE-CANARY"
    image_url = "https://public-looking.example/image.png"
    html = (
        "<html><head><title>Wechat image SSRF</title></head><body>"
        f'<div id="js_content"><img data-src="{image_url}"/></div>'
        "</body></html>"
    )
    fetcher = MagicMock()
    fetcher.fetch = AsyncMock(
        side_effect=PKVRuntimeError(
            ErrorCode.SSRF_TARGET_FORBIDDEN,
            f"forbidden {canary} C:\\private",
            stage="network_policy",
            recoverable=False,
        )
    )
    processor._safe_fetcher = fetcher

    with patch.object(processor, "_fetch_html", new=AsyncMock(return_value=html)):
        entry = await processor.process("https://mp.weixin.qq.com/s/article")

    assert entry.processing_issues == [
        {
            "code": ErrorCode.SSRF_TARGET_FORBIDDEN.value,
            "message": "禁止访问内网地址或其他非公网目标",
            "severity": "warning",
            "recoverable": False,
            "stage": "network_policy",
        }
    ]
    assert image_url in entry.content
    assert canary not in repr(entry.processing_issues)
    assert canary not in caplog.text
    assert r"C:\private" not in caplog.text
    assert list(layout.tmp_dir.iterdir()) == []


@pytest.mark.asyncio
async def test_wechat_image_userinfo_rejection_is_not_full_success(
    tmp_path: Path,
    caplog,
):
    processor, layout = _processor_with_layout(tmp_path)
    username = "WECHAT-IMAGE-URL-USER"
    password = "WECHAT-IMAGE-URL-PASSWORD"
    image_url = f"https://{username}:{password}@public.example/a.png"
    html = (
        "<html><head><title>Wechat image URL</title></head><body>"
        f'<div id="js_content"><img data-src="{image_url}"/></div>'
        "</body></html>"
    )
    fetcher = MagicMock()
    fetcher.fetch = AsyncMock()
    processor._safe_fetcher = fetcher

    with patch.object(processor, "_fetch_html", new=AsyncMock(return_value=html)):
        entry = await processor.process("https://mp.weixin.qq.com/s/article")

    assert entry.processing_issues == [
        {
            "code": ErrorCode.URL_INVALID.value,
            "message": "URL 格式无效",
            "severity": "warning",
            "recoverable": False,
            "stage": "network_policy",
        }
    ]
    fetcher.fetch.assert_not_awaited()
    assert "https://public.example/a.png" in entry.content
    assert username not in entry.content
    assert password not in entry.content
    assert username not in repr(entry.processing_issues)
    assert password not in repr(entry.processing_issues)
    assert username not in caplog.text
    assert password not in caplog.text
    assert list(layout.tmp_dir.iterdir()) == []


def _image_container(urls: list[str]):
    html = "<div>" + "".join(f'<img data-src="{url}"/>' for url in urls) + "</div>"
    return BeautifulSoup(html, "lxml").find("div")


def _assert_resource_limit_issue(issue: dict, *, stage: str, count: int, limit: int):
    assert set(issue) == {
        "code",
        "message",
        "severity",
        "recoverable",
        "stage",
        "count",
        "limit",
    }
    assert issue["code"] == ErrorCode.PROCESSOR_RESOURCE_LIMIT.value
    assert issue["stage"] == stage
    assert issue["count"] == count
    assert issue["limit"] == limit
    assert issue["severity"] == "warning"
    assert issue["recoverable"] is False


@pytest.mark.asyncio
async def test_wechat_image_count_budget_stops_before_max_plus_one(tmp_path: Path):
    processor, layout = _processor_with_layout(tmp_path)
    processor.max_images = 2
    processor.max_image_bytes = 10
    processor.max_total_image_bytes = 20
    secret = "pkv-image-secret"
    fetcher = MagicMock()
    fetcher.fetch = AsyncMock(
        side_effect=[
            SafeResponse("https://img.example/1.png", 200, {}, b"1"),
            SafeResponse("https://img.example/2.png", 200, {}, b"2"),
        ]
    )
    processor._safe_fetcher = fetcher
    content = _image_container(
        [
            "https://img.example/1.png",
            "https://img.example/2.png",
            f"https://img.example/3.png?token={secret}",
        ]
    )

    issues = await processor._download_images(
        content,
        base_url="https://mp.weixin.qq.com/s/article",
    )

    assert fetcher.fetch.await_count == 2
    _assert_resource_limit_issue(
        issues[0],
        stage="wechat_image_count",
        count=2,
        limit=2,
    )
    assert secret not in repr(issues)
    assert str(layout.tmp_dir) not in repr(issues)
    published = repr(content)
    assert "file:" not in published.lower()
    assert secret not in published
    assert "token=redacted" in published
    assert all("data-src" not in img.attrs for img in content.find_all("img"))


@pytest.mark.asyncio
async def test_wechat_duplicate_image_url_is_fetched_once(tmp_path: Path):
    processor, _layout = _processor_with_layout(tmp_path)
    processor.max_images = 2
    processor.max_image_bytes = 10
    processor.max_total_image_bytes = 20
    fetcher = MagicMock()
    fetcher.fetch = AsyncMock(
        return_value=SafeResponse(
            "https://img.example/shared.png",
            200,
            {},
            b"img",
        )
    )
    processor._safe_fetcher = fetcher
    content = _image_container(
        ["https://img.example/shared.png", "https://img.example/shared.png"]
    )

    issues = await processor._download_images(
        content,
        base_url="https://mp.weixin.qq.com/s/article",
    )

    assert issues == []
    assert fetcher.fetch.await_count == 1
    image_sources = [img.get("src") for img in content.find_all("img")]
    assert image_sources[0] == image_sources[1]
    assert image_sources[0] == "https://img.example/shared.png"


@pytest.mark.asyncio
async def test_wechat_budget_stop_sanitizes_all_remaining_image_sources(tmp_path: Path):
    processor, _layout = _processor_with_layout(tmp_path)
    processor.max_images = 1
    processor.max_image_bytes = 10
    processor.max_total_image_bytes = 10
    fetcher = MagicMock()
    fetcher.fetch = AsyncMock(
        return_value=SafeResponse("https://img.example/1.png", 200, {}, b"1")
    )
    processor._safe_fetcher = fetcher
    secret = "UNFETCHED-IMAGE-SECRET"
    content = _image_container(
        [
            "https://img.example/1.png",
            f"https://img.example/2.png?To%4Ben={secret}",
            "file:///C:/PRIVATE_PATH_CANARY/secret.png",
        ]
    )

    issues = await processor._download_images(
        content,
        base_url="https://mp.weixin.qq.com/s/article",
    )

    assert fetcher.fetch.await_count == 1
    _assert_resource_limit_issue(
        issues[0],
        stage="wechat_image_count",
        count=1,
        limit=1,
    )
    image_sources = [img.get("src") for img in content.find_all("img")]
    assert image_sources == [
        "https://img.example/1.png",
        "https://img.example/2.png?To%4Ben=redacted",
        None,
    ]
    rendered = repr(content)
    assert "data-src" not in rendered
    assert "file:" not in rendered.lower()
    assert secret not in rendered
    assert "PRIVATE_PATH_CANARY" not in rendered


@pytest.mark.asyncio
async def test_wechat_cumulative_byte_budget_caps_next_fetch_and_stops(tmp_path: Path):
    processor, layout = _processor_with_layout(tmp_path)
    processor.max_images = 3
    processor.max_image_bytes = 4
    processor.max_total_image_bytes = 5
    fetcher = MagicMock()
    fetcher.fetch = AsyncMock(
        side_effect=[
            SafeResponse("https://img.example/1.png", 200, {}, b"123"),
            SafeResponse("https://img.example/2.png", 200, {}, b"456"),
        ]
    )
    processor._safe_fetcher = fetcher
    content = _image_container(
        [
            "https://img.example/1.png",
            "https://img.example/2.png",
            "https://img.example/3.png",
        ]
    )

    issues = await processor._download_images(
        content,
        base_url="https://mp.weixin.qq.com/s/article",
    )

    assert fetcher.fetch.await_count == 2
    assert [
        call.kwargs["max_response_bytes"]
        for call in fetcher.fetch.await_args_list
    ] == [4, 2]
    _assert_resource_limit_issue(
        issues[0],
        stage="wechat_image_total_bytes",
        count=5,
        limit=5,
    )
    assert len(list(layout.tmp_dir.iterdir())) == 1


@pytest.mark.asyncio
async def test_wechat_failed_image_attempts_cannot_bypass_total_budget(tmp_path: Path):
    processor, _layout = _processor_with_layout(tmp_path)
    processor.max_images = 5
    processor.max_image_bytes = 3
    processor.max_total_image_bytes = 5
    fetcher = MagicMock()
    fetcher.fetch = AsyncMock(side_effect=[OSError("partial reset"), OSError("reset")])
    processor._safe_fetcher = fetcher
    content = _image_container(
        [
            "https://img.example/1.png",
            "https://img.example/2.png",
            "https://img.example/3.png",
        ]
    )

    issues = await processor._download_images(
        content,
        base_url="https://mp.weixin.qq.com/s/article",
    )

    assert fetcher.fetch.await_count == 2
    assert [
        call.kwargs["max_response_bytes"]
        for call in fetcher.fetch.await_args_list
    ] == [3, 2]
    _assert_resource_limit_issue(
        issues[0],
        stage="wechat_image_total_bytes",
        count=5,
        limit=5,
    )
