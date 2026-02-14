"""
Base processor definitions for content handling.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Dict, Optional

from bs4 import BeautifulSoup
import html2text

from src.storage.markdown_store import Entry
from src.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class ExtractedMetadata:
    """Structured metadata extracted from HTML."""

    title: str = ""
    author: str = ""
    published_time: str = ""
    description: str = ""

    def to_dict(self) -> Dict[str, str]:
        """Convert to dict, omitting empty values."""
        data = {
            "title": self.title,
            "author": self.author,
            "published_time": self.published_time,
            "description": self.description,
        }
        return {key: value for key, value in data.items() if value}


class BaseProcessor(ABC):
    """Base class for content processors."""

    @classmethod
    @abstractmethod
    def can_handle(cls, url: str) -> bool:
        """Return True if this processor can handle the given URL."""

    @abstractmethod
    async def process(self, url: str) -> Entry:
        """Process the URL and return an Entry."""

    def _extract_metadata(self, soup: BeautifulSoup) -> Dict[str, str]:
        """
        Extract common metadata from HTML.

        Subclasses can override to provide richer metadata extraction.
        """
        metadata = ExtractedMetadata()

        og_title = self._get_meta_content(soup, property="og:title")
        metadata.title = og_title or self._get_title_text(soup)

        metadata.author = self._get_meta_content(soup, name="author")
        metadata.published_time = self._get_meta_content(soup, property="article:published_time")

        description = self._get_meta_content(soup, name="description")
        og_description = self._get_meta_content(soup, property="og:description")
        metadata.description = description or og_description

        return metadata.to_dict()

    def _html_to_markdown(self, html: str) -> str:
        """Convert HTML to Markdown using a consistent configuration."""
        if not html or not html.strip():
            return ""

        converter = html2text.HTML2Text()
        converter.ignore_links = False
        converter.ignore_images = False
        converter.body_width = 0
        converter.unicode_snob = True
        converter.protect_links = True

        markdown = converter.handle(html)
        markdown = markdown.replace("\r\n", "\n")
        markdown = re.sub(r"\n{3,}", "\n\n", markdown)
        return markdown.strip()

    def _get_meta_content(self, soup: BeautifulSoup, **attrs: str) -> Optional[str]:
        tag = soup.find("meta", attrs=attrs)
        if tag and tag.get("content"):
            return tag["content"].strip()
        return None

    def _get_title_text(self, soup: BeautifulSoup) -> str:
        title_tag = soup.find("title")
        if title_tag and title_tag.get_text(strip=True):
            return title_tag.get_text(strip=True)
        h1_tag = soup.find("h1")
        if h1_tag and h1_tag.get_text(strip=True):
            return h1_tag.get_text(strip=True)
        return ""
