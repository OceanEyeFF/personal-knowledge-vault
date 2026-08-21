"""
Zhihu content processor.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from bs4 import BeautifulSoup, Tag

from src.processors.base import BaseProcessor
from src.processors.safe_fetch import SafeFetcher, describe_url_target, parse_http_target
from src.runtime.errors import ErrorCode, PKVRuntimeError
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

_ZHIHU_CONTENT_HOSTS = frozenset(
    {"zhihu.com", "www.zhihu.com", "zhuanlan.zhihu.com"}
)


class ZhihuProcessor(BaseProcessor):
    """Processor for Zhihu questions and posts.

    支持可选的 Cookie 注入（通过 %USERPROFILE%\\.pkv\\config.yaml 配置），
    用于绕过知乎登录墙获取完整内容。
    """

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
            "processors.zhihu.user_agent",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
        )
        self._cookie_str: Optional[str] = runtime_config.zhihu_cookie

    @classmethod
    def can_handle(cls, url: str) -> bool:
        """Return True for Zhihu URLs."""
        try:
            hostname = parse_http_target(url).hostname
        except Exception:
            return False
        return hostname in _ZHIHU_CONTENT_HOSTS

    async def process(self, url: str) -> Entry:
        """
        Process a Zhihu page and return an Entry.

        Args:
            url: Zhihu URL.

        Returns:
            Entry with extracted content.
        """
        logger.info("ZhihuProcessor processing target=%s", describe_url_target(url))
        html = await self._fetch_html(url)
        if not html:
            raise ValueError("Empty HTML content returned by target")

        # 登录墙检测
        if self._is_login_wall(html):
            if self._cookie_str:
                logger.warning(
                    "⚠️ 知乎登录墙检测：已配置 Cookie 但仍被拦截，Cookie 可能已过期。"
                    "请重新从浏览器复制 Cookie 到 %USERPROFILE%\\.pkv\\config.yaml 的 "
                    "processors.zhihu.cookie"
                )
            else:
                logger.warning(
                    "⚠️ 知乎登录墙检测：该页面需要登录才能查看完整内容。\n"
                    "解决方法：\n"
                    "  1. 在 %USERPROFILE%\\.pkv\\config.yaml 中配置 processors.zhihu.cookie\n"
                    "  2. 或者直接复制页面文本，使用「文本归档」功能\n"
                    "请仅编辑 %USERPROFILE%\\.pkv\\config.yaml"
                )
            raise ValueError(
                "知乎登录墙：该页面需要登录才能查看完整内容。"
                "请配置 processors.zhihu.cookie 或使用文本归档功能"
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

        logger.info(
            "ZhihuProcessor completed target=%s content_length=%s",
            describe_url_target(url),
            len(markdown),
        )
        return entry

    async def _fetch_html(self, url: str) -> str:
        """Fetch HTML through the only published, DNS-pinned transport."""
        return await self._fetch_with_requests(url)

    async def _fetch_with_requests(self, url: str) -> str:
        """Compatibility name for the SSRF-safe HTTP fetch path."""
        headers = {"User-Agent": self.user_agent}
        if self._cookie_str:
            target = parse_http_target(url)
            hostname = target.hostname
            if hostname not in _ZHIHU_CONTENT_HOSTS:
                raise PKVRuntimeError(
                    ErrorCode.SSRF_TARGET_FORBIDDEN,
                    "禁止向非知乎域名发送 Cookie",
                    stage="network_policy",
                    recoverable=False,
                )
            if target.scheme != "https":
                raise PKVRuntimeError(
                    ErrorCode.SSRF_TARGET_FORBIDDEN,
                    "禁止通过非 HTTPS 请求发送 Cookie",
                    stage="network_policy",
                    recoverable=False,
                )
            headers["Cookie"] = self._cookie_str
        response = await self._fetch_public_url(url, headers=headers)
        return response.text

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
