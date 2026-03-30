"""
Wechat article processor.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Dict, Optional
from urllib.parse import urlparse
import hashlib

from bs4 import BeautifulSoup, Tag
from playwright.async_api import async_playwright
import httpx
import requests

from src.processors.base import BaseProcessor
from src.storage.markdown_store import Entry
from src.utils.config import get_config
from src.utils.logger import get_logger

logger = get_logger(__name__)


class WechatProcessor(BaseProcessor):
    """Processor for Wechat public articles."""

    def __init__(self, timeout: float = 20.0, user_agent: Optional[str] = None):
        """
        Initialize the processor.

        Args:
            timeout: HTTP timeout in seconds.
            user_agent: Optional custom User-Agent.
        """
        config = get_config()
        self.timeout = timeout
        self.user_agent = user_agent or config.get_env(
            "USER_AGENT",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
        )
        self.tmp_dir = config.tmp_dir

    @classmethod
    def can_handle(cls, url: str) -> bool:
        """Return True for Wechat article URLs."""
        return "mp.weixin.qq.com" in url

    async def process(self, url: str) -> Entry:
        """
        Process a Wechat article and return an Entry.

        Args:
            url: Wechat article URL.

        Returns:
            Entry with extracted content.
        """
        logger.info("WechatProcessor processing url=%s", url)
        html = await self._fetch_html(url)
        if not html:
            raise ValueError(f"Empty HTML content for url={url}")

        soup = BeautifulSoup(html, "lxml")
        metadata = self._extract_metadata(soup)
        metadata["source_url"] = url
        metadata["source_type"] = "wechat"

        content_tag = self._extract_content_tag(soup)
        await self._download_images(content_tag)
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

        logger.info("WechatProcessor completed url=%s title=%s", url, title)
        return entry

    async def _fetch_html(self, url: str) -> str:
        """Fetch HTML using Playwright with a requests fallback."""
        try:
            return await self._fetch_with_playwright(url)
        except Exception as exc:
            logger.warning("Playwright fetch failed, fallback to requests: %s", exc)
            return await self._fetch_with_requests(url)

    async def _fetch_with_playwright(self, url: str) -> str:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled"],
            )
            page = await browser.new_page()
            await page.set_extra_http_headers({"User-Agent": self.user_agent})
            await page.goto(url, wait_until="networkidle", timeout=int(self.timeout * 1000))
            html = await page.content()
            await browser.close()
            return html

    async def _fetch_with_requests(self, url: str) -> str:
        headers = {"User-Agent": self.user_agent}

        def _request() -> str:
            response = requests.get(url, headers=headers, timeout=self.timeout)
            response.raise_for_status()
            response.encoding = response.apparent_encoding or response.encoding
            return response.text

        try:
            return await asyncio.to_thread(_request)
        except requests.RequestException as exc:
            logger.error("Failed to fetch url=%s error=%s", url, exc)
            raise ValueError(f"Failed to fetch url={url}: {exc}") from exc

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

    async def _download_images(self, content_tag: Tag) -> None:
        """Download images to tmp directory and update src attributes."""
        if content_tag is None:
            return

        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        headers = {"User-Agent": self.user_agent}

        async with httpx.AsyncClient(timeout=self.timeout, headers=headers) as client:
            for img in content_tag.find_all("img"):
                img_url = img.get("data-src") or img.get("src")
                if not img_url:
                    continue

                local_path = await self._download_image(client, img_url)
                if local_path:
                    img["src"] = local_path
                    if "data-src" in img.attrs:
                        del img.attrs["data-src"]

    async def _download_image(self, client: httpx.AsyncClient, url: str) -> Optional[str]:
        try:
            response = await client.get(url)
            response.raise_for_status()

            url_path = urlparse(url).path
            ext = Path(url_path).suffix or ".jpg"
            digest = hashlib.md5(url.encode("utf-8")).hexdigest()
            filename = f"wechat_{digest}{ext}"

            target_path = self.tmp_dir / filename
            with open(target_path, "wb") as file:
                file.write(response.content)

            relative_path = Path(".data/tmp") / filename
            return str(relative_path)
        except httpx.HTTPError as exc:
            logger.warning("Failed to download image url=%s error=%s", url, exc)
            return None
