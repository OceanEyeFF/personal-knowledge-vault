"""Internal headless facade implementation for :mod:`pkv_kernel`.

External wrappers import only :mod:`pkv_kernel`.  This ``src.*`` module may
compose application/domain infrastructure, but remains implementation-private
and is not an external integration surface.
"""

from src.ai.chat_provider import (
    ChatProvider,
    ChatStream,
    is_strict_chat_stream_event,
    is_supported_chat_finish_reason,
)
from src.ai.provider_factory import ChatProviderSettings, validate_provider_base_url
from src.application.validation import validate_text_length, validate_url_security_result
from src.kernel.facade import (
    KernelChatSessions,
    KnowledgeKernel,
    bootstrap_kernel,
    configure_kernel,
    get_kernel,
    reload_kernel,
    reset_kernel,
)
from src.kernel.preview import (
    PreviewIssue,
    PreviewOutcome,
    is_strict_preview_outcome,
    load_preview_with_store,
)
from src.processors.safe_fetch import describe_url_target
from src.relations.citations import sanitize_public_source_url
from src.retrieval.result import (
    RetrievalIssue,
    SearchResponse,
    SearchResult,
    is_strict_search_response,
)
from src.runtime.bootstrap import bootstrap_runtime, project_bootstrap_error
from src.runtime.errors import ErrorCode, PKVRuntimeError
from src.runtime.layout import open_user_file_nofollow
from src.workflow.models import WorkflowResult
from src.utils.config import (
    Config,
    get_config,
    redact_url_credentials,
    set_yaml_config_values,
    url_contains_credentials,
)
from src.utils.logger import LoggerSetup

__all__ = [
    "ChatProvider",
    "ChatProviderSettings",
    "ChatStream",
    "Config",
    "ErrorCode",
    "KernelChatSessions",
    "KnowledgeKernel",
    "LoggerSetup",
    "PKVRuntimeError",
    "PreviewIssue",
    "PreviewOutcome",
    "RetrievalIssue",
    "SearchResponse",
    "SearchResult",
    "bootstrap_kernel",
    "bootstrap_runtime",
    "configure_kernel",
    "describe_url_target",
    "get_config",
    "get_kernel",
    "is_strict_chat_stream_event",
    "is_strict_search_response",
    "is_strict_preview_outcome",
    "is_supported_chat_finish_reason",
    "load_preview_with_store",
    "open_user_file_nofollow",
    "project_bootstrap_error",
    "redact_url_credentials",
    "reload_kernel",
    "reset_kernel",
    "sanitize_public_source_url",
    "set_yaml_config_values",
    "url_contains_credentials",
    "validate_provider_base_url",
    "validate_text_length",
    "validate_url_security_result",
    "WorkflowResult",
]
