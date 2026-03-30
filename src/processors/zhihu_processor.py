"""
Zhihu content processor.
"""

from __future__ import annotations

import asyncio
import re
from typing import Dict, List, Optional

from bs4 import BeautifulSoup, Tag
from playwright.async_api import async_playwright
import requests

from src.processors.base import BaseProcessor
from src.storage.markdown_store import Entry
from src.utils.config import get_config
from src.utils.logger import get_logger

logger = get_logger(__name__)

# 登录墙特征文本
_LOGIN_WALL_MARKERS = [
    "请您登录后查看",
    "登录知乎",
    "请登录后查看更多",
    "登录即可查看",
]


class ZhihuProcessor(BaseProcessor):
    """Processor for Zhihu questions and posts.

    支持可选的 Cookie 注入（通过环境变量 ZHIHU_COOKIE 配置），
    用于绕过知乎登录墙获取完整内容。
    """

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
        self._cookie_str: Optional[str] = config.zhihu_cookie

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

        # 登录墙检测
        if self._is_login_wall(html):
            if self._cookie_str:
                logger.warning(
                    "⚠️ 知乎登录墙检测：已配置 Cookie 但仍被拦截，Cookie 可能已过期。"
                    "请重新从浏览器复制 Cookie 到 .env 文件的 ZHIHU_COOKIE 配置项"
                )
            else:
                logger.warning(
                    "⚠️ 知乎登录墙检测：该页面需要登录才能查看完整内容。\n"
                    "解决方法：\n"
                    "  1. 在 .env 文件中配置 ZHIHU_COOKIE（从浏览器复制登录 Cookie）\n"
                    "  2. 或者直接复制页面文本，使用「文本归档」功能\n"
                    "详见 .env.example 中的 ZHIHU_COOKIE 说明"
                )
            raise ValueError(
                "知乎登录墙：该页面需要登录才能查看完整内容。"
                "请配置 ZHIHU_COOKIE 环境变量或使用文本归档功能"
            )

        soup = BeautifulSoup(html, "lxml")
        self._preserve_latex(soup)
        metadata = self._extract_metadata(soup)
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
            published_at=metadata.get("published_time"),
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
            context = await browser.new_context(user_agent=self.user_agent)

            # Cookie 注入
            if self._cookie_str:
                cookies = self._parse_cookie_str(self._cookie_str, ".zhihu.com")
                await context.add_cookies(cookies)
                logger.info("已注入知乎 Cookie (%d 个)", len(cookies))

            page = await context.new_page()
            await page.goto(url, wait_until="networkidle", timeout=int(self.timeout * 1000))
            html = await page.content()
            await browser.close()
            return html

    async def _fetch_with_requests(self, url: str) -> str:
        headers = {"User-Agent": self.user_agent}
        if self._cookie_str:
            headers["Cookie"] = self._cookie_str

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

    # ------------------------------------------------------------------
    # 登录墙检测 & Cookie 解析
    # ------------------------------------------------------------------

    def _is_login_wall(self, html: str) -> bool:
        """检测 HTML 是否为知乎登录墙页面。

        Args:
            html: 页面 HTML 内容。

        Returns:
            True 表示检测到登录墙。
        """
        if not html:
            return False
        for marker in _LOGIN_WALL_MARKERS:
            if marker in html:
                return True
        return False

    @staticmethod
    def _parse_cookie_str(cookie_str: str, domain: str) -> List[Dict[str, str]]:
        """将浏览器 Cookie 字符串解析为 Playwright cookie 列表。

        Args:
            cookie_str: 从浏览器复制的 Cookie 原始字符串。
            domain: Cookie 所属域名（如 ".zhihu.com"）。

        Returns:
            Playwright add_cookies() 接受的字典列表。
        """
        cookies: List[Dict[str, str]] = []
        for pair in cookie_str.split(";"):
            pair = pair.strip()
            if "=" not in pair:
                continue
            name, _, value = pair.partition("=")
            cookies.append({
                "name": name.strip(),
                "value": value.strip(),
                "domain": domain,
                "path": "/",
            })
        return cookies

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
