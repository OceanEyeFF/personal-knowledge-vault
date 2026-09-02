"""Safety contracts for repository-tracked test fixtures."""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlsplit

PROJECT_ROOT = Path(__file__).parent.parent.parent


def test_tracked_manual_url_template_contains_only_reserved_example_hosts() -> None:
    template = PROJECT_ROOT / "tests" / "fixtures" / "test_urls.example.json"
    payload = json.loads(template.read_text(encoding="utf-8"))

    urls = [
        case["url"]
        for cases in payload["test_cases"].values()
        for case in cases
    ]

    assert urls
    assert {urlsplit(url).hostname for url in urls} == {"example.com"}
