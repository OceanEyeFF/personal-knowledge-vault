"""
Content processor registry and factory.

Provides processor selection for different content sources.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Type

from src.processors.base import BaseProcessor
from src.runtime.errors import ErrorCode, PKVRuntimeError
from src.utils.logger import get_logger

logger = get_logger(__name__)

_PROCESSORS: Optional[List[Type[BaseProcessor]]] = None


def normalize_processor_name(name: str) -> str:
    """Normalize a configured processor identifier to its stable registry key."""
    normalized = re.sub(r"[-\s]+", "_", name.strip().lower())
    if normalized.endswith("_processor"):
        normalized = normalized[: -len("_processor")]
    aliases = {
        "aichat": "ai_chat",
        "textfallback": "text_fallback",
    }
    return aliases.get(normalized, normalized)


def _processor_name(processor_class: Type[BaseProcessor]) -> str:
    name = processor_class.__name__
    if name.endswith("Processor"):
        name = name[: -len("Processor")]
    snake = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", name)
    snake = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", snake).lower()
    return normalize_processor_name(snake)


def _load_processors() -> List[Type[BaseProcessor]]:
    """Load processor classes in priority order."""
    from src.processors.ai_chat_processor import AIChatProcessor
    from src.processors.chat_processor import ChatProcessor
    from src.processors.generic_processor import GenericProcessor
    from src.processors.text_fallback_processor import TextFallbackProcessor
    from src.processors.wechat_processor import WechatProcessor
    from src.processors.zhihu_processor import ZhihuProcessor

    processors: List[Type[BaseProcessor]] = [
        WechatProcessor,
        ZhihuProcessor,
        ChatProcessor,
        AIChatProcessor,
        TextFallbackProcessor,
        # GenericProcessor must be last as a fallback.
        GenericProcessor,
    ]
    return processors


def get_processor(url: str) -> BaseProcessor:
    """
    Get a processor instance for the given URL.

    Args:
        url: Target URL or path.

    Returns:
        A processor instance capable of handling the URL.
    """
    if not url or not url.strip():
        raise ValueError("URL cannot be empty")

    global _PROCESSORS
    if _PROCESSORS is None:
        _PROCESSORS = _load_processors()

    for processor_class in _PROCESSORS:
        if processor_class.can_handle(url):
            logger.info("Selected processor: %s", processor_class.__name__)
            return processor_class()

    # Fallback to generic processor (should always be last)
    from src.processors.generic_processor import GenericProcessor

    logger.warning("No specific processor matched. Falling back to GenericProcessor")
    return GenericProcessor()


def get_processor_registry() -> Dict[str, Type[BaseProcessor]]:
    """Return the available explicit processor routes keyed by stable names."""
    global _PROCESSORS
    if _PROCESSORS is None:
        _PROCESSORS = _load_processors()
    return {_processor_name(processor): processor for processor in _PROCESSORS}


def get_available_processor_names() -> tuple[str, ...]:
    """Return available explicit processor names in deterministic order."""
    return tuple(sorted(get_processor_registry()))


def is_processor_available(processor_name: str) -> bool:
    """Check an explicit route after applying the public name normalization."""
    return normalize_processor_name(processor_name) in get_processor_registry()


def get_processor_by_name(processor_name: str) -> BaseProcessor:
    """Construct exactly the configured processor; never fall back to auto."""
    normalized = normalize_processor_name(processor_name)
    processor_class = get_processor_registry().get(normalized)
    if processor_class is None:
        raise PKVRuntimeError(
            ErrorCode.WORKFLOW_PROCESSOR_UNKNOWN,
            f"未知或不可用 processor: {processor_name}",
            stage="workflow_processor_selection",
            recoverable=True,
        )
    logger.info("Selected explicit processor: %s", processor_class.__name__)
    return processor_class()
