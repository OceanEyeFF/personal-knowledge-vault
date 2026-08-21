"""PKV runtime contracts.

This package owns the process-wide distinction between immutable bundled
resources and mutable per-user data.  Product modules must consume a
``RuntimeLayout`` instead of deriving repository-relative paths themselves.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from src.runtime.bootstrap import RuntimeContext, bootstrap_runtime
from src.runtime.errors import (
    ErrorCode,
    OperationStatus,
    PKVRuntimeError,
    StorageStage,
)
from src.runtime.layout import RuntimeLayout
from src.runtime.write_lease import VaultWriteLease, write_lease_scope


# ``src.ai.provider_factory`` imports ``src.runtime.errors``.  Importing the
# lifecycle module while this package is being initialized would in turn import
# the provider factory and create a package-level cycle for every public Kernel
# import.  Keep the lifecycle API public, but resolve it only when a caller
# actually uses a lifecycle symbol.  This is intentionally a package boundary
# fix rather than an import-order accident: ``src.runtime.errors`` must remain
# safe for low-level modules to import.
_LIFECYCLE_PUBLIC_NAMES = frozenset(
    {
        "LiveProviderProbe",
        "ProviderProbe",
        "ProviderValidation",
        "RuntimeAction",
        "RuntimeActionKind",
        "RuntimeConfirmation",
        "RuntimeExecution",
        "RuntimeInspection",
        "RuntimeIssue",
        "RuntimeIssueSeverity",
        "RuntimePlan",
        "RuntimeReadiness",
        "confirm_runtime_plan",
        "execute_runtime_plan",
        "inspect_runtime",
        "plan_runtime",
    }
)

if TYPE_CHECKING:
    from src.runtime.lifecycle import (
        LiveProviderProbe,
        ProviderProbe,
        ProviderValidation,
        RuntimeAction,
        RuntimeActionKind,
        RuntimeConfirmation,
        RuntimeExecution,
        RuntimeInspection,
        RuntimeIssue,
        RuntimeIssueSeverity,
        RuntimePlan,
        RuntimeReadiness,
        confirm_runtime_plan,
        execute_runtime_plan,
        inspect_runtime,
        plan_runtime,
    )


def __getattr__(name: str):
    """Lazily resolve lifecycle exports without weakening ``src.runtime`` API."""

    if name in _LIFECYCLE_PUBLIC_NAMES:
        from src.runtime import lifecycle

        value = getattr(lifecycle, name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    """Expose lazy lifecycle names to normal introspection tools."""

    return sorted(set(globals()) | _LIFECYCLE_PUBLIC_NAMES)

__all__ = [
    "ErrorCode",
    "LiveProviderProbe",
    "OperationStatus",
    "PKVRuntimeError",
    "ProviderProbe",
    "ProviderValidation",
    "RuntimeAction",
    "RuntimeActionKind",
    "RuntimeConfirmation",
    "RuntimeContext",
    "RuntimeExecution",
    "RuntimeInspection",
    "RuntimeIssue",
    "RuntimeIssueSeverity",
    "RuntimeLayout",
    "RuntimePlan",
    "RuntimeReadiness",
    "StorageStage",
    "VaultWriteLease",
    "bootstrap_runtime",
    "confirm_runtime_plan",
    "execute_runtime_plan",
    "inspect_runtime",
    "plan_runtime",
    "write_lease_scope",
]
