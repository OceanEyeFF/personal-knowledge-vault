"""Production provider construction seams shared by GUI, CLI and MCP.

The factory accepts immutable snapshots instead of a mutable ``Config``
object.  Tests may replace the factory callable, while release code always
uses the same OpenAI-compatible construction path.
"""

from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import math
from typing import Any, Callable, Protocol, TypeVar
from urllib.parse import urlsplit

from src.runtime.errors import ErrorCode, PKVRuntimeError


OPENAI_COMPATIBLE = "openai_compatible"

_MAX_PROVIDER_TIMEOUT_SECONDS = 600.0
_MAX_PROVIDER_RETRIES = 10
_MAX_CHAT_TOKENS = 1_000_000
_MAX_EMBEDDING_DIMENSIONS = 65_536
_MAX_PROVIDER_USAGE_COUNT = 1_000_000_000
_MISSING = object()


@dataclass(frozen=True)
class EmbeddingProviderSettings:
    provider: str
    api_key: str
    base_url: str
    model: str
    dimensions: int | None
    timeout_seconds: float
    max_retries: int


@dataclass(frozen=True)
class ChatProviderSettings:
    provider: str
    api_key: str
    base_url: str
    model: str
    max_tokens: int
    temperature: float
    timeout_seconds: float
    max_retries: int


class ConfigLike(Protocol):
    llm_provider: str
    llm_api_key: str | None
    llm_base_url: str
    llm_model: str
    llm_max_tokens: int
    llm_temperature: float
    llm_timeout_seconds: float
    llm_max_retries: int
    embd_provider: str
    embd_api_key: str | None
    embd_base_url: str
    embd_model: str
    embedding_dim: int | None
    embd_timeout_seconds: float
    embd_max_retries: int


def embedding_settings_from_config(config: ConfigLike) -> EmbeddingProviderSettings:
    try:
        raw_dimensions = _config_value(
            config,
            "ai.embedding.dim",
            "embedding_dim",
            1536,
        )
        if isinstance(raw_dimensions, str) and raw_dimensions.strip().lower() == "auto":
            raw_dimensions = config.embedding_dim
        settings = EmbeddingProviderSettings(
            provider=_config_value(
                config,
                "ai.embedding.provider",
                "embd_provider",
                OPENAI_COMPATIBLE,
            ),
            api_key=_config_value(
                config,
                "ai.embedding.api_key",
                "embd_api_key",
                None,
            ),
            base_url=_config_value(
                config,
                "ai.embedding.base_url",
                "embd_base_url",
                "https://api.openai.com/v1",
            ),
            model=_config_value(
                config,
                "ai.embedding.model",
                "embd_model",
                "text-embedding-3-small",
            ),
            dimensions=raw_dimensions,
            timeout_seconds=_config_value(
                config,
                "ai.embedding.timeout_seconds",
                "embd_timeout_seconds",
                30.0,
            ),
            max_retries=_config_value(
                config,
                "ai.embedding.max_retries",
                "embd_max_retries",
                3,
            ),
        )
        validate_embedding_provider_settings(settings)
        return settings
    except PKVRuntimeError:
        raise
    except (TypeError, ValueError, OverflowError) as exc:
        _invalid_from_exception(exc)


def chat_settings_from_config(config: ConfigLike) -> ChatProviderSettings:
    try:
        settings = ChatProviderSettings(
            provider=_config_value(
                config,
                "ai.llm.provider",
                "llm_provider",
                OPENAI_COMPATIBLE,
            ),
            api_key=_config_value(
                config,
                "ai.llm.api_key",
                "llm_api_key",
                None,
            ),
            base_url=_config_value(
                config,
                "ai.llm.base_url",
                "llm_base_url",
                "https://api.deepseek.com/v1",
            ),
            model=_config_value(
                config,
                "ai.llm.model",
                "llm_model",
                "deepseek-chat",
            ),
            max_tokens=_config_value(
                config,
                "ai.llm.max_tokens",
                "llm_max_tokens",
                2000,
            ),
            temperature=_config_value(
                config,
                "ai.llm.temperature",
                "llm_temperature",
                0.7,
            ),
            timeout_seconds=_config_value(
                config,
                "ai.llm.timeout_seconds",
                "llm_timeout_seconds",
                30.0,
            ),
            max_retries=_config_value(
                config,
                "ai.llm.max_retries",
                "llm_max_retries",
                2,
            ),
        )
        validate_chat_provider_settings(settings)
        return settings
    except PKVRuntimeError:
        raise
    except (TypeError, ValueError, OverflowError) as exc:
        _invalid_from_exception(exc)


T = TypeVar("T")


def create_embedding_client(
    settings: EmbeddingProviderSettings,
    *,
    client_type: Callable[..., T] | None = None,
    dimension_sink: Callable[[int], None] | None = None,
) -> T:
    """Construct the configured embedding client with explicit keywords."""

    validate_embedding_provider_settings(settings)
    if dimension_sink is not None and not callable(dimension_sink):
        _invalid("Embedding dimension sink 必须可调用")
    if client_type is None:
        from src.ai.openai_client import OpenAIClient

        return OpenAIClient(  # type: ignore[return-value]
            settings=settings,
            dimension_sink=dimension_sink,
        )
    client_kwargs: dict[str, Any] = dict(
        api_key=settings.api_key,
        base_url=settings.base_url,
        model=settings.model,
        dimensions=settings.dimensions,
        timeout=settings.timeout_seconds,
        max_retries=settings.max_retries,
    )
    if dimension_sink is not None:
        client_kwargs["dimension_sink"] = dimension_sink
    return client_type(**client_kwargs)


def create_embedder(
    config: ConfigLike,
    *,
    client_type: Callable[..., Any] | None = None,
):
    """Build the production ``Embedder`` through the frozen provider seam."""

    from src.ai.embedder import Embedder

    settings = embedding_settings_from_config(config)
    dimension_sink: Callable[[int], None] | None = None
    if settings.dimensions is None:
        try:
            auto_dimensions = getattr(config, "embedding_dim_is_auto", False)
        except Exception as exc:
            _invalid_from_exception(exc)
        if auto_dimensions is True:
            try:
                candidate_sink = getattr(
                    config,
                    "set_runtime_embedding_dim",
                    None,
                )
            except Exception as exc:
                _invalid_from_exception(exc)
            if not callable(candidate_sink):
                _invalid("Embedding auto 维度缺少运行期持久化接口")
            dimension_sink = candidate_sink
    client = create_embedding_client(
        settings,
        client_type=client_type,
        dimension_sink=dimension_sink,
    )
    return Embedder(openai_client=client)


def validate_embedding_provider_settings(
    settings: EmbeddingProviderSettings,
) -> None:
    """Validate an embedding snapshot, including direct dataclass callers."""

    _validate_common_settings(settings)
    if settings.dimensions is not None:
        _require_integer(settings.dimensions, "Embedding dimensions")
        if not 1 <= settings.dimensions <= _MAX_EMBEDDING_DIMENSIONS:
            _invalid(f"Embedding dimensions 必须位于 [1, {_MAX_EMBEDDING_DIMENSIONS}]")


def validate_chat_provider_settings(settings: ChatProviderSettings) -> None:
    """Validate a chat snapshot, including direct dataclass callers."""

    _validate_common_settings(settings)
    _require_integer(settings.max_tokens, "LLM max_tokens")
    if not 1 <= settings.max_tokens <= _MAX_CHAT_TOKENS:
        _invalid(f"LLM max_tokens 必须位于 [1, {_MAX_CHAT_TOKENS}]")
    _require_real(settings.temperature, "LLM temperature")
    if (
        not _is_finite_real(settings.temperature)
        or not 0.0 <= settings.temperature <= 2.0
    ):
        _invalid("LLM temperature 必须是位于 [0, 2] 的有限数")


def validate_provider_name(provider: Any) -> None:
    """Accept only the sole release provider identifier, with exact typing."""

    if type(provider) is not str or provider != OPENAI_COMPATIBLE:
        _invalid("不支持的 Provider 类型")


def safe_provider_usage_count(value: Any) -> int | None:
    """Project untrusted provider usage metadata onto a bounded integer."""

    if type(value) is int and 0 <= value <= _MAX_PROVIDER_USAGE_COUNT:
        return value
    return None


def _validate_common_settings(
    settings: EmbeddingProviderSettings | ChatProviderSettings,
) -> None:
    validate_provider_name(settings.provider)
    _require_text(settings.api_key, "Provider API Key")
    _require_text(settings.model, "Provider model")
    _require_text(settings.base_url, "Provider base_url")
    _require_real(settings.timeout_seconds, "Provider timeout_seconds")
    if (
        not _is_finite_real(settings.timeout_seconds)
        or not 0 < settings.timeout_seconds <= _MAX_PROVIDER_TIMEOUT_SECONDS
    ):
        _invalid(
            "Provider timeout_seconds 必须是位于 "
            f"(0, {_MAX_PROVIDER_TIMEOUT_SECONDS}] 的有限数"
        )
    _require_integer(settings.max_retries, "Provider max_retries")
    if not 0 <= settings.max_retries <= _MAX_PROVIDER_RETRIES:
        _invalid(f"Provider max_retries 必须位于 [0, {_MAX_PROVIDER_RETRIES}]")
    validate_provider_base_url(settings.base_url)


def validate_provider_base_url(base_url: str) -> None:
    """Validate the transport boundary used by every provider client.

    Remote endpoints must use HTTPS.  Plain HTTP is reserved for explicit
    numeric loopback addresses so an external local harness remains usable
    without making hostname resolution part of the trust decision.
    """

    if type(base_url) is not str or not base_url:
        _invalid("Provider base_url 必须是非空字符串")
    if base_url != base_url.strip() or any(
        ord(character) <= 0x20 or ord(character) == 0x7F for character in base_url
    ):
        _invalid("Provider base_url 格式无效")
    try:
        parsed = urlsplit(base_url)
        hostname = parsed.hostname
        port = parsed.port
    except (TypeError, ValueError, OverflowError):
        _invalid("Provider base_url 格式无效")
        return
    if parsed.scheme not in {"http", "https"} or not hostname:
        _invalid("Provider base_url 必须是有效的 http/https URL")
    if port is not None and not 1 <= port <= 65_535:
        _invalid("Provider base_url 端口必须位于 [1, 65535]")
    if parsed.username is not None or parsed.password is not None:
        _invalid("Provider base_url 不得包含认证信息")
    if parsed.scheme == "http" and not _is_numeric_loopback(hostname):
        _invalid("远程 Provider 必须使用 HTTPS；HTTP 仅允许数字 loopback 地址")


# Compatibility for any internal caller that imported the original helper.
_validate_provider_url = validate_provider_base_url


def _config_value(
    config: ConfigLike,
    path: str,
    attribute: str,
    default: Any,
) -> Any:
    """Read real ``Config`` values before coercing properties hide bad input."""

    getter = getattr(config, "get", None)
    if callable(getter):
        value = getter(path, _MISSING)
        if value is not _MISSING:
            return value
        return default
    return getattr(config, attribute)


def _require_text(value: Any, label: str) -> None:
    if type(value) is not str or not value.strip():
        _invalid(f"{label} 必须是非空字符串")


def _require_integer(value: Any, label: str) -> None:
    if type(value) is not int:
        _invalid(f"{label} 必须是整数")


def _require_real(value: Any, label: str) -> None:
    if type(value) not in {int, float}:
        _invalid(f"{label} 必须是数字")


def _is_finite_real(value: int | float) -> bool:
    # Python ints are mathematically finite; passing a huge int to
    # math.isfinite first attempts a float conversion and can overflow.
    return type(value) is int or math.isfinite(value)


def _is_numeric_loopback(hostname: str) -> bool:
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _invalid(message: str) -> None:
    raise PKVRuntimeError(
        ErrorCode.PROVIDER_CONFIG_INVALID,
        message,
        stage="provider_configuration",
        recoverable=True,
    )


def _invalid_from_exception(exc: Exception) -> None:
    raise PKVRuntimeError(
        ErrorCode.PROVIDER_CONFIG_INVALID,
        "Provider 配置值类型无效",
        stage="provider_configuration",
        recoverable=True,
    ) from exc
