"""Fail-closed path gateway for every Markdown Vault file operation."""

from __future__ import annotations

import errno
import hashlib
import os
import stat
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional, Union

from src.runtime.errors import ErrorCode, PKVRuntimeError, StorageStage


PathLike = Union[str, os.PathLike[str]]
_REPARSE_FLAG = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


def _path_key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _is_within(path: Path, root: Path, *, allow_equal: bool = False) -> bool:
    path_key = _path_key(path)
    root_key = _path_key(root)
    try:
        common = os.path.commonpath((path_key, root_key))
    except ValueError:
        return False
    return common == root_key and (allow_equal or path_key != root_key)


def _lstat(path: Path, *, missing_ok: bool = False) -> os.stat_result | None:
    try:
        return os.lstat(path)
    except FileNotFoundError:
        if missing_ok:
            return None
        raise
    except OSError as exc:
        raise PKVRuntimeError(
            ErrorCode.PATH_STATE_UNDETERMINED,
            f"无法判定 Vault 路径状态: {path}",
        ) from exc


def _is_link_or_reparse(info: os.stat_result) -> bool:
    attributes = getattr(info, "st_file_attributes", 0)
    return stat.S_ISLNK(info.st_mode) or bool(attributes & _REPARSE_FLAG)


def _assert_file_identity(path: Path, info: os.stat_result) -> None:
    """Reject any file identity that must never be opened or mutated."""

    if _is_link_or_reparse(info):
        raise PKVRuntimeError(
            ErrorCode.PATH_LINK_UNSAFE,
            f"Vault 路径不得包含链接或 reparse point: {path}",
        )
    if not stat.S_ISREG(info.st_mode):
        raise PKVRuntimeError(
            ErrorCode.PATH_NOT_REGULAR_FILE,
            f"Vault 路径不是普通文件: {path}",
        )
    if info.st_nlink > 1:
        raise PKVRuntimeError(
            ErrorCode.PATH_LINK_UNSAFE,
            f"Vault 文件不得是硬链接: {path}",
        )


def _open_regular_readonly(target: Path) -> int:
    """Open a Vault file without following a final-component link.

    POSIX uses ``O_NOFOLLOW`` so a symlink swapped in after validation fails
    the open itself; Windows lacks the flag and is covered by the post-open
    fstat identity comparison in the caller.
    """

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    if os.name == "posix":
        flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        return os.open(target, flags)
    except OSError as exc:
        loop_errno = getattr(errno, "ELOOP", None)
        if loop_errno is not None and getattr(exc, "errno", None) == loop_errno:
            raise PKVRuntimeError(
                ErrorCode.PATH_LINK_UNSAFE,
                f"Vault 路径在打开时变为链接: {target}",
            ) from exc
        if getattr(exc, "errno", None) == errno.ENOENT:
            raise FileNotFoundError(f"文件不存在: {target}") from exc
        raise PKVRuntimeError(
            ErrorCode.PATH_STATE_UNDETERMINED,
            f"无法打开 Vault 文件: {target}",
        ) from exc


def _reverify_identity(path: Path, expected: os.stat_result) -> None:
    """Confirm ``path`` still names the exact file recorded before a mutation."""

    try:
        current = _lstat(path)
    except FileNotFoundError:
        raise PKVRuntimeError(
            ErrorCode.PATH_STATE_UNDETERMINED,
            f"Vault 路径在操作前消失: {path}",
        ) from None
    _assert_file_identity(path, current)
    if (current.st_dev, current.st_ino) != (expected.st_dev, expected.st_ino):
        raise PKVRuntimeError(
            ErrorCode.PATH_STATE_UNDETERMINED,
            f"Vault 路径在操作前被替换: {path}",
        )


def _move_no_clobber(source: Path, target: Path) -> None:
    """Move one regular file without ever replacing an existing target."""

    if os.name == "nt":
        # Unlike os.replace(), Windows rename fails when target already exists.
        os.rename(source, target)
        return

    # POSIX rename would clobber.  A same-filesystem hard-link publication is
    # atomic and refuses an existing target, after which the old name is
    # removed.  If that removal fails, remove only the link we can still prove
    # names the same inode, leaving the original fact in place.
    os.link(source, target, follow_symlinks=False)
    try:
        source.unlink()
    except Exception:
        try:
            source_info = _lstat(source)
            target_info = _lstat(target)
            if (
                source_info is not None
                and target_info is not None
                and (source_info.st_dev, source_info.st_ino)
                == (target_info.st_dev, target_info.st_ino)
            ):
                target.unlink()
        except Exception:
            # Preserve both names rather than risk deleting an identity that
            # changed during rollback; the caller will surface a repair state.
            pass
        raise


@dataclass(frozen=True)
class QuarantinedVaultFile:
    """A reversible primary-file delete prepared inside the Vault boundary."""

    original_path: Path
    quarantine_path: Path
    st_dev: int | None = None
    st_ino: int | None = None
    sha256: str | None = None

    @property
    def expected_identity(self) -> tuple[int, int] | None:
        if self.st_dev is None or self.st_ino is None:
            return None
        return (self.st_dev, self.st_ino)


@dataclass(frozen=True)
class PublishedVaultFile:
    """Path plus the exact file identity/content published by this operation."""

    path: Path
    st_dev: int
    st_ino: int
    sha256: str

    @property
    def identity(self) -> tuple[int, int]:
        return (self.st_dev, self.st_ino)


class VaultPathGateway:
    """Resolve and operate on Vault paths without following user-controlled links.

    SQLite persists only ``relative_name`` values.  Absolute paths are accepted
    at API boundaries solely for compatibility, and are immediately reduced to
    a contained Vault-relative path.
    """

    _INTERNAL_DIRS = frozenset({".pkv-quarantine"})

    @classmethod
    def _is_internal_component(cls, name: str) -> bool:
        """Match reserved directory names with host-filesystem case semantics."""

        key = os.path.normcase(name)
        return any(key == os.path.normcase(internal) for internal in cls._INTERNAL_DIRS)

    def __init__(self, vault_dir: PathLike, *, create: bool = True) -> None:
        self.vault_dir = Path(os.path.abspath(os.path.normpath(os.fspath(vault_dir))))
        if create:
            self._validate_creation_ancestor()
            self.vault_dir.mkdir(parents=True, exist_ok=True)
        self._validate_root()

    def _validate_creation_ancestor(self) -> None:
        """Reject redirected ancestors before mkdir can write through them."""

        cursor = self.vault_dir
        while not os.path.lexists(cursor):
            if cursor.parent == cursor:
                break
            cursor = cursor.parent
        info = _lstat(cursor)
        assert info is not None
        if _is_link_or_reparse(info) or not stat.S_ISDIR(info.st_mode):
            raise PKVRuntimeError(
                ErrorCode.PATH_LINK_UNSAFE,
                f"Vault 创建父路径不安全: {cursor}",
            )
        try:
            canonical = cursor.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise PKVRuntimeError(
                ErrorCode.PATH_STATE_UNDETERMINED,
                f"无法 canonicalize Vault 创建父路径: {cursor}",
            ) from exc
        if _path_key(canonical) != _path_key(cursor):
            raise PKVRuntimeError(
                ErrorCode.PATH_LINK_UNSAFE,
                f"Vault 创建父路径包含 canonical redirect: {cursor}",
            )

    def _validate_root(self) -> None:
        info = _lstat(self.vault_dir)
        assert info is not None
        if _is_link_or_reparse(info):
            raise PKVRuntimeError(
                ErrorCode.PATH_LINK_UNSAFE,
                f"Vault 根不得是符号链接、junction 或 reparse point: {self.vault_dir}",
            )
        if not stat.S_ISDIR(info.st_mode):
            raise PKVRuntimeError(
                ErrorCode.PATH_NOT_REGULAR_FILE,
                f"Vault 根不是目录: {self.vault_dir}",
            )
        try:
            canonical = self.vault_dir.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise PKVRuntimeError(
                ErrorCode.PATH_STATE_UNDETERMINED,
                f"无法 canonicalize Vault 根: {self.vault_dir}",
            ) from exc
        if _path_key(canonical) != _path_key(self.vault_dir):
            raise PKVRuntimeError(
                ErrorCode.PATH_LINK_UNSAFE,
                f"Vault 根的父路径包含 canonical redirect: {self.vault_dir}",
            )

    def _relative(self, candidate: PathLike) -> Path:
        raw = Path(candidate)
        if raw.is_absolute():
            absolute = Path(os.path.abspath(os.path.normpath(os.fspath(raw))))
            if not _is_within(absolute, self.vault_dir):
                raise PKVRuntimeError(
                    ErrorCode.PATH_OUTSIDE_VAULT,
                    f"路径越过 Vault: {absolute}",
                )
            relative = Path(os.path.relpath(absolute, self.vault_dir))
        else:
            relative = raw

        if not relative.parts or relative == Path("."):
            raise PKVRuntimeError(
                ErrorCode.PATH_OUTSIDE_VAULT,
                "Vault 文件路径不能为空",
            )
        if relative.is_absolute() or any(part in ("", ".", "..") for part in relative.parts):
            raise PKVRuntimeError(
                ErrorCode.PATH_OUTSIDE_VAULT,
                f"Vault 相对路径非法: {candidate}",
            )
        return relative

    def resolve(
        self,
        candidate: PathLike,
        *,
        must_exist: bool = False,
        require_file: bool = False,
        allow_internal: bool = False,
    ) -> Path:
        """Return a contained lexical path after validating every existing node."""

        self._validate_root()
        relative = self._relative(candidate)
        if not allow_internal and self._is_internal_component(relative.parts[0]):
            raise PKVRuntimeError(
                ErrorCode.PATH_OUTSIDE_VAULT,
                f"Vault 内部保留路径不可由外部访问: {relative.as_posix()}",
            )
        target = self.vault_dir / relative
        if not _is_within(target, self.vault_dir):
            raise PKVRuntimeError(
                ErrorCode.PATH_OUTSIDE_VAULT,
                f"路径越过 Vault: {candidate}",
            )

        cursor = self.vault_dir
        target_info: os.stat_result | None = None
        for index, part in enumerate(relative.parts):
            cursor = cursor / part
            info = _lstat(cursor, missing_ok=True)
            if info is None:
                if must_exist:
                    raise FileNotFoundError(f"文件不存在: {target}")
                break
            if _is_link_or_reparse(info):
                raise PKVRuntimeError(
                    ErrorCode.PATH_LINK_UNSAFE,
                    f"Vault 路径不得包含链接或 reparse point: {cursor}",
                )
            if index < len(relative.parts) - 1 and not stat.S_ISDIR(info.st_mode):
                raise PKVRuntimeError(
                    ErrorCode.PATH_STATE_UNDETERMINED,
                    f"Vault 路径父节点不是目录: {cursor}",
                )
            target_info = info

        if must_exist and target_info is None:
            raise FileNotFoundError(f"文件不存在: {target}")
        if require_file and target_info is not None:
            if not stat.S_ISREG(target_info.st_mode):
                raise PKVRuntimeError(
                    ErrorCode.PATH_NOT_REGULAR_FILE,
                    f"Vault 路径不是普通文件: {target}",
                )
            if target_info.st_nlink > 1:
                raise PKVRuntimeError(
                    ErrorCode.PATH_LINK_UNSAFE,
                    f"Vault 文件不得是硬链接: {target}",
                )

        # ``resolve`` is a second independent containment check. Existing links
        # were rejected above; a disagreement is treated as an unsafe race.
        try:
            canonical = target.resolve(strict=must_exist)
        except FileNotFoundError:
            raise FileNotFoundError(f"文件不存在: {target}") from None
        except (OSError, RuntimeError) as exc:
            raise PKVRuntimeError(
                ErrorCode.PATH_STATE_UNDETERMINED,
                f"无法 canonicalize Vault 路径: {target}",
            ) from exc
        if not _is_within(canonical, self.vault_dir):
            raise PKVRuntimeError(
                ErrorCode.PATH_OUTSIDE_VAULT,
                f"canonical 路径越过 Vault: {canonical}",
            )
        return target

    def relative_name(self, candidate: PathLike) -> str:
        """Return the canonical DB representation: a POSIX Vault-relative name."""

        target = self.resolve(candidate, must_exist=False)
        return target.relative_to(self.vault_dir).as_posix()

    def file_identity(
        self,
        candidate: PathLike,
        *,
        allow_internal: bool = False,
    ) -> tuple[int, int]:
        """Return the verified identity of one contained regular Vault file."""

        target = self.resolve(
            candidate,
            must_exist=True,
            require_file=True,
            allow_internal=allow_internal,
        )
        info = _lstat(target)
        assert info is not None
        _assert_file_identity(target, info)
        _reverify_identity(target, info)
        return (info.st_dev, info.st_ino)

    def file_fingerprint(
        self,
        candidate: PathLike,
        *,
        allow_internal: bool = False,
    ) -> tuple[tuple[int, int], str]:
        """Return a stable ``((st_dev, st_ino), sha256)`` snapshot.

        The final component is opened without following links, and identity plus
        size/timestamps are checked before and after hashing.  Callers use both
        values: an inode alone does not detect an editor truncating and rewriting
        the same file in place.
        """

        target = self.resolve(
            candidate,
            must_exist=True,
            require_file=True,
            allow_internal=allow_internal,
        )
        before = _lstat(target)
        assert before is not None
        _assert_file_identity(target, before)
        fd = _open_regular_readonly(target)
        try:
            opened = os.fstat(fd)
            _assert_file_identity(target, opened)
            if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                raise PKVRuntimeError(
                    ErrorCode.PATH_STATE_UNDETERMINED,
                    f"Vault 文件在摘要打开期间被替换: {target}",
                )
            digest = hashlib.sha256()
            while True:
                chunk = os.read(fd, 64 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
            after_fd = os.fstat(fd)
            after_path = _lstat(target)
            assert after_path is not None
            _assert_file_identity(target, after_path)
            stable_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
            if any(getattr(opened, field) != getattr(after_fd, field) for field in stable_fields):
                raise PKVRuntimeError(
                    ErrorCode.PATH_STATE_UNDETERMINED,
                    f"Vault 文件在计算摘要期间发生变化: {target}",
                )
            if any(getattr(after_fd, field) != getattr(after_path, field) for field in stable_fields):
                raise PKVRuntimeError(
                    ErrorCode.PATH_STATE_UNDETERMINED,
                    f"Vault 文件路径在计算摘要期间发生变化: {target}",
                )
            return (opened.st_dev, opened.st_ino), digest.hexdigest()
        except OSError as exc:
            raise PKVRuntimeError(
                ErrorCode.PATH_STATE_UNDETERMINED,
                f"无法计算 Vault 文件摘要: {target}",
            ) from exc
        finally:
            os.close(fd)

    def file_sha256(
        self,
        candidate: PathLike,
        *,
        allow_internal: bool = False,
    ) -> str:
        """Return the verified SHA-256 of one contained regular file."""

        return self.file_fingerprint(
            candidate, allow_internal=allow_internal
        )[1]

    def ensure_directory(self, relative_dir: PathLike) -> Path:
        relative = self._relative(relative_dir)
        if self._is_internal_component(relative.parts[0]):
            raise PKVRuntimeError(
                ErrorCode.PATH_OUTSIDE_VAULT,
                f"Vault 内部保留目录不可由外部创建: {relative.as_posix()}",
            )
        cursor = self.vault_dir
        for part in relative.parts:
            parent_info = _lstat(cursor)
            assert parent_info is not None
            if _is_link_or_reparse(parent_info) or not stat.S_ISDIR(parent_info.st_mode):
                raise PKVRuntimeError(
                    ErrorCode.PATH_LINK_UNSAFE,
                    f"Vault 子目录父路径不安全: {cursor}",
                )
            parent_identity = (parent_info.st_dev, parent_info.st_ino)
            candidate = cursor / part
            info = _lstat(candidate, missing_ok=True)
            if info is None:
                try:
                    candidate.mkdir()
                except FileExistsError:
                    pass
                except OSError as exc:
                    raise PKVRuntimeError(
                        ErrorCode.PATH_STATE_UNDETERMINED,
                        f"无法创建 Vault 子目录: {candidate}",
                    ) from exc
                info = _lstat(candidate)
            assert info is not None
            current_parent = _lstat(cursor)
            assert current_parent is not None
            if (current_parent.st_dev, current_parent.st_ino) != parent_identity:
                raise PKVRuntimeError(
                    ErrorCode.PATH_STATE_UNDETERMINED,
                    f"Vault 子目录父路径在创建期间被替换: {cursor}",
                )
            if _is_link_or_reparse(info) or not stat.S_ISDIR(info.st_mode):
                raise PKVRuntimeError(
                    ErrorCode.PATH_LINK_UNSAFE,
                    f"Vault 子目录不安全: {candidate}",
                )
            cursor = candidate
        return cursor

    def unique_markdown_path(self, relative_dir: PathLike, stem: str) -> Path:
        directory = self.ensure_directory(relative_dir)
        candidate = directory / f"{stem}.md"
        counter = 1
        while os.path.lexists(candidate):
            self.resolve(candidate, must_exist=True, require_file=True)
            candidate = directory / f"{stem}-{counter}.md"
            counter += 1
        return self.resolve(candidate)

    def write_text_atomic(self, candidate: PathLike, text: str) -> Path:
        """Publish a complete new file without replacing an existing entry."""

        return self.write_text_atomic_record(candidate, text).path

    def write_text_atomic_record(
        self, candidate: PathLike, text: str
    ) -> PublishedVaultFile:
        """Publish a new file and return its operation-bound identity."""

        expected_sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
        target = self.resolve(candidate)
        parent_relative = target.parent.relative_to(self.vault_dir)
        parent = self.vault_dir if parent_relative == Path(".") else self.ensure_directory(parent_relative)
        target = self.resolve(target)
        parent_info = _lstat(parent)
        assert parent_info is not None
        if _is_link_or_reparse(parent_info) or not stat.S_ISDIR(parent_info.st_mode):
            raise PKVRuntimeError(
                ErrorCode.PATH_LINK_UNSAFE,
                f"Vault 发布父目录不安全: {parent}",
            )
        parent_identity = (parent_info.st_dev, parent_info.st_ino)

        descriptor, temp_name = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".tmp", dir=parent
        )
        temp_path = Path(temp_name)
        published_to_target = False
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
                # 句柄身份: 写入句柄必须仍指向我们创建的临时文件。
                opened = os.fstat(handle.fileno())
                _assert_file_identity(temp_path, opened)
            temp_info = _lstat(temp_path)
            assert temp_info is not None
            if (temp_info.st_dev, temp_info.st_ino) != (opened.st_dev, opened.st_ino):
                raise PKVRuntimeError(
                    ErrorCode.PATH_STATE_UNDETERMINED,
                    f"Vault 临时文件在写入期间被替换: {temp_path}",
                )
            # Revalidate the parent immediately before publication.
            self.resolve(parent_relative / target.name)
            current_parent = _lstat(parent)
            assert current_parent is not None
            if (current_parent.st_dev, current_parent.st_ino) != parent_identity:
                raise PKVRuntimeError(
                    ErrorCode.PATH_STATE_UNDETERMINED,
                    f"Vault 发布父目录在写入期间被替换: {parent}",
                )
            if os.name == "nt":
                # Windows rename is atomic and refuses an existing destination.
                os.rename(temp_path, target)
                published_to_target = True
            else:
                # POSIX rename replaces its destination. A same-filesystem link
                # gives atomic no-clobber publication; unlinking the temporary
                # name immediately returns the published file to link count 1.
                os.link(temp_path, target, follow_symlinks=False)
                published_to_target = True
                try:
                    temp_path.unlink()
                except OSError:
                    # The target is already a complete published identity.  A
                    # leftover second name is cleanup debt, not a failed
                    # archive; the finally block retries it.
                    pass
            published = self.resolve(target, must_exist=True, require_file=True)
            published_info = _lstat(published)
            assert published_info is not None
            if (published_info.st_dev, published_info.st_ino) != (
                opened.st_dev,
                opened.st_ino,
            ):
                raise PKVRuntimeError(
                    ErrorCode.PATH_STATE_UNDETERMINED,
                    f"Vault 文件发布后身份不符: {target}",
                )
            published_identity, published_sha256 = self.file_fingerprint(published)
            if published_identity != (opened.st_dev, opened.st_ino):
                raise PKVRuntimeError(
                    ErrorCode.PATH_STATE_UNDETERMINED,
                    f"Vault 文件发布后摘要身份不符: {target}",
                )
            if published_sha256 != expected_sha256:
                raise PKVRuntimeError(
                    ErrorCode.PATH_STATE_UNDETERMINED,
                    f"Vault 文件发布后内容摘要不符: {target}",
                )
            current_parent = _lstat(parent)
            assert current_parent is not None
            if (current_parent.st_dev, current_parent.st_ino) != parent_identity:
                raise PKVRuntimeError(
                    ErrorCode.PATH_STATE_UNDETERMINED,
                    f"Vault 发布父目录在发布后被替换: {parent}",
                )
            return PublishedVaultFile(
                published,
                opened.st_dev,
                opened.st_ino,
                published_sha256,
            )
        except Exception as exc:
            if published_to_target:
                raise PKVRuntimeError(
                    ErrorCode.STORAGE_REPAIR_REQUIRED,
                    f"Vault 文件已发布但发布后校验失败: {target}",
                    stage=StorageStage.PRIMARY_COMMITTED.value,
                    recoverable=True,
                ) from exc
            raise
        finally:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass

    def read_text(self, candidate: PathLike) -> str:
        target = self.resolve(candidate, must_exist=True, require_file=True)
        identity = _lstat(target)
        _assert_file_identity(target, identity)
        fd = _open_regular_readonly(target)
        try:
            opened = os.fstat(fd)
            _assert_file_identity(target, opened)
            if (opened.st_dev, opened.st_ino) != (identity.st_dev, identity.st_ino):
                raise PKVRuntimeError(
                    ErrorCode.PATH_STATE_UNDETERMINED,
                    f"Vault 文件在打开期间被替换: {target}",
                )
            chunks: list[bytes] = []
            while True:
                chunk = os.read(fd, 64 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            return b"".join(chunks).decode("utf-8")
        except OSError as exc:
            raise PKVRuntimeError(
                ErrorCode.PATH_STATE_UNDETERMINED,
                f"无法读取 Vault 文件: {target}",
            ) from exc
        finally:
            os.close(fd)

    def delete(self, candidate: PathLike) -> bool:
        return self.delete_if_identity(candidate, expected_identity=None)

    def delete_if_identity(
        self,
        candidate: PathLike,
        *,
        expected_identity: tuple[int, int] | None,
        expected_sha256: str | None = None,
    ) -> bool:
        """Delete only the exact identity and content expected by the caller."""

        try:
            target = self.resolve(candidate, must_exist=True, require_file=True)
        except FileNotFoundError:
            return False
        try:
            identity = _lstat(target)
        except FileNotFoundError:
            return False
        _assert_file_identity(target, identity)
        if expected_identity is not None and (
            identity.st_dev,
            identity.st_ino,
        ) != expected_identity:
            raise PKVRuntimeError(
                ErrorCode.PATH_STATE_UNDETERMINED,
                f"拒绝删除已被替换的 Vault 文件: {target}",
            )
        observed_identity, observed_sha256 = self.file_fingerprint(target)
        if observed_identity != (identity.st_dev, identity.st_ino):
            raise PKVRuntimeError(
                ErrorCode.PATH_STATE_UNDETERMINED,
                f"拒绝删除身份不稳定的 Vault 文件: {target}",
            )
        if expected_sha256 is not None and observed_sha256 != expected_sha256:
            raise PKVRuntimeError(
                ErrorCode.PATH_STATE_UNDETERMINED,
                f"拒绝删除内容已变化的 Vault 文件: {target}",
            )
        _reverify_identity(target, identity)
        target.unlink()
        if os.path.lexists(target):
            raise PKVRuntimeError(
                ErrorCode.PATH_STATE_UNDETERMINED,
                f"删除后路径仍存在: {target}",
            )
        return True

    def quarantine(
        self,
        candidate: PathLike,
        *,
        operation_id: Optional[str] = None,
        expected_identity: tuple[int, int] | None = None,
        expected_sha256: str | None = None,
    ) -> QuarantinedVaultFile:
        original = self.resolve(candidate, must_exist=True, require_file=True)
        identity = _lstat(original)
        _assert_file_identity(original, identity)
        if expected_identity is not None and (
            identity.st_dev,
            identity.st_ino,
        ) != expected_identity:
            raise PKVRuntimeError(
                ErrorCode.PATH_STATE_UNDETERMINED,
                f"拒绝隔离已被替换的 Vault 文件: {original}",
            )
        observed_identity, observed_sha256 = self.file_fingerprint(original)
        if observed_identity != (identity.st_dev, identity.st_ino):
            raise PKVRuntimeError(
                ErrorCode.PATH_STATE_UNDETERMINED,
                f"拒绝隔离身份不稳定的 Vault 文件: {original}",
            )
        if expected_sha256 is not None and observed_sha256 != expected_sha256:
            raise PKVRuntimeError(
                ErrorCode.PATH_STATE_UNDETERMINED,
                f"拒绝隔离内容已变化的 Vault 文件: {original}",
            )
        original_parent_info = _lstat(original.parent)
        assert original_parent_info is not None
        original_parent_identity = (
            original_parent_info.st_dev,
            original_parent_info.st_ino,
        )
        quarantine_dir = self.vault_dir / ".pkv-quarantine"
        vault_info = _lstat(self.vault_dir)
        assert vault_info is not None
        vault_identity = (vault_info.st_dev, vault_info.st_ino)
        info = _lstat(quarantine_dir, missing_ok=True)
        if info is None:
            quarantine_dir.mkdir()
            info = _lstat(quarantine_dir)
        assert info is not None
        current_vault = _lstat(self.vault_dir)
        assert current_vault is not None
        if (current_vault.st_dev, current_vault.st_ino) != vault_identity:
            raise PKVRuntimeError(
                ErrorCode.PATH_STATE_UNDETERMINED,
                f"Vault 根在隔离目录创建期间被替换: {self.vault_dir}",
            )
        if _is_link_or_reparse(info) or not stat.S_ISDIR(info.st_mode):
            raise PKVRuntimeError(
                ErrorCode.PATH_LINK_UNSAFE,
                f"Vault quarantine 目录不安全: {quarantine_dir}",
            )
        quarantine_dir_identity = (info.st_dev, info.st_ino)
        if operation_id is not None:
            self._validate_operation_id(operation_id)
            name = f"{operation_id}-{original.name}"
        else:
            name = f"{uuid.uuid4().hex}-{original.name}"
        quarantine = quarantine_dir / name
        if os.path.lexists(quarantine):
            raise PKVRuntimeError(
                ErrorCode.PATH_STATE_UNDETERMINED,
                f"隔离目标已存在，未移动任何文件: {quarantine}",
            )
        # 移动前复核: 记录的 (dev, ino) 必须仍指向同一个文件。
        _reverify_identity(original, identity)
        current_original_parent = _lstat(original.parent)
        current_quarantine_dir = _lstat(quarantine_dir)
        assert current_original_parent is not None and current_quarantine_dir is not None
        if (
            current_original_parent.st_dev,
            current_original_parent.st_ino,
        ) != original_parent_identity or (
            current_quarantine_dir.st_dev,
            current_quarantine_dir.st_ino,
        ) != quarantine_dir_identity:
            raise PKVRuntimeError(
                ErrorCode.PATH_STATE_UNDETERMINED,
                "Vault 隔离源目录或目标目录在移动前被替换",
            )
        try:
            _move_no_clobber(original, quarantine)
        except FileExistsError as exc:
            raise PKVRuntimeError(
                ErrorCode.PATH_STATE_UNDETERMINED,
                f"隔离目标并发创建，未移动任何文件: {quarantine}",
            ) from exc
        # Post-move validation: the quarantine entry must still be the same
        # file that was recorded.  On failure restore the primary file, or
        # report a compensation failure with both recorded paths for repair.
        try:
            moved = _lstat(quarantine)
            assert moved is not None
            _assert_file_identity(quarantine, moved)
            if (moved.st_dev, moved.st_ino) != (identity.st_dev, identity.st_ino):
                raise PKVRuntimeError(
                    ErrorCode.PATH_STATE_UNDETERMINED,
                    f"隔离后文件身份与记录不符: {quarantine}",
                )
            moved_identity, moved_sha256 = self.file_fingerprint(
                quarantine, allow_internal=True
            )
            if moved_identity != (identity.st_dev, identity.st_ino):
                raise PKVRuntimeError(
                    ErrorCode.PATH_STATE_UNDETERMINED,
                    f"隔离后文件摘要身份不符: {quarantine}",
                )
            if moved_sha256 != observed_sha256:
                raise PKVRuntimeError(
                    ErrorCode.PATH_STATE_UNDETERMINED,
                    f"隔离后文件内容摘要不符: {quarantine}",
                )
            current_quarantine_dir = _lstat(quarantine_dir)
            assert current_quarantine_dir is not None
            if (
                current_quarantine_dir.st_dev,
                current_quarantine_dir.st_ino,
            ) != quarantine_dir_identity:
                raise PKVRuntimeError(
                    ErrorCode.PATH_STATE_UNDETERMINED,
                    f"Vault 隔离目录在移动后被替换: {quarantine_dir}",
                )
        except Exception as exc:
            try:
                # A failed validation does not authorize moving an arbitrary
                # replacement back into the primary path.  Re-acquire and prove
                # the quarantine identity before compensating.  A transient
                # inspection failure may therefore recover; an identity mismatch
                # becomes an explicit repair state.
                rollback_identity = _lstat(quarantine)
                assert rollback_identity is not None
                _assert_file_identity(quarantine, rollback_identity)
                if (rollback_identity.st_dev, rollback_identity.st_ino) != (
                    identity.st_dev,
                    identity.st_ino,
                ):
                    raise OSError(f"隔离文件身份已变化: {quarantine}")
                if (
                    self.file_sha256(quarantine, allow_internal=True)
                    != observed_sha256
                ):
                    raise OSError(f"隔离文件内容已变化: {quarantine}")
                if os.path.lexists(original):
                    raise OSError(f"恢复目标已存在: {original}")
                _move_no_clobber(quarantine, original)
            except Exception as restore_error:
                raise PKVRuntimeError(
                    ErrorCode.STORAGE_COMPENSATION_FAILED,
                    f"隔离后校验失败且恢复失败: {exc} (restore: {restore_error})",
                    stage=StorageStage.DELETE_QUARANTINED.value,
                    recoverable=True,
                ) from exc
            raise PKVRuntimeError(
                ErrorCode.PATH_STATE_UNDETERMINED,
                f"隔离后校验失败，已恢复原文件: {exc}",
                recoverable=True,
            ) from exc
        return QuarantinedVaultFile(
            original,
            quarantine,
            identity.st_dev,
            identity.st_ino,
            observed_sha256,
        )

    @staticmethod
    def _validate_operation_id(operation_id: str) -> None:
        if not operation_id or any(ch not in "0123456789abcdef-" for ch in operation_id):
            raise ValueError("operation_id 非法")

    def plan_quarantine_path(
        self,
        candidate: PathLike,
        *,
        operation_id: str,
    ) -> Path:
        """Deterministic no-clobber quarantine target for an operation.

        Performs no move; the caller journals this exact target before the
        quarantine move.  The target is derived from the operation id so a
        crashed restart can prove where the primary file went.
        """
        original = self.resolve(candidate, must_exist=True, require_file=True)
        self._validate_operation_id(operation_id)
        return self.vault_dir / ".pkv-quarantine" / f"{operation_id}-{original.name}"


    def _require_quarantine_location(self, path: Path) -> None:
        """The quarantine entry must live directly inside the internal dir."""

        relative = path.relative_to(self.vault_dir)
        if (
            not self._is_internal_component(relative.parts[0])
            or len(relative.parts) != 2
        ):
            raise PKVRuntimeError(
                ErrorCode.PATH_OUTSIDE_VAULT,
                f"隔离路径必须位于内部隔离目录: {path}",
            )

    def restore(self, item: QuarantinedVaultFile) -> Path:
        quarantine = self.resolve(
            item.quarantine_path,
            must_exist=True,
            require_file=True,
            allow_internal=True,
        )
        original = self.resolve(item.original_path)
        # 两条路径均由 resolve 约束在 Vault 内; 隔离路径还必须属于内部目录。
        self._require_quarantine_location(quarantine)
        if os.path.lexists(original):
            raise PKVRuntimeError(
                ErrorCode.STORAGE_COMPENSATION_FAILED,
                f"无法恢复被隔离文件，目标已存在: {original}",
            )
        identity = _lstat(quarantine)
        _assert_file_identity(quarantine, identity)
        if item.expected_identity is not None and (
            identity.st_dev,
            identity.st_ino,
        ) != item.expected_identity:
            raise PKVRuntimeError(
                ErrorCode.STORAGE_COMPENSATION_FAILED,
                f"拒绝恢复身份已变化的隔离文件: {quarantine}",
                stage=StorageStage.DELETE_QUARANTINED.value,
                recoverable=True,
            )
        if item.sha256 is not None and self.file_sha256(
            quarantine, allow_internal=True
        ) != item.sha256:
            raise PKVRuntimeError(
                ErrorCode.STORAGE_COMPENSATION_FAILED,
                f"拒绝恢复内容已变化的隔离文件: {quarantine}",
                stage=StorageStage.DELETE_QUARANTINED.value,
                recoverable=True,
            )
        _reverify_identity(quarantine, identity)
        try:
            _move_no_clobber(quarantine, original)
        except FileExistsError as exc:
            raise PKVRuntimeError(
                ErrorCode.STORAGE_COMPENSATION_FAILED,
                f"无法恢复被隔离文件，目标并发出现: {original}",
            ) from exc
        except OSError as exc:
            raise PKVRuntimeError(
                ErrorCode.STORAGE_COMPENSATION_FAILED,
                f"无法恢复被隔离文件: {original}",
                stage=StorageStage.DELETE_QUARANTINED.value,
                recoverable=True,
            ) from exc
        # 复核: 恢复后的文件必须仍是同一个文件。
        try:
            restored = _lstat(original)
            assert restored is not None
            _assert_file_identity(original, restored)
            if (restored.st_dev, restored.st_ino) != (identity.st_dev, identity.st_ino):
                raise PKVRuntimeError(
                    ErrorCode.PATH_STATE_UNDETERMINED,
                    f"恢复后文件身份与记录不符: {original}",
                )
            if item.sha256 is not None and self.file_sha256(original) != item.sha256:
                raise PKVRuntimeError(
                    ErrorCode.PATH_STATE_UNDETERMINED,
                    f"恢复后文件内容摘要不符: {original}",
                )
        except Exception as exc:
            raise PKVRuntimeError(
                ErrorCode.STORAGE_COMPENSATION_FAILED,
                f"恢复后校验失败: {exc}",
                stage=StorageStage.DELETE_QUARANTINED.value,
                recoverable=True,
            ) from exc
        return self.resolve(original, must_exist=True, require_file=True)

    def finalize_quarantine(self, item: QuarantinedVaultFile) -> None:
        quarantine = self.resolve(
            item.quarantine_path,
            must_exist=True,
            require_file=True,
            allow_internal=True,
        )
        self._require_quarantine_location(quarantine)
        identity = _lstat(quarantine)
        _assert_file_identity(quarantine, identity)
        if item.expected_identity is not None and (
            identity.st_dev,
            identity.st_ino,
        ) != item.expected_identity:
            raise PKVRuntimeError(
                ErrorCode.PATH_STATE_UNDETERMINED,
                f"拒绝清理身份已变化的隔离文件: {quarantine}",
            )
        if item.sha256 is not None and self.file_sha256(
            quarantine, allow_internal=True
        ) != item.sha256:
            raise PKVRuntimeError(
                ErrorCode.PATH_STATE_UNDETERMINED,
                f"拒绝清理内容已变化的隔离文件: {quarantine}",
            )
        _reverify_identity(quarantine, identity)
        quarantine.unlink()
        if os.path.lexists(quarantine):
            raise PKVRuntimeError(
                ErrorCode.PATH_STATE_UNDETERMINED,
                f"删除后路径仍存在: {quarantine}",
            )

    def iter_markdown(self, subdir: PathLike | None = None) -> Iterator[Path]:
        if subdir is None:
            search_root = self.vault_dir
        else:
            search_root = self.resolve(subdir, must_exist=False)
            if not search_root.exists():
                return
            info = _lstat(search_root)
            assert info is not None
            if not stat.S_ISDIR(info.st_mode):
                raise PKVRuntimeError(
                    ErrorCode.PATH_STATE_UNDETERMINED,
                    f"Vault 搜索路径不是目录: {search_root}",
                )

        for current, directories, files in os.walk(search_root, followlinks=False):
            current_path = Path(current)
            safe_directories: list[str] = []
            for name in directories:
                if self._is_internal_component(name):
                    continue
                directory = current_path / name
                info = _lstat(directory)
                assert info is not None
                if _is_link_or_reparse(info):
                    raise PKVRuntimeError(
                        ErrorCode.PATH_LINK_UNSAFE,
                        f"Vault 枚举遇到链接目录: {directory}",
                    )
                safe_directories.append(name)
            directories[:] = safe_directories
            for name in files:
                if not name.lower().endswith(".md"):
                    continue
                yield self.resolve(
                    current_path / name,
                    must_exist=True,
                    require_file=True,
                )
