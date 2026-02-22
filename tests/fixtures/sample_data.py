"""Sample HTML fixtures for test database generation."""

from __future__ import annotations

from pathlib import Path
from typing import List, Tuple


def _read_fixture(filename: str) -> str:
    return (Path(__file__).parent / filename).read_text(encoding="utf-8")


def _variant(html: str, replacements: dict[str, str]) -> str:
    for old, new in replacements.items():
        html = html.replace(old, new)
    return html


_BASE_WECHAT_HTML = _read_fixture("wechat_sample.html")
_BASE_ZHIHU_HTML = _read_fixture("zhihu_sample.html")


def _build_wechat_samples() -> List[Tuple[str, str]]:
    samples = []
    for idx in range(1, 4):
        samples.append(
            (
                f"https://mp.weixin.qq.com/s/sample-{idx}",
                _variant(
                    _BASE_WECHAT_HTML,
                    {
                        "Wechat Sample Title": f"Wechat Sample Title {idx}",
                        "Wechat sample description.": f"Wechat sample description {idx}.",
                        "Wechat main content paragraph.": f"Wechat main content paragraph {idx}.",
                    },
                ),
            )
        )
    return samples


def _build_zhihu_samples() -> List[Tuple[str, str]]:
    samples = []
    for idx in range(1, 4):
        samples.append(
            (
                f"https://www.zhihu.com/question/sample-{idx}/answer/{idx}",
                _variant(
                    _BASE_ZHIHU_HTML,
                    {
                        "Why is the sky blue?": f"Why is the sky blue? ({idx})",
                        "Best answer content.": f"Best answer content {idx}.",
                        "Low score answer.": f"Low score answer {idx}.",
                    },
                ),
            )
        )
    return samples


WECHAT_SAMPLES: List[Tuple[str, str]] = _build_wechat_samples()
ZHIHU_SAMPLES: List[Tuple[str, str]] = _build_zhihu_samples()
