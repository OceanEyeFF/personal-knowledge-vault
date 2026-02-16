"""
CLI UI unit tests.
"""

import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple
from unittest.mock import Mock, patch

import pytest

# Add project root to Python path
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from src.cli import ui


class DummyProgress:
    """Lightweight progress stub for show_progress tests."""

    def __init__(self) -> None:
        self.tasks: List[Dict[str, Any]] = []
        self.updated: List[Tuple[int, Dict[str, Any]]] = []
        self.entered = False
        self.exited = False

    def add_task(self, description: str, total: Any = None) -> int:
        self.tasks.append({"description": description, "total": total})
        return 7

    def update(self, task_id: int, **kwargs: Any) -> None:
        self.updated.append((task_id, kwargs))

    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None

    def __enter__(self) -> "DummyProgress":
        self.entered = True
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.exited = True
        return None


class DummyTable:
    """Minimal Table stub to capture columns and rows."""

    def __init__(self, title: str | None = None) -> None:
        self.title = title
        self.columns: List[Dict[str, Any]] = []
        self.rows: List[Tuple[str, ...]] = []

    def add_column(self, name: str, style: Any = None, justify: str = "left") -> None:
        self.columns.append({"name": name, "style": style, "justify": justify})

    def add_row(self, *values: str) -> None:
        self.rows.append(values)


def test_show_progress_creates_handle_and_task() -> None:
    dummy_progress = DummyProgress()

    with patch("src.cli.ui._build_progress", return_value=dummy_progress) as mock_build:
        handle = ui.show_progress("Loading", total=5)

    mock_build.assert_called_once_with(5)
    assert handle.progress is dummy_progress
    assert handle.task_id == 7
    assert dummy_progress.tasks == [{"description": "Loading", "total": 5}]


def test_show_progress_handle_delegates_updates() -> None:
    dummy_progress = DummyProgress()

    with patch("src.cli.ui._build_progress", return_value=dummy_progress):
        handle = ui.show_progress("Working", total=None)

    handle.advance(3)
    handle.update(completed=10)

    assert dummy_progress.updated == [
        (7, {"advance": 3}),
        (7, {"completed": 10}),
    ]


def test_show_progress_context_manager_uses_progress() -> None:
    dummy_progress = DummyProgress()

    with patch("src.cli.ui._build_progress", return_value=dummy_progress):
        handle = ui.show_progress("Context", total=1)

    with handle:
        pass

    assert dummy_progress.entered is True
    assert dummy_progress.exited is True


def test_format_table_builds_columns_rows_and_alignment() -> None:
    headers = ["Name", ("Score", "green")]
    rows = [
        {"Name": "Alice", "Score": "100"},
        {"Score": 50, "Name": "Bob"},
        {"Name": "Cara", "Score": None},
    ]

    with patch("src.cli.ui.Table", DummyTable):
        table = ui.format_table(headers, rows, title="Scores")

    assert isinstance(table, DummyTable)
    assert table.title == "Scores"
    assert table.columns == [
        {"name": "Name", "style": None, "justify": "left"},
        {"name": "Score", "style": "green", "justify": "right"},
    ]
    assert table.rows == [
        ("Alice", "100"),
        ("Bob", "50"),
        ("Cara", ""),
    ]


def test_format_table_supports_mapping_headers_and_padding() -> None:
    headers = [
        {"title": "Item", "style": "bold"},
        {"header": "Qty", "color": "yellow"},
    ]
    rows = [
        ["Tea"],
        {"Item": "Coffee", "Qty": 2},
    ]

    with patch("src.cli.ui.Table", DummyTable):
        table = ui.format_table(headers, rows)

    assert table.columns == [
        {"name": "Item", "style": "bold", "justify": "left"},
        {"name": "Qty", "style": "yellow", "justify": "right"},
    ]
    assert table.rows == [
        ("Tea", ""),
        ("Coffee", "2"),
    ]


def test_format_table_empty_rows_defaults_left() -> None:
    headers = ["Only", {"title": "Count"}]
    rows: List[List[str]] = []

    with patch("src.cli.ui.Table", DummyTable):
        table = ui.format_table(headers, rows)

    assert table.columns == [
        {"name": "Only", "style": None, "justify": "left"},
        {"name": "Count", "style": None, "justify": "left"},
    ]
    assert table.rows == []


@pytest.mark.parametrize(
    "style, expected_style, expected_border",
    [
        ("cyan", "cyan", None),
        (("green", "blue"), "green", "blue"),
        ({"style": "red", "border_style": "yellow"}, "red", "yellow"),
        ({"color": "magenta", "border": "white"}, "magenta", "white"),
    ],
)
def test_show_panel_prints_panel_and_returns_it(
    style: Any,
    expected_style: Any,
    expected_border: Any,
) -> None:
    mock_console = Mock()
    sentinel_panel = object()

    with patch("src.cli.ui.Panel", return_value=sentinel_panel) as mock_panel:
        with patch("src.cli.ui._CONSOLE", mock_console):
            result = ui.show_panel("Title", "Content", style=style)

    mock_panel.assert_called_once_with(
        "Content",
        title="Title",
        style=expected_style,
        border_style=expected_border,
    )
    mock_console.print.assert_called_once_with(sentinel_panel)
    assert result is sentinel_panel


def test_confirm_action_delegates_to_rich_confirm() -> None:
    mock_console = Mock()

    with patch("src.cli.ui._CONSOLE", mock_console):
        with patch("src.cli.ui.Confirm.ask", return_value=True) as mock_ask:
            result = ui.confirm_action("Proceed?")

    mock_ask.assert_called_once_with("Proceed?", console=mock_console)
    assert result is True
