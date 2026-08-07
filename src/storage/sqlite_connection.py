"""Fail-closed connections for an already bootstrapped SQLite database."""

from __future__ import annotations

import errno
import os
import sqlite3
import stat
from pathlib import Path

from src.runtime.errors import ErrorCode, PKVRuntimeError
from src.runtime.layout import validate_path_components


_SQLITE_HEADER = b"SQLite format 3\x00"
_REPARSE_FLAG = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


def _lstat(path: Path) -> os.stat_result:
    try:
        return os.lstat(path)
    except FileNotFoundError as exc:
        raise PKVRuntimeError(
            ErrorCode.DATABASE_MISSING,
            f"数据库不存在: {path}",
        ) from exc
    except OSError as exc:
        raise PKVRuntimeError(
            ErrorCode.PATH_STATE_UNDETERMINED,
            f"无法判定数据库文件状态: {path}",
        ) from exc


def _is_link_or_reparse(info: os.stat_result) -> bool:
    attributes = getattr(info, "st_file_attributes", 0)
    return stat.S_ISLNK(info.st_mode) or bool(attributes & _REPARSE_FLAG)


def _assert_regular_identity(path: Path, info: os.stat_result) -> None:
    """Reject links, reparse points and hard links; require a regular file."""

    if _is_link_or_reparse(info) or info.st_nlink > 1:
        raise PKVRuntimeError(
            ErrorCode.DATA_ROOT_UNSAFE,
            f"数据库不得是链接、reparse point 或硬链接: {path}",
        )
    if not stat.S_ISREG(info.st_mode):
        raise PKVRuntimeError(
            ErrorCode.DATABASE_NOT_SQLITE,
            f"数据库路径不是普通文件: {path}",
        )


def _open_readonly_no_follow(path: Path) -> int:
    """Open without following a final-component symlink (POSIX O_NOFOLLOW)."""

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    if os.name == "posix":
        flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        return os.open(path, flags)
    except FileNotFoundError as exc:
        raise PKVRuntimeError(
            ErrorCode.DATABASE_MISSING,
            f"数据库在打开前消失: {path}",
        ) from exc
    except OSError as exc:
        loop_errno = getattr(errno, "ELOOP", None)
        if loop_errno is not None and getattr(exc, "errno", None) == loop_errno:
            raise PKVRuntimeError(
                ErrorCode.DATA_ROOT_UNSAFE,
                f"数据库在打开时变为链接: {path}",
            ) from exc
        raise PKVRuntimeError(
            ErrorCode.PATH_STATE_UNDETERMINED,
            f"无法打开数据库文件: {path}",
        ) from exc


def _validate_existing_sqlite_file_with_identity(
    db_path: Path,
) -> tuple[Path, os.stat_result, tuple[int, int]]:
    """Validate a database and return the identity of the handle actually read.

    Identity is verified twice: the path is lstat'ed before the open, and the
    opened handle is fstat'ed before any byte is read.  A mismatch means the
    file was replaced between validation and open and is rejected fail-closed.
    """

    path = validate_path_components(Path(db_path), label="数据库文件")
    if not os.path.lexists(path):
        raise PKVRuntimeError(
            ErrorCode.DATABASE_MISSING,
            f"数据库不存在: {path}",
        )
    try:
        parent_info = os.lstat(path.parent)
    except OSError as exc:
        raise PKVRuntimeError(
            ErrorCode.PATH_STATE_UNDETERMINED,
            f"无法判定数据库父目录状态: {path.parent}",
        ) from exc
    if not stat.S_ISDIR(parent_info.st_mode) or _is_link_or_reparse(parent_info):
        raise PKVRuntimeError(
            ErrorCode.DATA_ROOT_UNSAFE,
            f"数据库父目录不安全: {path.parent}",
        )
    parent_identity = (parent_info.st_dev, parent_info.st_ino)
    info = _lstat(path)
    _assert_regular_identity(path, info)
    fd = _open_readonly_no_follow(path)
    try:
        opened = os.fstat(fd)
        _assert_regular_identity(path, opened)
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            raise PKVRuntimeError(
                ErrorCode.PATH_STATE_UNDETERMINED,
                f"数据库文件在打开期间被替换: {path}",
            )
        header = b""
        while len(header) < len(_SQLITE_HEADER):
            chunk = os.read(fd, len(_SQLITE_HEADER) - len(header))
            if not chunk:
                break
            header += chunk
        current = _lstat(path)
        _assert_regular_identity(path, current)
        if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
            raise PKVRuntimeError(
                ErrorCode.PATH_STATE_UNDETERMINED,
                f"数据库文件在校验期间被替换: {path}",
            )
        current_parent = os.lstat(path.parent)
        if (current_parent.st_dev, current_parent.st_ino) != parent_identity:
            raise PKVRuntimeError(
                ErrorCode.PATH_STATE_UNDETERMINED,
                f"数据库父目录在校验期间被替换: {path.parent}",
            )
    except OSError as exc:
        raise PKVRuntimeError(
            ErrorCode.DATABASE_NOT_SQLITE,
            f"数据库文件不可读: {path}",
        ) from exc
    finally:
        os.close(fd)
    if header != _SQLITE_HEADER:
        raise PKVRuntimeError(
            ErrorCode.DATABASE_NOT_SQLITE,
            f"数据库文件缺少 SQLite header: {path}",
        )
    return path, opened, parent_identity


def validate_existing_sqlite_file(db_path: Path) -> Path:
    """Validate one existing database without following links or creating it."""

    path, _, _ = _validate_existing_sqlite_file_with_identity(db_path)
    return path


def connect_existing_sqlite(
    db_path: Path,
    *,
    read_only: bool = False,
) -> sqlite3.Connection:
    """Open ``mode=rw``/``mode=ro`` so a missing DB is never auto-created.

    The same file identity is verified before and after ``sqlite3.connect``:
    the connection must refer to the exact file that was validated.  Any
    post-connect verification or ``query_only`` failure closes the handle and
    raises a stable :class:`PKVRuntimeError`.
    """

    path, identity, parent_identity = _validate_existing_sqlite_file_with_identity(db_path)
    mode = "ro" if read_only else "rw"
    # Lexical absolute URI: never resolve symlinks for the connection target.
    uri = f"{Path(os.path.abspath(os.fspath(path))).as_uri()}?mode={mode}"
    try:
        connection = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        # 验证后文件消失/被替换: 分类为缺失或保持原状, 绝不创建数据库。
        try:
            current = _lstat(path)
            _assert_regular_identity(path, current)
        except PKVRuntimeError as state_exc:
            raise state_exc from exc
        raise PKVRuntimeError(
            ErrorCode.DATABASE_NOT_SQLITE,
            f"无法打开既有 SQLite 数据库: {path}",
        ) from exc
    try:
        current = _lstat(path)
        _assert_regular_identity(path, current)
        if (current.st_dev, current.st_ino) != (identity.st_dev, identity.st_ino):
            raise PKVRuntimeError(
                ErrorCode.PATH_STATE_UNDETERMINED,
                f"数据库在验证与连接之间被替换: {path}",
            )
        current_parent = os.lstat(path.parent)
        if (current_parent.st_dev, current_parent.st_ino) != parent_identity:
            raise PKVRuntimeError(
                ErrorCode.PATH_STATE_UNDETERMINED,
                f"数据库父目录在连接期间被替换: {path.parent}",
            )
        if read_only:
            connection.execute("PRAGMA query_only = ON")
    except Exception as exc:
        connection.close()
        if isinstance(exc, PKVRuntimeError):
            raise
        if isinstance(exc, sqlite3.Error):
            raise PKVRuntimeError(
                ErrorCode.DATABASE_NOT_SQLITE,
                f"数据库只读加固失败: {path}",
            ) from exc
        raise
    return connection
