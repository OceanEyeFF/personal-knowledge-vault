"""
Chat transcript processor.

Supports TXT and JSON chat logs and generates a Markdown transcript.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from src.ai.deepseek_client import DeepSeekClient
from src.processors.base import BaseProcessor
from src.storage.markdown_store import Entry
from src.utils.config import get_config
from src.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class ChatMessage:
    """Structured chat message."""

    timestamp: str
    sender: str
    message: str


class ChatProcessor(BaseProcessor):
    """Processor for chat transcripts in TXT or JSON format."""

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
    def can_handle(cls, url: str) -> bool:
        """Return True for .txt or .json chat files."""
        lowered = url.lower()
        return lowered.endswith(".txt") or lowered.endswith(".json")

    async def process(self, url: str) -> Entry:
        """
        Process a chat transcript and return an Entry.

        Args:
            url: File path to the chat transcript.

        Returns:
            Entry with parsed chat content.
        """
        logger.info("ChatProcessor processing file=%s", url)
        file_path = Path(url)

        if not file_path.exists():
            raise FileNotFoundError(f"Chat transcript not found: {file_path}")

        raw_text = await asyncio.to_thread(file_path.read_text, encoding="utf-8")
        messages = self._parse_messages(file_path, raw_text)

        conversation_text = "\n".join(
            f"{msg.sender}: {msg.message}" for msg in messages if msg.message
        ).strip()

        summary, tags = await asyncio.to_thread(self._generate_summary_and_tags, conversation_text)

        topic = file_path.stem
        markdown = self._build_markdown(topic, summary, messages)

        summary_one_sentence = summary.splitlines()[0] if summary else ""
        if len(summary_one_sentence) > 50:
            summary_one_sentence = summary_one_sentence[:50]

        entry = Entry(
            title=f"聊天记录 - {topic}",
            source_type="chat",
            source_url=str(file_path),
            abstract=summary,
            summary_one_sentence=summary_one_sentence,
            summary_100_words=summary,
            tags=tags,
            content=markdown,
        )

        entry.metadata = {
            "source_type": "chat",
            "source_url": str(file_path),
            "message_count": len(messages),
            "participants": sorted({msg.sender for msg in messages if msg.sender}),
            "parsed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

        logger.info("ChatProcessor completed file=%s messages=%s", url, len(messages))
        return entry

    def _parse_messages(self, file_path: Path, raw_text: str) -> List[ChatMessage]:
        """Parse raw chat content into structured messages."""
        if file_path.suffix.lower() == ".json":
            return self._parse_json_chat(raw_text)
        return self._parse_text_chat(raw_text)

    def _parse_text_chat(self, raw_text: str) -> List[ChatMessage]:
        """Parse plain text chat logs into message objects."""
        messages: List[ChatMessage] = []

        blocks = re.split(r"\n\s*\n", raw_text.strip())
        for block in blocks:
            lines = [line.strip() for line in block.splitlines() if line.strip()]
            if not lines:
                continue

            header = lines[0]
            tokens = header.split()
            timestamp = ""
            sender = ""

            if len(tokens) >= 3:
                timestamp = " ".join(tokens[:2])
                sender = " ".join(tokens[2:])
            elif len(tokens) == 2:
                timestamp, sender = tokens
            else:
                sender = tokens[0]

            message = "\n".join(lines[1:]).strip()
            messages.append(ChatMessage(timestamp=timestamp, sender=sender, message=message))

        return messages

    def _parse_json_chat(self, raw_text: str) -> List[ChatMessage]:
        """Parse JSON chat logs into message objects."""
        try:
            data = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON chat format: {exc}") from exc

        if isinstance(data, dict):
            data = data.get("messages") or data.get("chat") or data.get("data") or []

        if not isinstance(data, list):
            raise ValueError("JSON chat format must be a list of messages")

        messages: List[ChatMessage] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            timestamp = str(item.get("timestamp", ""))
            sender = str(item.get("sender", ""))
            message = str(item.get("message", ""))
            messages.append(ChatMessage(timestamp=timestamp, sender=sender, message=message))

        return messages

    def _generate_summary_and_tags(self, conversation_text: str) -> tuple[str, List[str]]:
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
        return (conversation_text[:300] + "...") if len(conversation_text) > 300 else conversation_text

    def _fallback_tags(self, conversation_text: str) -> List[str]:
        """Fallback tags based on participant names."""
        participants = re.findall(r"^([^:]{1,30}):", conversation_text, flags=re.MULTILINE)
        tags = list(dict.fromkeys(participants))[:3]
        tags.extend(["chat", "conversation"])
        return tags[:5]

    def _build_markdown(self, topic: str, summary: str, messages: List[ChatMessage]) -> str:
        """Build Markdown transcript content."""
        lines = [
            f"# 聊天记录 - {topic}",
            "",
            "## 对话摘要",
            summary,
            "",
            "## 对话内容",
            "",
        ]

        for msg in messages:
            sender = msg.sender or "Unknown"
            timestamp = f" ({msg.timestamp})" if msg.timestamp else ""
            lines.append(f"**{sender}**{timestamp}:")
            if msg.message:
                quoted_message = msg.message.replace("\n", "\n> ")
                lines.append(f"> {quoted_message}")
            else:
                lines.append("> ")
            lines.append("")

        return "\n".join(lines).strip()
