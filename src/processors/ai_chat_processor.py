"""
AI chat conversation processor.

Supports ChatGPT and DeepSeek exports in HTML/Markdown formats.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
import re
from pathlib import Path
from typing import List, Optional, Tuple

from bs4 import BeautifulSoup

from src.ai.deepseek_client import DeepSeekClient
from src.processors.base import BaseProcessor
from src.storage.markdown_store import Entry
from src.utils.config import get_config
from src.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class AIChatMessage:
    """Structured AI chat message."""

    role: str
    content: str


class AIChatProcessor(BaseProcessor):
    """Processor for AI chat exports from ChatGPT and DeepSeek."""

    _CHATGPT_MD_USER = re.compile(r"^\*\*(You|User)\*\*:\s*(.*)$", re.IGNORECASE)
    _CHATGPT_MD_ASSISTANT = re.compile(r"^\*\*(ChatGPT|Assistant)\*\*:\s*(.*)$", re.IGNORECASE)
    _DEEPSEEK_MD_HEADER = re.compile(r"^###\s*(.+?)\s*$")

    def __init__(
        self,
        max_summary_words: int = 120,
        deepseek_client: Optional[DeepSeekClient] = None,
    ):
        """
        Initialize the processor.

        Args:
            max_summary_words: Summary length in words.
            deepseek_client: Optional injected DeepSeek client (for testing).
        """
        config = get_config()
        self.max_summary_words = int(config.get("chat.summary_max_words", max_summary_words))
        self._deepseek_client = deepseek_client
        self._deepseek_model = config.get("ai.deepseek.model", "deepseek-chat")
        self._summary_temperature = float(config.get("ai.deepseek.temperature", 0.7))

    @classmethod
    def can_handle(cls, url_or_text: str) -> bool:
        """Return True if the input looks like a supported AI chat export."""
        if not url_or_text or not url_or_text.strip():
            return False

        candidate = url_or_text.strip()
        if candidate.startswith("http://") or candidate.startswith("https://"):
            return False

        path = Path(candidate)
        if path.exists() and path.is_file():
            try:
                sample = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                return False
            return cls._looks_like_ai_chat(sample)

        return cls._looks_like_ai_chat(candidate)

    @classmethod
    def _looks_like_ai_chat(cls, text: str) -> bool:
        """Detect AI chat markers in text content."""
        if cls._looks_like_chatgpt_html(text):
            return True
        if cls._looks_like_deepseek_html(text):
            return True
        if cls._looks_like_chatgpt_markdown(text):
            return True
        if cls._looks_like_deepseek_markdown(text):
            return True
        return False

    @classmethod
    def _looks_like_chatgpt_html(cls, text: str) -> bool:
        if "data-turn" not in text and "data-message-author-role" not in text:
            return False
        return True

    @classmethod
    def _looks_like_deepseek_html(cls, text: str) -> bool:
        if "message" not in text:
            return False
        return bool(re.search(r"class=[\"'].*message.*\b(user|assistant)\b", text, re.IGNORECASE))

    @classmethod
    def _looks_like_chatgpt_markdown(cls, text: str) -> bool:
        has_user = bool(re.search(r"^\*\*(You|User)\*\*:", text, re.MULTILINE | re.IGNORECASE))
        has_assistant = bool(
            re.search(r"^\*\*(ChatGPT|Assistant)\*\*:", text, re.MULTILINE | re.IGNORECASE)
        )
        return has_user and has_assistant

    @classmethod
    def _looks_like_deepseek_markdown(cls, text: str) -> bool:
        has_user = bool(re.search(r"^###\s*用户\b", text, re.MULTILINE))
        has_assistant = bool(re.search(r"^###\s*DeepSeek\b", text, re.MULTILINE))
        return has_user and has_assistant

    async def process(self, url_or_text: str) -> Entry:
        """
        Process an AI chat export and return an Entry.

        Args:
            url_or_text: File path or raw chat content.

        Returns:
            Entry with parsed AI chat content.
        """
        logger.info("AIChatProcessor processing input=%s", url_or_text)

        content, file_path = await self._load_content(url_or_text)
        platform, content_format = self._detect_format(content)
        messages = self._parse_messages(content, platform, content_format)

        if not messages:
            fallback_content = self._html_to_markdown(content) if content_format == "html" else content
            messages = [AIChatMessage(role="user", content=fallback_content.strip())]

        conversation_text = "\n".join(
            f"{self._display_name(platform, msg.role)}: {msg.content}"
            for msg in messages
            if msg.content
        ).strip()

        summary, tags = await asyncio.to_thread(self._generate_summary_and_tags, conversation_text)

        topic = self._infer_topic(messages, content, content_format)
        if not topic and file_path is not None:
            topic = file_path.stem
        topic = topic or "AI 对话"

        title = f"AI 对话 - {topic}"
        markdown = self._build_markdown(title, summary, messages, platform)

        summary_one_sentence = summary.splitlines()[0] if summary else ""
        if len(summary_one_sentence) > 50:
            summary_one_sentence = summary_one_sentence[:50]

        source_url = str(file_path) if file_path is not None else None

        entry = Entry(
            title=title,
            source_type="ai_chat",
            source_url=source_url,
            abstract=summary,
            summary_one_sentence=summary_one_sentence,
            summary_100_words=summary,
            tags=tags,
            content=markdown,
        )

        entry.metadata = {
            "source_type": "ai_chat",
            "source_url": source_url or "",
            "ai_platform": "ChatGPT" if platform == "chatgpt" else "DeepSeek",
            "format": content_format,
            "message_count": len(messages),
            "participants": sorted({self._display_name(platform, msg.role) for msg in messages}),
            "parsed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

        logger.info(
            "AIChatProcessor completed format=%s platform=%s messages=%s",
            content_format,
            platform,
            len(messages),
        )
        return entry

    async def _load_content(self, url_or_text: str) -> Tuple[str, Optional[Path]]:
        """Load content from file or return raw text."""
        if not url_or_text or not url_or_text.strip():
            raise ValueError("Input cannot be empty")

        candidate = url_or_text.strip()
        path = Path(candidate)
        if path.exists() and path.is_file():
            text = await asyncio.to_thread(path.read_text, encoding="utf-8", errors="ignore")
            return text, path

        if self._looks_like_file_path(candidate):
            raise FileNotFoundError(f"AI chat export not found: {candidate}")

        return candidate, None

    def _looks_like_file_path(self, value: str) -> bool:
        if "\n" in value or value.startswith("http://") or value.startswith("https://"):
            return False
        suffix = Path(value).suffix.lower()
        return suffix in {".html", ".htm", ".md", ".markdown"}

    def _detect_format(self, content: str) -> Tuple[str, str]:
        """Detect AI platform and content format."""
        if self._looks_like_chatgpt_html(content):
            return "chatgpt", "html"
        if self._looks_like_chatgpt_markdown(content):
            return "chatgpt", "markdown"
        if self._looks_like_deepseek_html(content):
            return "deepseek", "html"
        if self._looks_like_deepseek_markdown(content):
            return "deepseek", "markdown"
        raise ValueError("Unsupported AI chat format")

    def _parse_messages(self, content: str, platform: str, content_format: str) -> List[AIChatMessage]:
        if platform == "chatgpt" and content_format == "html":
            return self._parse_chatgpt_html(content)
        if platform == "chatgpt" and content_format == "markdown":
            return self._parse_chatgpt_markdown(content)
        if platform == "deepseek" and content_format == "html":
            return self._parse_deepseek_html(content)
        if platform == "deepseek" and content_format == "markdown":
            return self._parse_deepseek_markdown(content)
        return []

    def _parse_chatgpt_html(self, html: str) -> List[AIChatMessage]:
        soup = BeautifulSoup(html, "lxml")
        for tag in soup.find_all(["script", "style", "noscript"]):
            tag.decompose()

        nodes = soup.select("[data-message-author-role]")
        if not nodes:
            nodes = soup.select("[data-turn]")

        messages: List[AIChatMessage] = []
        for node in nodes:
            role = self._normalize_role(node.get("data-message-author-role", ""))
            if not role:
                role = self._normalize_role(node.get("data-turn", ""))
            if not role:
                role = self._role_from_class(node.get("class", []))
            if not role:
                continue

            content_tag = node.select_one(".markdown") or node.select_one(".content") or node
            message = self._html_to_markdown(str(content_tag))
            message = self._clean_message_text(message)
            if message:
                messages.append(AIChatMessage(role=role, content=message))

        return messages

    def _parse_deepseek_html(self, html: str) -> List[AIChatMessage]:
        soup = BeautifulSoup(html, "lxml")
        for tag in soup.find_all(["script", "style", "noscript"]):
            tag.decompose()

        nodes = soup.select(".message.user, .message.assistant")
        messages: List[AIChatMessage] = []

        for node in nodes:
            role = self._role_from_class(node.get("class", []))
            if not role:
                continue
            content_tag = node.select_one(".content") or node
            message = self._html_to_markdown(str(content_tag))
            message = self._clean_message_text(message)
            if message:
                messages.append(AIChatMessage(role=role, content=message))

        return messages

    def _parse_chatgpt_markdown(self, markdown: str) -> List[AIChatMessage]:
        messages: List[AIChatMessage] = []
        current_role = ""
        current_lines: List[str] = []

        for line in markdown.splitlines():
            stripped = line.strip()
            user_match = self._CHATGPT_MD_USER.match(stripped)
            assistant_match = self._CHATGPT_MD_ASSISTANT.match(stripped)

            if user_match or assistant_match:
                if current_role:
                    content = "\n".join(current_lines).strip()
                    if content:
                        messages.append(AIChatMessage(role=current_role, content=content))
                if user_match:
                    current_role = "user"
                    current_lines = [user_match.group(2)] if user_match.group(2) else []
                else:
                    current_role = "assistant"
                    current_lines = [assistant_match.group(2)] if assistant_match.group(2) else []
                continue

            if current_role:
                current_lines.append(line)

        if current_role:
            content = "\n".join(current_lines).strip()
            if content:
                messages.append(AIChatMessage(role=current_role, content=content))

        return messages

    def _parse_deepseek_markdown(self, markdown: str) -> List[AIChatMessage]:
        messages: List[AIChatMessage] = []
        current_role = ""
        current_lines: List[str] = []

        for line in markdown.splitlines():
            header = self._DEEPSEEK_MD_HEADER.match(line.strip())
            if header:
                role = self._normalize_deepseek_role(header.group(1))
                if role:
                    if current_role:
                        content = "\n".join(current_lines).strip()
                        if content:
                            messages.append(AIChatMessage(role=current_role, content=content))
                    current_role = role
                    current_lines = []
                    continue

            if current_role:
                current_lines.append(line)

        if current_role:
            content = "\n".join(current_lines).strip()
            if content:
                messages.append(AIChatMessage(role=current_role, content=content))

        return messages

    def _normalize_role(self, raw_value: str) -> str:
        value = raw_value.strip().lower()
        if not value:
            return ""
        if "user" in value or value in {"you", "human"}:
            return "user"
        if "assistant" in value or "chatgpt" in value or value == "ai":
            return "assistant"
        return ""

    def _normalize_deepseek_role(self, label: str) -> str:
        if not label:
            return ""
        normalized = label.strip().lower()
        if "用户" in label or "user" in normalized:
            return "user"
        if "deepseek" in normalized or "assistant" in normalized or "助手" in label:
            return "assistant"
        return ""

    def _role_from_class(self, classes: List[str]) -> str:
        lowered = {cls.lower() for cls in classes}
        if "user" in lowered:
            return "user"
        if "assistant" in lowered or "chatgpt" in lowered:
            return "assistant"
        return ""

    def _clean_message_text(self, message: str) -> str:
        cleaned = message.strip()
        cleaned = re.sub(
            r"^(You|User|ChatGPT|Assistant|DeepSeek AI|DeepSeek|用户|助手)\s*:?\s*",
            "",
            cleaned,
            flags=re.IGNORECASE,
        )
        return cleaned.strip()

    def _generate_summary_and_tags(self, conversation_text: str) -> Tuple[str, List[str]]:
        """Generate summary and tags using DeepSeek, with graceful fallback."""
        if not conversation_text:
            return "对话内容为空。", ["chat", "empty", "conversation"]

        try:
            client = self._deepseek_client or DeepSeekClient(model=self._deepseek_model)
            summary = client.summarize(
                conversation_text,
                max_words=self.max_summary_words,
                temperature=self._summary_temperature,
            )
            tags = client.extract_tags(conversation_text, num_tags=5, temperature=0.3)
            return summary, tags
        except Exception as exc:
            logger.warning("AI summary generation failed: %s", exc)
            summary = self._fallback_summary(conversation_text)
            tags = self._fallback_tags(conversation_text)
            return summary, tags

    def _fallback_summary(self, conversation_text: str) -> str:
        """Fallback summary by truncating the conversation."""
        return (
            conversation_text[:300] + "..." if len(conversation_text) > 300 else conversation_text
        )

    def _fallback_tags(self, conversation_text: str) -> List[str]:
        """Fallback tags based on role labels."""
        participants = re.findall(r"^([^:]{1,30}):", conversation_text, flags=re.MULTILINE)
        tags = list(dict.fromkeys(participants))[:3]
        tags.extend(["chat", "conversation"])
        return tags[:5]

    def _infer_topic(self, messages: List[AIChatMessage], content: str, content_format: str) -> str:
        """Infer a topic from the first user message or HTML title."""
        if content_format == "html":
            soup = BeautifulSoup(content, "lxml")
            title = self._get_title_text(soup)
            if title:
                return title

        for msg in messages:
            if msg.role == "user" and msg.content:
                return self._shorten_title(msg.content)

        for msg in messages:
            if msg.content:
                return self._shorten_title(msg.content)

        return ""

    def _shorten_title(self, text: str, max_length: int = 32) -> str:
        cleaned = text.strip().splitlines()[0] if text.strip() else ""
        cleaned = re.sub(r"^#+\s*", "", cleaned)
        cleaned = re.sub(r"^>\s*", "", cleaned)
        cleaned = re.sub(r"[`*_]", "", cleaned)
        if len(cleaned) > max_length:
            cleaned = cleaned[:max_length]
        return cleaned.strip()

    def _display_name(self, platform: str, role: str) -> str:
        mapping = {
            "chatgpt": {"user": "You", "assistant": "ChatGPT"},
            "deepseek": {"user": "用户", "assistant": "DeepSeek AI"},
        }
        return mapping.get(platform, {}).get(role, role.title())

    def _build_markdown(
        self,
        title: str,
        summary: str,
        messages: List[AIChatMessage],
        platform: str,
    ) -> str:
        """Build Markdown transcript content."""
        lines = [
            f"# {title}",
            "",
            "## 对话摘要",
            summary,
            "",
            "## 对话内容",
            "",
        ]

        for msg in messages:
            sender = self._display_name(platform, msg.role) or "Unknown"
            lines.append(f"**{sender}**:")
            if msg.content:
                quoted_message = msg.content.replace("\n", "\n> ")
                lines.append(f"> {quoted_message}")
            else:
                lines.append("> ")
            lines.append("")

        return "\n".join(lines).strip()
