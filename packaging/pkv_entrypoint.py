"""Single frozen dispatcher for the three published PKV executables."""

from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
import sys
from typing import Any


_ENTRYPOINTS = {
    "pkv": ("src.main", "main"),
    "pkv-gui": ("src.gui.app", "main"),
    "pkv-mcp": ("src.mcp.server", "main"),
}
_UNKNOWN_ENTRYPOINT_EXIT = 64


def _entrypoint_name(executable: str | os.PathLike[str] | None = None) -> str:
    path = Path(sys.executable if executable is None else executable)
    return path.stem.casefold()


def dispatch(executable: str | os.PathLike[str] | None = None) -> int:
    """Dispatch solely by the frozen executable name.

    No test-only flag is published: each executable has one immutable product
    role, and all user arguments remain available to that role's normal parser.
    """

    name = _entrypoint_name(executable)
    target = _ENTRYPOINTS.get(name)
    if target is None:
        sys.stderr.write(
            json.dumps(
                {
                    "code": "entrypoint_unknown",
                    "recoverable": False,
                    "stage": "entrypoint_dispatch",
                    "status": "error",
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        )
        return _UNKNOWN_ENTRYPOINT_EXIT

    module_name, function_name = target
    if name == "pkv-gui":
        # This must happen before src.gui.app imports MainWindow/qasync.
        os.environ["QT_API"] = "pyside6"
    module = importlib.import_module(module_name)
    result: Any = getattr(module, function_name)()
    return result if type(result) is int else 0


if __name__ == "__main__":
    raise SystemExit(dispatch())
