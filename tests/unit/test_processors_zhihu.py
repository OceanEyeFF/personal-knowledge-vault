# ruff: noqa: E402

"""
Zhihu processor unit tests.

覆盖:
- URL 识别
- 正常页面处理
- 登录墙检测与友好提示
- Cookie 解析与注入
"""

import sys
from pathlib import Path

# Add project root to Python path
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.processors.zhihu_processor import ZhihuProcessor
from src.processors.safe_fetch import SafeResponse
from src.runtime.errors import ErrorCode, PKVRuntimeError


# ============================================================
# Fixtures
# ============================================================

@pytest.fixture
def zhihu_html() -> str:
    """Load Zhihu HTML fixture (normal page)."""
    fixture_path = Path(__file__).parent.parent / "fixtures" / "zhihu_sample.html"
    return fixture_path.read_text(encoding="utf-8")


@pytest.fixture
def login_wall_html() -> str:
    """Load Zhihu login wall HTML fixture."""
    fixture_path = Path(__file__).parent.parent / "fixtures" / "zhihu_login_wall.html"
    return fixture_path.read_text(encoding="utf-8")


# ============================================================
# URL 识别
# ============================================================

def test_zhihu_can_handle():
    """can_handle should recognize Zhihu URLs."""
    assert ZhihuProcessor.can_handle("https://www.zhihu.com/question/123456")
    assert ZhihuProcessor.can_handle("https://zhihu.com/question/123456")
    assert ZhihuProcessor.can_handle("https://zhuanlan.zhihu.com/p/123456")
    assert not ZhihuProcessor.can_handle("https://example.com")
    assert not ZhihuProcessor.can_handle("https://owned.zhihu.com/question/123456")
    assert not ZhihuProcessor.can_handle(
        "https://evil.example/?next=www.zhihu.com"
    )


# ============================================================
# 正常页面处理
# ============================================================

@pytest.mark.asyncio
async def test_zhihu_process(zhihu_html: str):
    """Processor should extract the best answer content."""
    processor = ZhihuProcessor()

    with patch.object(processor, "_fetch_html", new=AsyncMock(return_value=zhihu_html)):
        entry = await processor.process("https://www.zhihu.com/question/123456")

    assert entry.title == "Why is the sky blue?"
    assert entry.source_type == "zhihu"
    assert entry.source_url == "https://www.zhihu.com/question/123456"
    assert "Best answer content." in entry.content
    assert "Low score answer." not in entry.content

    metadata = getattr(entry, "metadata", {})
    assert metadata.get("author") == "Zhihu Author"
    assert metadata.get("published_time") == "2026-02-13 09:00:00"
    assert entry.published_at == "2026-02-13 09:00:00"


@pytest.mark.asyncio
async def test_zhihu_process_without_publish_time_keeps_published_at_empty():
    processor = ZhihuProcessor()
    html = """
    <html>
      <head>
        <title>Zhihu No Time</title>
        <meta name="author" content="Zhihu Author"/>
      </head>
      <body>
        <div class="RichContent-inner"><p>Best answer content.</p></div>
      </body>
    </html>
    """

    with patch.object(processor, "_fetch_html", new=AsyncMock(return_value=html)):
        entry = await processor.process("https://www.zhihu.com/question/654321")

    metadata = getattr(entry, "metadata", {})
    assert "published_time" not in metadata
    assert entry.published_at is None


@pytest.mark.asyncio
async def test_zhihu_empty_response_error_does_not_echo_url_secret():
    processor = ZhihuProcessor()
    secret = "pkv-url-secret"

    with patch.object(processor, "_fetch_html", new=AsyncMock(return_value="")):
        with pytest.raises(ValueError) as exc_info:
            await processor.process(
                f"https://www.zhihu.com/question/123?token={secret}"
            )

    assert secret not in str(exc_info.value)


# ============================================================
# 登录墙检测
# ============================================================

class TestLoginWallDetection:
    """测试知乎登录墙检测功能。"""

    def test_is_login_wall_detected(self, login_wall_html: str):
        """登录墙 HTML 被正确识别。"""
        processor = ZhihuProcessor()
        assert processor._is_login_wall(login_wall_html) is True

    def test_normal_page_not_login_wall(self, zhihu_html: str):
        """正常页面不误判为登录墙。"""
        processor = ZhihuProcessor()
        assert processor._is_login_wall(zhihu_html) is False

    def test_empty_html_not_login_wall(self):
        """空 HTML 不误判为登录墙。"""
        processor = ZhihuProcessor()
        assert processor._is_login_wall("") is False
        assert processor._is_login_wall(None) is False

    def test_login_wall_markers(self):
        """各种登录墙特征文本均可检测。"""
        processor = ZhihuProcessor()
        assert processor._is_login_wall("<p>请您登录后查看内容</p>") is True
        assert processor._is_login_wall("<a>登录知乎</a>") is True
        assert processor._is_login_wall("<p>请登录后查看更多</p>") is True
        assert processor._is_login_wall("<p>登录即可查看</p>") is True

    @pytest.mark.asyncio
    async def test_process_raises_on_login_wall_no_cookie(self, login_wall_html: str):
        """遇到登录墙且无 Cookie 时抛 ValueError 并提示配置方法。"""
        processor = ZhihuProcessor()
        processor._cookie_str = None

        with patch.object(processor, "_fetch_html", new=AsyncMock(return_value=login_wall_html)):
            with pytest.raises(ValueError, match="知乎登录墙"):
                await processor.process("https://www.zhihu.com/question/123456")

    @pytest.mark.asyncio
    async def test_process_raises_on_login_wall_with_expired_cookie(self, login_wall_html: str):
        """Cookie 过期仍触发登录墙时，提示 Cookie 过期。"""
        processor = ZhihuProcessor()
        processor._cookie_str = "_zap=abc; d_c0=xyz"

        with patch.object(processor, "_fetch_html", new=AsyncMock(return_value=login_wall_html)):
            with pytest.raises(ValueError, match="知乎登录墙"):
                await processor.process("https://www.zhihu.com/question/123456")


# ============================================================
# Cookie 解析
# ============================================================

class TestCookieParsing:
    """测试 Cookie 字符串解析。"""

    def test_parse_cookie_str_basic(self):
        """基本 Cookie 字符串解析。"""
        cookies = ZhihuProcessor._parse_cookie_str(
            "_zap=abc123; d_c0=AEBxyz; _xsrf=token456",
            ".zhihu.com",
        )
        assert len(cookies) == 3
        assert cookies[0] == {"name": "_zap", "value": "abc123", "domain": ".zhihu.com", "path": "/"}
        assert cookies[1] == {"name": "d_c0", "value": "AEBxyz", "domain": ".zhihu.com", "path": "/"}
        assert cookies[2] == {"name": "_xsrf", "value": "token456", "domain": ".zhihu.com", "path": "/"}

    def test_parse_cookie_str_with_spaces(self):
        """Cookie 字符串含额外空格时正确解析。"""
        cookies = ZhihuProcessor._parse_cookie_str(
            "  _zap = abc123 ;  d_c0=xyz  ",
            ".zhihu.com",
        )
        assert len(cookies) == 2
        assert cookies[0]["name"] == "_zap"
        assert cookies[0]["value"] == "abc123"

    def test_parse_cookie_str_with_value_containing_equals(self):
        """Cookie 值包含等号时正确解析（只分割第一个等号）。"""
        cookies = ZhihuProcessor._parse_cookie_str(
            "token=abc=def=ghi",
            ".zhihu.com",
        )
        assert len(cookies) == 1
        assert cookies[0]["name"] == "token"
        assert cookies[0]["value"] == "abc=def=ghi"

    def test_parse_cookie_str_empty(self):
        """空 Cookie 字符串返回空列表。"""
        cookies = ZhihuProcessor._parse_cookie_str("", ".zhihu.com")
        assert cookies == []

    def test_parse_cookie_str_skips_invalid(self):
        """无效的 Cookie 对（无等号）被跳过。"""
        cookies = ZhihuProcessor._parse_cookie_str(
            "_zap=abc; invalid_no_value; d_c0=xyz",
            ".zhihu.com",
        )
        assert len(cookies) == 2


# ============================================================
# Cookie 注入
# ============================================================

class TestCookieInjection:
    """测试 Cookie 只注入 DNS-pinned safe-fetch 请求。"""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "hostname,path",
        [
            ("zhihu.com", "/question/123"),
            ("www.zhihu.com", "/question/123"),
            ("zhuanlan.zhihu.com", "/p/123"),
        ],
    )
    async def test_cookie_only_injected_to_exact_content_hosts(
        self,
        hostname: str,
        path: str,
    ):
        """安全传输请求注入 Cookie 到 headers。"""
        target_url = f"https://{hostname}{path}"
        fetcher = MagicMock()
        fetcher.fetch = AsyncMock(return_value=SafeResponse(
            url=target_url,
            status_code=200,
            headers={"content-type": "text/html; charset=utf-8"},
            content="<html><body>正常内容</body></html>".encode(),
        ))
        processor = ZhihuProcessor(safe_fetcher=fetcher)
        processor._cookie_str = "_zap=abc; d_c0=xyz"

        html = await processor._fetch_with_requests(target_url)

        headers = fetcher.fetch.await_args.kwargs["headers"]
        assert headers.get("Cookie") == "_zap=abc; d_c0=xyz"
        assert fetcher.fetch.await_args.args == (target_url,)
        assert "正常内容" in html

    @pytest.mark.asyncio
    async def test_no_cookie_no_header(self):
        """无 Cookie 时安全传输 headers 不包含 Cookie 字段。"""
        fetcher = MagicMock()
        fetcher.fetch = AsyncMock(return_value=SafeResponse(
            url="https://www.zhihu.com/question/123",
            status_code=200,
            headers={"content-type": "text/html; charset=utf-8"},
            content="<html><body>内容</body></html>".encode(),
        ))
        processor = ZhihuProcessor(safe_fetcher=fetcher)
        processor._cookie_str = None

        await processor._fetch_with_requests("https://www.zhihu.com/question/123")

        headers = fetcher.fetch.await_args.kwargs["headers"]
        assert "Cookie" not in headers

    @pytest.mark.asyncio
    async def test_cookie_is_never_sent_to_non_zhihu_target(self):
        fetcher = MagicMock()
        fetcher.fetch = AsyncMock()
        processor = ZhihuProcessor(safe_fetcher=fetcher)
        processor._cookie_str = "session=secret"

        with pytest.raises(PKVRuntimeError) as exc_info:
            await processor._fetch_with_requests(
                "https://evil.example/?next=www.zhihu.com"
            )

        assert exc_info.value.code is ErrorCode.SSRF_TARGET_FORBIDDEN
        fetcher.fetch.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cookie_is_never_sent_to_arbitrary_zhihu_subdomain(
        self,
        caplog,
    ):
        cookie_canary = "ZHIHU-COOKIE-SECRET-CANARY"
        url_canary = "ZHIHU-URL-SECRET-CANARY"
        fetcher = MagicMock()
        fetcher.fetch = AsyncMock()
        processor = ZhihuProcessor(safe_fetcher=fetcher)
        processor._cookie_str = f"session={cookie_canary}"

        with pytest.raises(PKVRuntimeError) as exc_info:
            await processor._fetch_with_requests(
                f"https://owned.zhihu.com/question/123?token={url_canary}"
            )

        assert exc_info.value.code is ErrorCode.SSRF_TARGET_FORBIDDEN
        assert exc_info.value.stage == "network_policy"
        assert exc_info.value.recoverable is False
        assert cookie_canary not in repr(exc_info.value.to_dict())
        assert url_canary not in repr(exc_info.value.to_dict())
        assert cookie_canary not in caplog.text
        assert url_canary not in caplog.text
        fetcher.fetch.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cookie_is_never_sent_over_plain_http(self):
        fetcher = MagicMock()
        fetcher.fetch = AsyncMock()
        processor = ZhihuProcessor(safe_fetcher=fetcher)
        processor._cookie_str = "session=secret"

        with pytest.raises(PKVRuntimeError) as exc_info:
            await processor._fetch_with_requests(
                "http://www.zhihu.com/question/123"
            )

        assert exc_info.value.code is ErrorCode.SSRF_TARGET_FORBIDDEN
        fetcher.fetch.assert_not_awaited()
