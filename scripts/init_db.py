"""
Retired raw SQLite initialization entrypoint.

Current initialization is a confirmed runtime lifecycle action.  Running this
script must not create a configured data root or schema.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts._legacy_maintenance import reject_legacy_maintenance_entrypoint


def main() -> int:
    """Reject the retired raw initializer before Config() is constructed."""

    return reject_legacy_maintenance_entrypoint("scripts/init_db.py")


if __name__ == "__main__":
    raise SystemExit(main())
