"""Single-source runtime layout for bundled resources and user data."""

from __future__ import annotations

import errno
import os
import stat
import sys
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.runtime.errors import ErrorCode, PKVRuntimeError


PathLike = str | os.PathLike[str]
_DEVICE_PATH_PREFIXES = ("\\\\?\\", "\\\\.\\", "//?/", "//./")
_CHILD_SPECS = {
    "DB_PATH": ("db_path", Path("db") / "knowledge_vault.db"),
    "VAULT_DIR": ("vault_dir", Path("vault")),
    "VECTOR_DIR": ("vector_index_dir", Path("vectors")),
    "LOG_DIR": ("log_dir", Path("logs")),
    "TMP_DIR": ("tmp_dir", Path("tmp")),
}
_STORAGE_CONFIG_KEYS = {
    "DB_PATH": "db_path",
    "VAULT_DIR": "vault_dir",
    "VECTOR_DIR": "vector_index_dir",
    "LOG_DIR": "log_dir",
    "TMP_DIR": "tmp_dir",
}


def _coerce_pathlike(value: object, *, env_key: str) -> PathLike:
    """Accept only strings/PathLike from env or YAML storage config."""

    if isinstance(value, (str, os.PathLike)):
        return value
    raise PKVRuntimeError(
        ErrorCode.DATA_ROOT_UNSAFE,
        f"{env_key} 配置值必须是文件路径: {value!r}",
    )


def _raw_path_is_remote_or_device(value: PathLike) -> bool:
    text = os.fspath(value).strip()
    normalized = text.replace("/", "\\")
    return normalized.startswith("\\\\") or text.startswith(_DEVICE_PATH_PREFIXES)


def lexically_within(path: Path, root: Path, *, allow_equal: bool = False) -> bool:
    """Public containment predicate used by storage adapters."""

    return _is_lexically_within(path, root, allow_equal=allow_equal)


def validate_path_components(path: Path, *, label: str) -> Path:
    """Reject link/reparse/hardlink components without a containment root.

    Every *existing* component from the filesystem root down to the leaf is
    lstat-verified.  A missing component means everything below it is unborn
    and therefore cannot yet be a link.
    """

    candidate = Path(os.path.abspath(os.path.normpath(os.fspath(path))))
    existing: list[Path] = []
    cursor = Path(candidate.anchor)
    for part in candidate.parts[1:]:
        cursor = cursor / part
        if not os.path.lexists(cursor):
            break
        existing.append(cursor)
    for component in existing:
        _require_safe_existing_path(component, label=label)
    return candidate


def validate_directory_components(path: Path, *, label: str) -> Path:
    """Local variant: existing components and the leaf must be directories."""

    candidate = validate_path_components(path, label=label)
    if os.path.lexists(candidate):
        info = os.lstat(candidate)
        if not stat.S_ISDIR(info.st_mode):
            raise PKVRuntimeError(
                ErrorCode.DATA_ROOT_UNSAFE,
                f"{label}不是目录: {candidate}",
            )
    return candidate


def ensure_safe_directory(path: Path, *, label: str) -> Path:
    """Create a directory chain and verify every existing component fail-closed."""

    candidate = validate_path_components(path, label=label)
    _create_directory_chain(candidate)
    return validate_directory_components(candidate, label=label)


def verify_fd_matches_path(fd: int, path: Path, *, label: str) -> None:
    """Fail closed unless ``fd`` and ``path`` denote the same non-link file.

    POSIX compares ``(st_dev, st_ino)``.  On Windows both ``fstat`` and
    ``lstat`` expose the NTFS file index as ``st_ino``, so the same identity
    check applies; filesystems that report a zero index on both sides pass
    through without claiming absolute certainty.
    """

    try:
        fd_stat = os.fstat(fd)
        path_stat = os.lstat(path)
    except OSError as exc:
        raise PKVRuntimeError(
            ErrorCode.PATH_STATE_UNDETERMINED,
            f"无法判定{label}身份: {path}",
        ) from exc
    attributes = getattr(path_stat, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if stat.S_ISLNK(path_stat.st_mode) or bool(attributes & reparse_flag):
        raise PKVRuntimeError(
            ErrorCode.DATA_ROOT_UNSAFE,
            f"{label}被替换为链接: {path}",
        )
    if not stat.S_ISREG(fd_stat.st_mode):
        raise PKVRuntimeError(
            ErrorCode.DATA_ROOT_UNSAFE,
            f"{label}不是普通文件: {path}",
        )
    if stat.S_ISREG(path_stat.st_mode) and path_stat.st_nlink > 1:
        raise PKVRuntimeError(
            ErrorCode.DATA_ROOT_UNSAFE,
            f"{label}是硬链接文件: {path}",
        )
    fd_identity = (fd_stat.st_dev, fd_stat.st_ino)
    path_identity = (path_stat.st_dev, path_stat.st_ino)
    if fd_identity != path_identity:
        raise PKVRuntimeError(
            ErrorCode.DATA_ROOT_UNSAFE,
            f"{label}在打开后被替换: {path}",
        )


def _mode_to_open_flags(mode: str) -> int:
    plus = "+" in mode
    if mode.startswith("r"):
        return os.O_RDWR if plus else os.O_RDONLY
    if mode.startswith("w"):
        # Do not request O_TRUNC here: on platforms without O_NOFOLLOW, a leaf
        # swapped between validation and os.open() would be truncated before
        # descriptor/path identity can be checked.  ``open_user_file_nofollow``
        # truncates only after that check succeeds.
        return (os.O_RDWR if plus else os.O_WRONLY) | os.O_CREAT
    if mode.startswith("a"):
        return (os.O_RDWR if plus else os.O_WRONLY) | os.O_CREAT | os.O_APPEND
    if mode.startswith("x"):
        return (os.O_RDWR if plus else os.O_WRONLY) | os.O_CREAT | os.O_EXCL
    raise ValueError(f"不支持的打开模式: {mode}")


def open_user_file_nofollow(
    path: Path,
    mode: str = "rb",
    *,
    label: str = "用户可写文件",
    encoding: str | None = None,
    errors: str | None = None,
    newline: str | None = None,
):
    """Open a leaf with O_NOFOLLOW and verify fd identity before returning.

    On platforms where Python exposes no ``O_NOFOLLOW`` (Windows before
    3.12) the pre-open link check plus the post-open fstat/lstat identity
    check remain the strongest provable contract.
    """

    target = validate_path_components(path, label=label)
    flags = _mode_to_open_flags(mode)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if nofollow:
        flags |= nofollow
    try:
        descriptor = os.open(target, flags, 0o600)
    except OSError as exc:
        if nofollow and getattr(exc, "errno", None) == errno.ELOOP:
            raise PKVRuntimeError(
                ErrorCode.DATA_ROOT_UNSAFE,
                f"{label}是符号链接或 reparse point: {target}",
            ) from exc
        raise
    try:
        verify_fd_matches_path(descriptor, target, label=label)
        if mode.startswith("w"):
            os.ftruncate(descriptor, 0)
            os.lseek(descriptor, 0, os.SEEK_SET)
    except BaseException:
        os.close(descriptor)
        raise
    return os.fdopen(
        descriptor,
        mode,
        encoding=encoding,
        errors=errors,
        newline=newline,
    )


def _require_existing_directory(path: Path, *, label: str) -> os.stat_result:
    try:
        if _is_reparse_or_symlink(path):
            raise PKVRuntimeError(
                ErrorCode.DATA_ROOT_UNSAFE,
                f"{label}不得是符号链接、junction 或 reparse point: {path}",
            )
        info = os.lstat(path)
    except PKVRuntimeError:
        raise
    except OSError as exc:
        raise PKVRuntimeError(
            ErrorCode.PATH_STATE_UNDETERMINED,
            f"无法判定{label}状态: {path}",
        ) from exc
    if not stat.S_ISDIR(info.st_mode):
        raise PKVRuntimeError(
            ErrorCode.DATA_ROOT_UNSAFE,
            f"{label}不是目录: {path}",
        )
    return info


def atomic_publish_file(
    path: Path,
    *,
    label: str = "用户可写文件",
    writer: Callable[[Path], None] | None = None,
    data: bytes | None = None,
    extra_validate: Callable[[Path], None] | None = None,
    pre_replace: Callable[[], None] | None = None,
) -> None:
    """Write a complete temp file next to a validated target, then publish it.

    ``writer(temp_path)`` must fill the temporary file completely; ``data``
    is written directly when no writer is given.  The target is re-checked
    before ``os.replace`` (so a link/hardlink leaf is never overwritten
    through) and again after publication.  The parent directory identity is
    captured before ``mkstemp`` and re-verified after it, so a parent
    replaced by a link during the write is rejected.  The residual race
    between the final check and ``os.replace`` cannot be eliminated without
    dirfd-relative rename APIs that neither Python's ``os.replace`` nor
    Windows' ``MoveFileEx`` expose.
    """

    if (writer is None) == (data is None):
        raise ValueError("必须且只能提供 writer 或 data 之一")

    def _check(target: Path) -> None:
        if extra_validate is not None:
            extra_validate(target)
        else:
            validate_path_components(target, label=label)

    target = Path(os.path.abspath(os.path.normpath(os.fspath(path))))
    _check(target)
    parent = target.parent
    parent_info = _require_existing_directory(parent, label=f"{label}目录")
    parent_identity = (parent_info.st_dev, parent_info.st_ino)
    descriptor, raw_path = tempfile.mkstemp(
        dir=parent,
        prefix=f".{target.name}.",
        suffix=".tmp",
    )
    temp_path = Path(raw_path)
    temp_identity = os.fstat(descriptor)
    descriptor_open = True
    try:
        parent_after = os.lstat(parent)
        if (parent_after.st_dev, parent_after.st_ino) != parent_identity:
            raise PKVRuntimeError(
                ErrorCode.DATA_ROOT_UNSAFE,
                f"{label}的父目录在写入期间被替换: {parent}",
            )
        if writer is not None:
            verify_fd_matches_path(descriptor, temp_path, label=f"{label}临时文件")
            writer(temp_path)
            verify_fd_matches_path(descriptor, temp_path, label=f"{label}临时文件")
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor_open = False
        else:
            with os.fdopen(descriptor, "wb") as temp_file:
                descriptor_open = False
                temp_file.write(data)
                temp_file.flush()
                os.fsync(temp_file.fileno())
        _check(temp_path)
        published_temp = os.lstat(temp_path)
        if (published_temp.st_dev, published_temp.st_ino) != (
            temp_identity.st_dev,
            temp_identity.st_ino,
        ):
            raise PKVRuntimeError(
                ErrorCode.DATA_ROOT_UNSAFE,
                f"{label}临时文件在发布前被替换: {temp_path}",
            )
        _check(target)
        if pre_replace is not None:
            pre_replace()
        # The callback may deliberately yield to a concurrent writer; re-check
        # both the destination and its parent after it returns.
        _check(target)
        parent_after = os.lstat(parent)
        if (parent_after.st_dev, parent_after.st_ino) != parent_identity:
            raise PKVRuntimeError(
                ErrorCode.DATA_ROOT_UNSAFE,
                f"{label}的父目录在发布前被替换: {parent}",
            )
        os.replace(temp_path, target)
        temp_path = None
        _check(target)
    finally:
        if descriptor_open:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if temp_path is not None:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass


def _lexical_absolute(value: PathLike, *, anchor: Path) -> Path:
    if _raw_path_is_remote_or_device(value):
        raise PKVRuntimeError(
            ErrorCode.DATA_ROOT_UNSAFE,
            "运行时路径不得使用 UNC、网络共享或设备路径",
        )
    path = Path(os.fspath(value))
    if not path.is_absolute():
        path = anchor / path
    return Path(os.path.abspath(os.path.normpath(os.fspath(path))))


def _is_lexically_within(path: Path, root: Path, *, allow_equal: bool = False) -> bool:
    path_key = os.path.normcase(os.fspath(path))
    root_key = os.path.normcase(os.fspath(root))
    try:
        common = os.path.commonpath((path_key, root_key))
    except ValueError:
        return False
    if common != root_key:
        return False
    return allow_equal or path_key != root_key


def _is_reparse_or_symlink(path: Path) -> bool:
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise PKVRuntimeError(
            ErrorCode.PATH_STATE_UNDETERMINED,
            f"无法判定路径状态: {path}",
        ) from exc
    attributes = getattr(info, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(info.st_mode) or bool(attributes & reparse_flag)


def _require_safe_existing_path(path: Path, *, label: str) -> None:
    try:
        if _is_reparse_or_symlink(path):
            raise PKVRuntimeError(
                ErrorCode.DATA_ROOT_UNSAFE,
                f"{label}不得是符号链接、junction 或 reparse point: {path}",
            )
        info = os.lstat(path)
        canonical = path.resolve(strict=True)
    except PKVRuntimeError:
        raise
    except (OSError, RuntimeError) as exc:
        raise PKVRuntimeError(
            ErrorCode.PATH_STATE_UNDETERMINED,
            f"无法判定{label}状态: {path}",
        ) from exc
    if stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
        raise PKVRuntimeError(
            ErrorCode.DATA_ROOT_UNSAFE,
            f"{label}不得是硬链接文件: {path}",
        )
    if os.path.normcase(os.path.abspath(os.fspath(canonical))) != os.path.normcase(
        os.path.abspath(os.fspath(path))
    ):
        raise PKVRuntimeError(
            ErrorCode.DATA_ROOT_UNSAFE,
            f"{label}的父路径包含链接或 canonical redirect: {path}",
        )


def _create_directory_chain(
    path: Path, *, trusted_parent: Path | None = None
) -> None:
    """Create a directory without following links below ``trusted_parent``."""

    missing: list[Path] = []
    cursor = path
    while not os.path.lexists(cursor):
        missing.append(cursor)
        if cursor.parent == cursor:
            break
        cursor = cursor.parent

    if os.path.lexists(cursor):
        if trusted_parent is None or cursor == trusted_parent or _is_lexically_within(
            cursor, trusted_parent, allow_equal=True
        ):
            _require_safe_existing_path(cursor, label="目录")
        if not cursor.is_dir():
            raise PKVRuntimeError(
                ErrorCode.DATA_ROOT_UNSAFE,
                f"目录父路径不是目录: {cursor}",
            )

    for candidate in reversed(missing):
        try:
            candidate.mkdir()
        except FileExistsError:
            pass
        except OSError as exc:
            raise PKVRuntimeError(
                ErrorCode.DATA_ROOT_UNSAFE,
                f"无法创建用户数据目录: {candidate}",
            ) from exc
        _require_safe_existing_path(candidate, label="用户数据目录")
        if not candidate.is_dir():
            raise PKVRuntimeError(
                ErrorCode.DATA_ROOT_UNSAFE,
                f"用户数据路径不是目录: {candidate}",
            )


def _default_resource_root() -> Path:
    frozen_root = getattr(sys, "_MEIPASS", None)
    if frozen_root:
        return Path(frozen_root)
    return Path(__file__).resolve().parents[2]


def _default_user_data_root(environment: Mapping[str, str]) -> Path:
    if os.name == "nt":
        local_app_data = environment.get("LOCALAPPDATA")
        base = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
        return base / "PersonalKnowledgeVault"
    xdg_data_home = environment.get("XDG_DATA_HOME")
    base = Path(xdg_data_home) if xdg_data_home else Path.home() / ".local" / "share"
    return base / "personal-knowledge-vault"


@dataclass(frozen=True)
class RuntimeLayout:
    """Resolved immutable resource paths and contained mutable paths."""

    resources_root: Path
    user_data_root: Path
    base_config_path: Path
    workflows_dir: Path
    migrations_dir: Path
    prompts_dir: Path
    custom_dict_path: Path
    local_config_path: Path
    db_path: Path
    vault_dir: Path
    vector_index_dir: Path
    log_dir: Path
    tmp_dir: Path
    backup_dir: Path
    runtime_state_dir: Path

    @classmethod
    def resolve(
        cls,
        *,
        resources_root: Path | None = None,
        user_data_root: Path | None = None,
        base_config_path: Path | None = None,
        local_config_path: Path | None = None,
        storage_config: Mapping[str, Any] | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> RuntimeLayout:
        env = dict(os.environ if environment is None else environment)
        raw_resources: PathLike = resources_root or _default_resource_root()
        resources_lexical = _lexical_absolute(raw_resources, anchor=Path.cwd())
        if os.path.lexists(resources_lexical):
            # Reject a linked resources root before ``resolve`` can follow it.
            _require_safe_existing_path(resources_lexical, label="bundled resources 根")
        try:
            resolved_resources = resources_lexical.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise PKVRuntimeError(
                ErrorCode.RESOURCE_MISSING,
                f"bundled resources 根不存在: {resources_lexical}",
            ) from exc
        if not resolved_resources.is_dir():
            raise PKVRuntimeError(
                ErrorCode.RESOURCE_MISSING,
                f"bundled resources 根不是目录: {resolved_resources}",
            )

        raw_data_root: PathLike
        if user_data_root is not None:
            raw_data_root = user_data_root
        elif env.get("PKV_DATA_ROOT"):
            raw_data_root = env["PKV_DATA_ROOT"]
        elif env.get("DATA_DIR"):
            raw_data_root = env["DATA_DIR"]
        else:
            raw_data_root = _default_user_data_root(env)
        data_root = _lexical_absolute(raw_data_root, anchor=resolved_resources)

        storage = dict(storage_config or {})
        resolved_children: dict[str, Path] = {}
        for env_key, (attribute, default_suffix) in _CHILD_SPECS.items():
            configured_key = _STORAGE_CONFIG_KEYS[env_key]
            configured_value = storage.get(configured_key)
            if configured_value is None:
                raw_child: PathLike = env.get(env_key) or (data_root / default_suffix)
            else:
                raw_child = _coerce_pathlike(configured_value, env_key=env_key)
                env_child = env.get(env_key)
                if env_child:
                    raw_child = env_child
            child = _lexical_absolute(raw_child, anchor=resolved_resources)
            if not _is_lexically_within(child, data_root):
                raise PKVRuntimeError(
                    ErrorCode.DATA_ROOT_UNSAFE,
                    f"{env_key} 必须位于用户数据根内: {child}",
                )
            resolved_children[attribute] = child

        raw_local = local_config_path or data_root / "config" / "local.yaml"
        local_path = _lexical_absolute(raw_local, anchor=resolved_resources)
        if not _is_lexically_within(local_path, data_root):
            raise PKVRuntimeError(
                ErrorCode.DATA_ROOT_UNSAFE,
                f"local config 必须位于用户数据根内: {local_path}",
            )

        raw_base = base_config_path or resolved_resources / "config" / "config.yaml"
        base_path = _lexical_absolute(raw_base, anchor=resolved_resources)

        return cls(
            resources_root=resolved_resources,
            user_data_root=data_root,
            base_config_path=base_path,
            workflows_dir=resolved_resources / "config" / "workflows",
            migrations_dir=resolved_resources / "scripts" / "migrations",
            prompts_dir=resolved_resources / "src" / "ai" / "prompts",
            custom_dict_path=resolved_resources / "config" / "custom_dict.txt",
            local_config_path=local_path,
            db_path=resolved_children["db_path"],
            vault_dir=resolved_children["vault_dir"],
            vector_index_dir=resolved_children["vector_index_dir"],
            log_dir=resolved_children["log_dir"],
            tmp_dir=resolved_children["tmp_dir"],
            backup_dir=data_root / "backups",
            runtime_state_dir=data_root / "runtime",
        )

    def validate_bundled_resources(self) -> None:
        required = {
            "基础配置": (self.base_config_path, "file"),
            "工作流目录": (self.workflows_dir, "directory"),
            "URL 归档工作流": (self.workflows_dir / "archive-url.yaml", "file"),
            "文本归档工作流": (self.workflows_dir / "archive-text.yaml", "file"),
            "迁移目录": (self.migrations_dir, "directory"),
            "Prompt 目录": (self.prompts_dir, "directory"),
            "摘要 Prompt": (self.prompts_dir / "summarize.txt", "file"),
            "标签 Prompt": (self.prompts_dir / "extract_tags.txt", "file"),
            "自定义词典": (self.custom_dict_path, "file"),
        }
        for label, (path, expected_kind) in required.items():
            if not os.path.lexists(path):
                raise PKVRuntimeError(
                    ErrorCode.RESOURCE_MISSING,
                    f"bundled resource 缺失（{label}）: {path}",
                )
            # Reject lexical links/reparse points *before* ``resolve`` follows
            # them, then verify the canonical target stays inside the root.
            _require_safe_existing_path(path, label=f"bundled resource（{label}）")
            resolved = path.resolve(strict=True)
            if not _is_lexically_within(resolved, self.resources_root):
                raise PKVRuntimeError(
                    ErrorCode.RESOURCE_NOT_READABLE,
                    f"bundled resource 越过资源根（{label}）: {resolved}",
                )
            if expected_kind == "file" and not resolved.is_file():
                raise PKVRuntimeError(
                    ErrorCode.RESOURCE_NOT_READABLE,
                    f"bundled resource 不是普通文件（{label}）: {resolved}",
                )
            if expected_kind == "directory" and not resolved.is_dir():
                raise PKVRuntimeError(
                    ErrorCode.RESOURCE_NOT_READABLE,
                    f"bundled resource 不是目录（{label}）: {resolved}",
                )

    def validate_bundled_path(self, path: Path, *, label: str = "bundled resource") -> Path:
        """Validate one read-only resource before opening it."""

        lexical = _lexical_absolute(path, anchor=self.resources_root)
        if not os.path.lexists(lexical):
            raise PKVRuntimeError(
                ErrorCode.RESOURCE_MISSING,
                f"{label} 缺失: {lexical}",
            )
        if not _is_lexically_within(lexical, self.resources_root, allow_equal=False):
            raise PKVRuntimeError(
                ErrorCode.RESOURCE_NOT_READABLE,
                f"{label} 越过 bundled resources 根: {lexical}",
            )
        # lstat first: a symlink/reparse resource is rejected without following
        # it; the canonical comparison is a second independent containment check.
        _require_safe_existing_path(lexical, label=label)
        resolved = lexical.resolve(strict=True)
        if not _is_lexically_within(resolved, self.resources_root):
            raise PKVRuntimeError(
                ErrorCode.RESOURCE_NOT_READABLE,
                f"{label} 越过 bundled resources 根: {resolved}",
            )
        if not resolved.is_file():
            raise PKVRuntimeError(
                ErrorCode.RESOURCE_NOT_READABLE,
                f"{label}不是普通文件: {resolved}",
            )
        return resolved

    def validate_user_file(
        self,
        path: Path,
        *,
        label: str = "用户文件",
        allow_missing: bool = True,
    ) -> Path:
        """Validate a contained user file and every existing path component."""

        candidate = _lexical_absolute(path, anchor=self.user_data_root)
        if not _is_lexically_within(candidate, self.user_data_root):
            raise PKVRuntimeError(
                ErrorCode.DATA_ROOT_UNSAFE,
                f"{label}越过用户数据根: {candidate}",
            )
        if os.path.lexists(self.user_data_root):
            _require_safe_existing_path(self.user_data_root, label="用户数据根")
            if not self.user_data_root.is_dir():
                raise PKVRuntimeError(
                    ErrorCode.DATA_ROOT_UNSAFE,
                    f"用户数据根不是目录: {self.user_data_root}",
                )
        cursor = self.user_data_root
        relative = candidate.relative_to(self.user_data_root)
        for index, part in enumerate(relative.parts):
            cursor = cursor / part
            if not os.path.lexists(cursor):
                if allow_missing:
                    return candidate
                raise PKVRuntimeError(
                    ErrorCode.PATH_STATE_UNDETERMINED,
                    f"{label}不存在: {candidate}",
                )
            _require_safe_existing_path(cursor, label=label)
            try:
                info = os.lstat(cursor)
            except OSError as exc:
                raise PKVRuntimeError(
                    ErrorCode.PATH_STATE_UNDETERMINED,
                    f"无法判定{label}状态: {cursor}",
                ) from exc
            if index < len(relative.parts) - 1 and not stat.S_ISDIR(info.st_mode):
                raise PKVRuntimeError(
                    ErrorCode.DATA_ROOT_UNSAFE,
                    f"{label}父路径不是目录: {cursor}",
                )
        if not candidate.is_file():
            raise PKVRuntimeError(
                ErrorCode.DATA_ROOT_UNSAFE,
                f"{label}不是普通文件: {candidate}",
            )
        return candidate

    def ensure_user_directories(self) -> None:
        """Create only the declared mutable directory tree, fail-closed on links."""

        _create_directory_chain(self.user_data_root)
        _require_safe_existing_path(self.user_data_root, label="用户数据根")
        if not self.user_data_root.is_dir():
            raise PKVRuntimeError(
                ErrorCode.DATA_ROOT_UNSAFE,
                f"用户数据根不是目录: {self.user_data_root}",
            )

        directories = (
            self.local_config_path.parent,
            self.db_path.parent,
            self.vault_dir,
            self.vector_index_dir,
            self.log_dir,
            self.tmp_dir,
            self.backup_dir,
            self.runtime_state_dir,
        )
        for directory in directories:
            if not _is_lexically_within(directory, self.user_data_root):
                raise PKVRuntimeError(
                    ErrorCode.DATA_ROOT_UNSAFE,
                    f"用户可写目录越过数据根: {directory}",
                )
            _create_directory_chain(directory, trusted_parent=self.user_data_root)
            _require_safe_existing_path(directory, label="用户可写目录")

    def writable_user_path(
        self,
        path: Path,
        *,
        label: str = "用户可写文件",
    ) -> Path:
        """Unified contained-user-file gateway for writable leaves.

        Log files, ``ui.ini``, vector lock/metadata sidecars, temp images and
        other mutable leaves must open their targets through this validator so
        a link/reparse replacement can never redirect the write outside the
        declared user data root.
        """

        return self.validate_user_file(path, label=label, allow_missing=True)

    def validate_user_directory(
        self,
        path: Path,
        *,
        label: str = "用户可写目录",
        allow_missing: bool = True,
    ) -> Path:
        """Validate a contained directory leaf and every existing component."""

        candidate = _lexical_absolute(path, anchor=self.user_data_root)
        if not _is_lexically_within(candidate, self.user_data_root):
            raise PKVRuntimeError(
                ErrorCode.DATA_ROOT_UNSAFE,
                f"{label}越过用户数据根: {candidate}",
            )
        if os.path.lexists(self.user_data_root):
            _require_safe_existing_path(self.user_data_root, label="用户数据根")
            if not self.user_data_root.is_dir():
                raise PKVRuntimeError(
                    ErrorCode.DATA_ROOT_UNSAFE,
                    f"用户数据根不是目录: {self.user_data_root}",
                )
        cursor = self.user_data_root
        relative = candidate.relative_to(self.user_data_root)
        for index, part in enumerate(relative.parts):
            cursor = cursor / part
            if not os.path.lexists(cursor):
                if allow_missing:
                    return candidate
                raise PKVRuntimeError(
                    ErrorCode.PATH_STATE_UNDETERMINED,
                    f"{label}不存在: {candidate}",
                )
            _require_safe_existing_path(cursor, label=label)
            try:
                info = os.lstat(cursor)
            except OSError as exc:
                raise PKVRuntimeError(
                    ErrorCode.PATH_STATE_UNDETERMINED,
                    f"无法判定{label}状态: {cursor}",
                ) from exc
            if not stat.S_ISDIR(info.st_mode):
                raise PKVRuntimeError(
                    ErrorCode.DATA_ROOT_UNSAFE,
                    f"{label}父路径不是目录: {cursor}",
                )
        return candidate

    def open_user_file(
        self,
        path: Path,
        mode: str = "r",
        *,
        label: str = "用户可写文件",
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
    ):
        """Open a contained leaf via O_NOFOLLOW + fd identity verification."""

        allow_missing = any(flag in mode for flag in ("w", "a", "x", "+"))
        target = self.validate_user_file(
            path,
            label=label,
            allow_missing=allow_missing,
        )
        return open_user_file_nofollow(
            target,
            mode,
            label=label,
            encoding=encoding,
            errors=errors,
            newline=newline,
        )

    def atomic_publish_user_file(
        self,
        target: Path,
        *,
        label: str = "用户可写文件",
        writer: Callable[[Path], None] | None = None,
        data: bytes | None = None,
        pre_replace: Callable[[], None] | None = None,
    ) -> None:
        """Write a complete temp file inside the data root, then publish it.

        Pre/post containment+link checks wrap a same-directory temp file and
        an atomic ``os.replace``; the publish can never follow a link placed
        at the target (replace replaces the link itself), and a parent
        directory swapped for a link during the write is detected by the
        directory identity check.
        """

        def _check(candidate: Path) -> None:
            self.validate_user_file(candidate, label=label, allow_missing=True)

        atomic_publish_file(
            target,
            label=label,
            writer=writer,
            data=data,
            extra_validate=_check,
            pre_replace=pre_replace,
        )

    def as_dict(self) -> dict[str, Path]:
        return {
            "resources_root": self.resources_root,
            "user_data_root": self.user_data_root,
            "base_config_path": self.base_config_path,
            "local_config_path": self.local_config_path,
            "db_path": self.db_path,
            "vault_dir": self.vault_dir,
            "vector_index_dir": self.vector_index_dir,
            "log_dir": self.log_dir,
            "tmp_dir": self.tmp_dir,
            "backup_dir": self.backup_dir,
        }
