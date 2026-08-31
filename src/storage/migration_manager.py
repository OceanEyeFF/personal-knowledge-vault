"""
数据库迁移管理器

负责管理数据库 Schema 的版本升级和回滚
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
import stat
import tempfile
import uuid
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from src.runtime.errors import ErrorCode, PKVRuntimeError
from src.runtime.layout import (
    ensure_safe_directory,
    open_user_file_nofollow,
    validate_directory_components,
    validate_path_components,
    verify_fd_matches_path,
)
from src.storage.sqlite_connection import (
    connect_existing_sqlite,
    validate_existing_sqlite_file,
)
from src.storage.sqlite_store import SQLiteStore

logger = logging.getLogger(__name__)


_CREATE_TABLE_PATTERN = re.compile(
    r"(?im)^\s*CREATE\s+(?:VIRTUAL\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?"
    r"([A-Za-z_][A-Za-z0-9_]*)"
)


def _read_migration_text(migration_file: Path) -> str:
    """Read one bundled SQL file through descriptor/path identity checks."""

    with open_user_file_nofollow(
        migration_file,
        "r",
        label="迁移脚本",
        encoding="utf-8",
    ) as handle:
        return handle.read()


def _tables_created_by_sql(migration_file: Path) -> frozenset[str]:
    """Tables (re)created by one bundled migration script."""
    try:
        sql = _read_migration_text(migration_file)
    except OSError:
        return frozenset()
    return frozenset(_CREATE_TABLE_PATTERN.findall(sql))


EXPECTED_TABLES_BY_VERSION = {
    "1.1.1": ("chat_sessions",),
    "1.1.2": ("review_queue", "review_history"),
    "1.2.0": ("knowledge_relations",),
    "1.2.4": ("storage_operation_commits",),
    "1.2.5": (
        "ai_automation_tasks",
        "ai_token_reservations",
        "ai_token_usage",
    ),
}
APPLICATION_TABLES_BY_MIGRATION = {
    "001_initial_schema.sql": frozenset(
        {
            "schema_version",
            "knowledge_items",
            "content_chunks",
            "tags",
            "knowledge_tags",
            "video_timestamps",
            "knowledge_items_fts",
        }
    ),
    "002_add_cli_tables.sql": frozenset(
        {"cli_command_history", "cli_user_preferences"}
    ),
    "004_add_chat_sessions.sql": frozenset({"chat_sessions"}),
    "005_add_review_system.sql": frozenset({"review_queue", "review_history"}),
    "006_add_relations_foundation.sql": frozenset({"knowledge_relations"}),
    "010_add_storage_operation_commits.sql": frozenset(
        {"storage_operation_commits"}
    ),
    "011_add_ai_automation_ledger.sql": frozenset(
        {"ai_automation_tasks", "ai_token_reservations", "ai_token_usage"}
    ),
}
FTS_REBUILD_VERSIONS = {"1.2.2", "1.2.3"}
LATEST_REQUIRED_TABLES = frozenset(
    {
        "schema_version",
        "knowledge_items",
        "content_chunks",
        "tags",
        "knowledge_tags",
        "video_timestamps",
        "knowledge_items_fts",
        "cli_command_history",
        "cli_user_preferences",
        "chat_sessions",
        "review_queue",
        "review_history",
        "knowledge_relations",
        "storage_operation_commits",
        "ai_automation_tasks",
        "ai_token_reservations",
        "ai_token_usage",
    }
)


class DatabaseState(str, Enum):
    """Mutually exclusive startup interpretation of a database path."""

    FRESH = "fresh"
    READY = "ready"
    UPGRADE_REQUIRED = "upgrade_required"
    FUTURE_VERSION = "future_version"


@dataclass(frozen=True)
class DatabaseInspection:
    state: DatabaseState
    current_version: str
    latest_version: str
    pending_versions: tuple[str, ...] = ()


class MigrationManager:
    """数据库迁移管理器"""

    def __init__(
        self,
        db_path: Path,
        migrations_dir: Path,
        *,
        read_only: bool = False,
        backup_dir: Path | None = None,
        _work_file_fd: int | None = None,
    ):
        """
        初始化迁移管理器

        Args:
            db_path: 数据库文件路径
            migrations_dir: 迁移脚本目录路径
            read_only: 仅允许检查数据库和迁移链，不创建目录或执行写入操作
        """
        self.db_path = Path(db_path)
        self.migrations_dir = Path(migrations_dir)
        self.read_only = read_only
        self._work_file_fd = _work_file_fd
        self.backup_dir = Path(backup_dir) if backup_dir is not None else (
            self.db_path.parent.parent / "backups"
        )
        # 本实例创建的 sidecar 锁文件身份 (st_dev, st_ino)；release 前用
        # lstat 核对，防止删除被外部替换的他人文件。未获取锁时为 None。
        self._publication_lock_identity: tuple[int, int] | None = None

        # Migration scripts are bundled, read-only resources. A missing bundle
        # is an installation error and must never be "repaired" under source.
        if not self.migrations_dir.exists():
            logger.warning("迁移目录不存在")

    def _require_writable(self, operation: str) -> None:
        """Reject mutating operations when this manager is read-only."""
        if self.read_only:
            raise RuntimeError(
                f"MigrationManager is read-only; cannot perform {operation}"
            )

    def _connect(self) -> sqlite3.Connection:
        """Open the database, optionally enforcing SQLite read-only mode."""
        if self._work_file_fd is not None:
            verify_fd_matches_path(
                self._work_file_fd,
                self.db_path,
                label="迁移工作数据库",
            )
            guarded = os.fstat(self._work_file_fd)
            if guarded.st_size == 0:
                uri = (
                    f"{Path(os.path.abspath(os.fspath(self.db_path))).as_uri()}"
                    "?mode=rw"
                )
                connection = sqlite3.connect(uri, uri=True)
            else:
                connection = connect_existing_sqlite(
                    self.db_path,
                    read_only=self.read_only,
                )
            try:
                verify_fd_matches_path(
                    self._work_file_fd,
                    self.db_path,
                    label="迁移工作数据库",
                )
            except Exception:
                connection.close()
                raise
            return connection
        if os.path.lexists(self.db_path):
            return connect_existing_sqlite(
                self.db_path,
                read_only=self.read_only,
            )
        if self.read_only:
            return connect_existing_sqlite(self.db_path, read_only=True)
        # Writable missing targets are used only as off-path migration work DBs.
        return sqlite3.connect(self.db_path)

    def _guarded_work_file_is_empty(self) -> bool:
        if self._work_file_fd is None:
            return False
        verify_fd_matches_path(
            self._work_file_fd,
            self.db_path,
            label="迁移工作数据库",
        )
        return os.fstat(self._work_file_fd).st_size == 0

    @contextmanager
    def _connection(self):
        """Transaction-aware connection context that always closes the handle."""
        conn = self._connect()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def get_current_version(self) -> str:
        """
        获取当前数据库版本

        Returns:
            版本号字符串（如 "1.0.0"），如果数据库未初始化返回 "0.0.0"
        """
        if not os.path.lexists(self.db_path):
            logger.info("数据库文件不存在，版本: 0.0.0")
            return "0.0.0"
        if self._guarded_work_file_is_empty():
            logger.info("受保护的迁移工作数据库为空，版本: 0.0.0")
            return "0.0.0"
        self._validate_database_file()

        try:
            with self._connection() as conn:
                if not self._table_exists(conn, "schema_version"):
                    raise PKVRuntimeError(
                        ErrorCode.DATABASE_VERSION_TABLE_MISSING,
                        "已有数据库缺少 schema_version；不能解释为 fresh database",
                    )
                self._validate_schema_version_contract(conn)
                row = conn.execute(
                    """
                    SELECT version FROM schema_version
                    ORDER BY version_id DESC LIMIT 1
                    """
                ).fetchone()
        except PKVRuntimeError:
            raise
        except sqlite3.DatabaseError as exc:
            raise PKVRuntimeError(
                ErrorCode.DATABASE_VERSION_TABLE_INVALID,
                "schema_version 不可读取",
            ) from exc

        if row is None or not self._is_semver(row[0]):
            raise PKVRuntimeError(
                ErrorCode.DATABASE_VERSION_TABLE_INVALID,
                "schema_version 为空或包含非法版本号",
            )
        version = str(row[0])
        logger.info("当前数据库版本: %s", version)
        return version

    def latest_version(self) -> str:
        """Return the validated highest version in the bundled migration chain."""

        migrations = self._migration_chain()
        if not migrations:
            raise PKVRuntimeError(
                ErrorCode.RESOURCE_MISSING,
                f"迁移资源为空: {self.migrations_dir}",
            )
        return migrations[-1][0]

    def inspect_database(self) -> DatabaseInspection:
        """Classify startup state without mutating the database or filesystem."""

        return self._inspect_database(exempt_tables=frozenset())

    def _inspect_database(self, *, exempt_tables: frozenset[str]) -> DatabaseInspection:
        """Shared strict classification with an upgrade-repair exemption.

        ``exempt_tables`` are declared tables whose absence is tolerated
        because a pending migration recreates them (e.g. the legacy
        ``knowledge_fts`` database lacks ``knowledge_items_fts`` until
        008/009 recreate it).  Startup classification passes an empty set and
        therefore stays strict; ``apply_all_pending`` passes
        ``_tables_created_by_pending()`` for its pre-flight and locked
        re-check.
        """

        latest = self.latest_version()
        if not os.path.lexists(self.db_path):
            return DatabaseInspection(DatabaseState.FRESH, "0.0.0", latest)
        if self._guarded_work_file_is_empty():
            return DatabaseInspection(DatabaseState.FRESH, "0.0.0", latest)

        current = self.get_current_version()
        with self._connection() as conn:
            self._validate_integrity(conn)
            applied = tuple(
                str(row[0])
                for row in conn.execute(
                    "SELECT version FROM schema_version ORDER BY version_id ASC"
                ).fetchall()
            )

        migration_chain = self._migration_chain()
        chain_versions = tuple(version for version, _ in migration_chain)
        comparison = self._version_compare(current, latest)
        if comparison > 0:
            return DatabaseInspection(DatabaseState.FUTURE_VERSION, current, latest)

        unknown = [version for version in applied if version not in chain_versions]
        if unknown:
            raise PKVRuntimeError(
                ErrorCode.DATABASE_SCHEMA_DRIFT,
                f"schema_version 含未知版本: {', '.join(unknown)}",
            )

        expected_applied = tuple(
            version
            for version in chain_versions
            if self._version_compare(version, current) <= 0
        )
        if applied != expected_applied:
            raise PKVRuntimeError(
                ErrorCode.DATABASE_SCHEMA_DRIFT,
                "schema_version 不是 bundled migration chain 的完整前缀",
            )

        with self._connection() as conn:
            tables = {
                str(row[0])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        required_tables: set[str] = set()
        for version, migration_file in migration_chain:
            if self._version_compare(version, current) <= 0:
                required_tables.update(
                    APPLICATION_TABLES_BY_MIGRATION.get(migration_file.name, ())
                )
        missing = sorted((required_tables - tables) - exempt_tables)
        if missing:
            raise PKVRuntimeError(
                ErrorCode.DATABASE_SCHEMA_DRIFT,
                f"数据库缺少已声明版本必需表: {', '.join(missing)}",
            )

        if comparison < 0:
            pending = tuple(
                version
                for version in chain_versions
                if self._version_compare(version, current) > 0
            )
            return DatabaseInspection(
                DatabaseState.UPGRADE_REQUIRED,
                current,
                latest,
                pending,
            )

        missing = sorted(LATEST_REQUIRED_TABLES - tables)
        if missing:
            raise PKVRuntimeError(
                ErrorCode.DATABASE_SCHEMA_DRIFT,
                f"数据库缺少当前版本必需表: {', '.join(missing)}",
            )
        return DatabaseInspection(DatabaseState.READY, current, latest)

    def require_ready(self) -> DatabaseInspection:
        """Return READY/FRESH inspection or raise a stable startup rejection."""

        inspection = self.inspect_database()
        if inspection.state is DatabaseState.UPGRADE_REQUIRED:
            raise PKVRuntimeError(
                ErrorCode.DATABASE_UPGRADE_REQUIRED,
                (
                    f"数据库版本 {inspection.current_version} 低于发布版本 "
                    f"{inspection.latest_version}；Developer Preview 不执行原地升级"
                ),
            )
        if inspection.state is DatabaseState.FUTURE_VERSION:
            raise PKVRuntimeError(
                ErrorCode.DATABASE_FUTURE_VERSION,
                (
                    f"数据库版本 {inspection.current_version} 高于当前应用支持版本 "
                    f"{inspection.latest_version}"
                ),
            )
        return inspection

    def initialize_fresh(self) -> DatabaseInspection:
        """Build a fresh database off-path and publish it only after validation."""

        self._require_writable("initialize_fresh")
        inspection = self.inspect_database()
        if inspection.state is not DatabaseState.FRESH:
            raise PKVRuntimeError(
                ErrorCode.MIGRATION_FAILED,
                f"仅数据库文件不存在时允许 fresh 初始化: {self.db_path}",
            )

        parent = ensure_safe_directory(self.db_path.parent, label="数据库目录")
        parent_info = os.lstat(parent)
        parent_identity = (parent_info.st_dev, parent_info.st_ino)
        descriptor, temp_name = tempfile.mkstemp(
            prefix=f".{self.db_path.name}.", suffix=".initializing", dir=parent
        )
        temp_path = Path(temp_name)
        descriptor_open = True
        try:
            verify_fd_matches_path(descriptor, temp_path, label="fresh 初始化工作数据库")
            current_parent = os.lstat(parent)
            if (current_parent.st_dev, current_parent.st_ino) != parent_identity:
                raise PKVRuntimeError(
                    ErrorCode.PATH_STATE_UNDETERMINED,
                    f"数据库目录在 fresh 初始化期间被替换: {parent}",
                )
            temporary = MigrationManager(
                temp_path,
                self.migrations_dir,
                backup_dir=self.backup_dir,
                _work_file_fd=descriptor,
            )
            fts_alignment_required = False
            for version, migration_file in temporary._migration_chain():
                temporary.apply_migration(migration_file, auto_backup=False)
                fts_alignment_required = (
                    fts_alignment_required or version in FTS_REBUILD_VERSIONS
                )
            if fts_alignment_required:
                SQLiteStore(temp_path).rebuild_fts5_index()
            ready = temporary.inspect_database()
            if ready.state is not DatabaseState.READY:
                raise PKVRuntimeError(
                    ErrorCode.MIGRATION_FAILED,
                    f"fresh 初始化未达到 READY: {ready.state.value}",
                )
            if os.path.lexists(self.db_path):
                raise PKVRuntimeError(
                    ErrorCode.MIGRATION_LOCKED,
                    f"发布 fresh database 前目标已被其他进程创建: {self.db_path}",
                )
            verify_fd_matches_path(
                descriptor, temp_path, label="fresh 初始化工作数据库"
            )
            work_identity = os.fstat(descriptor)
            expected_identity = (work_identity.st_dev, work_identity.st_ino)
            os.close(descriptor)
            descriptor_open = False
            temporary._work_file_fd = None
            self._publish_no_clobber(
                temp_path,
                self.db_path,
                expected_identity=expected_identity,
            )
            return self.inspect_database()
        except PKVRuntimeError:
            raise
        except Exception as exc:
            raise PKVRuntimeError(
                ErrorCode.MIGRATION_FAILED,
                "fresh database 初始化失败",
            ) from exc
        finally:
            if descriptor_open:
                with suppress(OSError):
                    os.close(descriptor)
            with suppress(OSError):
                temp_path.unlink()

    @staticmethod
    def _publish_no_clobber(
        source: Path,
        target: Path,
        *,
        expected_identity: tuple[int, int] | None = None,
    ) -> None:
        """Atomically publish ``source`` without ever replacing ``target``.

        Windows ``os.rename`` refuses an existing destination; POSIX uses a
        same-filesystem hard link (``rename`` would silently clobber).  A
        concurrently created target therefore maps to ``MIGRATION_LOCKED`` and
        its original bytes are preserved.  Unrelated I/O errors are
        publication failures (``MIGRATION_FAILED``), never lock collisions.
        """

        source = validate_path_components(source, label="迁移发布源")
        target = validate_path_components(target, label="迁移发布目标")
        parent = validate_directory_components(target.parent, label="数据库目录")
        parent_info = os.lstat(parent)
        parent_identity = (parent_info.st_dev, parent_info.st_ino)
        try:
            source_info = os.lstat(source)
        except OSError as exc:
            raise PKVRuntimeError(
                ErrorCode.MIGRATION_FAILED,
                f"无法读取迁移发布源: {source}",
            ) from exc
        if not stat.S_ISREG(source_info.st_mode) or source_info.st_nlink > 1:
            raise PKVRuntimeError(
                ErrorCode.MIGRATION_FAILED,
                f"迁移发布源不是独占普通文件: {source}",
            )
        source_identity = (source_info.st_dev, source_info.st_ino)
        if expected_identity is not None and source_identity != expected_identity:
            raise PKVRuntimeError(
                ErrorCode.MIGRATION_FAILED,
                f"迁移发布源身份已变化: {source}",
            )
        if os.path.lexists(target):
            raise PKVRuntimeError(
                ErrorCode.MIGRATION_LOCKED,
                f"发布目标已被其他进程创建，拒绝覆盖: {target}",
            )
        current_parent = os.lstat(parent)
        if (current_parent.st_dev, current_parent.st_ino) != parent_identity:
            raise PKVRuntimeError(
                ErrorCode.MIGRATION_FAILED,
                f"数据库目录在发布前被替换: {parent}",
            )
        try:
            if os.name == "nt":
                os.rename(source, target)
            else:
                # Hard link is atomic and refuses an existing destination; the
                # source afterwards is only a second name for the same inode.
                os.link(source, target, follow_symlinks=False)
        except FileExistsError as exc:
            raise PKVRuntimeError(
                ErrorCode.MIGRATION_LOCKED,
                f"发布目标已被其他进程创建，拒绝覆盖: {target}",
            ) from exc
        except OSError as exc:
            raise PKVRuntimeError(
                ErrorCode.MIGRATION_FAILED,
                f"无法原子发布迁移结果: {target}",
            ) from exc
        if os.name != "nt":
            try:
                source.unlink()
            except OSError as exc:
                # 链接成功后目标已完整发布; 残留的源硬链接由调用方 finally
                # 清理, 不得把成功误报为锁冲突。
                logger.warning(
                    "发布成功但临时源文件清理失败: error_type=%s",
                    type(exc).__name__,
                )
        try:
            published = os.lstat(target)
            if (published.st_dev, published.st_ino) != source_identity:
                raise PKVRuntimeError(
                    ErrorCode.MIGRATION_FAILED,
                    f"迁移发布后数据库身份不一致: {target}",
                )
            current_parent = os.lstat(parent)
            if (current_parent.st_dev, current_parent.st_ino) != parent_identity:
                raise PKVRuntimeError(
                    ErrorCode.MIGRATION_FAILED,
                    f"数据库目录在发布后被替换: {parent}",
                )
        except PKVRuntimeError:
            raise
        except OSError as exc:
            raise PKVRuntimeError(
                ErrorCode.MIGRATION_FAILED,
                f"迁移发布后无法验证数据库: {target}",
            ) from exc

    def _publication_lock_path(self) -> Path:
        """Sidecar lock path contained next to the database file."""
        return self.db_path.parent / f".{self.db_path.name}.migrate.lock"

    def _acquire_publication_lock(self) -> None:
        """Serialize migration publication against another migration process.

        The lock is created atomically (``O_CREAT|O_EXCL``) next to the
        database so it stays inside the same contained user-data directory.  A
        pre-existing lock means another migration process is running and the
        operation fails closed with ``MIGRATION_LOCKED``.  If that process
        crashed, the operator must remove the stale lock before retrying;
        failing closed is safer than risking a concurrent ``os.replace``.

        The identity (``st_dev``/``st_ino``) of the file this instance
        created is recorded right after creation; ``_release_publication_lock``
        re-checks it with ``lstat`` so an externally replaced lock path is
        never deleted (fail-closed with an explicit warning).
        """
        lock_path = self._publication_lock_path()
        try:
            descriptor = os.open(
                lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
            )
        except FileExistsError as exc:
            raise PKVRuntimeError(
                ErrorCode.MIGRATION_LOCKED,
                f"另一个迁移进程正在执行，拒绝并发迁移: {lock_path}",
            ) from exc
        except OSError as exc:
            raise PKVRuntimeError(
                ErrorCode.MIGRATION_FAILED,
                f"无法创建迁移发布锁: {lock_path}",
            ) from exc
        try:
            # 锁的存在性才是互斥依据; pid 仅用于人工诊断。同时记录本实例
            # 创建的锁文件身份，release 前核对，避免误删他人文件。
            identity = os.fstat(descriptor)
            self._publication_lock_identity = (identity.st_dev, identity.st_ino)
            with suppress(OSError):
                os.write(descriptor, f"pid={os.getpid()}\n".encode("ascii"))
        finally:
            os.close(descriptor)

    def _release_publication_lock(self) -> None:
        """释放本实例创建的迁移发布锁 (fail-closed)。

        release 前先 ``lstat`` 核对锁路径仍指向本实例创建的文件
        (``st_dev``/``st_ino`` 一致) 才 ``unlink``；若锁路径已被外部替换为
        他人文件，绝不删除，记录明确告警并保留现场。
        """
        lock_path = self._publication_lock_path()
        identity = self._publication_lock_identity
        if identity is None:
            logger.warning(
                "未记录迁移发布锁身份，拒绝删除锁文件 (fail-closed)"
            )
            return
        try:
            info = os.lstat(lock_path)
        except FileNotFoundError:
            logger.warning("迁移发布锁已不存在（可能已被外部清理）")
            return
        except OSError as exc:
            logger.warning(
                "无法核对迁移发布锁身份，拒绝删除锁文件: error_type=%s",
                type(exc).__name__,
            )
            return
        if (info.st_dev, info.st_ino) != identity:
            logger.warning("迁移发布锁路径已被外部替换，拒绝删除他人文件")
            return
        try:
            os.unlink(lock_path)
        except OSError as exc:
            logger.warning(
                "迁移发布锁清理失败: error_type=%s",
                type(exc).__name__,
            )

    @staticmethod
    def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone() is not None

    @staticmethod
    def _is_semver(value: object) -> bool:
        return bool(re.fullmatch(r"\d+\.\d+\.\d+", str(value or "")))

    def _validate_database_file(self) -> None:
        if self._guarded_work_file_is_empty():
            return
        validate_existing_sqlite_file(self.db_path)

    @staticmethod
    def _validate_schema_version_contract(conn: sqlite3.Connection) -> None:
        columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(schema_version)")
        }
        if not {"version_id", "version"}.issubset(columns):
            raise PKVRuntimeError(
                ErrorCode.DATABASE_VERSION_TABLE_INVALID,
                "schema_version 表结构无效",
            )

    @staticmethod
    def _validate_integrity(conn: sqlite3.Connection) -> None:
        try:
            result = conn.execute("PRAGMA integrity_check").fetchone()
            foreign_keys = conn.execute("PRAGMA foreign_key_check").fetchall()
        except sqlite3.DatabaseError as exc:
            raise PKVRuntimeError(
                ErrorCode.DATABASE_INTEGRITY_FAILED,
                "SQLite 完整性检查无法执行",
            ) from exc
        if result is None or str(result[0]).lower() != "ok" or foreign_keys:
            raise PKVRuntimeError(
                ErrorCode.DATABASE_INTEGRITY_FAILED,
                "SQLite integrity_check 或 foreign_key_check 失败",
            )

    def _migration_chain(self) -> list[tuple[str, Path]]:
        validate_directory_components(self.migrations_dir, label="迁移目录")
        if not self.migrations_dir.is_dir():
            raise PKVRuntimeError(
                ErrorCode.RESOURCE_MISSING,
                f"迁移目录不存在: {self.migrations_dir}",
            )
        chain: list[tuple[str, Path]] = []
        previous: str | None = None
        seen: set[str] = set()
        for path in sorted(self.migrations_dir.glob("*.sql")):
            metadata = self._read_migration_metadata(path)
            version = metadata.get("version")
            if not version or not all(metadata.values()) or not self._is_semver(version):
                raise PKVRuntimeError(
                    ErrorCode.RESOURCE_NOT_READABLE,
                    f"迁移脚本头部无效: {path.name}",
                )
            if version in seen or (
                previous is not None and self._version_compare(version, previous) <= 0
            ):
                raise PKVRuntimeError(
                    ErrorCode.RESOURCE_NOT_READABLE,
                    f"迁移版本链非严格递增: {path.name}",
                )
            seen.add(version)
            previous = version
            chain.append((version, path))
        if not chain:
            raise PKVRuntimeError(
                ErrorCode.RESOURCE_MISSING,
                f"迁移资源为空: {self.migrations_dir}",
            )
        return chain

    def get_pending_migrations(self) -> list[tuple[str, Path]]:
        """
        获取待执行的迁移脚本

        Returns:
            (版本号, 脚本路径) 的列表，按版本号升序排列
        """
        current_version = self.get_current_version()
        migrations: list[tuple[str, Path]] = []
        for version, migration_file in self._migration_chain():
            if self._version_compare(version, current_version) > 0:
                migrations.append((version, migration_file))
                logger.debug("待迁移脚本已识别")

        logger.info(f"找到 {len(migrations)} 个待执行的迁移脚本")
        return migrations

    def run_health_check(self) -> dict[str, Any]:
        """
        执行迁移链健康检查（只读）。

        Returns:
            包含脚本链检查、数据库状态和问题列表的结果字典
        """
        migration_files = sorted(self.migrations_dir.glob("*.sql"))
        script_checks: list[dict[str, Any]] = []
        issues: list[str] = []
        if not self.migrations_dir.is_dir():
            issues.append(f"迁移目录不存在: {self.migrations_dir}")
        elif not migration_files:
            issues.append(f"迁移资源为空: {self.migrations_dir}")
        seen_versions: dict[str, str] = {}
        previous_version: str | None = None

        for migration_file in migration_files:
            metadata = self._read_migration_metadata(migration_file)
            version = metadata.get("version")
            description = metadata.get("description")
            header_ok = all(metadata.values())
            check: dict[str, Any] = {
                "file": migration_file.name,
                "version": version,
                "description": description,
                "has_standard_headers": header_ok,
            }

            if not header_ok:
                missing = [
                    key for key, value in metadata.items() if not value
                ]
                issues.append(
                    f"{migration_file.name} 缺少标准头字段: {', '.join(missing)}"
                )

            if version:
                if not re.match(r"^\d+\.\d+\.\d+$", version):
                    issues.append(f"{migration_file.name} 的版本号格式无效: {version}")
                elif version in seen_versions:
                    issues.append(
                        f"{migration_file.name} 与 {seen_versions[version]} 使用了重复版本号 {version}"
                    )
                else:
                    seen_versions[version] = migration_file.name

                if (
                    previous_version
                    and re.match(r"^\d+\.\d+\.\d+$", previous_version)
                    and re.match(r"^\d+\.\d+\.\d+$", version)
                    and self._version_compare(version, previous_version) <= 0
                ):
                    issues.append(
                        f"{migration_file.name} 的版本号 {version} 未严格高于前一个脚本版本 {previous_version}"
                    )

                previous_version = version

            script_checks.append(check)

        db_info: dict[str, Any] = {
            "db_path": str(self.db_path),
            "db_exists": os.path.lexists(self.db_path),
            "schema_version_exists": False,
            "current_version": "0.0.0",
            "applied_versions": [],
            "pending_migrations": [],
            "table_drift": [],
        }

        if db_info["db_exists"]:
            with self._connection() as conn:
                cursor = conn.execute(
                    """
                    SELECT name FROM sqlite_master
                    WHERE type='table' AND name='schema_version'
                    """
                )
                schema_version_exists = cursor.fetchone() is not None
                db_info["schema_version_exists"] = schema_version_exists

                if schema_version_exists:
                    applied_rows = conn.execute(
                        """
                        SELECT version
                        FROM schema_version
                        ORDER BY version_id ASC
                        """
                    ).fetchall()
                    applied_versions = [row[0] for row in applied_rows]
                    db_info["applied_versions"] = applied_versions
                    db_info["current_version"] = (
                        applied_versions[-1] if applied_versions else "0.0.0"
                    )

                    known_versions = set(seen_versions.keys())
                    if (
                        db_info["current_version"] != "0.0.0"
                        and db_info["current_version"] not in known_versions
                    ):
                        issues.append(
                            f"数据库当前版本 {db_info['current_version']} 不在当前迁移链定义中"
                        )

                table_names = {
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }

                applied_versions = set(db_info["applied_versions"])
                table_drift: list[str] = []
                for version, expected_tables in EXPECTED_TABLES_BY_VERSION.items():
                    has_tables = all(table in table_names for table in expected_tables)
                    if has_tables and version not in applied_versions:
                        table_drift.append(
                            f"数据库已存在 {', '.join(expected_tables)}，但 schema_version 中缺少 {version}"
                        )
                    if version in applied_versions and not has_tables:
                        table_drift.append(
                            f"schema_version 已记录 {version}，但数据库缺少表 {', '.join(expected_tables)}"
                        )

                db_info["table_drift"] = table_drift
                issues.extend(table_drift)

            try:
                pending = self.get_pending_migrations()
            except PKVRuntimeError as exc:
                issues.append(str(exc))
            else:
                db_info["pending_migrations"] = [
                    {"version": version, "file": path.name}
                    for version, path in pending
                ]

        return {
            "healthy": len(issues) == 0,
            "scripts": script_checks,
            "database": db_info,
            "issues": issues,
        }

    def apply_migration(self, migration_file: Path, auto_backup: bool = True):
        """
        执行迁移脚本

        Args:
            migration_file: 迁移脚本文件路径
            auto_backup: 是否自动备份数据库

        Raises:
            Exception: 迁移执行失败
        """
        self._require_writable("apply_migration")
        logger.info("开始迁移")
        ensure_safe_directory(self.db_path.parent, label="数据库目录")
        metadata = self._read_migration_metadata(migration_file)
        version = metadata.get("version")
        description = metadata.get("description") or migration_file.name

        if os.path.lexists(self.db_path):
            self._validate_database_file()

        if auto_backup and os.path.lexists(self.db_path):
            try:
                self._backup_database(migration_file.name)
            except Exception as exc:
                raise PKVRuntimeError(
                    ErrorCode.MIGRATION_BACKUP_FAILED,
                    f"迁移前备份失败，已拒绝执行: {migration_file.name}",
                ) from exc

        if version == "1.2.1" and self._knowledge_items_has_timeline_columns():
            with self._connection() as conn:
                self._ensure_timeline_indexes(conn)
                self._record_schema_version(conn, version, description)
            logger.info("✓ 跳过迁移（目标列已存在）")
            return

        # 执行 SQL 脚本
        conn = self._connect()
        try:
            sql = _read_migration_text(migration_file)

            if re.search(
                r"(?im)^\s*(?:BEGIN(?:\s+(?:TRANSACTION|IMMEDIATE|EXCLUSIVE|DEFERRED))?|COMMIT|ROLLBACK)\s*;",
                sql,
            ):
                raise PKVRuntimeError(
                    ErrorCode.MIGRATION_FAILED,
                    f"迁移脚本不得自行控制顶层事务: {migration_file.name}",
                )

            # ``executescript`` otherwise commits implicitly. An explicit
            # wrapper makes DDL, data changes and schema_version one unit.
            conn.executescript(f"BEGIN IMMEDIATE;\n{sql}\nCOMMIT;")

            logger.info("✓ 迁移成功")

        except PKVRuntimeError:
            with suppress(sqlite3.Error):
                conn.rollback()
            raise
        except Exception as error:
            with suppress(sqlite3.Error):
                conn.rollback()
            logger.error(
                "✗ 迁移失败: error_type=%s",
                type(error).__name__,
            )
            raise PKVRuntimeError(
                ErrorCode.MIGRATION_FAILED,
                f"迁移脚本执行失败: {migration_file.name}",
            ) from error

        finally:
            conn.close()

    def _tables_created_by_pending(self) -> frozenset[str]:
        """Tables the pending migration chain will (re)create for this db."""
        current = self.get_current_version()
        created: set[str] = set()
        for version, migration_file in self._migration_chain():
            if self._version_compare(version, current) > 0:
                created.update(_tables_created_by_sql(migration_file))
        return frozenset(created)

    def apply_all_pending(self, auto_backup: bool = True) -> int:
        """
        执行所有待迁移脚本

        迁移事务全程受 sidecar 发布锁 (``.<db>.migrate.lock``) 互斥保护:

        1. 只读预检: 既有 schema 漂移 (未知版本/非完整前缀/完整性失败/
           缺失且待执行迁移不会重建的旧表) 在复制任何数据前直接拒绝;
        2. 原子获取发布锁, 与另一个迁移进程互斥;
        3. 锁内复检: 若其他进程已完成迁移链则直接返回;
        4. 离线构建完整迁移产物 (备份 → 复制 → 逐脚本应用 → FTS 对齐);
        5. 产物必须通过 ``inspect_database() == READY`` 才允许发布;
        6. 发布前指纹复核；升级库用 ``os.replace`` 原子替换，fresh
           工作副本则必须使用 no-clobber 发布，避免覆盖在最终检查后
           并发创建的数据库。

        Args:
            auto_backup: 是否自动备份

        Returns:
            成功执行的迁移脚本数量

        Raises:
            Exception: 迁移执行失败
        """
        self._require_writable("apply_all_pending")
        pending = self.get_pending_migrations()

        if not pending:
            logger.info("没有待执行的迁移")
            return 0

        # 预检 (只读): 拒绝既有 schema 漂移, 不复制任何数据。仅放行待执行
        # 迁移链会重建的缺失表 (如旧 knowledge_fts 库缺 knowledge_items_fts)。
        inspection = self._inspect_database(
            exempt_tables=self._tables_created_by_pending()
        )
        if inspection.state not in (
            DatabaseState.FRESH,
            DatabaseState.UPGRADE_REQUIRED,
        ):
            raise PKVRuntimeError(
                ErrorCode.MIGRATION_FAILED,
                f"apply_all_pending 不支持数据库状态: {inspection.state.value}",
            )

        ensure_safe_directory(self.db_path.parent, label="数据库目录")
        self._acquire_publication_lock()
        try:
            # 锁内复检: 另一个迁移进程可能在预检后完成了迁移链。
            locked_inspection = self._inspect_database(
                exempt_tables=self._tables_created_by_pending()
            )
            if locked_inspection.state is DatabaseState.READY:
                logger.info("迁移链已由其他进程完成，无需重复执行")
                return 0
            if locked_inspection.state not in (
                DatabaseState.FRESH,
                DatabaseState.UPGRADE_REQUIRED,
            ):
                raise PKVRuntimeError(
                    ErrorCode.MIGRATION_FAILED,
                    f"锁定后数据库状态不支持迁移: {locked_inspection.state.value}",
                )
            pending = self.get_pending_migrations()
            if not pending:
                return 0

            original_fingerprint = self._database_fingerprint()
            if auto_backup and os.path.lexists(self.db_path):
                try:
                    self._backup_database("all-pending")
                except Exception as exc:
                    raise PKVRuntimeError(
                        ErrorCode.MIGRATION_BACKUP_FAILED,
                        "迁移链备份失败，已拒绝执行",
                    ) from exc

            descriptor, work_name = tempfile.mkstemp(
                prefix=f".{self.db_path.name}.", suffix=".migrating", dir=self.db_path.parent
            )
            work_path = Path(work_name)
            descriptor_open = True
            try:
                verify_fd_matches_path(descriptor, work_path, label="迁移工作数据库")
                if os.path.lexists(self.db_path):
                    self._copy_database(self.db_path, work_path)
                    verify_fd_matches_path(
                        descriptor, work_path, label="迁移工作数据库"
                    )

                work_manager = MigrationManager(
                    work_path,
                    self.migrations_dir,
                    backup_dir=self.backup_dir,
                    _work_file_fd=descriptor,
                )
                success_count = 0
                fts_alignment_required = False
                for version, migration_file in pending:
                    work_manager.apply_migration(migration_file, auto_backup=False)
                    success_count += 1
                    fts_alignment_required = (
                        fts_alignment_required or version in FTS_REBUILD_VERSIONS
                    )

                if fts_alignment_required:
                    SQLiteStore(work_path).rebuild_fts5_index()

                # 产物必须整体 READY (版本链前缀、必需表、完整性) 才可发布。
                ready = work_manager.inspect_database()
                if ready.state is not DatabaseState.READY:
                    raise PKVRuntimeError(
                        ErrorCode.MIGRATION_FAILED,
                        f"迁移链产物未达到 READY: {ready.state.value}",
                    )

                if self._database_fingerprint() != original_fingerprint:
                    raise PKVRuntimeError(
                        ErrorCode.MIGRATION_LOCKED,
                        "迁移期间数据库被其他进程修改，拒绝发布结果",
                    )
                verify_fd_matches_path(
                    descriptor, work_path, label="迁移工作数据库"
                )
                work_info = os.fstat(descriptor)
                work_identity = (work_info.st_dev, work_info.st_ino)
                os.close(descriptor)
                descriptor_open = False
                work_manager._work_file_fd = None
                if original_fingerprint is None:
                    # Legacy/admin callers may still apply the full chain to a
                    # missing DB.  Preserve that compatibility without allowing
                    # the final fingerprint-to-publish race to clobber a database
                    # concurrently created by another process.
                    self._publish_no_clobber(
                        work_path,
                        self.db_path,
                        expected_identity=work_identity,
                    )
                else:
                    # Existing-database upgrade semantics intentionally replace
                    # the locked, fingerprint-checked original atomically.
                    source_info = os.lstat(work_path)
                    if (source_info.st_dev, source_info.st_ino) != work_identity:
                        raise PKVRuntimeError(
                            ErrorCode.MIGRATION_LOCKED,
                            "迁移工作数据库在发布前被替换",
                        )
                    os.replace(work_path, self.db_path)
                    published = os.lstat(self.db_path)
                    if (published.st_dev, published.st_ino) != work_identity:
                        raise PKVRuntimeError(
                            ErrorCode.MIGRATION_FAILED,
                            "迁移发布后数据库身份不一致",
                        )
                    validate_existing_sqlite_file(self.db_path)
                logger.info(f"迁移完成: 成功执行 {success_count} 个脚本")
                return success_count
            except PKVRuntimeError:
                raise
            except Exception as exc:
                raise PKVRuntimeError(
                    ErrorCode.MIGRATION_FAILED,
                    "迁移链失败，原数据库保持不变",
                ) from exc
            finally:
                if descriptor_open:
                    with suppress(OSError):
                        os.close(descriptor)
                with suppress(OSError):
                    work_path.unlink()
        finally:
            self._release_publication_lock()

    def _remove_applied_versions(self, versions: list[str]) -> None:
        """移除已记录的迁移版本，用于后置校验失败后的可重试回滚。"""
        self._require_writable("_remove_applied_versions")
        if not versions:
            return

        placeholders = ", ".join("?" for _ in versions)
        with self._connection() as conn:
            conn.execute(
                f"DELETE FROM schema_version WHERE version IN ({placeholders})",
                tuple(versions),
            )

    def check_and_prompt_upgrade(self) -> bool:
        """
        检查并提示升级

        Returns:
            True: 有待升级的迁移，False: 已是最新版本
        """
        pending = self.get_pending_migrations()

        if not pending:
            logger.info("数据库 Schema 已是最新版本")
            return False

        print("")
        print("=" * 60)
        print(f" 检测到 {len(pending)} 个待升级的 Schema 变更")
        print("=" * 60)
        print("")

        for version, migration_file in pending:
            # 读取迁移描述
            description = self._get_migration_description(migration_file)
            print(f"  • {migration_file.name}")
            print(f"    版本: v{version}")
            if description:
                print(f"    说明: {description}")
            print("")

        print("建议:")
        print("  1. 备份数据: .\\scripts\\backup-data.ps1")
        print("  2. 执行升级: python scripts/migrate.py")
        print("  3. 验证数据: python -m src.main stats")
        print("")

        return True

    def _backup_database(self, migration_name: str) -> Path:
        """
        使用 SQLite online backup API 生成迁移前一致性副本。

        Args:
            migration_name: 迁移脚本名称（用于备份说明）
        """
        self._require_writable("_backup_database")
        if not os.path.lexists(self.db_path):
            raise FileNotFoundError(f"数据库不存在，无法备份: {self.db_path}")
        self._validate_database_file()
        ensure_safe_directory(self.backup_dir, label="数据库备份目录")
        safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "-", migration_name).strip("-.")
        backup_path = self.backup_dir / (
            f"{self.db_path.stem}-{safe_name or 'migration'}-{uuid.uuid4().hex}.db"
        )
        logger.info("自动备份数据库开始")
        try:
            self._copy_database(self.db_path, backup_path)
            conn = sqlite3.connect(backup_path)
            try:
                self._validate_integrity(conn)
            finally:
                conn.close()
            return backup_path
        except Exception:
            with suppress(FileNotFoundError):
                backup_path.unlink()
            raise

    @staticmethod
    def _copy_database(source: Path, destination: Path) -> None:
        source_uri = f"{source.resolve().as_uri()}?mode=ro"
        source_conn = sqlite3.connect(source_uri, uri=True)
        try:
            destination_conn = sqlite3.connect(destination)
            try:
                source_conn.backup(destination_conn)
                destination_conn.commit()
            finally:
                destination_conn.close()
        finally:
            # destination 连接构造失败时 source 也必须关闭 (fail-closed),
            # 且无论成功/失败 source 恰好关闭一次。
            source_conn.close()

    def _database_fingerprint(self) -> tuple[int, int, int, int, int] | None:
        """On-disk identity used for the publication-time re-check.

        Includes device, inode/file-index, size, mtime and ctime so a
        same-size/same-mtime replacement is still detected (on Windows ctime
        is the creation time and changes on ``os.replace``).
        """
        if not os.path.lexists(self.db_path):
            return None
        try:
            info = os.stat(self.db_path)
        except OSError as exc:
            raise PKVRuntimeError(
                ErrorCode.MIGRATION_FAILED,
                f"无法读取数据库指纹: {self.db_path}",
            ) from exc
        return (
            info.st_dev,
            info.st_ino,
            info.st_size,
            info.st_mtime_ns,
            info.st_ctime_ns,
        )

    def _parse_version_from_file(self, file_path: Path) -> str:
        """
        从迁移文件解析版本号

        Args:
            file_path: 迁移脚本文件路径

        Returns:
            版本号字符串（如 "1.0.0"）
        """
        try:
            for line in _read_migration_text(file_path).splitlines():
                # 匹配 "-- Version: 1.0.0" 格式
                if line.strip().startswith("-- Version:"):
                    version = line.split(":")[-1].strip()
                    return version
        except OSError:
            logger.warning("无法读取迁移文件")

        # 如果没有找到版本号，使用文件名序号生成版本号
        # 例如: 001_xxx.sql -> "0.0.1"
        match = re.match(r"(\d+)_", file_path.name)
        if match:
            try:
                seq = int(match.group(1))
            except ValueError:
                seq = 0
            return f"0.0.{seq}"

        logger.warning("无法解析迁移版本号")
        return "0.0.0"

    def _get_migration_description(self, file_path: Path) -> str | None:
        """
        从迁移文件读取描述信息

        Args:
            file_path: 迁移脚本文件路径

        Returns:
            描述字符串，如果未找到返回 None
        """
        try:
            for line in _read_migration_text(file_path).splitlines():
                if line.strip().startswith("-- Description:"):
                    return line.split(":")[-1].strip()
        except OSError:
            logger.warning("无法读取迁移文件")

        return None

    def _read_migration_metadata(self, file_path: Path) -> dict[str, str | None]:
        """
        读取迁移脚本标准头部。

        Args:
            file_path: 迁移脚本路径

        Returns:
            包含 migration/version/description 的字典
        """
        metadata: dict[str, str | None] = {
            "migration": None,
            "version": None,
            "description": None,
        }

        try:
            for line in _read_migration_text(file_path).splitlines():
                stripped = line.strip()
                if stripped.startswith("-- Migration:"):
                    metadata["migration"] = stripped.split(":", 1)[-1].strip()
                elif stripped.startswith("-- Version:"):
                    metadata["version"] = stripped.split(":", 1)[-1].strip()
                elif stripped.startswith("-- Description:"):
                    metadata["description"] = stripped.split(":", 1)[-1].strip()

                if all(metadata.values()):
                    break
        except OSError:
            logger.warning("无法读取迁移文件")

        return metadata

    def _knowledge_items_has_timeline_columns(self) -> bool:
        """检查 knowledge_items 是否已具备 timeline 真实时间列。"""
        if not os.path.lexists(self.db_path):
            return False

        with self._connection() as conn:
            cursor = conn.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type='table' AND name='knowledge_items'
                """
            )
            if cursor.fetchone() is None:
                return False

            columns = {
                row[1] for row in conn.execute("PRAGMA table_info(knowledge_items)")
            }
        return {"event_time", "published_at"}.issubset(columns)

    def _ensure_timeline_indexes(self, conn: sqlite3.Connection) -> None:
        """补齐 timeline 时间字段索引。"""
        self._require_writable("_ensure_timeline_indexes")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_knowledge_event_time ON knowledge_items(event_time)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_knowledge_published_at ON knowledge_items(published_at)"
        )

    def _record_schema_version(
        self,
        conn: sqlite3.Connection,
        version: str | None,
        description: str,
    ) -> None:
        """写入 schema_version 记录。"""
        self._require_writable("_record_schema_version")
        if not version:
            return
        conn.execute(
            """
            INSERT OR IGNORE INTO schema_version (version, description)
            VALUES (?, ?)
            """,
            (version, description),
        )

    def _version_compare(self, v1: str, v2: str) -> int:
        """
        比较版本号

        Args:
            v1: 版本号 1
            v2: 版本号 2

        Returns:
            1: v1 > v2, 0: v1 == v2, -1: v1 < v2
        """
        parts1 = self._version_to_tuple(v1)
        parts2 = self._version_to_tuple(v2)

        if parts1 > parts2:
            return 1
        elif parts1 < parts2:
            return -1
        else:
            return 0

    def _version_to_tuple(self, version: str) -> tuple:
        """
        将版本号字符串转换为元组（用于比较）

        Args:
            version: 版本号字符串（如 "1.0.0"）

        Returns:
            版本号元组（如 (1, 0, 0)）
        """
        try:
            return tuple(int(x) for x in version.split("."))
        except ValueError:
            logger.warning("无效的版本号格式")
            return (0, 0, 0)
