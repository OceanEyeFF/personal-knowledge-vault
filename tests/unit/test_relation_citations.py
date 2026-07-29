"""Public citation/source sanitization regressions."""

import pytest

from src.relations.citations import (
    build_entry_locator,
    is_local_reference,
    resolve_citation_source,
    sanitize_public_evidence,
    sanitize_public_source_url,
)


@pytest.mark.parametrize(
    "local_reference",
    [
        r"C:\Users\fixture\chat.html",
        r"\\fixture-server\share\chat.md",
        r"\Windows\System32\secret.txt",
        r"\??\C:\Windows\System32\secret.txt",
        "file:///C:/Users/fixture/chat.md",
        "file://fixture-server/share/chat.md",
        "file://localhost/C:/Users/fixture/chat.md",
    ],
)
def test_local_source_urls_are_cleared_and_fall_back_to_entry_resource(
    local_reference: str,
) -> None:
    assert is_local_reference(local_reference) is True
    assert sanitize_public_source_url(local_reference) == ""
    assert resolve_citation_source(7, source_url=local_reference) == (
        build_entry_locator(7)
    )


def test_nested_evidence_redacts_local_references_by_value() -> None:
    payload = {
        "knowledge_id": 7,
        "source_url": "file:///C:/Users/fixture/source.md",
        "source": r"C:\Users\fixture\source.md",
        "evidence_payload": {
            "raw_target": r"\\fixture-server\share\target.md",
            "origin": "file://fixture-server/share/origin.md",
            "nested": {"value": "/home/fixture/private.md"},
        },
    }

    sanitized = sanitize_public_evidence(payload)

    assert sanitized["source_url"] == ""
    assert sanitized["source"] == "pkv://entries/7"
    assert sanitized["evidence_payload"] == {
        "raw_target": "[redacted-local-reference]",
        "origin": "[redacted-local-reference]",
        "nested": {"value": "[redacted-local-reference]"},
    }


def test_nested_evidence_redacts_local_references_in_dynamic_keys() -> None:
    sanitized = sanitize_public_evidence(
        {
            r"\Windows\System32\private-tag": "first",
            r"\??\C:\private-tag": "second",
            "[redacted-local-reference]": "third",
        }
    )

    assert sanitized == {
        "[redacted-local-reference]": "first",
        "[redacted-local-reference]#2": "second",
        "[redacted-local-reference]#3": "third",
    }
