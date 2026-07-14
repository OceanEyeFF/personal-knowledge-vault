"""
TextProcessor 白盒覆盖测试。
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.utils.text_utils import TextProcessor  # noqa: E402


def test_prepare_fts5_data_keeps_english_terms_when_input_is_string() -> None:
    data = TextProcessor.prepare_fts5_data(
        title="Human Bottlenecks",
        summary="AI productivity bottlenecks",
        keywords="Knowledge Bottleneck,Executive Function",
        tags="AI Augmentation Limits,Serious Context of Use",
    )

    assert "Knowledge" in data["keywords"]
    assert "Executive" in data["keywords"]
    assert "Augmentation" in data["tags"]
    assert "Context" in data["tags"]
    assert "A   u   g" not in data["tags"]


def test_prepare_fts5_data_keeps_english_terms_when_input_is_list() -> None:
    data = TextProcessor.prepare_fts5_data(
        title="Human Bottlenecks",
        summary="AI productivity bottlenecks",
        keywords=["Knowledge Bottleneck", "Executive Function"],
        tags=["AI Augmentation Limits", "Serious Context of Use"],
    )

    assert "Knowledge" in data["keywords"]
    assert "Executive" in data["keywords"]
    assert "Augmentation" in data["tags"]
    assert "Context" in data["tags"]
