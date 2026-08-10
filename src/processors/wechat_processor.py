"""
Wechat article processor.
"""

from __future__ import annotations

from pathlib import Path
import re
from typing import Dict, Optional
from urllib.parse import urljoin, urlparse
import hashlib

from bs4 import BeautifulSoup, Tag

from src.processors.base import BaseProcessor
from src.processors.safe_fetch import (
    SafeFetcher,
    SafeFetchResponseLimitError,
    describe_url_target,
    parse_http_target,
)
from src.relations.citations import sanitize_public_source_url
from src.runtime.errors import ErrorCode, PKVRuntimeError
from src.runtime.layout import atomic_publish_file, validate_directory_components
from src.storage.markdown_store import Entry
from src.utils.config import get_config
from src.utils.logger import get_logger

logger = get_logger(__name__)

MAX_WECHAT_IMAGES = 32
MAX_WECHAT_IMAGE_BYTES = 20 * 1024 * 1024
MAX_WECHAT_TOTAL_IMAGE_BYTES = 50 * 1024 * 1024


class WechatProcessor(BaseProcessor):
    """Processor for Wechat public articles."""

    def __init__(
        self,
        timeout: float = 20.0,
        user_agent: Optional[str] = None,
        safe_fetcher: SafeFetcher | None = None,
        max_images: int = MAX_WECHAT_IMAGES,
        max_image_bytes: int = MAX_WECHAT_IMAGE_BYTES,
        max_total_image_bytes: int = MAX_WECHAT_TOTAL_IMAGE_BYTES,
    ):
        """
        Initialize the processor.

        Args:
            timeout: HTTP timeout in seconds.
            user_agent: Optional custom User-Agent.
        """
        config = get_config()
        self._config = config
        self.timeout = timeout
        self._init_safe_fetcher(
            timeout_seconds=timeout,
            safe_fetcher=safe_fetcher,
        )
        self.user_agent = user_agent or config.get(
            "processors.wechat.user_agent",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
        )
        self.tmp_dir = config.tmp_dir
        self.max_images = _bounded_positive_int(
            max_images,
            hard_limit=MAX_WECHAT_IMAGES,
            label="max_images",
        )
        self.max_image_bytes = _bounded_positive_int(
            max_image_bytes,
            hard_limit=MAX_WECHAT_IMAGE_BYTES,
            label="max_image_bytes",
        )
        self.max_total_image_bytes = _bounded_positive_int(
            max_total_image_bytes,
            hard_limit=MAX_WECHAT_TOTAL_IMAGE_BYTES,
            label="max_total_image_bytes",
        )

    @classmethod
    def can_handle(cls, url: str) -> bool:
        """Return True for Wechat article URLs."""
        try:
            return parse_http_target(url).hostname == "mp.weixin.qq.com"
        except Exception:
            return False

    async def process(self, url: str) -> Entry:
        """
        Process a Wechat article and return an Entry.

        Args:
            url: Wechat article URL.

        Returns:
            Entry with extracted content.
        """
        logger.info("WechatProcessor processing target=%s", describe_url_target(url))
        html = await self._fetch_html(url)
        if not html:
            raise ValueError("Empty HTML content returned by target")

        soup = BeautifulSoup(html, "lxml")
        metadata = self._extract_metadata(soup)
        metadata["source_url"] = url
        metadata["source_type"] = "wechat"

        content_tag = self._extract_content_tag(soup)
        processing_issues = (
            await self._download_images(content_tag, base_url=url)
        ) or []
        markdown = self._html_to_markdown(str(content_tag))

        title = metadata.get("title") or "Untitled"
        abstract = metadata.get("description", "")

        entry = Entry(
            title=title,
            source_type="wechat",
            source_url=url,
            published_at=metadata.get("published_time"),
            abstract=abstract,
            content=markdown,
        )
        entry.metadata = metadata
        entry.processing_issues = processing_issues

        logger.info(
            "WechatProcessor completed target=%s content_length=%s image_issues=%s",
            describe_url_target(url),
            len(markdown),
            len(processing_issues),
        )
        return entry

    async def _fetch_html(self, url: str) -> str:
        """Fetch HTML through the only published, DNS-pinned transport."""
        return await self._fetch_with_requests(url)

    async def _fetch_with_requests(self, url: str) -> str:
        """Compatibility name for the SSRF-safe HTTP fetch path."""
        headers = {"User-Agent": self.user_agent}
        response = await self._fetch_public_url(url, headers=headers)
        return response.text

    def _extract_metadata(self, soup: BeautifulSoup) -> Dict[str, str]:
        """Extract metadata using Wechat-specific selectors."""
        metadata = super()._extract_metadata(soup)

        title_tag = soup.find("h1", id="activity-name")
        if title_tag and title_tag.get_text(strip=True):
            metadata.setdefault("title", title_tag.get_text(strip=True))

        author_tag = soup.find("meta", attrs={"name": "author"})
        if author_tag and author_tag.get("content"):
            metadata.setdefault("author", author_tag["content"].strip())

        if "author" not in metadata:
            author_span = soup.find("span", class_="rich_media_meta_text")
            if author_span and author_span.get_text(strip=True):
                metadata["author"] = author_span.get_text(strip=True)

        published_tag = soup.find("meta", property="article:published_time")
        if published_tag and published_tag.get("content"):
            metadata.setdefault("published_time", published_tag["content"].strip())

        if "published_time" not in metadata:
            time_tag = soup.find("em", id="publish_time")
            if time_tag and time_tag.get_text(strip=True):
                metadata["published_time"] = time_tag.get_text(strip=True)

        return metadata

    def _extract_content_tag(self, soup: BeautifulSoup) -> Tag:
        content_tag = soup.find("div", id="js_content")
        if content_tag is None:
            content_tag = soup.find("div", class_=lambda value: value and "rich_media_content" in value)
        if content_tag is None:
            content_tag = soup.find("body") or soup

        for tag in content_tag.find_all(["section", "span"]):
            tag.unwrap()

        for tag in content_tag.find_all(["script", "style"]):
            tag.decompose()

        return content_tag

    async def _download_images(
        self,
        content_tag: Tag,
        *,
        base_url: str,
    ) -> list[dict[str, object]]:
        """Download deduplicated images under one archive-wide resource budget."""
        if content_tag is None:
            return []

        self._prepare_tmp_dir()
        attempted = 0
        bytes_used = 0
        seen_urls: set[str] = set()
        download_candidates: list[tuple[str, str]] = []

        for img in content_tag.find_all("img"):
            raw_url = img.get("data-src") or img.get("src")
            if not raw_url:
                img.attrs.pop("data-src", None)
                continue
            img_url = urljoin(base_url, raw_url)
            public_url = sanitize_public_source_url(img_url)
            img.attrs.pop("data-src", None)
            if public_url:
                # Temporary downloads are an internal processing detail.  The
                # persisted Markdown must retain only a public-safe source URL,
                # never an absolute ``file://`` path under the runtime DataRoot.
                img["src"] = public_url
            else:
                img.attrs.pop("src", None)
                continue
            try:
                cache_key = parse_http_target(img_url).url
            except PKVRuntimeError as exc:
                return [_runtime_image_issue(exc)]
            except Exception:
                continue
            download_candidates.append((cache_key, img_url))

        for cache_key, img_url in download_candidates:
            if cache_key in seen_urls:
                continue
            seen_urls.add(cache_key)

            if attempted >= self.max_images:
                return [
                    _resource_limit_issue(
                        stage="wechat_image_count",
                        count=attempted,
                        limit=self.max_images,
                    )
                ]
            remaining = self.max_total_image_bytes - bytes_used
            if remaining <= 0:
                return [
                    _resource_limit_issue(
                        stage="wechat_image_total_bytes",
                        count=bytes_used,
                        limit=self.max_total_image_bytes,
                    )
                ]

            attempted += 1
            request_limit = min(self.max_image_bytes, remaining)
            try:
                local_path, downloaded_bytes, limit_reached = (
                    await self._download_image_with_budget(
                        img_url,
                        max_response_bytes=request_limit,
                    )
                )
            except PKVRuntimeError as exc:
                # A rejected image subresource must remain observable at the
                # workflow boundary.  Treating it like an ordinary broken image
                # would turn an SSRF denial into an apparent full success.
                return [_runtime_image_issue(exc)]
            if limit_reached:
                if request_limit == remaining:
                    stage = "wechat_image_total_bytes"
                    count = self.max_total_image_bytes
                    limit = self.max_total_image_bytes
                else:
                    stage = "wechat_image_response_bytes"
                    count = request_limit
                    limit = self.max_image_bytes
                return [
                    _resource_limit_issue(
                        stage=stage,
                        count=count,
                        limit=limit,
                    )
                ]
            if local_path is None:
                # A failed response may already have consumed an unknown number
                # of bytes before the transport aborted.  Debit its full
                # allocation so repeated partial failures cannot bypass the
                # archive-wide network budget.
                bytes_used += request_limit
                if bytes_used >= self.max_total_image_bytes:
                    return [
                        _resource_limit_issue(
                            stage="wechat_image_total_bytes",
                            count=self.max_total_image_bytes,
                            limit=self.max_total_image_bytes,
                        )
                    ]
                continue
            bytes_used += downloaded_bytes
        return []

    async def _download_image(self, url: str) -> Optional[str]:
        local_path, _downloaded_bytes, _limit_reached = (
            await self._download_image_with_budget(
                url,
                max_response_bytes=self.max_image_bytes,
            )
        )
        return local_path

    async def _download_image_with_budget(
        self,
        url: str,
        *,
        max_response_bytes: int,
    ) -> tuple[Optional[str], int, bool]:
        try:
            response = await self._fetch_public_url(
                url,
                headers={"User-Agent": self.user_agent},
                max_response_bytes=max_response_bytes,
            )
            if len(response.content) > max_response_bytes:
                return None, 0, True

            url_path = urlparse(url).path
            ext = Path(url_path).suffix.lower()
            if not re.fullmatch(r"\.[a-z0-9]{1,5}", ext):
                ext = ".img"
            digest = hashlib.md5(url.encode("utf-8")).hexdigest()
            filename = f"wechat_{digest}{ext}"

            target_path = self.tmp_dir / filename
            self._write_image_file(target_path, response.content)

            return target_path.as_uri(), len(response.content), False
        except SafeFetchResponseLimitError:
            logger.warning(
                "Failed to download image target=%s error_type=response_limit",
                describe_url_target(url),
            )
            return None, 0, True
        except PKVRuntimeError:
            # Preserve stable security/path error metadata for the processor's
            # structured degradation issue.  Never log the exception prose.
            raise
        except Exception as exc:
            logger.warning(
                "Failed to download image target=%s error_type=%s",
                describe_url_target(url),
                type(exc).__name__,
            )
            return None, 0, False

    def _prepare_tmp_dir(self) -> None:
        """通过统一目录合同准备微信图片临时目录。"""
        layout = getattr(self._config, "layout", None)
        if layout is not None:
            layout.ensure_user_directories()
            layout.validate_user_directory(self.tmp_dir, label="临时目录")
        else:
            validate_directory_components(self.tmp_dir, label="临时目录")
            self.tmp_dir.mkdir(parents=True, exist_ok=True)
            validate_directory_components(self.tmp_dir, label="临时目录")

    def _write_image_file(self, target_path: Path, content: bytes) -> None:
        """写完整临时文件后原子发布；链接/硬链接叶子在任何写入前拒绝。"""
        # Keep this leaf operation safe when called directly as well as through
        # ``_download_images``; the atomic publisher deliberately requires an
        # already-created, validated parent directory.
        self._prepare_tmp_dir()
        layout = getattr(self._config, "layout", None)
        if layout is not None:
            layout.atomic_publish_user_file(
                target_path,
                label="微信图片临时文件",
                data=content,
            )
        else:
            atomic_publish_file(
                target_path,
                label="微信图片临时文件",
                data=content,
            )


def _bounded_positive_int(value: int, *, hard_limit: int, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return min(value, hard_limit)


def _resource_limit_issue(
    *,
    stage: str,
    count: int,
    limit: int,
) -> dict[str, object]:
    return {
        "code": ErrorCode.PROCESSOR_RESOURCE_LIMIT.value,
        "message": "页面图片达到资源预算，部分图片未下载",
        "severity": "warning",
        "recoverable": False,
        "stage": stage,
        "count": int(count),
        "limit": int(limit),
    }


_IMAGE_NETWORK_ERROR_MESSAGES = {
    ErrorCode.URL_INVALID: "URL 格式无效",
    ErrorCode.SSRF_TARGET_FORBIDDEN: "禁止访问内网地址或其他非公网目标",
    ErrorCode.SSRF_RESOLUTION_FAILED: "目标主机无法安全解析",
    ErrorCode.SSRF_REDIRECT_LIMIT: "网页重定向次数超过安全限制",
}


def _runtime_image_issue(exc: PKVRuntimeError) -> dict[str, object]:
    """Project an image runtime error without trusting exception-authored text."""

    code = (
        exc.code
        if type(exc.code) is ErrorCode
        else ErrorCode.WORKFLOW_STEP_FAILED
    )
    is_network_policy = code in _IMAGE_NETWORK_ERROR_MESSAGES
    return {
        "code": code.value,
        "message": _IMAGE_NETWORK_ERROR_MESSAGES.get(
            code,
            "图片资源处理失败，已跳过下载",
        ),
        "severity": "warning",
        "recoverable": exc.recoverable is True,
        "stage": "network_policy" if is_network_policy else "wechat_image_download",
    }
