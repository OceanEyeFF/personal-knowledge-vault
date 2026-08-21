"""Data-root-wide, nonblocking writer lease.

``VaultWriteLease`` is the runtime foundation for PKV's single-writer / many-
reader contract.  It deliberately uses an OS advisory lock on the stable
``<data-root>/runtime/write.lease`` file as the cross-process authority.  The
file is an opaque lock anchor, not an ownership record: no PID, path, or
holder metadata is persisted, and release intentionally never unlinks it.
That lets the operating system release a crashed process's advisory lock
without treating a stale file's existence as a lock.

Acquisition is synchronous and nonblocking.  A process-local gate closes the
advisory-lock semantics gap for concurrent threads/tasks in one process.  A
lease may be nested only through the same ``VaultWriteLease`` instance and the
same executing thread/task::

    with VaultWriteLease(layout) as lease:
        with lease.reenter():
            write_more()

Do not pass that instance to another thread/task as a re-entrancy token; it
will receive the same ``write_busy`` error as an independent writer.

Cancellation note: a normal ``with`` unwind (including task cancellation)
releases the lease when no tracked worker remains.  A worker started through
the private runtime helper defers physical release until its executor future
settles.  Product code must still keep the logical operation alive until every
durable worker has completed; ordinary ``asyncio.to_thread`` inheritance never
grants this private re-entry authority.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar, copy_context
import errno
import os
from pathlib import Path
import threading
from typing import TYPE_CHECKING, Any, BinaryIO, Iterator, Self, TypeVar

from src.runtime.errors import ErrorCode, PKVRuntimeError
from src.runtime.layout import ensure_safe_directory, verify_fd_matches_path

if TYPE_CHECKING:
    from src.runtime.layout import RuntimeLayout


_LOCK_FILENAME = "write.lease"
_LOCK_ANCHOR = b"\0"
_STAGE = "write_lease"
_WRITE_BUSY_MESSAGE = "知识库当前有其他写入操作"
_LOCK_UNAVAILABLE_MESSAGE = "无法安全建立知识库写入锁"

# Advisory locks are commonly process-scoped.  This gate is intentionally
# keyed by the validated canonical lock path so a second thread/task in the
# same Python process cannot be admitted merely because the kernel sees the
# same process as the current holder.
_LOCAL_GATES_GUARD = threading.Lock()
_LOCAL_GATES: dict[str, threading.Lock] = {}

_Owner = tuple[int, int | None]
_T = TypeVar("_T")


def _current_owner() -> _Owner:
    """Return the executing thread plus asyncio task identity when available."""

    try:
        task = asyncio.current_task()
    except RuntimeError:
        task = None
    return (threading.get_ident(), id(task) if task is not None else None)


def _busy_error() -> PKVRuntimeError:
    """Build the stable, deliberately non-identifying busy failure."""

    return PKVRuntimeError(
        ErrorCode.WRITE_BUSY,
        _WRITE_BUSY_MESSAGE,
        stage=_STAGE,
        recoverable=True,
    )


def _unavailable_error() -> PKVRuntimeError:
    """Fail closed without projecting an OS error, holder, or filesystem path."""

    return PKVRuntimeError(
        ErrorCode.DATA_ROOT_UNSAFE,
        _LOCK_UNAVAILABLE_MESSAGE,
        stage=_STAGE,
        recoverable=False,
    )


def _normalized_lock_key(lock_path: Path) -> str:
    return os.path.normcase(os.path.abspath(os.path.normpath(os.fspath(lock_path))))


def _local_gate_for(lock_path: Path) -> threading.Lock:
    key = _normalized_lock_key(lock_path)
    with _LOCAL_GATES_GUARD:
        gate = _LOCAL_GATES.get(key)
        if gate is None:
            gate = threading.Lock()
            _LOCAL_GATES[key] = gate
        return gate


def _is_lock_contention(error: OSError) -> bool:
    """Recognize only the platform's documented nonblocking-lock conflicts."""

    if error.errno in {errno.EAGAIN, errno.EWOULDBLOCK}:
        return True
    if os.name == "nt":
        # ``msvcrt.locking(LK_NBLCK)`` reports a sharing/lock violation as
        # ``EACCES`` on some Python/Windows combinations, sometimes with a
        # Windows error number retained.  The file has already opened safely,
        # so this branch only classifies the locking call itself.
        return error.errno == errno.EACCES or getattr(error, "winerror", None) in {
            32,
            33,
        }
    return False


def _acquire_os_lock(lock_file: BinaryIO) -> None:
    """Take the one-byte advisory lock without waiting."""

    lock_file.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _release_os_lock(lock_file: BinaryIO) -> None:
    """Release the advisory lock; closing the file is a final OS-level fallback."""

    lock_file.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _safe_lease_path(layout: RuntimeLayout) -> Path:
    """Validate the declared runtime directory before any filesystem mutation."""

    try:
        runtime_dir = layout.validate_user_directory(
            layout.runtime_state_dir,
            label="知识库写入锁目录",
            allow_missing=True,
        )
        return runtime_dir / _LOCK_FILENAME
    except Exception as error:
        raise _unavailable_error() from error


def _open_lock_file(layout: RuntimeLayout, lock_path: Path) -> BinaryIO:
    """Open one safe opaque lock anchor, creating no unrelated runtime paths."""

    try:
        runtime_dir = lock_path.parent
        ensure_safe_directory(runtime_dir, label="知识库写入锁目录")
        safe_path = layout.writable_user_path(lock_path, label="知识库写入锁文件")
        lock_file = layout.open_user_file(
            safe_path,
            "a+b",
            label="知识库写入锁文件",
        )
        try:
            # Windows byte-range locks require a byte at the locked offset.
            # Concurrent first openers may append more than one anchor byte;
            # that is harmless because the file is opaque and byte zero remains
            # the sole advisory-lock region.  We intentionally do not serialize
            # ownership metadata in this file.
            lock_file.seek(0, os.SEEK_END)
            if lock_file.tell() == 0:
                lock_file.write(_LOCK_ANCHOR)
                lock_file.flush()
                os.fsync(lock_file.fileno())
            verify_fd_matches_path(
                lock_file.fileno(),
                safe_path,
                label="知识库写入锁文件",
            )
            lock_file.seek(0)
            return lock_file
        except BaseException:
            lock_file.close()
            raise
    except Exception as error:
        raise _unavailable_error() from error


class _WorkerReentryCapability:
    """One-shot private authority for one tracked durable worker.

    This is deliberately not installed in its owner's task ContextVar state.
    Only the private worker runner below installs it immediately inside the
    executor thread.  Copying an application's ordinary ContextVar state to a
    new task or thread therefore never grants worker re-entry.
    """

    def __init__(self, lease: "VaultWriteLease") -> None:
        self._lease = lease
        self._owner: _Owner | None = None
        self._active = False
        self._depth = 0


class VaultWriteLease:
    """One nonblocking, data-root-scoped writer lease.

    Constructing this object is pure.  The first ``acquire()``/``with`` creates
    only the safe runtime lock anchor when it does not exist.  ``release()`` is
    idempotent after a completed release, but only the acquiring thread/task
    may release a live lease.
    """

    def __init__(self, layout: RuntimeLayout) -> None:
        self._layout = layout
        self._depth = 0
        self._owner: _Owner | None = None
        self._gate: threading.Lock | None = None
        self._lock_file: BinaryIO | None = None
        self._worker_capabilities: set[_WorkerReentryCapability] = set()
        self._release_pending = False

    @property
    def held(self) -> bool:
        """Whether this instance currently owns its local and OS lock layers."""

        return self._depth > 0 or self._release_pending

    def acquire(self) -> Self:
        """Acquire immediately or raise a typed ``write_busy`` runtime error."""

        owner = _current_owner()
        if self._release_pending:
            # A tracked worker is still completing after its owner unwound.  It
            # continues to hold the physical lease, but cannot be re-acquired
            # as a fresh logical operation.
            raise _busy_error()
        if self._depth > 0:
            if self._owner != owner:
                raise _busy_error()
            self._depth += 1
            return self

        lock_path = _safe_lease_path(self._layout)
        gate = _local_gate_for(lock_path)
        if not gate.acquire(blocking=False):
            raise _busy_error()

        lock_file: BinaryIO | None = None
        os_locked = False
        try:
            lock_file = _open_lock_file(self._layout, lock_path)
            try:
                _acquire_os_lock(lock_file)
            except OSError as error:
                if _is_lock_contention(error):
                    raise _busy_error() from None
                raise _unavailable_error() from error
            os_locked = True
            try:
                verify_fd_matches_path(
                    lock_file.fileno(),
                    lock_path,
                    label="知识库写入锁文件",
                )
            except Exception as error:
                raise _unavailable_error() from error
        except BaseException:
            if os_locked and lock_file is not None:
                try:
                    _release_os_lock(lock_file)
                except OSError:
                    pass
            if lock_file is not None:
                try:
                    lock_file.close()
                except OSError:
                    pass
            gate.release()
            raise

        self._owner = owner
        self._depth = 1
        self._gate = gate
        self._lock_file = lock_file
        return self

    def reenter(self) -> Self:
        """Return this lease for an explicitly nested scope owned by the caller.

        Use ``with lease.reenter():`` inside a live root lease.  Re-entrancy is
        intentionally rejected for another task/thread even if it was handed
        the same Python object.
        """

        if self._depth <= 0:
            raise RuntimeError("write lease is not held")
        if self._owner != _current_owner():
            raise _busy_error()
        return self

    def _issue_worker_capability(self) -> _WorkerReentryCapability:
        """Authorize one known executor worker from the owning task only."""

        if self._depth <= 0 or self._owner != _current_owner():
            raise _busy_error()
        capability = _WorkerReentryCapability(self)
        self._worker_capabilities.add(capability)
        return capability

    def _activate_worker_capability(
        self,
        capability: _WorkerReentryCapability,
    ) -> None:
        if (
            capability._lease is not self
            or capability not in self._worker_capabilities
            or capability._active
            or not self.held
        ):
            raise _busy_error()
        capability._owner = _current_owner()
        capability._active = True

    @contextmanager
    def _reenter_from_worker(
        self,
        capability: _WorkerReentryCapability,
    ) -> Iterator[Self]:
        """Permit only a registered worker to nest its own write scope.

        This does not alter the physical lease depth: its owner remains the
        outer application task, and release is deferred until the registered
        worker has actually completed.
        """

        if (
            capability._lease is not self
            or capability not in self._worker_capabilities
            or not capability._active
            or capability._owner != _current_owner()
            or not self.held
        ):
            raise _busy_error()
        capability._depth += 1
        try:
            yield self
        finally:
            capability._depth -= 1

    def _complete_worker_capability(
        self,
        capability: _WorkerReentryCapability,
    ) -> None:
        """Consume a worker authority only after its executor future settles."""

        if capability._lease is not self or capability not in self._worker_capabilities:
            return
        capability._active = False
        capability._owner = None
        self._worker_capabilities.remove(capability)
        if self._release_pending and not self._worker_capabilities:
            self._finalize_release()

    def _finalize_release(self) -> None:
        """Drop the OS/local lock layers once no tracked worker remains."""

        lock_file = self._lock_file
        gate = self._gate
        self._owner = None
        self._lock_file = None
        self._gate = None
        self._release_pending = False

        release_error: OSError | None = None
        try:
            if lock_file is not None:
                try:
                    _release_os_lock(lock_file)
                except OSError as error:
                    release_error = error
                try:
                    lock_file.close()
                except OSError as error:
                    if release_error is None:
                        release_error = error
        finally:
            if gate is not None:
                gate.release()

        if release_error is not None:
            raise _unavailable_error() from release_error

    def release(self) -> None:
        """Release one nested scope and the physical lock at the outer boundary."""

        if self._depth <= 0:
            return
        if self._owner != _current_owner():
            raise RuntimeError("write lease must be released by its acquiring task")

        self._depth -= 1
        if self._depth > 0:
            return

        if self._worker_capabilities:
            # ``asyncio`` cancellation may unwind the owner before an executor
            # thread finishes its durable suboperation.  Keep the OS lock until
            # that thread's completion callback consumes its explicit
            # capability; no other writer can enter the interim state.
            self._release_pending = True
            return

        self._finalize_release()

    close = release

    def __enter__(self) -> Self:
        return self.acquire()

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> bool:
        del exc_type, exc, traceback
        self.release()
        return False


_ACTIVE_WRITE_LEASES: ContextVar[dict[str, VaultWriteLease]] = ContextVar(
    "pkv_active_write_leases",
    default={},
)
_ACTIVE_WRITE_WORKERS: ContextVar[dict[str, _WorkerReentryCapability]] = ContextVar(
    "pkv_active_write_workers",
    default={},
)


async def _run_tracked_write_worker(
    layout: RuntimeLayout,
    operation: Callable[..., _T],
    /,
    *args: Any,
    **kwargs: Any,
) -> _T:
    """Run one known internal durable worker under an explicit lease authority.

    This helper is intentionally private to runtime/application implementation.
    It requires an already-active ``write_lease_scope`` owned by the caller;
    unlike ordinary ``asyncio.to_thread``, it installs a one-shot capability
    only inside the spawned worker immediately around ``operation``.  A copied
    ContextVar in an arbitrary task/thread is insufficient for re-entry.

    The executor future is shielded.  If the caller is cancelled, the worker is
    allowed to finish and its completion callback consumes the capability.  If
    the outer scope unwinds first, ``VaultWriteLease`` defers physical release
    until that callback, preserving the data-root single-writer contract.
    """

    lock_path = _safe_lease_path(layout)
    key = _normalized_lock_key(lock_path)
    lease = _ACTIVE_WRITE_LEASES.get().get(key)
    if lease is None:
        raise RuntimeError("tracked write worker requires an active write lease")
    capability = lease._issue_worker_capability()
    parent_context = copy_context()

    def invoke() -> _T:
        def run_operation() -> _T:
            lease._activate_worker_capability(capability)
            active_workers = _ACTIVE_WRITE_WORKERS.get()
            token = _ACTIVE_WRITE_WORKERS.set(active_workers | {key: capability})
            try:
                return operation(*args, **kwargs)
            finally:
                _ACTIVE_WRITE_WORKERS.reset(token)

        return parent_context.run(run_operation)

    loop = asyncio.get_running_loop()
    try:
        future = loop.run_in_executor(None, invoke)
    except BaseException:
        lease._complete_worker_capability(capability)
        raise
    future.add_done_callback(
        lambda _: lease._complete_worker_capability(capability)
    )
    return await asyncio.shield(future)


@contextmanager
def write_lease_scope(layout: RuntimeLayout) -> Iterator[VaultWriteLease]:
    """Enter the current logical write scope for ``layout``.

    Product mutation boundaries should use this helper rather than constructing
    an independent lease at every storage/config call.  It publishes the outer
    lease in a ``ContextVar`` keyed by the normalized runtime lock path, then
    re-enters that exact instance for nested work in the same task/thread.
    ``asyncio.create_task`` inherits the ContextVar value, but the lease owner
    check still rejects that new task with ``write_busy``; a thread receives the
    same result whether or not its context was explicitly copied.

    ContextVar state is removed before the physical outer lease is released,
    so later operations obtain a fresh lease and cannot accidentally reuse a
    completed logical operation.
    """

    lock_path = _safe_lease_path(layout)
    key = _normalized_lock_key(lock_path)
    active_leases = _ACTIVE_WRITE_LEASES.get()
    active_lease = active_leases.get(key)
    if active_lease is not None:
        worker_capability = _ACTIVE_WRITE_WORKERS.get().get(key)
        if worker_capability is not None:
            with active_lease._reenter_from_worker(worker_capability) as lease:
                yield lease
            return
        with active_lease.reenter() as lease:
            yield lease
        return

    with VaultWriteLease(layout) as lease:
        token = _ACTIVE_WRITE_LEASES.set(active_leases | {key: lease})
        try:
            yield lease
        finally:
            _ACTIVE_WRITE_LEASES.reset(token)


def has_active_write_lease(layout: RuntimeLayout) -> bool:
    """Return whether this task/thread may append mutation-owned runtime logs.

    This is intentionally narrower than merely finding a physical lease for the
    same process.  A copied ``ContextVar`` in an unrelated task or thread is not
    sufficient: the caller must be the owning task/thread or the one private
    tracked worker capability.  It lets operational logging remain console-only
    for reads while a file handler safely persists records emitted by a supported
    mutation already protected by the root lease.
    """

    try:
        key = _normalized_lock_key(_safe_lease_path(layout))
    except PKVRuntimeError:
        return False
    lease = _ACTIVE_WRITE_LEASES.get().get(key)
    if lease is None or not lease.held:
        return False
    worker = _ACTIVE_WRITE_WORKERS.get().get(key)
    if worker is not None:
        return (
            worker._lease is lease
            and worker in lease._worker_capabilities
            and worker._active
            and worker._owner == _current_owner()
        )
    return lease._owner == _current_owner()


__all__ = ["VaultWriteLease", "has_active_write_lease", "write_lease_scope"]
