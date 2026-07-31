"""Path guards for the offline evaluation harness."""

from __future__ import annotations

import os
from pathlib import Path
import stat
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

    candidate_lexical = Path(os.path.abspath(path))
    root_values = list(allowed_roots)
    roots_lexical = [Path(os.path.abspath(root)) for root in root_values]
    matching_roots = [
        root for root in roots_lexical if _is_within(candidate_lexical, root)
    ]
    if not matching_roots:
        # Reject external/UNC/config candidates before resolve() can probe them.
        production_lexical = Path(os.path.abspath(PRODUCTION_ROOT))
        if _is_within(candidate_lexical, production_lexical):
            raise RuntimeError(f"{purpose}不得位于生产 .data: {candidate_lexical}")
        raise RuntimeError(f"{purpose}只能位于显式隔离目录")

    lexical_root = max(matching_roots, key=lambda item: len(item.parts))
    _reject_unsafe_links_between(
        lexical_root,
        candidate_lexical,
        purpose=purpose,
    )
    candidate = reject_production_path(candidate_lexical, purpose=purpose)
    root = reject_production_path(
        lexical_root,
        purpose=f"{purpose}允许根目录",
    )
    if _is_within(candidate, root):
        return candidate
    raise RuntimeError(f"{purpose}只能位于显式隔离目录")


def _reject_unsafe_links_between(
    root: Path,
    candidate: Path,
    *,
    purpose: str,
) -> None:
    """Walk with lstat so an accepted lexical path cannot redirect resolve/read."""

    relative = candidate.relative_to(root)
    paths = [root]
    for index in range(1, len(relative.parts) + 1):
        paths.append(root / Path(*relative.parts[:index]))
    for index, current in enumerate(paths):
        try:
            path_stat = os.lstat(current)
        except (FileNotFoundError, NotADirectoryError):
            return
        except OSError as exc:
            raise RuntimeError(f"{purpose}路径不可安全检查: {current}") from exc
        file_attributes = getattr(path_stat, "st_file_attributes", 0)
        if stat.S_ISLNK(path_stat.st_mode) or bool(file_attributes & 0x400):
            raise RuntimeError(f"{purpose}路径不得包含 symlink/junction: {current}")
        is_leaf = index == len(paths) - 1
        if not is_leaf and not stat.S_ISDIR(path_stat.st_mode):
            raise RuntimeError(f"{purpose}路径链包含非目录节点: {current}")
        if is_leaf and stat.S_ISREG(path_stat.st_mode) and path_stat.st_nlink > 1:
            raise RuntimeError(f"{purpose}路径不得使用硬链接文件: {current}")


def _is_within(candidate: Path, root: Path) -> bool:
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True
