"""R3 contract tests for the data-root-wide nonblocking writer lease."""

from __future__ import annotations

import asyncio
import contextvars
import errno
import io
import logging
import os
from pathlib import Path
import subprocess
import sys
import threading

import pytest

import src.runtime.write_lease as write_lease_module
from src.runtime import VaultWriteLease, write_lease_scope
from src.runtime.errors import ErrorCode, PKVRuntimeError
from src.runtime.layout import RuntimeLayout
from src.runtime.write_lease import has_active_write_lease
from src.utils.logger import LoggerSetup


_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _isolated_data_root(tmp_path: Path) -> Path:
    """Keep this subprocess contract below the runner-owned .data-test root."""

    # ``scripts/run-test.ps1`` pins pytest's ``tmp_path`` below its requested
    # ``.data-test`` root.  Keeping a test-local child prevents a prior run's
    # retained lock anchor from affecting the construction-purity assertion.
    return tmp_path / "lease-data"


def _layout(tmp_path: Path) -> RuntimeLayout:
    return RuntimeLayout.resolve(
        resources_root=_PROJECT_ROOT,
        user_data_root=_isolated_data_root(tmp_path),
        environment={},
    )


def _assert_busy(error: PKVRuntimeError, layout: RuntimeLayout) -> None:
    assert error.code is ErrorCode.WRITE_BUSY
    assert error.stage == "write_lease"
    assert error.recoverable is True
    assert str(layout.runtime_state_dir) not in str(error)
    assert str(os.getpid()) not in str(error)


def _relative_entries(root: Path) -> list[str]:
    return sorted(path.relative_to(root).as_posix() for path in root.rglob("*"))


def test_construction_is_pure_and_acquire_creates_only_runtime_lock_anchor(
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    lock_path = layout.runtime_state_dir / "write.lease"

    lease = VaultWriteLease(layout)

    assert not layout.user_data_root.exists()
    assert lease.held is False

    with lease:
        assert lease.held is True
        assert lock_path.is_file()

    assert lease.held is False
    assert lock_path.is_file()
    assert lock_path.read_bytes().startswith(b"\0")
    assert _relative_entries(layout.user_data_root) == ["runtime", "runtime/write.lease"]


def test_same_lease_reenters_but_other_task_and_thread_are_busy(tmp_path: Path) -> None:
    layout = _layout(tmp_path)

    async def task_scenario() -> PKVRuntimeError:
        with VaultWriteLease(layout) as lease:
            with lease.reenter() as nested:
                assert nested is lease
                assert lease.held is True

            async def contend() -> PKVRuntimeError:
                with pytest.raises(PKVRuntimeError) as captured:
                    with lease.reenter():
                        pass
                return captured.value

            task_error = await asyncio.create_task(contend())
            with pytest.raises(PKVRuntimeError) as independent:
                with VaultWriteLease(layout):
                    pass
            _assert_busy(independent.value, layout)
            return task_error

    task_error = asyncio.run(task_scenario())
    _assert_busy(task_error, layout)

    thread_errors: list[PKVRuntimeError] = []
    with VaultWriteLease(layout):
        def contend_from_thread() -> None:
            try:
                with VaultWriteLease(layout):
                    pass
            except PKVRuntimeError as error:
                thread_errors.append(error)

        contender = threading.Thread(target=contend_from_thread)
        contender.start()
        contender.join(timeout=10)
        assert contender.is_alive() is False

    assert len(thread_errors) == 1
    _assert_busy(thread_errors[0], layout)


def test_scope_reenters_current_task_only_and_clears_after_outer_exit(
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    same_root_layout = RuntimeLayout.resolve(
        resources_root=_PROJECT_ROOT,
        user_data_root=layout.user_data_root,
        environment={},
    )

    async def scenario() -> tuple[VaultWriteLease, PKVRuntimeError, PKVRuntimeError]:
        with write_lease_scope(layout) as outer:
            with write_lease_scope(same_root_layout) as nested:
                assert nested is outer

            async def contend_from_inherited_task() -> PKVRuntimeError:
                with pytest.raises(PKVRuntimeError) as captured:
                    with write_lease_scope(layout):
                        pass
                return captured.value

            task_error = await asyncio.create_task(contend_from_inherited_task())

            copied_context = contextvars.copy_context()
            thread_errors: list[PKVRuntimeError] = []

            def contend_from_copied_thread_context() -> None:
                try:
                    with write_lease_scope(layout):
                        pass
                except PKVRuntimeError as error:
                    thread_errors.append(error)

            contender = threading.Thread(
                target=copied_context.run,
                args=(contend_from_copied_thread_context,),
            )
            contender.start()
            contender.join(timeout=10)
            assert contender.is_alive() is False
            assert len(thread_errors) == 1
            return outer, task_error, thread_errors[0]

    previous_lease, task_error, thread_error = asyncio.run(scenario())
    _assert_busy(task_error, layout)
    _assert_busy(thread_error, layout)

    with write_lease_scope(layout) as next_lease:
        assert next_lease is not previous_lease


_HOLDER_SCRIPT = """
from pathlib import Path
import sys

from src.runtime.layout import RuntimeLayout
from src.runtime.write_lease import VaultWriteLease

layout = RuntimeLayout.resolve(
    resources_root=Path(sys.argv[1]),
    user_data_root=Path(sys.argv[2]),
    environment={},
)
lease = VaultWriteLease(layout)
try:
    lease.acquire()
    print("LEASE_HELD", flush=True)
    sys.stdin.readline()
finally:
    lease.release()
"""


def _start_holder(layout: RuntimeLayout) -> subprocess.Popen[str]:
    environment = os.environ.copy()
    existing_pythonpath = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = str(_PROJECT_ROOT) + (
        os.pathsep + existing_pythonpath if existing_pythonpath else ""
    )
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            _HOLDER_SCRIPT,
            str(_PROJECT_ROOT),
            str(layout.user_data_root),
        ],
        cwd=_PROJECT_ROOT,
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
    )
    assert holder.stdout is not None
    readiness = holder.stdout.readline().strip()
    if readiness != "LEASE_HELD":
        output, _ = holder.communicate(timeout=10)
        raise AssertionError(f"lease holder did not become ready: {readiness!r}\n{output}")
    return holder


def _release_holder(holder: subprocess.Popen[str]) -> None:
    if holder.poll() is None:
        assert holder.stdin is not None
        holder.stdin.write("\n")
        holder.stdin.flush()
    output, _ = holder.communicate(timeout=10)
    assert holder.returncode == 0, output


def test_cross_process_holder_makes_contender_busy_then_release_allows_write(
    tmp_path: Path,
) -> None:
    """A holds the OS lock, B gets write_busy, then B succeeds after release."""

    layout = _layout(tmp_path)
    lock_path = layout.runtime_state_dir / "write.lease"
    holder = _start_holder(layout)
    try:
        with pytest.raises(PKVRuntimeError) as captured:
            VaultWriteLease(layout).acquire()
        _assert_busy(captured.value, layout)
        assert _relative_entries(layout.user_data_root) == ["runtime", "runtime/write.lease"]
    finally:
        _release_holder(holder)

    with VaultWriteLease(layout) as lease:
        assert lease.held is True

    # The stable anchor is deliberately retained.  A process crash therefore
    # releases the OS advisory lock by descriptor close, never by stale-file
    # deletion.
    assert lock_path.is_file()


def test_crashed_holder_releases_os_lock_without_deleting_anchor(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    lock_path = layout.runtime_state_dir / "write.lease"
    holder = _start_holder(layout)
    try:
        holder.kill()
        output, _ = holder.communicate(timeout=10)
        assert holder.returncode not in (None, 0), output
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.communicate(timeout=10)

    # Process death drops the descriptor-owned OS advisory lock.  The stable
    # anchor remains, so a stale path alone can never mean "writer active".
    assert lock_path.is_file()
    with VaultWriteLease(layout):
        pass


def test_unsafe_lock_anchor_fails_closed_without_becoming_busy(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    lock_path = layout.runtime_state_dir / "write.lease"
    lock_path.mkdir(parents=True)

    with pytest.raises(PKVRuntimeError) as captured:
        VaultWriteLease(layout).acquire()

    error = captured.value
    assert error.code is ErrorCode.DATA_ROOT_UNSAFE
    assert error.code is not ErrorCode.WRITE_BUSY
    assert error.stage == "write_lease"
    assert error.recoverable is False
    assert str(lock_path) not in str(error)


def test_unknown_lock_error_fails_closed_and_releases_local_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = _layout(tmp_path)

    def fail_lock(_: object) -> None:
        raise OSError(errno.EIO, f"unexpected {layout.runtime_state_dir}")

    with monkeypatch.context() as scoped:
        scoped.setattr(write_lease_module, "_acquire_os_lock", fail_lock)
        with pytest.raises(PKVRuntimeError) as captured:
            VaultWriteLease(layout).acquire()

    error = captured.value
    assert error.code is ErrorCode.DATA_ROOT_UNSAFE
    assert error.code is not ErrorCode.WRITE_BUSY
    assert error.stage == "write_lease"
    assert error.recoverable is False
    assert str(layout.runtime_state_dir) not in str(error)

    # A failed unknown lock operation must not strand the in-process gate.
    with VaultWriteLease(layout):
        pass


def test_context_releases_when_cancellation_unwinds(tmp_path: Path) -> None:
    layout = _layout(tmp_path)

    async def cancelled_operation() -> None:
        with VaultWriteLease(layout):
            raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(cancelled_operation())

    with VaultWriteLease(layout):
        pass


def test_tracked_worker_reentry_is_explicit_not_ambient_context(tmp_path: Path) -> None:
    """Only the runtime runner grants a worker nested access to its parent lease."""

    layout = _layout(tmp_path)

    async def scenario() -> PKVRuntimeError:
        with write_lease_scope(layout) as outer:
            def ordinary_worker() -> PKVRuntimeError:
                with pytest.raises(PKVRuntimeError) as captured:
                    with write_lease_scope(layout):
                        pass
                return captured.value

            # ``asyncio.to_thread`` copies ContextVars by default.  That copied
            # state alone must stay insufficient for a nested writer scope.
            ordinary_error = await asyncio.to_thread(ordinary_worker)

            def authorized_worker() -> VaultWriteLease:
                with write_lease_scope(layout) as nested:
                    return nested

            nested = await write_lease_module._run_tracked_write_worker(
                layout,
                authorized_worker,
            )
            assert nested is outer
            return ordinary_error

    error = asyncio.run(scenario())
    _assert_busy(error, layout)


def test_tracked_worker_keeps_physical_lease_after_owner_cancellation(
    tmp_path: Path,
) -> None:
    """Cancellation cannot release a lease while its durable worker is still alive."""

    layout = _layout(tmp_path)
    started = threading.Event()
    allow_finish = threading.Event()
    finished = threading.Event()

    async def operation() -> None:
        with write_lease_scope(layout):
            def durable_worker() -> None:
                started.set()
                assert allow_finish.wait(timeout=10)
                finished.set()

            await write_lease_module._run_tracked_write_worker(
                layout,
                durable_worker,
            )

    async def scenario() -> None:
        task = asyncio.create_task(operation())
        assert await asyncio.to_thread(started.wait, 10)

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        with pytest.raises(PKVRuntimeError) as captured:
            with VaultWriteLease(layout):
                pass
        _assert_busy(captured.value, layout)

        allow_finish.set()
        assert await asyncio.to_thread(finished.wait, 10)
        # The executor completion callback releases the deferred physical lease
        # on the event loop that owns the operation task.
        await asyncio.sleep(0)

        with VaultWriteLease(layout):
            pass

    asyncio.run(scenario())


def test_persistent_logger_is_inert_without_write_lease_then_writes_inside_one(
    tmp_path: Path,
) -> None:
    """A read log cannot create ``pkv.log``; a covered mutation can record it."""

    layout = _layout(tmp_path)
    log_path = layout.log_dir / "pkv.log"
    root_logger = logging.getLogger()
    original_handlers = list(root_logger.handlers)
    original_level = root_logger.level
    original_initialized = LoggerSetup._initialized
    try:
        LoggerSetup._initialized = False
        LoggerSetup.setup(
            level="INFO",
            log_file=log_path,
            path_validator=layout.writable_user_path,
            console_stream=io.StringIO(),
            delay=True,
            create_parent=False,
            emit_guard=lambda: has_active_write_lease(layout),
        )

        root_logger.info("read-path diagnostic")
        assert not log_path.exists()
        assert not layout.log_dir.exists()

        with write_lease_scope(layout):
            # Bootstrap/setup owns directory creation.  The logger itself must
            # not make an unleased parent directory as a side effect of setup.
            layout.ensure_user_directories()
            root_logger.info("covered mutation")

        assert log_path.is_file()
        assert "covered mutation" in log_path.read_text(encoding="utf-8")
        assert "read-path diagnostic" not in log_path.read_text(encoding="utf-8")
    finally:
        for handler in root_logger.handlers:
            handler.close()
        root_logger.handlers[:] = original_handlers
        root_logger.setLevel(original_level)
        LoggerSetup._initialized = original_initialized
