"""Deterministic W2 oracles for Chat rich-text and URL-log boundaries."""

from __future__ import annotations

import logging
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import QTextBrowser

from src.gui.utils.knowledge_ref import (
    KnowledgeReference,
    format_reference_card_html,
)
from src.gui.views.chat_view import (
    ChatView,
    SafeMessageBrowser,
    render_markdown,
)
from src.gui.viewmodels.chat_viewmodel import ChatViewModel
from src.storage.sqlite_store import SQLiteStore


_HTTP_CANARY = "https://127.0.0.1:9/pixel?token=RICH_TEXT_SECRET"
_FILE_CANARY = "file:///etc/passwd"


def test_raw_html_is_literal_in_markdown_and_reference_cards(qtbot) -> None:
    raw = (
        f'<img src="{_HTTP_CANARY}">'
        f'<a href="{_FILE_CANARY}">file</a>'
        "<style>body{display:none}</style>"
    )
    rendered = render_markdown(raw, role="assistant")
    card = format_reference_card_html(
        KnowledgeReference(
            knowledge_id=7,
            title=raw,
            source_type='<img src="file:///etc/shadow">',
            summary=raw,
            token_count=3,
        )
    )

    assert '<img src="https://127.0.0.1' not in rendered
    assert f'<a href="{_FILE_CANARY}"' not in rendered
    assert "&lt;img" in rendered
    assert '<img src="file:///etc/shadow">' not in card
    assert "&lt;img" in card

    browser = SafeMessageBrowser()
    qtbot.addWidget(browser)
    with patch.object(QTextBrowser, "loadResource", autospec=True) as base_load:
        browser.append(rendered)
        browser.append(card)
        qtbot.wait(10)

    base_load.assert_not_called()
    document_html = browser.document().toHtml().lower()
    assert "<img" not in document_html
    assert f'<a href="{_FILE_CANARY}"' not in document_html


def test_message_browser_blocks_resources_and_gates_clicked_schemes(qtbot) -> None:
    browser = SafeMessageBrowser()
    qtbot.addWidget(browser)

    with patch.object(QTextBrowser, "loadResource", autospec=True) as base_load:
        assert browser.loadResource(2, QUrl(_HTTP_CANARY)) is None
        assert browser.loadResource(2, QUrl(_FILE_CANARY)) is None
        assert browser.loadResource(2, QUrl("qrc:/secret")) is None
    base_load.assert_not_called()

    with patch(
        "src.gui.views.chat_view.QDesktopServices.openUrl",
        return_value=True,
    ) as open_url:
        browser._open_allowed_link(QUrl(_FILE_CANARY))
        browser._open_allowed_link(QUrl("qrc:/secret"))
        browser._open_allowed_link(QUrl("javascript:alert(1)"))
        open_url.assert_not_called()

        allowed = QUrl("https://docs.example/guide")
        browser._open_allowed_link(allowed)
        open_url.assert_called_once_with(allowed)


def test_url_status_uses_origin_only_and_escapes_all_status_scalars(
    qtbot,
    caplog,
) -> None:
    browser = SafeMessageBrowser()
    qtbot.addWidget(browser)
    view = SimpleNamespace(
        _pending_url_archives=set(),
        viewmodel=SimpleNamespace(current_session_id="session-a"),
        chat_area=SimpleNamespace(message_display=browser),
    )
    url = (
        "https://user:password@example.com/private/path"
        "?api_key=URL_QUERY_SECRET&token=SECOND_SECRET"
    )

    with caplog.at_level(logging.INFO):
        ChatView._on_url_archive_started(
            view,
            "session-a",
            "operation-a",
            url,
        )
        ChatView._on_url_archive_warning(
            view,
            "session-a",
            "operation-a",
            url,
            f'<img src="{_HTTP_CANARY}">',
        )

    combined = browser.toPlainText() + caplog.text
    assert "URL_QUERY_SECRET" not in combined
    assert "SECOND_SECRET" not in combined
    assert "password" not in combined
    assert "<img" in browser.toPlainText()
    assert "<img" not in browser.document().toHtml().lower()


@pytest.mark.asyncio
async def test_url_archive_viewmodel_logs_never_include_path_or_credentials(
    qtbot,
    caplog,
) -> None:
    url = (
        "https://user:password@example.com/private/path"
        "?api_key=VM_URL_SECRET&token=VM_SECOND_SECRET"
    )
    config = SimpleNamespace(
        db_path=":memory:",
        llm_api_key="unused",
        llm_base_url="https://chat.example/v1",
        llm_model="unused",
        llm_max_tokens=1,
    )
    store = SimpleNamespace(
        query_by_url=lambda candidate: {
            "knowledge_id": 1,
            "title": "existing",
        }
    )
    vm = ChatViewModel(
        config=config,
        store=store,
        provider_factory=lambda settings: None,
    )

    with caplog.at_level(logging.INFO, logger="pkv.gui.viewmodels.chat"):
        await vm.archive_url_and_inject.__wrapped__(
            vm,
            url,
            "session-a",
            "operation-a",
        )

    assert "VM_URL_SECRET" not in caplog.text
    assert "VM_SECOND_SECRET" not in caplog.text
    assert "password" not in caplog.text
    assert "/private/path" not in caplog.text


def test_untrusted_session_title_cannot_forge_multiline_logs(qtbot, caplog) -> None:
    title = "normal-title\r\nFORGED_LOG_LINE title_secret"
    config = SimpleNamespace(
        db_path=":memory:",
        llm_api_key="unused",
        llm_base_url="https://chat.example/v1",
        llm_model="unused",
        llm_max_tokens=1,
    )
    vm_store = SimpleNamespace(create_session=lambda session_id, value: None)
    vm = ChatViewModel(
        config=config,
        store=vm_store,
        provider_factory=lambda settings: None,
    )

    sqlite_store = SQLiteStore.__new__(SQLiteStore)
    connection = MagicMock()
    sqlite_store.get_connection = MagicMock(
        return_value=nullcontext(connection)
    )

    with caplog.at_level(logging.INFO):
        vm.create_new_session(title=title)
        sqlite_store.create_session("fixture-session", title)

    assert "FORGED_LOG_LINE" not in caplog.text
    assert "title_secret" not in caplog.text
