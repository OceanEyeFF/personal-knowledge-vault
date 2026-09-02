"""MigrationManager publication atomicity and concurrency-safety tests.

Covers ``_publish_no_clobber`` (B): concurrent-target refusal with byte
preservation on Windows/POSIX, error-classification precision, and the POSIX
link-success/unlink-failure ambiguity.  Covers ``apply_all_pending`` (C):
pre-copy schema-drift rejection, sidecar publication lock, unchanged original
bytes on every failure path, and exception-safe connection cleanup.
"""

from __future__ import annotations

import errno
import os
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.runtime import ErrorCode, PKVRuntimeError  # noqa: E402
from src.storage import migration_manager as mm_module  # noqa: E402
from src.storage import sqlite_connection as sc  # noqa: E402
from src.storage.migration_manager import DatabaseState, MigrationManager  # noqa: E402

MIGRATIONS_DIR = PROJECT_ROOT / "scripts" / "migrations"


def _apply(manager: MigrationManager, name: str) -> None:
    manager.apply_migration(MIGRATIONS_DIR / name, auto_backup=False)


# ---------------------------------------------------------------------------
# _publish_no_clobber -- fresh-initialization publication (B)
# ---------------------------------------------------------------------------


def test_publish_no_clobber_refuses_existing_target_and_preserves_bytes(
    tmp_path: Path,
) -> None:
    target = tmp_path / "vault.db"
    target.write_bytes(b"concurrent-writer-bytes")
    source = tmp_path / "candidate.db"
    source.write_bytes(b"fresh-db-bytes")

    with pytest.raises(PKVRuntimeError) as exc_info:
        MigrationManager._publish_no_clobber(source, target)

    assert exc_info.value.code is ErrorCode.MIGRATION_LOCKED
    assert target.read_bytes() == b"concurrent-writer-bytes"
    assert source.read_bytes() == b"fresh-db-bytes"


def test_publish_no_clobber_publishes_when_target_absent(tmp_path: Path) -> None:
    target = tmp_path / "vault.db"
    source = tmp_path / "candidate.db"
    source.write_bytes(b"fresh-db-bytes")

    MigrationManager._publish_no_clobber(source, target)

    assert target.read_bytes() == b"fresh-db-bytes"
    assert not source.exists()


def test_publish_no_clobber_does_not_misclassify_io_errors_as_lock_collision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if os.name == "nt":
        pytest.skip("POSIX-only link branch")

    target = tmp_path / "vault.db"
    source = tmp_path / "candidate.db"
    source.write_bytes(b"fresh-db-bytes")

    def boom_link(src, dst, **kwargs):
        raise PermissionError("permission denied")

    monkeypatch.setattr(os, "link", boom_link)

    with pytest.raises(PKVRuntimeError) as exc_info:
        MigrationManager._publish_no_clobber(source, target)

    assert exc_info.value.code is ErrorCode.MIGRATION_FAILED
    assert not target.exists()


def test_publish_no_clobber_link_success_with_unlink_failure_is_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if os.name == "nt":
        pytest.skip("POSIX-only unlink branch")

    target = tmp_path / "vault.db"
    source = tmp_path / "candidate.db"
    source.write_bytes(b"fresh-db-bytes")
    original_unlink = os.unlink

    def failing_unlink(path):
        if Path(path) == source:
            raise PermissionError("cannot unlink leftover source")
        return original_unlink(path)

    monkeypatch.setattr(os, "unlink", failing_unlink)

    # 链接成功后即使清理失败也必须视为发布成功, 不得误报锁冲突。
    MigrationManager._publish_no_clobber(source, target)

    assert target.read_bytes() == b"fresh-db-bytes"


def test_initialize_fresh_refuses_concurrent_target_and_preserves_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "data" / "db" / "vault.db"
    manager = MigrationManager(
        db_path, MIGRATIONS_DIR, backup_dir=tmp_path / "data" / "backups"
    )
    real_publish = MigrationManager._publish_no_clobber

    def racing_publish(source: Path, target: Path, **kwargs) -> None:
        # ``initialize_fresh`` first asks a nested temporary manager to build
        # its off-path database.  Inject the race only at the final publication
        # boundary, otherwise the fixture races (and later cleans up) the
        # temporary manager's own target instead of ``db_path``.
        if target == db_path:
            target.write_bytes(b"concurrent-writer-bytes")
        real_publish(source, target, **kwargs)

    monkeypatch.setattr(
        MigrationManager, "_publish_no_clobber", staticmethod(racing_publish)
    )

    with pytest.raises(PKVRuntimeError) as exc_info:
        manager.initialize_fresh()

    assert exc_info.value.code is ErrorCode.MIGRATION_LOCKED
    assert db_path.read_bytes() == b"concurrent-writer-bytes"
    assert not list(db_path.parent.glob("*.initializing"))


# ---------------------------------------------------------------------------
# apply_all_pending -- pre-copy drift rejection (C)
# ---------------------------------------------------------------------------


def test_apply_all_pending_rejects_missing_old_table_before_any_copy(
    tmp_path: Path,
) -> None:
    """schema_version 前缀完整但旧表缺失 (且无待执行迁移重建) 必须在复制前拒绝。"""
    db_path = tmp_path / "legacy.db"
    manager = MigrationManager(db_path, MIGRATIONS_DIR)
    _apply(manager, "001_initial_schema.sql")
    _apply(manager, "002_add_cli_tables.sql")
    _apply(manager, "004_add_chat_sessions.sql")
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            "INSERT INTO schema_version (version, description) VALUES ('1.1.2', 'review recorded')"
        )
    before = db_path.read_bytes()

    with pytest.raises(PKVRuntimeError) as exc_info:
        manager.apply_all_pending(auto_backup=False)

    assert exc_info.value.code is ErrorCode.DATABASE_SCHEMA_DRIFT
    assert db_path.read_bytes() == before
    assert not list(db_path.parent.glob("*.migrating"))
    assert not manager._publication_lock_path().exists()


def test_apply_all_pending_allows_legacy_fts_repair_upgrade(tmp_path: Path) -> None:
    """缺失表可由待执行迁移重建 (knowledge_items_fts ← 008/009) 时允许升级。"""
    db_path = tmp_path / "legacy-fts.db"
    manager = MigrationManager(db_path, MIGRATIONS_DIR)
    _apply(manager, "001_initial_schema.sql")
    # 把 001 的现代 FTS 合同替换为旧 knowledge_fts 合同。
    # ``with sqlite3.connect(...)`` 只提交不关闭; 残留句柄会让 Windows 的
    # os.replace 报 WinError 5, 必须确定性关闭所有 raw 连接。
    with closing(sqlite3.connect(str(db_path))) as conn:
        conn.executescript(
            """
            DROP TRIGGER IF EXISTS knowledge_items_ai;
            DROP TRIGGER IF EXISTS knowledge_items_au;
            DROP TRIGGER IF EXISTS knowledge_items_ad;
            DROP TABLE IF EXISTS knowledge_items_fts;
            CREATE VIRTUAL TABLE knowledge_fts USING fts5(
                knowledge_id UNINDEXED, title, content, keywords, tags,
                tokenize = 'porter unicode61'
            );
            """
        )
        conn.commit()

    assert manager.apply_all_pending(auto_backup=False) == 10

    with closing(sqlite3.connect(str(db_path))) as conn:
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        version = conn.execute(
            "SELECT version FROM schema_version ORDER BY version_id DESC LIMIT 1"
        ).fetchone()[0]

    assert "knowledge_fts" not in tables
    assert "knowledge_items_fts" in tables
    assert version == "1.2.6"


# ---------------------------------------------------------------------------
# apply_all_pending -- publication lock (C)
# ---------------------------------------------------------------------------


def test_apply_all_pending_refuses_when_publication_lock_held(tmp_path: Path) -> None:
    db_path = tmp_path / "data" / "db" / "vault.db"
    manager = MigrationManager(
        db_path, MIGRATIONS_DIR, backup_dir=tmp_path / "data" / "backups"
    )
    _apply(manager, "001_initial_schema.sql")
    before = db_path.read_bytes()

    lock_path = manager._publication_lock_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text("pid=99999\n")

    with pytest.raises(PKVRuntimeError) as exc_info:
        manager.apply_all_pending(auto_backup=False)

    assert exc_info.value.code is ErrorCode.MIGRATION_LOCKED
    assert db_path.read_bytes() == before
    assert lock_path.exists()  # 绝不删除其他进程的锁
    assert not list(db_path.parent.glob("*.migrating"))


def test_release_publication_lock_removes_own_lock(tmp_path: Path) -> None:
    """正常流程：release 只删除本实例创建的锁文件。"""
    db_path = tmp_path / "data" / "db" / "vault.db"
    manager = MigrationManager(db_path, MIGRATIONS_DIR)
    lock_path = manager._publication_lock_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    manager._acquire_publication_lock()
    assert lock_path.exists()

    manager._release_publication_lock()

    assert not lock_path.exists()


def test_release_publication_lock_refuses_externally_replaced_lock(
    tmp_path: Path,
) -> None:
    """锁路径被外部替换后 release 绝不删除他人文件，保留现场并 fail-closed。"""
    db_path = tmp_path / "data" / "db" / "vault.db"
    manager = MigrationManager(db_path, MIGRATIONS_DIR)
    lock_path = manager._publication_lock_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    manager._acquire_publication_lock()
    os.unlink(lock_path)
    lock_path.write_text("pid=99999\n")  # 外部替换为他人文件

    manager._release_publication_lock()

    # 绝不删除别人的文件，外部内容原样保留。
    assert lock_path.exists()
    assert lock_path.read_text(encoding="utf-8") == "pid=99999\n"


def test_release_publication_lock_without_identity_fails_closed(
    tmp_path: Path,
) -> None:
    """未记录锁身份时 fail-closed：不删除任何文件。"""
    db_path = tmp_path / "data" / "db" / "vault.db"
    manager = MigrationManager(db_path, MIGRATIONS_DIR)
    lock_path = manager._publication_lock_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text("pid=99999\n")

    manager._release_publication_lock()

    assert lock_path.exists()


def test_apply_all_pending_fresh_publish_never_clobbers_concurrent_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "vault.db"
    manager = MigrationManager(db_path, MIGRATIONS_DIR)
    real_publish = MigrationManager._publish_no_clobber
    concurrent_bytes = b"concurrent database owner"

    def publish_after_concurrent_create(source: Path, target: Path, **kwargs) -> None:
        target.write_bytes(concurrent_bytes)
        real_publish(source, target, **kwargs)

    monkeypatch.setattr(
        MigrationManager,
        "_publish_no_clobber",
        staticmethod(publish_after_concurrent_create),
    )

    with pytest.raises(PKVRuntimeError) as exc_info:
        manager.apply_all_pending(auto_backup=False)

    assert exc_info.value.code is ErrorCode.MIGRATION_LOCKED
    assert db_path.read_bytes() == concurrent_bytes
    assert not manager._publication_lock_path().exists()
    assert not list(db_path.parent.glob("*.migrating"))


def test_apply_all_pending_returns_zero_when_another_process_completed_chain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "vault.db"
    manager = MigrationManager(db_path, MIGRATIONS_DIR)
    real_acquire = manager._acquire_publication_lock

    def acquire_after_other_wins() -> None:
        MigrationManager(db_path, MIGRATIONS_DIR).apply_all_pending(auto_backup=False)
        real_acquire()

    monkeypatch.setattr(manager, "_acquire_publication_lock", acquire_after_other_wins)

    assert manager.apply_all_pending(auto_backup=False) == 0
    assert manager.inspect_database().state is DatabaseState.READY
    assert not manager._publication_lock_path().exists()


# ---------------------------------------------------------------------------
# apply_all_pending -- byte preservation and connection cleanup (C)
# ---------------------------------------------------------------------------


def test_apply_all_pending_failure_leaves_original_bytes_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "vault.db"
    manager = MigrationManager(db_path, MIGRATIONS_DIR)
    _apply(manager, "001_initial_schema.sql")
    before = db_path.read_bytes()

    monkeypatch.setattr(
        mm_module.SQLiteStore,
        "rebuild_fts5_index",
        lambda instance: (_ for _ in ()).throw(RuntimeError("rebuild failed")),
    )

    with pytest.raises(PKVRuntimeError) as exc_info:
        manager.apply_all_pending(auto_backup=False)

    assert exc_info.value.code is ErrorCode.MIGRATION_FAILED
    assert db_path.read_bytes() == before
    assert not list(db_path.parent.glob("*.migrating"))
    assert not manager._publication_lock_path().exists()


def test_apply_all_pending_closes_every_connection_on_success_and_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Factory-based tracker: every sqlite handle opened by apply_all_pending
    is closed exactly once on success, on rebuild failure, and on backup
    failure.  Real ``sqlite3.Connection.close`` is read-only, so tracking uses
    a ``sqlite3.connect(factory=...)`` subclass; this also covers the raw
    ``sqlite3.connect`` handles (``_connect`` missing-target branch and
    ``_copy_database`` source/destination) that the old wrapper missed.
    """
    open_connections: list[sqlite3.Connection] = []
    opened_total: list[sqlite3.Connection] = []
    close_counts: dict[sqlite3.Connection, int] = {}
    real_connect = sqlite3.connect

    class TrackingConnection(sqlite3.Connection):
        def close(self) -> None:
            close_counts[self] = close_counts.get(self, 0) + 1
            if self in open_connections:
                open_connections.remove(self)
            super().close()

    def tracked_connect(database, *args, **kwargs):
        conn = real_connect(database, *args, factory=TrackingConnection, **kwargs)
        open_connections.append(conn)
        opened_total.append(conn)
        return conn

    # 追踪所有 sqlite3.connect 调用: _connect 的 raw 分支、
    # connect_existing_sqlite 内部、_copy_database 的 source/destination。
    monkeypatch.setattr(mm_module.sqlite3, "connect", tracked_connect)

    def assert_all_closed() -> None:
        assert open_connections == []
        assert all(close_counts.get(conn, 0) == 1 for conn in opened_total)

    # 成功: fresh 链全部执行, 每个句柄恰好关闭一次。
    ok_manager = MigrationManager(tmp_path / "ok.db", MIGRATIONS_DIR)
    assert ok_manager.apply_all_pending(auto_backup=False) == 11
    assert_all_closed()
    assert ok_manager.inspect_database().state is DatabaseState.READY

    # 失败: FTS 重建失败, 工作副本/复制/检查句柄全部关闭。
    fail_manager = MigrationManager(tmp_path / "fail.db", MIGRATIONS_DIR)
    _apply(fail_manager, "001_initial_schema.sql")
    monkeypatch.setattr(
        mm_module.SQLiteStore,
        "rebuild_fts5_index",
        lambda instance: (_ for _ in ()).throw(RuntimeError("rebuild failed")),
    )
    with pytest.raises(PKVRuntimeError) as exc_info:
        fail_manager.apply_all_pending(auto_backup=False)
    assert exc_info.value.code is ErrorCode.MIGRATION_FAILED
    assert_all_closed()

    # 备份失败: 备份目标连接失败时, 已打开的源句柄必须关闭
    # (回归: _copy_database 的 source 连接曾因 destination 构造在
    # try/finally 之外而泄漏)。
    backup_manager = MigrationManager(
        tmp_path / "backup.db",
        MIGRATIONS_DIR,
        backup_dir=tmp_path / "data" / "backups",
    )
    _apply(backup_manager, "001_initial_schema.sql")
    backup_root = str(backup_manager.backup_dir)

    def destination_failing_connect(database, *args, **kwargs):
        if str(database).startswith(backup_root):
            raise sqlite3.OperationalError("backup destination locked")
        return tracked_connect(database, *args, **kwargs)

    monkeypatch.setattr(mm_module.sqlite3, "connect", destination_failing_connect)
    with pytest.raises(PKVRuntimeError) as exc_info:
        backup_manager.apply_all_pending(auto_backup=True)
    assert exc_info.value.code is ErrorCode.MIGRATION_BACKUP_FAILED
    assert_all_closed()


def test_copy_database_closes_source_when_destination_connect_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: destination-connect failure must close the already-opened
    source handle exactly once (source used to leak outside try/finally)."""
    source = tmp_path / "source.db"
    MigrationManager(source, MIGRATIONS_DIR).apply_migration(
        MIGRATIONS_DIR / "001_initial_schema.sql", auto_backup=False
    )
    destination = tmp_path / "dest.db"

    close_counts: dict[sqlite3.Connection, int] = {}
    opened: list[sqlite3.Connection] = []
    real_connect = sqlite3.connect

    class TrackingConnection(sqlite3.Connection):
        def close(self) -> None:
            close_counts[self] = close_counts.get(self, 0) + 1
            super().close()

    def tracked_connect(database, *args, **kwargs):
        if str(database) == str(destination):
            raise sqlite3.OperationalError("simulated destination connect failure")
        conn = real_connect(database, *args, factory=TrackingConnection, **kwargs)
        opened.append(conn)
        return conn

    monkeypatch.setattr(mm_module.sqlite3, "connect", tracked_connect)

    with pytest.raises(sqlite3.OperationalError):
        MigrationManager._copy_database(source, destination)

    assert len(opened) == 1  # 只有 source 句柄被打开
    assert close_counts.get(opened[0], 0) == 1  # 且恰好关闭一次


# ---------------------------------------------------------------------------
# sqlite_connection -- validate-to-open identity race (fault injection)
# ---------------------------------------------------------------------------


def _create_valid_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE t (x INTEGER)")
        conn.execute("INSERT INTO t VALUES (42)")
        conn.commit()
    finally:
        conn.close()


def test_validate_missing_db_raises_and_never_creates(tmp_path: Path) -> None:
    db = tmp_path / "missing.db"

    with pytest.raises(PKVRuntimeError) as exc_info:
        sc.validate_existing_sqlite_file(db)

    assert exc_info.value.code is ErrorCode.DATABASE_MISSING
    assert not db.exists()


def test_validate_rejects_symlink_db(tmp_path: Path) -> None:
    db = tmp_path / "vault.db"
    _create_valid_db(db)
    link = tmp_path / "link.db"
    try:
        link.symlink_to(db)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")

    with pytest.raises(PKVRuntimeError) as exc_info:
        sc.validate_existing_sqlite_file(link)

    assert exc_info.value.code is ErrorCode.DATA_ROOT_UNSAFE


def test_validate_rejects_hardlink_db(tmp_path: Path) -> None:
    db = tmp_path / "vault.db"
    _create_valid_db(db)
    hard = tmp_path / "hard.db"
    try:
        os.link(db, hard)
    except OSError as exc:
        pytest.skip(f"hard links unavailable: {exc}")

    try:
        with pytest.raises(PKVRuntimeError) as exc_info:
            sc.validate_existing_sqlite_file(hard)

        assert exc_info.value.code is ErrorCode.DATA_ROOT_UNSAFE
    finally:
        hard.unlink(missing_ok=True)


def test_validate_rejects_file_replaced_before_open_and_closes_handle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = tmp_path / "vault.db"
    other = tmp_path / "other.db"
    _create_valid_db(db)
    _create_valid_db(other)
    real_open = os.open
    real_close = os.close
    opened_fds: list[int] = []
    closed_fds: list[int] = []

    def redirecting_open(path, flags, *args, **kwargs):
        if Path(path) == db:
            path = other  # 模拟: 校验后、打开时被替换
        fd = real_open(path, flags, *args, **kwargs)
        opened_fds.append(fd)
        return fd

    def tracking_close(fd):
        closed_fds.append(fd)
        return real_close(fd)

    monkeypatch.setattr(sc.os, "open", redirecting_open)
    monkeypatch.setattr(sc.os, "close", tracking_close)

    with pytest.raises(PKVRuntimeError) as exc_info:
        sc.validate_existing_sqlite_file(db)

    assert exc_info.value.code is ErrorCode.PATH_STATE_UNDETERMINED
    assert opened_fds and all(fd in closed_fds for fd in opened_fds)


def test_validate_rejects_path_replaced_after_header_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = tmp_path / "vault.db"
    other = tmp_path / "other.db"
    _create_valid_db(db)
    _create_valid_db(other)
    real_lstat = sc.os.lstat
    original_info = real_lstat(db)
    replacement_info = real_lstat(other)
    db_lstat_calls = 0

    def changing_lstat(path):
        nonlocal db_lstat_calls
        if Path(path) == db:
            db_lstat_calls += 1
            return original_info if db_lstat_calls == 1 else replacement_info
        return real_lstat(path)

    monkeypatch.setattr(sc.os, "lstat", changing_lstat)

    with pytest.raises(PKVRuntimeError) as exc_info:
        sc.validate_existing_sqlite_file(db)

    assert exc_info.value.code is ErrorCode.PATH_STATE_UNDETERMINED


@pytest.mark.skipif(os.name == "nt", reason="O_NOFOLLOW is POSIX-only")
def test_validate_uses_no_follow_and_maps_eloop_to_unsafe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = tmp_path / "vault.db"
    _create_valid_db(db)
    real_open = os.open
    seen_flags: list[int] = []

    def no_follow_open(path, flags, *args, **kwargs):
        seen_flags.append(flags)
        if Path(path) == db:
            raise OSError(errno.ELOOP, "too many levels of symbolic links")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(sc.os, "open", no_follow_open)

    with pytest.raises(PKVRuntimeError) as exc_info:
        sc.validate_existing_sqlite_file(db)

    assert exc_info.value.code is ErrorCode.DATA_ROOT_UNSAFE
    assert seen_flags and seen_flags[0] & os.O_NOFOLLOW


def test_connect_missing_db_not_created(tmp_path: Path) -> None:
    db = tmp_path / "missing.db"

    with pytest.raises(PKVRuntimeError) as exc_info:
        sc.connect_existing_sqlite(db)

    assert exc_info.value.code is ErrorCode.DATABASE_MISSING
    assert not db.exists()


def test_connect_rejects_symlink_db_before_connect(tmp_path: Path) -> None:
    db = tmp_path / "vault.db"
    _create_valid_db(db)
    link = tmp_path / "link.db"
    try:
        link.symlink_to(db)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")

    with pytest.raises(PKVRuntimeError) as exc_info:
        sc.connect_existing_sqlite(link)

    assert exc_info.value.code is ErrorCode.DATA_ROOT_UNSAFE


def test_connect_returns_working_connection(tmp_path: Path) -> None:
    db = tmp_path / "vault.db"
    _create_valid_db(db)

    conn = sc.connect_existing_sqlite(db)
    try:
        assert conn.execute("SELECT x FROM t").fetchone() == (42,)
    finally:
        conn.close()

    ro = sc.connect_existing_sqlite(db, read_only=True)
    try:
        with pytest.raises(sqlite3.OperationalError):
            ro.execute("CREATE TABLE forbidden (x)")
    finally:
        ro.close()


def test_connect_detects_db_replaced_between_validate_and_connect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = tmp_path / "vault.db"
    other = tmp_path / "other.db"
    _create_valid_db(db)
    _create_valid_db(other)
    real_connect = sqlite3.connect
    open_connections: list[sqlite3.Connection] = []
    close_counts: dict[sqlite3.Connection, int] = {}

    class TrackingConnection(sqlite3.Connection):
        def close(self) -> None:
            close_counts[self] = close_counts.get(self, 0) + 1
            if self in open_connections:
                open_connections.remove(self)
            super().close()

    def swapping_connect(database, *args, **kwargs):
        os.replace(other, db)  # 验证后、连接前被替换
        conn = real_connect(database, *args, factory=TrackingConnection, **kwargs)
        open_connections.append(conn)
        return conn

    monkeypatch.setattr(sc.sqlite3, "connect", swapping_connect)

    with pytest.raises(PKVRuntimeError) as exc_info:
        sc.connect_existing_sqlite(db)

    assert exc_info.value.code is ErrorCode.PATH_STATE_UNDETERMINED
    assert open_connections == []  # 句柄必须已关闭
    assert close_counts and all(count == 1 for count in close_counts.values())


def test_connect_classifies_db_vanished_after_validate_as_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = tmp_path / "vault.db"
    _create_valid_db(db)
    real_connect = sqlite3.connect

    def vanishing_connect(database, *args, **kwargs):
        db.unlink()  # 验证后、连接前消失
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(sc.sqlite3, "connect", vanishing_connect)

    with pytest.raises(PKVRuntimeError) as exc_info:
        sc.connect_existing_sqlite(db)

    assert exc_info.value.code is ErrorCode.DATABASE_MISSING
    assert not db.exists()  # mode=rw 绝不重建


def test_connect_read_only_query_only_failure_closes_connection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = tmp_path / "vault.db"
    _create_valid_db(db)
    real_connect = sqlite3.connect
    open_connections: list[sqlite3.Connection] = []
    close_counts: dict[sqlite3.Connection, int] = {}

    class TrackingConnection(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            raise sqlite3.OperationalError("read-only enforcement failed")

        def close(self) -> None:
            close_counts[self] = close_counts.get(self, 0) + 1
            if self in open_connections:
                open_connections.remove(self)
            super().close()

    def tracked_connect(database, *args, **kwargs):
        conn = real_connect(database, *args, factory=TrackingConnection, **kwargs)
        open_connections.append(conn)
        return conn

    monkeypatch.setattr(sc.sqlite3, "connect", tracked_connect)

    with pytest.raises(PKVRuntimeError) as exc_info:
        sc.connect_existing_sqlite(db, read_only=True)

    assert exc_info.value.code is ErrorCode.DATABASE_NOT_SQLITE
    assert open_connections == []  # 句柄必须已关闭
    assert close_counts and all(count == 1 for count in close_counts.values())
