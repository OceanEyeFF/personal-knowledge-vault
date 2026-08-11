"""Executable GUI accessibility contract for installed-Artifact automation."""

from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from PySide6.QtCore import QItemSelectionModel, Qt
from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import QListWidgetItem, QWidget

from src import __version__
from src.ai.provider_factory import ChatProviderSettings
from src.gui.viewmodels.chat_viewmodel import _ChatRequest
from src.utils.config import Config


_PROJECT_ROOT = Path(__file__).resolve().parents[2]

_PUBLIC_AUTOMATION_IDS = frozenset({
    # Main shell.
    "pkv_main_window",
    "pkv_central",
    "pkv_view_stack",
    "nav_panel",
    "nav_list",
    "pkv_status_bar",
    "app_status",
    # Browser.
    "browser_view",
    "browser_splitter",
    "browser_tag_panel",
    "browser_tag_header",
    "browser_tag_status",
    "browser_tag_tree",
    "browser_entry_panel",
    "browser_entry_count",
    "browser_entry_status",
    "browser_entry_table",
    "browser_pagination",
    "browser_prev_page",
    "browser_page_status",
    "browser_next_page",
    "browser_preview_panel",
    "browser_preview_title",
    "browser_preview_status",
    "browser_preview_text",
    "browser_send_to_chat",
    # Search.
    "search_view",
    "search_bar",
    "search_input",
    "search_strategy",
    "search_submit",
    "search_splitter",
    "search_result_panel",
    "search_result_status",
    "search_result_table",
    "search_preview_panel",
    "search_preview_title",
    "search_preview_status",
    "search_preview_text",
    # Archive.
    "archive_view",
    "archive_tabs",
    "archive_url_tab",
    "archive_url_input",
    "archive_url_submit",
    "archive_text_tab",
    "archive_text_title",
    "archive_text_content",
    "archive_text_submit",
    "archive_progress_area",
    "archive_progress_bar",
    "archive_progress_status",
    "archive_result_frame",
    "archive_result_title",
    "archive_result_id",
    "archive_result_path",
    "archive_result_warning",
    "archive_go_browser",
    # Chat.
    "chat_view",
    "chat_splitter",
    "session_sidebar",
    "chat_new_session",
    "session_list",
    "chat_token_panel",
    "chat_token_panel_title",
    "chat_token_total",
    "chat_round_count",
    "chat_token_input",
    "chat_token_output",
    "chat_token_warning",
    "chat_area",
    "chat_messages",
    "chat_request_status",
    "chat_input_area",
    "chat_input",
    "chat_send",
    "chat_stop",
    "autocomplete_popup",
})

_AUTOMATED_LEAF_IDS = frozenset({
    "nav_list",
    "app_status",
    "browser_entry_count",
    "browser_entry_status",
    "browser_entry_table",
    "browser_preview_title",
    "browser_preview_status",
    "browser_preview_text",
    "browser_send_to_chat",
    "search_input",
    "search_strategy",
    "search_submit",
    "search_result_status",
    "search_result_table",
    "search_preview_title",
    "search_preview_status",
    "search_preview_text",
    "archive_tabs",
    "archive_url_input",
    "archive_url_submit",
    "archive_text_title",
    "archive_text_content",
    "archive_text_submit",
    "archive_progress_status",
    "archive_result_title",
    "archive_result_id",
    "archive_result_path",
    "archive_result_warning",
    "archive_go_browser",
    "chat_new_session",
    "session_list",
    "chat_messages",
    "chat_request_status",
    "chat_input",
    "chat_send",
    "chat_stop",
    "chat_round_count",
})


@pytest.fixture
def main_window(qtbot, tmp_path, monkeypatch):
    """Build the real widget tree against synthetic stores and a private root."""

    data_root = tmp_path / "runtime"
    runtime_paths = {
        "DATA_DIR": data_root,
        "DB_PATH": data_root / "db" / "knowledge_vault.db",
        "VAULT_DIR": data_root / "vault",
        "VECTOR_DIR": data_root / "vectors",
        "LOG_DIR": data_root / "logs",
        "TMP_DIR": data_root / "tmp",
    }
    for key, path in runtime_paths.items():
        monkeypatch.setenv(key, str(path))

    store = MagicMock()
    store.get_all_tags_with_count.return_value = []
    store.list_entries.return_value = []
    store.count_entries.return_value = 0
    store.get_statistics.return_value = {
        "total_entries": 0,
        "by_source_type": [],
        "top_tags": [],
    }
    isolated_config = Config(str(_PROJECT_ROOT / "config" / "config.yaml"))

    with (
        patch("src.gui.stores.get_sqlite_store", return_value=store),
        patch("src.gui.stores.get_markdown_store", return_value=MagicMock()),
        patch("src.gui.stores.get_bm25_retriever", return_value=MagicMock()),
        patch(
            "src.gui.viewmodels.settings_viewmodel.get_config",
            return_value=isolated_config,
        ),
        patch(
            "src.gui.viewmodels.chat_viewmodel.Config",
            return_value=isolated_config,
        ),
        patch("src.gui.main_window.get_config", return_value=isolated_config),
        patch("src.gui.main_window.LoggerSetup.add_file_handler"),
        patch("src.utils.config.get_config", return_value=isolated_config),
    ):
        from src.gui.main_window import MainWindow

        window = MainWindow()
        qtbot.addWidget(window)
        yield window


def _widget_by_id(window, automation_id: str) -> QWidget:
    matches = [
        widget
        for widget in (window, *window.findChildren(QWidget))
        if widget.objectName() == automation_id
    ]
    assert len(matches) == 1, automation_id
    return matches[0]


def _chat_request(session_id: str, request_id: str) -> _ChatRequest:
    """Build the real ViewModel request state used by scoped Qt signals."""

    settings = ChatProviderSettings(
        provider="openai_compatible",
        api_key="synthetic-key",
        base_url="http://127.0.0.1:1/v1",
        model="synthetic-model",
        max_tokens=64,
        temperature=0.0,
        timeout_seconds=1.0,
        max_retries=0,
    )
    return _ChatRequest(
        request_id=request_id,
        session_id=session_id,
        user_message="synthetic request",
        settings=settings,
        pre_messages=(),
        request_messages=(),
        pre_total_tokens=0,
        pre_round_count=0,
    )


def test_public_automation_ids_are_complete_unique_and_native(main_window) -> None:
    widgets = (main_window, *main_window.findChildren(QWidget))
    counts = Counter(
        widget.objectName()
        for widget in widgets
        if widget.objectName() in _PUBLIC_AUTOMATION_IDS
    )
    assert set(counts) == _PUBLIC_AUTOMATION_IDS
    assert all(count == 1 for count in counts.values())

    for automation_id in _PUBLIC_AUTOMATION_IDS:
        widget = _widget_by_id(main_window, automation_id)
        accessible_identifier = getattr(widget, "accessibleIdentifier", None)
        if callable(accessible_identifier):
            assert accessible_identifier() == automation_id

    assert not {
        "btn_send_to_chat",
        "btn_stop",
        "message_display",
    } & {widget.objectName() for widget in widgets}


def test_release_qt_lock_supports_native_accessible_identifiers() -> None:
    lock_path = (
        _PROJECT_ROOT
        / "packaging"
        / "locks"
        / "release-environment.v2.json"
    )
    release_lock = json.loads(lock_path.read_text(encoding="utf-8"))
    pyside = next(
        distribution
        for distribution in release_lock["distributions"]
        if distribution["name"].casefold() == "pyside6"
    )
    version = tuple(int(part) for part in pyside["version"].split("."))

    assert pyside["version"] == "6.11.1"
    assert version >= (6, 9)
    assert callable(getattr(QWidget, "setAccessibleIdentifier", None))
    assert callable(getattr(QWidget, "accessibleIdentifier", None))


def test_automated_leaf_ancestors_have_continuous_object_names(main_window) -> None:
    for automation_id in _AUTOMATED_LEAF_IDS:
        widget = _widget_by_id(main_window, automation_id)
        current = widget
        chain = []
        while current is not main_window:
            chain.append((type(current).__name__, current.objectName()))
            assert current.objectName(), (automation_id, chain)
            parent = current.parentWidget()
            assert parent is not None, (automation_id, chain)
            current = parent
        assert main_window.objectName() == "pkv_main_window"


def test_dynamic_status_labels_do_not_mask_their_visible_text(main_window) -> None:
    for automation_id in (
        "app_status",
        "search_result_status",
        "archive_result_title",
        "archive_result_warning",
        "chat_request_status",
        "chat_round_count",
    ):
        assert _widget_by_id(main_window, automation_id).accessibleName() == ""


def test_navigation_items_have_stable_accessible_names(main_window) -> None:
    nav = _widget_by_id(main_window, "nav_list")
    expected = ("浏览", "搜索", "归档", "对话", "统计", "设置")
    observed = tuple(
        nav.item(row).data(Qt.ItemDataRole.AccessibleTextRole)
        for row in range(nav.count())
    )
    assert observed == expected
    assert len(set(observed)) == len(observed)


def test_browser_and_search_selection_only_syncs_current_detail_row(
    main_window,
    qtbot,
) -> None:
    entries = [
        {
            "knowledge_id": 1,
            "title": "Selection source",
            "source_type": "text",
            "tags": "w4",
            "word_count": 2,
            "archived_at": "2026-08-10T00:00:00",
            "file_path": "synthetic-source.md",
        },
        {
            "knowledge_id": 2,
            "title": "Selection target",
            "source_type": "text",
            "tags": "w4",
            "word_count": 2,
            "archived_at": "2026-08-10T00:00:00",
            "file_path": "synthetic-target.md",
        },
    ]

    browser_table = _widget_by_id(main_window, "browser_entry_table")
    browser_model = browser_table.model()
    assert browser_model is not None
    browser_model.update_entries(entries)
    browser_selection = browser_table.selectionModel()
    assert browser_selection is not None
    browser_selection.setCurrentIndex(
        browser_model.index(0, 0),
        QItemSelectionModel.SelectionFlag.NoUpdate,
    )
    browser_table.clearSelection()
    browser_selection.select(
        browser_model.index(1, 0),
        QItemSelectionModel.SelectionFlag.Select,
    )

    qtbot.waitUntil(lambda: browser_table.currentIndex().row() == 1)
    assert _widget_by_id(main_window, "browser_preview_title").text() == (
        "预览: Selection target"
    )

    search_table = _widget_by_id(main_window, "search_result_table")
    search_model = search_table.model()
    assert search_model is not None
    search_model.update_entries(entries)
    search_selection = search_table.selectionModel()
    assert search_selection is not None
    search_selection.setCurrentIndex(
        search_model.index(0, 0),
        QItemSelectionModel.SelectionFlag.NoUpdate,
    )
    search_table.clearSelection()
    search_selection.select(
        search_model.index(1, 0),
        QItemSelectionModel.SelectionFlag.Select,
    )

    qtbot.waitUntil(lambda: search_table.currentIndex().row() == 1)
    assert _widget_by_id(main_window, "search_preview_title").text() == (
        "详情: Selection target"
    )


def test_browser_projection_reset_clears_stale_selection(main_window) -> None:
    browser = main_window._browser_view
    entry = {
        "knowledge_id": 1,
        "title": "Stale selection",
        "source_type": "text",
        "tags": "w4",
        "word_count": 2,
        "archived_at": "2026-08-10T00:00:00",
        "file_path": "synthetic.md",
    }
    browser._entry_model.update_entries([entry])

    with patch.object(browser, "_load_preview", return_value=True):
        browser._entry_view.setCurrentIndex(browser._entry_model.index(0, 0))
    assert browser._selected_entry is entry
    assert browser._send_to_chat_btn.isEnabled()

    browser._entry_model.update_entries([])

    assert browser._selected_entry is None
    assert browser._selected_content == ""
    assert not browser._send_to_chat_btn.isEnabled()
    assert browser._preview_title.text() == "预览"


def test_chat_session_selection_works_without_mouse_click(main_window) -> None:
    session_list = _widget_by_id(main_window, "session_list")
    item = QListWidgetItem("Synthetic session")
    item.setData(Qt.ItemDataRole.UserRole, "session-w4")
    observed = []
    main_window._chat_view.sidebar.session_selected.connect(observed.append)

    session_list.addItem(item)
    session_list.setCurrentItem(item)

    assert observed == ["session-w4"]


def test_chat_terminal_for_origin_session_does_not_leak_after_switch(
    main_window,
) -> None:
    chat = main_window._chat_view
    vm = chat.viewmodel
    status = chat.chat_area.request_status
    assert status.text() == "就绪"
    assert not status.isHidden()

    started = QSignalSpy(vm.chat_request_started)
    completed = QSignalSpy(vm.chat_request_completed)
    vm.current_session_id = "session-a"
    vm._active_request = _chat_request("session-a", "request-a")
    vm.chat_request_started.emit("session-a", "request-a")

    assert started.count() == 1
    assert status.text() == "请求中"
    assert status.property("requestStatus") == "running"

    vm.current_session_id = "session-b"
    chat._sync_request_status()
    assert status.text() == "请求中（其他会话）"
    assert status.property("requestStatus") == "running_other"

    vm._active_request = None
    with patch.object(chat.chat_area, "finish_assistant_message") as finish:
        vm.chat_request_completed.emit("session-a", "request-a")

    assert completed.count() == 1
    finish.assert_not_called()
    assert status.text() == "就绪"
    assert status.property("requestStatus") == "idle"


def test_chat_stale_terminal_signal_preserves_active_request_status(
    main_window,
) -> None:
    chat = main_window._chat_view
    vm = chat.viewmodel
    status = chat.chat_area.request_status
    completed = QSignalSpy(vm.chat_request_completed)

    vm.current_session_id = "session-a"
    vm._active_request = _chat_request("session-a", "request-new")
    vm.chat_request_started.emit("session-a", "request-new")
    vm.chat_request_completed.emit("session-a", "request-stale")

    assert completed.count() == 1
    assert chat._active_ui_request == ("session-a", "request-new")
    assert status.text() == "请求中"
    assert status.property("requestStatus") == "running"


def test_chat_busy_and_config_rejections_have_stable_visible_status(
    main_window,
) -> None:
    chat = main_window._chat_view
    vm = chat.viewmodel
    status = chat.chat_area.request_status
    rejected = QSignalSpy(vm.chat_request_rejected)

    vm.current_session_id = "session-a"
    vm._active_request = _chat_request("session-a", "request-active")
    vm.chat_request_started.emit("session-a", "request-active")
    vm._latest_attempt_id = "attempt-busy"
    vm.chat_request_rejected.emit(
        "session-a",
        "attempt-busy",
        "chat_busy",
        "another request is active",
    )

    assert rejected.count() == 1
    assert status.text() == "请求中"
    assert status.property("requestStatus") == "running"

    vm._active_request = None
    vm._latest_attempt_id = "attempt-config"
    vm.chat_request_rejected.emit(
        "session-a",
        "attempt-config",
        "provider_config_invalid",
        "provider configuration is invalid",
    )

    assert rejected.count() == 2
    assert status.text() == "未发送（错误代码：provider_config_invalid）"
    assert status.property("requestStatus") == "rejected"


def test_chat_round_count_is_persistent(main_window) -> None:
    chat = main_window._chat_view

    chat.sidebar.token_panel.update_stats(4, 5, 9, 1)
    assert chat.sidebar.token_panel.round_label.text() == "轮数: 1 / 3"


def test_qss_selectors_follow_unique_chat_and_browser_ids() -> None:
    old_selectors = ("#btn_send_to_chat", "#btn_stop", "#message_display")
    required_selectors = (
        "#browser_send_to_chat",
        "#chat_send",
        "#chat_stop",
        "#chat_messages",
        "#chat_token_panel",
        "#chat_token_panel_title",
        "#chat_token_warning",
    )
    for theme in ("light", "dark"):
        qss = (
            _PROJECT_ROOT / "src" / "gui" / "styles" / f"{theme}.qss"
        ).read_text(encoding="utf-8")
        assert all(selector in qss for selector in required_selectors)
        assert all(selector not in qss for selector in old_selectors)


def test_gui_version_reexports_root_version_and_about_uses_it(main_window) -> None:
    from src.gui import __version__ as gui_version

    assert gui_version == __version__
    with patch("PySide6.QtWidgets.QMessageBox.about") as about:
        main_window._show_about()
    assert f"版本: v{__version__}" in about.call_args.args[2]
