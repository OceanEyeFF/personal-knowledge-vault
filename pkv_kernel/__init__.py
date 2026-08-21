"""Public Python API for the headless Personal Knowledge Vault Kernel.

Desktop, web, and automation wrappers import this package instead of the
implementation namespace.  It deliberately re-exports only the stable Kernel
facade and its value contracts; no GUI framework is imported by this package.

K1a compatibility policy:

* ``__all__`` is the complete supported package-root Python surface for API
  major 1.  ``pkv_kernel.contracts`` and ``pkv_kernel.lifecycle`` are the two
  separately frozen, supported nested contract modules and define their own
  ``__all__`` values.
* Additive symbols/capabilities are compatible; removal or incompatible change
  requires a new API major.  A symbol is deprecated through at least one
  compatible SDK version with an observable ``DeprecationWarning`` before
  removal.
* Wrappers negotiate their SDK bounds and required capability identifiers with
  :func:`require_kernel_compatibility`; they never import ``src.*``.
"""

import importlib

from . import contracts as contracts

from src import __version__
from src.ai.chat_provider import ChatStreamEvent
from src.kernel import (
    ChatProvider,
    ChatProviderSettings,
    ChatStream,
    Config,
    ErrorCode,
    KernelChatSessions,
    KnowledgeKernel,
    PKVRuntimeError,
    PreviewIssue,
    PreviewOutcome,
    RetrievalIssue,
    SearchResponse,
    SearchResult,
    bootstrap_kernel,
    configure_kernel,
    describe_url_target,
    get_config,
    get_kernel,
    is_strict_chat_stream_event,
    is_strict_preview_outcome,
    is_strict_search_response,
    is_supported_chat_finish_reason,
    load_preview_with_store,
    project_bootstrap_error,
    redact_url_credentials,
    reload_kernel,
    reset_kernel,
    sanitize_public_source_url,
    url_contains_credentials,
    validate_provider_base_url,
    validate_text_length,
    validate_url_security_result,
    WorkflowResult,
)
from src.runtime.errors import OperationStatus, StorageStage
from src.storage.coordinator import StorageOperationResult
from pkv_kernel.contracts import (
    KERNEL_API_VERSION,
    KERNEL_CAPABILITIES,
    SUPPORTED_PLATFORMS,
    SUPPORTED_PYTHON,
    KernelCapabilities,
    KernelCompatibilityError,
    get_kernel_capabilities as _get_kernel_capabilities,
    require_kernel_compatibility as _require_kernel_compatibility,
    runtime_is_supported,
)


def get_kernel_capabilities() -> KernelCapabilities:
    """Return this build's immutable public version/capability handshake."""

    return _get_kernel_capabilities(__version__)


def require_kernel_compatibility(
    *,
    minimum_sdk_version: str | None = None,
    maximum_sdk_version: str | None = None,
    required_capabilities: tuple[str, ...] | list[str] | frozenset[str] = (),
) -> KernelCapabilities:
    """Validate Wrapper requirements against the currently imported SDK."""

    return _require_kernel_compatibility(
        __version__,
        minimum_sdk_version=minimum_sdk_version,
        maximum_sdk_version=maximum_sdk_version,
        required_capabilities=required_capabilities,
    )


def __getattr__(name: str):
    """Lazily resolve the lifecycle facade without recreating Core import cycles."""

    if name == "lifecycle":
        module = importlib.import_module(".lifecycle", __name__)
        globals()[name] = module
        return module
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    """Expose the lazy, supported lifecycle module to introspection tools."""

    return sorted(set(globals()) | {"lifecycle"})

__all__ = [
    "__version__",
    "ChatProvider",
    "ChatProviderSettings",
    "ChatStream",
    "ChatStreamEvent",
    "Config",
    "ErrorCode",
    "KernelChatSessions",
    "KernelCapabilities",
    "KernelCompatibilityError",
    "KERNEL_API_VERSION",
    "KERNEL_CAPABILITIES",
    "KnowledgeKernel",
    "OperationStatus",
    "PKVRuntimeError",
    "PreviewIssue",
    "PreviewOutcome",
    "RetrievalIssue",
    "SearchResponse",
    "SearchResult",
    "StorageOperationResult",
    "StorageStage",
    "SUPPORTED_PLATFORMS",
    "SUPPORTED_PYTHON",
    "bootstrap_kernel",
    "configure_kernel",
    "contracts",
    "describe_url_target",
    "get_config",
    "get_kernel_capabilities",
    "get_kernel",
    "is_strict_chat_stream_event",
    "is_strict_preview_outcome",
    "is_strict_search_response",
    "is_supported_chat_finish_reason",
    "load_preview_with_store",
    "lifecycle",
    "project_bootstrap_error",
    "redact_url_credentials",
    "reload_kernel",
    "require_kernel_compatibility",
    "reset_kernel",
    "runtime_is_supported",
    "sanitize_public_source_url",
    "url_contains_credentials",
    "validate_provider_base_url",
    "validate_text_length",
    "validate_url_security_result",
    "WorkflowResult",
]
