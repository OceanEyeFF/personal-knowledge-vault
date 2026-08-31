"""Lease-bound runtime file logging.

``pkv.log`` is a product data-root writer.  A process may configure a delayed
handler while READY, but only a mutation that owns the same immutable runtime
snapshot and the R3 lease may cause that handler to open, rotate, or append the
file.  Reload installs a new binding; an in-flight older mutation keeps its own
binding until it exits, so its records cannot be emitted through the new
snapshot's handler.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from threading import RLock
from typing import Any, Iterator


@dataclass(frozen=True)
class RuntimeFileLogBinding:
    """The immutable layout/snapshot identity authorised to write ``pkv.log``."""

    layout: Any
    snapshot_id: str

    @property
    def path(self):
        return self.layout.log_dir / "pkv.log"


@dataclass(frozen=True)
class _ActiveRuntimeFileLog:
    binding: RuntimeFileLogBinding
    owner: str


_ACTIVE_RUNTIME_FILE_LOG: ContextVar[_ActiveRuntimeFileLog | None] = ContextVar(
    "pkv_active_runtime_file_log",
    default=None,
)
_BINDING_COUNTS: dict[RuntimeFileLogBinding, int] = {}
_BINDING_LOCK = RLock()


def runtime_file_log_binding(config: Any, *, snapshot_id: str) -> RuntimeFileLogBinding:
    """Create one binding from an already-captured immutable Config snapshot."""

    layout = getattr(config, "layout", None)
    if layout is None:
        raise TypeError("runtime file logging requires an explicit Config.layout")
    if not isinstance(snapshot_id, str) or not snapshot_id:
        raise ValueError("runtime file logging snapshot_id must be a non-empty string")
    return RuntimeFileLogBinding(layout=layout, snapshot_id=snapshot_id)


def runtime_file_log_emit_allowed(binding: RuntimeFileLogBinding) -> bool:
    """Whether this record belongs to the current lease-owning mutation."""

    current = _ACTIVE_RUNTIME_FILE_LOG.get()
    if current is None or current.binding != binding:
        return False
    from src.runtime.write_lease import has_active_write_lease

    return has_active_write_lease(binding.layout)


def runtime_file_log_binding_is_active(binding: RuntimeFileLogBinding) -> bool:
    """Return whether an in-flight task still owns this binding.

    This supports deterministic handler retirement after reload without closing
    an older handler while its shielded mutation is still draining.
    """

    with _BINDING_LOCK:
        return _BINDING_COUNTS.get(binding, 0) > 0


@contextmanager
def runtime_file_log_scope(
    binding: RuntimeFileLogBinding,
    *,
    owner: str,
) -> Iterator[None]:
    """Bind log emission to one explicit mutation owner until it completes."""

    if not isinstance(binding, RuntimeFileLogBinding):
        raise TypeError("runtime file logging requires RuntimeFileLogBinding")
    if not isinstance(owner, str) or not owner:
        raise ValueError("runtime file logging owner must be non-empty")

    token: Token[_ActiveRuntimeFileLog | None] = _ACTIVE_RUNTIME_FILE_LOG.set(
        _ActiveRuntimeFileLog(binding=binding, owner=owner)
    )
    with _BINDING_LOCK:
        _BINDING_COUNTS[binding] = _BINDING_COUNTS.get(binding, 0) + 1
    try:
        yield
    finally:
        _ACTIVE_RUNTIME_FILE_LOG.reset(token)
        with _BINDING_LOCK:
            remaining = _BINDING_COUNTS.get(binding, 0) - 1
            if remaining > 0:
                _BINDING_COUNTS[binding] = remaining
            else:
                _BINDING_COUNTS.pop(binding, None)
        # Deferred import avoids a runtime <-> logger import cycle.  It only
        # closes handlers that a later reload marked retired.
        from src.utils.logger import LoggerSetup

        LoggerSetup.retire_inactive_runtime_file_handlers()


__all__ = [
    "RuntimeFileLogBinding",
    "runtime_file_log_binding",
    "runtime_file_log_binding_is_active",
    "runtime_file_log_emit_allowed",
    "runtime_file_log_scope",
]
