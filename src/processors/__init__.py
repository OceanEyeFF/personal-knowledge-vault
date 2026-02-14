"""
Content processor registry and factory.

Provides processor selection for different content sources.
"""

from __future__ import annotations

from typing import List, Optional, Type

from src.processors.base import BaseProcessor
from src.utils.logger import get_logger

logger = get_logger(__name__)

_PROCESSORS: Optional[List[Type[BaseProcessor]]] = None


def _load_processors() -> List[Type[BaseProcessor]]:
    """Load processor classes in priority order."""
    from src.processors.wechat_processor import WechatProcessor
    from src.processors.zhihu_processor import ZhihuProcessor
    from src.processors.chat_processor import ChatProcessor
    from src.processors.generic_processor import GenericProcessor

    return [
        WechatProcessor,
        ZhihuProcessor,
        ChatProcessor,
        # GenericProcessor must be last as a fallback.
        GenericProcessor,
    ]


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
            logger.info("Selected processor: %s for url=%s", processor_class.__name__, url)
            return processor_class()

    # Fallback to generic processor (should always be last)
    from src.processors.generic_processor import GenericProcessor

    logger.warning("No specific processor matched. Falling back to GenericProcessor for url=%s", url)
    return GenericProcessor()
