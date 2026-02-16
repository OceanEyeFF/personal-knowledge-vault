"""
Terminal UI helpers built on Rich.

This module keeps rendering logic in one place so CLI commands can focus on
data preparation and workflow orchestration.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional, Sequence, Tuple, Union

from rich.console import Console
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)
from rich.prompt import Confirm
from rich.table import Table

HeaderSpec = Union[str, Sequence[str], Mapping[str, Any]]
RowSpec = Union[Sequence[Any], Mapping[str, Any]]

_CONSOLE = Console()


def _parse_header(header: HeaderSpec) -> Tuple[str, Optional[str]]:
    """Normalize a header spec into (title, style)."""
    if isinstance(header, Mapping):
        title = (
            header.get("title")
            or header.get("header")
            or header.get("name")
            or header.get("text")
            or ""
        )
        style = header.get("style") or header.get("color")
        return str(title), str(style) if style else None

    if isinstance(header, (list, tuple)):
        if not header:
            return "", None
        title = header[0]
        style = header[1] if len(header) > 1 else None
        return str(title), str(style) if style else None

    return str(header), None


def _is_numeric(value: Any) -> bool:
    """Best-effort numeric detector for alignment decisions."""
    if value is None:
        return False
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return True
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return False
        text = text.replace(",", "")
        if text and text[0] == "$":
            text = text[1:]
        if text.endswith("%"):
            text = text[:-1]
        try:
            float(text)
            return True
        except ValueError:
            return False
    return False


def _infer_justification(values: Sequence[Any]) -> str:
    """Infer column justification based on data type."""
    meaningful = [value for value in values if value not in ("", None)]
    if not meaningful:
        return "left"
    if all(_is_numeric(value) for value in meaningful):
        return "right"
    return "left"


def _normalize_row(row: RowSpec, headers: Sequence[str]) -> Sequence[Any]:
    """Align a row with headers and pad as needed."""
    if isinstance(row, Mapping):
        return [row.get(name, "") for name in headers]

    if isinstance(row, (str, bytes)):
        return [row]

    try:
        values = list(row)
    except TypeError:
        return [row]

    if len(values) < len(headers):
        values.extend([""] * (len(headers) - len(values)))
    return values[: len(headers)]


def _build_progress(total: Optional[int]) -> Progress:
    columns = [
        SpinnerColumn(style="cyan"),
        TextColumn("{task.description}", justify="left"),
    ]
    if total is None:
        columns.append(TimeElapsedColumn())
    else:
        columns.extend(
            [
                BarColumn(bar_width=None),
                TaskProgressColumn(),
                TimeRemainingColumn(),
            ]
        )
    return Progress(*columns, console=_CONSOLE, transient=True)


@dataclass
class ProgressHandle:
    """A small wrapper that keeps a Progress instance and task id together."""

    progress: Progress
    task_id: int

    def __enter__(self) -> "ProgressHandle":
        self.progress.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb) -> Optional[bool]:
        return self.progress.__exit__(exc_type, exc, tb)

    def start(self) -> None:
        self.progress.start()

    def stop(self) -> None:
        self.progress.stop()

    def advance(self, step: float = 1) -> None:
        self.progress.update(self.task_id, advance=step)

    def update(self, **kwargs: Any) -> None:
        self.progress.update(self.task_id, **kwargs)


def show_progress(description: str, total: Optional[int] = None) -> ProgressHandle:
    """
    Create a progress handle that can be used as a context manager.

    Example:
        with show_progress("Processing", total=10) as handle:
            handle.advance()
    """
    progress = _build_progress(total)
    task_id = progress.add_task(description, total=total)
    return ProgressHandle(progress=progress, task_id=task_id)


def format_table(
    headers: Sequence[HeaderSpec],
    rows: Iterable[RowSpec],
    title: Optional[str] = None,
) -> Table:
    """Format data into a Rich Table with simple alignment heuristics."""
    header_specs = [_parse_header(header) for header in headers]
    header_names = [name for name, _ in header_specs]

    normalized_rows = [_normalize_row(row, header_names) for row in rows]
    column_values = list(zip(*normalized_rows)) if normalized_rows else []

    table = Table(title=title)
    for index, (name, style) in enumerate(header_specs):
        values = column_values[index] if column_values else []
        justify = _infer_justification(values)
        table.add_column(name, style=style, justify=justify)

    for row in normalized_rows:
        display_row = ["" if value is None else str(value) for value in row]
        table.add_row(*display_row)

    return table


def show_panel(title: str, content: Any, style: Optional[Any] = None) -> Panel:
    """
    Render a panel with optional style configuration.

    The style argument can be:
      - string: panel style
      - tuple/list: (panel_style, border_style)
      - mapping: {"style": "...", "border_style": "..."}
    """
    panel_style: Optional[str] = None
    border_style: Optional[str] = None

    if isinstance(style, Mapping):
        panel_style = style.get("style") or style.get("color")
        border_style = style.get("border_style") or style.get("border")
    elif isinstance(style, (tuple, list)):
        if style:
            panel_style = style[0]
        if len(style) > 1:
            border_style = style[1]
    elif isinstance(style, str):
        panel_style = style

    panel = Panel(content, title=title, style=panel_style, border_style=border_style)
    _CONSOLE.print(panel)
    return panel


def confirm_action(message: str) -> bool:
    """Prompt the user for confirmation and return True/False."""
    return Confirm.ask(message, console=_CONSOLE)


__all__ = [
    "ProgressHandle",
    "show_progress",
    "format_table",
    "show_panel",
    "confirm_action",
]
