"""
Generic web page processor.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from bs4 import BeautifulSoup, Tag

from src.processors.base import BaseProcessor
from src.processors.safe_fetch import SafeFetcher, describe_url_target
from src.storage.markdown_store import Entry
from src.utils.config import get_config
from src.utils.logger import get_logger

logger = get_logger(__name__)


class GenericProcessor(BaseProcessor):
    """Processor for generic web pages."""

    def __init__(
        self,
        timeout: float = 20.0,
        user_agent: Optional[str] = None,
        safe_fetcher: SafeFetcher | None = None,
        *,
        config: Any | None = None,
    ):
        """
        Initialize the processor.

        Args:
            timeout: HTTP timeout in seconds.
            user_agent: Optional custom User-Agent.
        """
        runtime_config = config if config is not None else get_config()
        self.timeout = timeout
        self._init_safe_fetcher(
            timeout_seconds=timeout,
            safe_fetcher=safe_fetcher,
        )
        self.user_agent = user_agent or runtime_config.get(
            "processors.generic.user_agent",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
        )

    @classmethod
    def can_handle(cls, url: str) -> bool:
        """Fallback processor that accepts any URL."""
        return True

    async def process(self, url: str) -> Entry:
        """
        Process a web page and return an Entry.

        Args:
            url: Target URL.

        Returns:
            Entry with extracted content.
        """
        logger.info("GenericProcessor processing target=%s", describe_url_target(url))
        html = await self._fetch_html(url)
        if not html:
            raise ValueError("Empty HTML content returned by target")

        soup = BeautifulSoup(html, "lxml")
        metadata = self._extract_metadata(soup)
        metadata["source_url"] = url
        metadata["source_type"] = "generic"

        content_html = self._extract_main_content_html(soup)
        markdown = self._html_to_markdown(content_html)

        title = metadata.get("title") or "Untitled"
        abstract = metadata.get("description", "")

        entry = Entry(
            title=title,
            source_type="generic",
            source_url=url,
            published_at=metadata.get("published_time"),
            abstract=abstract,
            content=markdown,
        )
        entry.metadata = metadata

        logger.info(
            "GenericProcessor completed target=%s content_length=%s",
            describe_url_target(url),
            len(markdown),
        )
        return entry

    async def _fetch_html(self, url: str) -> str:
        """Fetch HTML through the DNS-pinned SSRF-safe transport."""
        headers = {"User-Agent": self.user_agent}
        response = await self._fetch_public_url(url, headers=headers)
        return response.text

    def _extract_metadata(self, soup: BeautifulSoup) -> Dict[str, str]:
        """Extract Open Graph metadata with fallback to title tags."""
        metadata = super()._extract_metadata(soup)

        if "title" not in metadata:
            title_tag = soup.find("title")
            if title_tag and title_tag.get_text(strip=True):
                metadata["title"] = title_tag.get_text(strip=True)

        if "title" not in metadata:
            h1_tag = soup.find("h1")
            if h1_tag and h1_tag.get_text(strip=True):
                metadata["title"] = h1_tag.get_text(strip=True)

        return metadata

    def _extract_main_content_html(self, soup: BeautifulSoup) -> str:
        """
        Extract main content HTML with a heuristic approach.

        Priority:
        1) <article>
        2) <main>
        3) Longest high-density <div>/<section>
        """
        article = soup.find("article")
        if article and article.get_text(strip=True):
            return str(article)

        main = soup.find("main")
        if main and main.get_text(strip=True):
            return str(main)

        for tag_name in ["header", "footer", "nav", "aside", "script", "style", "noscript"]:
            for tag in soup.find_all(tag_name):
                tag.decompose()

        candidates = soup.find_all(["div", "section"])
        best_tag: Optional[Tag] = None
        best_score = 0

        for tag in candidates:
            text = tag.get_text(" ", strip=True)
            if not text:
                continue

            text_length = len(text)
            link_length = sum(len(a.get_text(" ", strip=True)) for a in tag.find_all("a"))
            score = max(text_length - link_length, 0)

            if score > best_score:
                best_score = score
                best_tag = tag

        if best_tag and best_tag.get_text(strip=True):
            return str(best_tag)

        body = soup.find("body")
        if body:
            return str(body)

        return str(soup)
