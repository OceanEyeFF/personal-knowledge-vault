"""
Text fallback processor.

Handles raw text inputs by detecting dialogue vs. article style and applying
graceful parsing with summary/tag extraction.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

from src.ai.deepseek_client import DeepSeekClient
from src.processors.base import BaseProcessor
from src.storage.markdown_store import Entry
from src.utils.config import get_config
from src.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class DialogueMessage:
    """Structured dialogue message."""

    speaker: str
    message: str


class TextFallbackProcessor(BaseProcessor):
    """Processor for raw text fallback inputs."""

    _SPEAKER_PATTERNS = [
        re.compile(
            r"^\s*(?:[-*]\s*)?(?:\d+[.)]\s*)?"
            r"(?P<speaker>[\w\u4e00-\u9fff][\w\u4e00-\u9fff\s]{0,30})\s*[:：]\s*(?P<message>.*)$"
        ),
        re.compile(
            r"^\s*(?:[-*]\s*)?"
            r"(?:\[(?P<speaker_bracket>[^\]]+)\]|【(?P<speaker_cn>[^】]+)】)"
            r"\s*[:：]?\s*(?P<message>.*)$"
        ),
    ]

    def __init__(
        self,
        max_summary_words: int = 160,
        deepseek_client: Optional[DeepSeekClient] = None,
    ):
        """
        Initialize the processor.

        Args:
            max_summary_words: Summary length in words.
            deepseek_client: Optional injected DeepSeek client (for testing).
        """
        config = get_config()
        self.max_summary_words = int(config.get("text_fallback.summary_max_words", max_summary_words))
        self._deepseek_client = deepseek_client
        self._deepseek_model = config.get("ai.deepseek.model", "deepseek-chat")
        self._summary_temperature = float(config.get("ai.deepseek.temperature", 0.7))

    @classmethod
    def can_handle(cls, url: str) -> bool:
        """Return True for non-URL text inputs."""
        if not url or not url.strip():
            return False
        return not re.match(r"^https?://", url.strip(), re.IGNORECASE)

    async def process(self, url: str) -> Entry:
        """
        Process raw text or local text file and return an Entry.

        Args:
            url: Raw text content or file path.

        Returns:
            Entry with parsed content.
        """
        logger.info("TextFallbackProcessor processing input length=%s", len(url) if url else 0)

        raw_text, source_path = await self._resolve_text(url)
        text = self._normalize_text(raw_text)
        if not text:
            return self._build_empty_entry(url, source_path)

        text_type = self._detect_text_type(text)
        messages: List[DialogueMessage] = []

        if text_type == "dialogue":
            messages = self._parse_dialogue(text)
            if not messages:
                logger.warning("Dialogue detected but no messages parsed; fallback to article.")
                text_type = "article"

        if text_type == "dialogue":
            conversation_text = self._build_conversation_text(messages)
            summary, tags = await asyncio.to_thread(
                self._generate_summary_and_tags,
                conversation_text,
                text_type,
                messages,
            )
            title = self._build_dialogue_title(source_path, messages)
            content = self._build_dialogue_markdown(title, summary, messages)
        else:
            summary, tags = await asyncio.to_thread(
                self._generate_summary_and_tags,
                text,
                text_type,
                messages,
            )
            title = self._extract_article_title(text, source_path)
            content = text.strip()

        summary_one_sentence = summary.splitlines()[0] if summary else ""
        if len(summary_one_sentence) > 50:
            summary_one_sentence = summary_one_sentence[:50]

        source_url = str(source_path) if source_path else None

        entry = Entry(
            title=title,
            source_type="text_fallback",
            source_url=source_url,
            abstract=summary,
            summary_one_sentence=summary_one_sentence,
            summary_100_words=summary,
            tags=tags,
            content=content,
        )

        entry.metadata = {
            "source_type": "text_fallback",
            "source_url": source_url,
            "text_type": text_type,
            "message_count": len(messages) if messages else 0,
            "participants": self._extract_participants(messages) if messages else [],
            "parsed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

        logger.info(
            "TextFallbackProcessor completed type=%s title=%s",
            text_type,
            title,
        )
        return entry

    async def _resolve_text(self, url: str) -> Tuple[str, Optional[Path]]:
        """Resolve input to text, loading from file if needed."""
        if not url or not url.strip():
            return "", None

        candidate = url.strip()
        if "\n" not in candidate:
            path = Path(candidate)
            if path.exists() and path.is_file():
                try:
                    text = await asyncio.to_thread(path.read_text, encoding="utf-8")
                except UnicodeDecodeError:
                    text = await asyncio.to_thread(path.read_text, encoding="utf-8", errors="ignore")
                return text, path

        return url, None

    def _normalize_text(self, text: str) -> str:
        """Normalize line endings and trim excessive blank lines."""
        if not text:
            return ""
        normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
        normalized = re.sub(r"\n{3,}", "\n\n", normalized)
        return normalized

    def _detect_text_type(self, text: str) -> str:
        """Detect whether text is a dialogue or an article."""
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if not lines:
            return "article"

        speaker_hits = 0
        speakers = set()
        for line in lines:
            parsed = self._parse_speaker_line(line)
            if parsed:
                speaker_hits += 1
                if parsed[0]:
                    speakers.add(parsed[0])

        ratio = speaker_hits / max(len(lines), 1)
        avg_len = sum(len(line) for line in lines) / max(len(lines), 1)
        unique_ratio = (len(speakers) / speaker_hits) if speaker_hits else 0

        if speaker_hits >= 3 and unique_ratio > 0.85:
            return "article"
        if speaker_hits >= 2 and len(speakers) >= 2 and ratio >= 0.2:
            return "dialogue"
        if speaker_hits >= 3 and ratio >= 0.15:
            return "dialogue"
        if speaker_hits >= 2 and avg_len < 40:
            return "dialogue"

        return "article"

    def _parse_dialogue(self, text: str) -> List[DialogueMessage]:
        """Parse dialogue text into messages."""
        messages: List[DialogueMessage] = []
        current_speaker = ""
        current_lines: List[str] = []

        for raw_line in text.splitlines():
            line = raw_line.rstrip()
            if not line.strip():
                if current_lines:
                    current_lines.append("")
                continue

            parsed = self._parse_speaker_line(line.strip())
            if parsed:
                if current_speaker or current_lines:
                    messages.append(
                        DialogueMessage(
                            speaker=current_speaker or "Unknown",
                            message="\n".join(current_lines).strip(),
                        )
                    )
                current_speaker, message = parsed
                current_lines = [message] if message else []
            else:
                if not current_speaker:
                    current_speaker = "Unknown"
                current_lines.append(line.strip())

        if current_speaker or current_lines:
            messages.append(
                DialogueMessage(
                    speaker=current_speaker or "Unknown",
                    message="\n".join(current_lines).strip(),
                )
            )

        return [msg for msg in messages if msg.speaker or msg.message]

    def _parse_speaker_line(self, line: str) -> Optional[Tuple[str, str]]:
        """Parse a speaker line and return (speaker, message)."""
        for pattern in self._SPEAKER_PATTERNS:
            match = pattern.match(line)
            if not match:
                continue
            speaker = (
                match.groupdict().get("speaker")
                or match.groupdict().get("speaker_bracket")
                or match.groupdict().get("speaker_cn")
                or ""
            )
            message = match.groupdict().get("message") or ""
            return self._normalize_speaker(speaker), message.strip()
        return None

    def _normalize_speaker(self, speaker: str) -> str:
        """Normalize speaker name."""
        if not speaker:
            return ""
        normalized = re.sub(r"\s+", " ", speaker).strip()
        return normalized

    def _build_conversation_text(self, messages: Iterable[DialogueMessage]) -> str:
        """Flatten dialogue messages into text for summarization."""
        lines = []
        for msg in messages:
            if not msg.message:
                continue
            speaker = msg.speaker or "Unknown"
            lines.append(f"{speaker}: {msg.message}".strip())
        return "\n".join(lines)

    def _build_dialogue_title(self, source_path: Optional[Path], messages: List[DialogueMessage]) -> str:
        """Build a dialogue title based on source or participants."""
        topic = ""
        if source_path:
            topic = source_path.stem
        else:
            participants = self._extract_participants(messages)
            if participants:
                topic = "、".join(participants[:2])
        return f"对话记录 - {topic}" if topic else "对话记录"

    def _extract_article_title(self, text: str, source_path: Optional[Path]) -> str:
        """Extract an article title from text or file name."""
        for line in text.splitlines():
            candidate = line.strip()
            if not candidate:
                continue
            if candidate.startswith("#"):
                candidate = candidate.lstrip("#").strip()
            if candidate and not self._parse_speaker_line(candidate) and len(candidate) <= 80:
                return candidate
            break

        if source_path:
            return source_path.stem
        return "未命名文本"

    def _build_dialogue_markdown(
        self,
        title: str,
        summary: str,
        messages: List[DialogueMessage],
    ) -> str:
        """Build Markdown transcript for dialogue content."""
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
            speaker = msg.speaker or "Unknown"
            lines.append(f"**{speaker}**:")
            if msg.message:
                quoted = msg.message.replace("\n", "\n> ")
                lines.append(f"> {quoted}")
            else:
                lines.append("> ")
            lines.append("")

        return "\n".join(lines).strip()

    def _generate_summary_and_tags(
        self,
        text: str,
        text_type: str,
        messages: List[DialogueMessage],
    ) -> tuple[str, List[str]]:
        """Generate summary and tags using DeepSeek, with graceful fallback."""
        if not text:
            return "内容为空。", ["text", "empty", "fallback"]

        try:
            client = self._deepseek_client or DeepSeekClient(model=self._deepseek_model)
            summary = client.summarize(
                text,
                max_words=self.max_summary_words,
                temperature=self._summary_temperature,
            )
            tags = client.extract_tags(text, num_tags=5, temperature=0.3)
            return summary, tags
        except Exception as exc:
            logger.warning("AI summary generation failed: %s", exc)
            summary = self._fallback_summary(text, text_type, messages)
            tags = self._fallback_tags(text, text_type, messages)
            return summary, tags

    def _fallback_summary(
        self,
        text: str,
        text_type: str,
        messages: List[DialogueMessage],
    ) -> str:
        """Fallback summary by truncating the content."""
        if text_type == "dialogue" and messages:
            participants = self._extract_participants(messages)
            if participants:
                prefix = "、".join(participants[:2])
                if len(participants) > 2:
                    prefix += " 等"
                base = f"对话参与者：{prefix}。"
            else:
                base = "对话摘要："
            snippet = text[:200] + "..." if len(text) > 200 else text
            return f"{base}\n{snippet}".strip()

        return (text[:300] + "...") if len(text) > 300 else text

    def _fallback_tags(
        self,
        text: str,
        text_type: str,
        messages: List[DialogueMessage],
    ) -> List[str]:
        """Fallback tags based on participants or simple keyword extraction."""
        if text_type == "dialogue":
            tags = self._extract_participants(messages)
            tags.extend(["dialogue", "conversation"])
            return self._dedupe_tags(tags)[:5]

        keywords = self._extract_keywords(text, limit=3)
        keywords.extend(["text", "article"])
        return self._dedupe_tags(keywords)[:5]

    def _extract_keywords(self, text: str, limit: int = 5) -> List[str]:
        """Extract simple keywords based on frequency."""
        tokens = re.findall(r"[A-Za-z]{3,}|[\u4e00-\u9fff]{2,}", text)
        if not tokens:
            return []

        counts = {}
        for token in tokens:
            counts[token] = counts.get(token, 0) + 1

        sorted_tokens = sorted(counts.items(), key=lambda item: item[1], reverse=True)
        return [token for token, _ in sorted_tokens[:limit]]

    def _extract_participants(self, messages: List[DialogueMessage]) -> List[str]:
        """Extract participant list from messages."""
        participants: List[str] = []
        for msg in messages:
            if msg.speaker and msg.speaker not in participants:
                participants.append(msg.speaker)
        return participants

    def _dedupe_tags(self, tags: Iterable[str]) -> List[str]:
        """Remove duplicates while preserving order."""
        seen = set()
        result = []
        for tag in tags:
            cleaned = tag.strip()
            if not cleaned or cleaned in seen:
                continue
            seen.add(cleaned)
            result.append(cleaned)
        return result

    def _build_empty_entry(self, url: str, source_path: Optional[Path]) -> Entry:
        """Build an empty Entry for blank input."""
        source_url = str(source_path) if source_path else None
        title = source_path.stem if source_path else "未命名文本"
        entry = Entry(
            title=title,
            source_type="text_fallback",
            source_url=source_url,
            abstract="内容为空。",
            summary_one_sentence="内容为空。",
            summary_100_words="内容为空。",
            tags=["text", "empty", "fallback"],
            content="",
        )
        entry.metadata = {
            "source_type": "text_fallback",
            "source_url": source_url,
            "text_type": "empty",
            "message_count": 0,
            "participants": [],
            "parsed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        return entry
