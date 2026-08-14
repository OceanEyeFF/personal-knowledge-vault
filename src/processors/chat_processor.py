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
from typing import Any, List, Optional
from urllib.parse import urlsplit

from src.ai.deepseek_client import DeepSeekClient
from src.processors.base import BaseProcessor
from src.processors.local_file_reader import read_local_text_file
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
        *,
        config: Any | None = None,
    ):
        """
        Initialize the processor.

        Args:
            max_summary_words: Summary length in words.
            deepseek_client: Optional injected DeepSeek client (for testing).
        """
        runtime_config = config if config is not None else get_config()
        self._runtime_config = runtime_config
        self.max_summary_words = int(
            runtime_config.get("chat.summary_max_words", max_summary_words)
        )
        self._deepseek_client = deepseek_client
        self._deepseek_model = runtime_config.llm_model
        self._summary_temperature = float(
            runtime_config.get("ai.llm.temperature", 0.7)
        )

    @classmethod
    def can_handle(cls, url: str) -> bool:
        """Return True for local .txt/.json inputs, never for remote URLs."""
        if not isinstance(url, str) or not url.strip():
            return False
        candidate = url.strip()
        try:
            parsed = urlsplit(candidate)
        except ValueError:
            return False
        # A Windows drive prefix (``C:\\``) is parsed as a one-letter scheme;
        # every other URI authority/scheme belongs to another processor.
        is_windows_drive = bool(re.match(r"^[A-Za-z]:[\\/]", candidate))
        if parsed.netloc or (parsed.scheme and not is_windows_drive):
            return False
        lowered = candidate.lower()
        return lowered.endswith(".txt") or lowered.endswith(".json")

    async def process(self, url: str, *, allow_local_file: bool = False) -> Entry:
        """
        Process a chat transcript and return an Entry.

        Args:
            url: File path to the chat transcript.

        Returns:
            Entry with parsed chat content.
        """
        if not allow_local_file:
            raise ValueError("ChatProcessor local files require process_file()")
        logger.info("ChatProcessor processing input_type=file")
        file_path = Path(url)
        raw_text = await asyncio.to_thread(read_local_text_file, file_path)
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
            source_url=None,
            abstract=summary,
            summary_one_sentence=summary_one_sentence,
            summary_100_words=summary,
            tags=tags,
            content=markdown,
        )

        entry.metadata = {
            "source_type": "chat",
            "source_url": None,
            "message_count": len(messages),
            "participants": sorted({msg.sender for msg in messages if msg.sender}),
            "parsed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

        logger.info("ChatProcessor completed messages=%s", len(messages))
        return entry

    async def process_file(self, file_path: str | Path) -> Entry:
        """Explicit local-file entry point used by trusted CLI imports."""

        return await self.process(str(file_path), allow_local_file=True)

    def _parse_messages(self, file_path: Path, raw_text: str) -> List[ChatMessage]:
        """Parse raw chat content into structured messages."""
        if file_path.suffix.lower() == ".json":
            return self._parse_json_chat(raw_text)
        return self._parse_text_chat(raw_text)

    def _parse_text_chat(self, raw_text: str) -> List[ChatMessage]:
        """Parse plain text chat logs into message objects.

        Supports two formats:
        1. Standard format: "YYYY-MM-DD HH:MM Sender\nMessage"
        2. WeChat email format: "Sender HH:MM\n\nMessage"
        """
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

            # 尝试解析不同格式
            # 格式 1: "YYYY-MM-DD HH:MM Sender" (标准格式)
            # 格式 2: "Sender HH:MM" (微信邮件格式)

            if len(tokens) >= 3:
                # 检查是否是标准格式 (日期 时间 发送者)
                if re.match(r"\d{4}-\d{2}-\d{2}", tokens[0]):
                    # 标准格式: "YYYY-MM-DD HH:MM Sender"
                    timestamp = " ".join(tokens[:2])
                    sender = " ".join(tokens[2:])
                else:
                    # 微信邮件格式: "Sender HH:MM" (发送者可能包含多个词)
                    # 最后一个 token 是时间，其他是发送者
                    if re.match(r"\d{1,2}:\d{2}", tokens[-1]):
                        timestamp = tokens[-1]
                        sender = " ".join(tokens[:-1])
                    else:
                        # 无法识别格式，假设全是发送者名称
                        sender = " ".join(tokens)
            elif len(tokens) == 2:
                # 两个 token: 可能是 "Sender HH:MM" 或 "HH:MM Sender"
                if re.match(r"\d{1,2}:\d{2}", tokens[0]):
                    # "HH:MM Sender"
                    timestamp = tokens[0]
                    sender = tokens[1]
                elif re.match(r"\d{1,2}:\d{2}", tokens[1]):
                    # "Sender HH:MM" (微信邮件格式)
                    sender = tokens[0]
                    timestamp = tokens[1]
                else:
                    # 两个词都不是时间，假设是发送者名称
                    sender = " ".join(tokens)
            else:
                # 只有一个 token，假设是发送者
                sender = tokens[0] if tokens else "Unknown"

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
            client = self._deepseek_client or DeepSeekClient(
                config=self._runtime_config,
            )
            summary = client.summarize(
                conversation_text,
                max_words=self.max_summary_words,
                temperature=self._summary_temperature,
            )
            tags = client.extract_tags(conversation_text, num_tags=5, temperature=0.3)
            return summary, tags
        except Exception as exc:
            logger.warning(
                "AI summary generation failed: error_type=%s",
                type(exc).__name__,
            )
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
