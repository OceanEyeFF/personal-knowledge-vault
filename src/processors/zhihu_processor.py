"""
Zhihu content processor.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime
from typing import Dict, Optional

from bs4 import BeautifulSoup, Tag
from playwright.async_api import async_playwright
import requests

from src.processors.base import BaseProcessor
from src.storage.markdown_store import Entry
from src.utils.config import get_config
from src.utils.logger import get_logger

logger = get_logger(__name__)


class ZhihuProcessor(BaseProcessor):
    """Processor for Zhihu questions and posts."""

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

    @classmethod
    def can_handle(cls, url: str) -> bool:
        """Return True for Zhihu URLs."""
        return "zhihu.com" in url

    async def process(self, url: str) -> Entry:
        """
        Process a Zhihu page and return an Entry.

        Args:
            url: Zhihu URL.

        Returns:
            Entry with extracted content.
        """
        logger.info("ZhihuProcessor processing url=%s", url)
        html = await self._fetch_html(url)
        if not html:
            raise ValueError(f"Empty HTML content for url={url}")

        soup = BeautifulSoup(html, "lxml")
        self._preserve_latex(soup)
        metadata = self._extract_metadata(soup)
        metadata.setdefault("published_time", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        metadata["source_url"] = url
        metadata["source_type"] = "zhihu"

        content_tag = self._extract_main_content_tag(soup)
        markdown = self._html_to_markdown(str(content_tag))

        title = metadata.get("title") or "Untitled"
        abstract = metadata.get("description", "")

        entry = Entry(
            title=title,
            source_type="zhihu",
            source_url=url,
            abstract=abstract,
            content=markdown,
        )
        entry.metadata = metadata

        logger.info("ZhihuProcessor completed url=%s title=%s", url, title)
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
        """Extract metadata using Zhihu-specific selectors."""
        metadata = super()._extract_metadata(soup)

        title_tag = soup.find("h1", class_="QuestionHeader-title")
        if title_tag and title_tag.get_text(strip=True):
            metadata.setdefault("title", title_tag.get_text(strip=True))

        post_title = soup.find("h1", class_="Post-Title")
        if post_title and post_title.get_text(strip=True):
            metadata.setdefault("title", post_title.get_text(strip=True))

        author_tag = soup.find("meta", attrs={"name": "author"})
        if author_tag and author_tag.get("content"):
            metadata.setdefault("author", author_tag["content"].strip())

        if "author" not in metadata:
            author_name = soup.select_one(".AuthorInfo-name")
            if author_name and author_name.get_text(strip=True):
                metadata["author"] = author_name.get_text(strip=True)

        published_tag = soup.find("meta", property="article:published_time")
        if published_tag and published_tag.get("content"):
            metadata.setdefault("published_time", published_tag["content"].strip())

        return metadata

    def _extract_main_content_tag(self, soup: BeautifulSoup) -> Tag:
        answers = soup.select(".RichContent-inner")
        if answers:
            return self._select_best_answer(answers)

        post_content = soup.select_one(".Post-RichTextContainer")
        if post_content:
            return post_content

        body = soup.find("body")
        return body or soup

    def _select_best_answer(self, answers: list[Tag]) -> Tag:
        best_answer = answers[0]
        best_score = self._extract_answer_score(best_answer)

        for answer in answers[1:]:
            score = self._extract_answer_score(answer)
            if score > best_score:
                best_score = score
                best_answer = answer

        return best_answer

    def _extract_answer_score(self, answer: Tag) -> int:
        parent = answer.find_parent(attrs={"data-score": True})
        if parent and parent.get("data-score"):
            return self._safe_int(parent.get("data-score"))

        for attr in ["data-vote-count", "data-votecount"]:
            parent = answer.find_parent(attrs={attr: True})
            if parent and parent.get(attr):
                return self._safe_int(parent.get(attr))

        button = answer.find_parent().find("button", class_=re.compile(r"VoteButton"))
        if button and button.get("aria-label"):
            return self._parse_vote_count(button.get("aria-label"))

        return 0

    def _parse_vote_count(self, text: str) -> int:
        match = re.search(r"(\d+)", text)
        if match:
            return self._safe_int(match.group(1))
        return 0

    def _safe_int(self, value: Optional[str]) -> int:
        try:
            return int(value) if value is not None else 0
        except ValueError:
            return 0

    def _preserve_latex(self, soup: BeautifulSoup) -> None:
        """Preserve LaTeX formulas by replacing known math spans with inline markers."""
        for tag in soup.select("span.ztext-math, span.math-inline"):
            latex = tag.get("data-tex") or tag.get_text(strip=True)
            if latex:
                tag.replace_with(f"${latex}$")

        for tag in soup.select("span.math-block"):
            latex = tag.get("data-tex") or tag.get_text(strip=True)
            if latex:
                tag.replace_with(f"\n\n$$\n{latex}\n$$\n\n")
