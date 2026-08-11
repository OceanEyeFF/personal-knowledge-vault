"""Stable accessibility identifiers for installed-Artifact automation.

The identifiers are part of the public desktop surface.  ``objectName`` keeps
the contract available on every supported Qt 6 release, while Qt 6.9+
``accessibleIdentifier`` exposes the same leaf identifier directly to native
accessibility bridges.
"""

from __future__ import annotations

import re

from PySide6.QtWidgets import QWidget


_AUTOMATION_ID = re.compile(r"[a-z][a-z0-9_]{0,63}")


def set_automation_id(widget: QWidget, automation_id: str) -> None:
    """Assign one validated, stable identifier to a GUI widget.

    ``setAccessibleIdentifier`` was added in Qt 6.9, so releases using an
    earlier supported Qt keep the ``objectName`` contract without pretending
    that the newer native bridge API exists.
    """

    if _AUTOMATION_ID.fullmatch(automation_id) is None:
        raise ValueError(f"Invalid GUI automation identifier: {automation_id!r}")
    widget.setObjectName(automation_id)
    set_accessible_identifier = getattr(
        widget,
        "setAccessibleIdentifier",
        None,
    )
    if callable(set_accessible_identifier):
        set_accessible_identifier(automation_id)


__all__ = ["set_automation_id"]
