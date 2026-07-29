"""Path guards for the offline evaluation harness."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable


PROJECT_ROOT = Path(__file__).resolve().parents[2]
PRODUCTION_ROOT = PROJECT_ROOT / ".data"


def reject_production_path(path: Path, *, purpose: str) -> Path:
    """Reject production `.data` before a caller reads, creates, or writes."""

    candidate = Path(os.path.abspath(path))
    production = Path(os.path.abspath(PRODUCTION_ROOT))
    if _is_within(candidate, production):
        raise RuntimeError(f"{purpose}不得位于生产 .data: {candidate}")

    # Resolve existing symlink/reparse parents as a second line of defense.
    candidate_resolved = candidate.resolve(strict=False)
    production_resolved = production.resolve(strict=False)
    if _is_within(candidate_resolved, production_resolved):
        raise RuntimeError(f"{purpose}不得位于生产 .data: {candidate_resolved}")
    return candidate_resolved


def require_path_within(
    path: Path,
    *,
    allowed_roots: Iterable[Path],
    purpose: str,
) -> Path:
    """Require a path to remain under one of the explicit isolated roots."""

    candidate = reject_production_path(path, purpose=purpose)
    roots = [
        reject_production_path(root, purpose=f"{purpose}允许根目录")
        for root in allowed_roots
    ]
    for root in roots:
        if _is_within(candidate, root):
            return candidate
    raise RuntimeError(f"{purpose}只能位于显式隔离目录")


def _is_within(candidate: Path, root: Path) -> bool:
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True
