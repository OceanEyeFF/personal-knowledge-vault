"""Structured GUI preview outcome and redaction contract tests."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.gui.utils.preview_loader import (
    PreviewOutcome,
    is_strict_preview_outcome,
    load_entry_preview,
    load_entry_preview_outcome,
)
from src.runtime.errors import ErrorCode, PKVRuntimeError


def test_full_markdown_returns_success_without_issue() -> None:
    store = MagicMock()
    store.load.return_value = SimpleNamespace(content="# Full content")

    outcome = load_entry_preview_outcome(
        {"file_path": "text/entry.md", "title": "Entry"},
        store,
    )

    assert outcome == PreviewOutcome(status="success", content="# Full content")
    assert outcome.issue is None


def test_missing_markdown_returns_explicit_degraded_safe_summary(
    caplog,
) -> None:
    canary = (
        r"C:\private\vault\secret.md"
        "\r\nAuthorization: Bearer FILE-SECRET-CANARY"
    )
    store = MagicMock()
    store.load.side_effect = FileNotFoundError(canary)
    caplog.set_level(logging.WARNING, logger="pkv.gui.utils.preview")

    outcome = load_entry_preview_outcome(
        {
            "file_path": r"C:\private\vault\secret.md",
            "title": "Safe title",
            "summary_one_sentence": "Safe summary",
            "source_url": (
                "https://user:pass@example.com/article"
                "?api_key=URL-SECRET-CANARY&view=full"
                "#token=FRAGMENT-SECRET-CANARY"
            ),
        },
        store,
    )

    assert outcome.status == "degraded"
    assert outcome.issue is not None
    assert outcome.issue.code is ErrorCode.RESOURCE_MISSING
    assert "Safe summary" in outcome.content
    assert "https://example.com/article" in outcome.content
    assert "api_key=redacted" in outcome.content
    public = repr(outcome) + caplog.text
    assert "FILE-SECRET-CANARY" not in public
    assert "URL-SECRET-CANARY" not in public
    assert "FRAGMENT-SECRET-CANARY" not in public
    assert "private" not in public
    assert "resource_missing" in caplog.text
    assert "FileNotFoundError" in caplog.text


def test_runtime_error_stage_and_message_are_not_exposed(caplog) -> None:
    canary = r"C:\private\vault.md api_key=RUNTIME-SECRET-CANARY"
    store = MagicMock()
    store.load.side_effect = PKVRuntimeError(
        ErrorCode.PATH_OUTSIDE_VAULT,
        canary,
        stage=canary,
    )
    caplog.set_level(logging.WARNING, logger="pkv.gui.utils.preview")

    outcome = load_entry_preview_outcome(
        {"file_path": "unsafe.md", "title": "Safe"},
        store,
    )

    assert outcome.status == "degraded"
    assert outcome.issue is not None
    assert outcome.issue.code is ErrorCode.PATH_OUTSIDE_VAULT
    assert outcome.issue.stage == "preview_markdown"
    public = repr(outcome) + caplog.text
    assert "RUNTIME-SECRET-CANARY" not in public
    assert "private" not in public


def test_unsafe_fallback_becomes_explicit_error() -> None:
    store = MagicMock()
    store.load.side_effect = OSError("do not expose")

    outcome = load_entry_preview_outcome(
        {"file_path": "entry.md", "tags": object()},
        store,
    )

    assert outcome.status == "error"
    assert outcome.content == ""
    assert outcome.issue is not None
    assert outcome.issue.code is ErrorCode.RESOURCE_NOT_READABLE
    assert outcome.issue.stage == "preview_summary"


def test_missing_path_is_degraded_not_normal_success() -> None:
    outcome = load_entry_preview_outcome(
        {"title": "No file", "summary_one_sentence": "Summary"},
        MagicMock(),
    )

    assert outcome.status == "degraded"
    assert outcome.issue is not None
    assert outcome.issue.code is ErrorCode.RESOURCE_MISSING


def test_legacy_text_adapter_keeps_chat_compatibility() -> None:
    store = MagicMock()
    store.load.return_value = SimpleNamespace(content="legacy content")

    assert load_entry_preview({"file_path": "entry.md"}, store) == "legacy content"


def test_outcome_rejects_error_with_content() -> None:
    with pytest.raises(ValueError):
        PreviewOutcome(status="error", content="unsafe", issue=None)


def test_adapter_validator_rejects_corrupted_exact_outcome() -> None:
    outcome = PreviewOutcome(status="success", content="valid")
    object.__setattr__(outcome, "content", "")

    assert is_strict_preview_outcome(outcome) is False
