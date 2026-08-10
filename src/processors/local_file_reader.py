"""Descriptor-verified reads for explicit local-file processor seams."""

from __future__ import annotations

from pathlib import Path

from src.runtime.layout import open_user_file_nofollow


def read_local_text_file(
    file_path: str | Path,
    *,
    errors: str | None = None,
) -> str:
    """Read one UTF-8 regular file without following links or path swaps."""

    with open_user_file_nofollow(
        Path(file_path),
        "r",
        label="本地导入源文件",
        encoding="utf-8",
        errors=errors,
    ) as handle:
        return handle.read()
