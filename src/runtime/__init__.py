"""PKV runtime contracts.

This package owns the process-wide distinction between immutable bundled
resources and mutable per-user data.  Product modules must consume a
``RuntimeLayout`` instead of deriving repository-relative paths themselves.
"""

from src.runtime.errors import (
    ErrorCode,
    OperationStatus,
    PKVRuntimeError,
    StorageStage,
)
from src.runtime.layout import RuntimeLayout
from src.runtime.bootstrap import RuntimeContext, bootstrap_runtime

__all__ = [
    "ErrorCode",
    "OperationStatus",
    "PKVRuntimeError",
    "RuntimeLayout",
    "RuntimeContext",
    "StorageStage",
    "bootstrap_runtime",
]
