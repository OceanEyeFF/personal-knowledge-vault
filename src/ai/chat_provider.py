"""Production OpenAI-compatible streaming chat provider.

External wrappers consume this small protocol instead of constructing an SDK client
itself.  Keeping provider construction behind this seam makes the request
configuration immutable and lets W3/W4 point the normal release path at an
external loopback provider without adding an alternate runtime branch.
"""

from __future__ import annotations

from dataclasses import dataclass
import inspect
from typing import (
    Any,
    AsyncIterator,
    Mapping,
    Protocol,
    Sequence,
    runtime_checkable,
)

from src.ai.openai_client import split_openai_transport_url
from src.ai.provider_factory import (
    ChatProviderSettings,
    safe_provider_usage_count,
    validate_chat_provider_settings,
)
from src.runtime.errors import ErrorCode, PKVRuntimeError
from src.utils.config import suppress_unsafe_http_transport_logs

_MISSING = object()
_CHAT_FINISH_REASONS = frozenset({"stop", "length", "content_filter"})


def is_supported_chat_finish_reason(value: Any) -> bool:
    """Return whether ``value`` is an exact supported completion marker."""

    return type(value) is str and value in _CHAT_FINISH_REASONS


@dataclass(frozen=True)
class ChatStreamEvent:
    """One normalized provider stream event."""

    content: str = ""
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    finish_reason: str | None = None

    def __post_init__(self) -> None:
        if type(self.content) is not str:
            raise ValueError("Chat stream content contract violation")
        for value in (self.prompt_tokens, self.completion_tokens):
            if value is not None and safe_provider_usage_count(value) is None:
                raise ValueError("Chat stream usage contract violation")
        if (
            self.finish_reason is not None
            and not is_supported_chat_finish_reason(self.finish_reason)
        ):
            raise ValueError("Chat stream finish contract violation")


def is_strict_chat_stream_event(value: Any) -> bool:
    """Validate even frozen instances that were corrupted after construction."""

    if type(value) is not ChatStreamEvent or type(value.content) is not str:
        return False
    return all(
        count is None or safe_provider_usage_count(count) is not None
        for count in (value.prompt_tokens, value.completion_tokens)
    ) and (
        value.finish_reason is None
        or is_supported_chat_finish_reason(value.finish_reason)
    )


@runtime_checkable
class ChatStream(Protocol):
    """Normalized asynchronous chat stream."""

    def __aiter__(self) -> AsyncIterator[ChatStreamEvent]: ...

    async def aclose(self) -> None: ...


@runtime_checkable
class ChatProvider(Protocol):
    """Provider boundary used by :class:`ChatViewModel`."""

    async def open_stream(
        self,
        messages: Sequence[Mapping[str, Any]],
    ) -> ChatStream: ...

    async def aclose(self) -> None: ...


class _OpenAIChatStream:
    """Translate SDK chunks into stable, provider-neutral events."""

    def __init__(self, stream: Any) -> None:
        self._stream = stream
        self._iterator = stream.__aiter__()
        self._closed = False
        self._finish_seen = False
        self._usage_seen = False

    def __aiter__(self) -> "_OpenAIChatStream":
        return self

    async def __anext__(self) -> ChatStreamEvent:
        if self._closed:
            raise StopAsyncIteration
        try:
            chunk = await self._iterator.__anext__()
            choices = getattr(chunk, "choices", _MISSING)
            if type(choices) is not list:
                raise TypeError("Chat stream choices contract violation")
            if choices:
                if self._finish_seen:
                    raise TypeError("Chat stream content after finish")
                if len(choices) != 1:
                    raise TypeError("Chat stream choice count contract violation")
                choice = choices[0]
                finish_reason = getattr(choice, "finish_reason", _MISSING)
                if finish_reason is _MISSING or (
                    finish_reason is not None
                    and not is_supported_chat_finish_reason(finish_reason)
                ):
                    raise TypeError("Chat stream finish contract violation")
                delta = getattr(choice, "delta", _MISSING)
                if delta is _MISSING:
                    raise TypeError("Chat stream delta contract violation")
                raw_content = getattr(delta, "content", _MISSING)
                if raw_content is _MISSING or (
                    raw_content is not None and type(raw_content) is not str
                ):
                    raise TypeError("Chat stream content contract violation")
                content = "" if raw_content is None else raw_content
            else:
                if not self._finish_seen or self._usage_seen:
                    raise TypeError("Chat stream usage order contract violation")
                content = ""
                finish_reason = None

            prompt_tokens: int | None = None
            completion_tokens: int | None = None
            usage = getattr(chunk, "usage", _MISSING)
            if usage is not None:
                if usage is _MISSING:
                    if not choices:
                        raise TypeError("Chat stream usage contract violation")
                else:
                    if self._usage_seen:
                        raise TypeError("Duplicate Chat stream usage")
                    prompt_tokens = getattr(usage, "prompt_tokens", _MISSING)
                    completion_tokens = getattr(
                        usage,
                        "completion_tokens",
                        _MISSING,
                    )
                    if (
                        safe_provider_usage_count(prompt_tokens) is None
                        or safe_provider_usage_count(completion_tokens) is None
                    ):
                        raise TypeError("Chat stream usage contract violation")
            elif not choices:
                raise TypeError("Empty Chat stream chunk contract violation")

            if finish_reason is not None:
                self._finish_seen = True
            if usage is not _MISSING and usage is not None:
                self._usage_seen = True

            return ChatStreamEvent(
                content=content,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                finish_reason=finish_reason,
            )
        except StopAsyncIteration as exc:
            if not self._finish_seen:
                raise PKVRuntimeError(
                    ErrorCode.PROVIDER_PROTOCOL_FAILED,
                    "Chat Provider 流未提供完成标记",
                    stage="provider_stream",
                    recoverable=True,
                ) from exc
            raise
        except PKVRuntimeError:
            raise
        except Exception as exc:
            raise PKVRuntimeError(
                ErrorCode.PROVIDER_PROTOCOL_FAILED,
                "Chat Provider 返回了无效事件",
                stage="provider_stream",
                recoverable=True,
            ) from exc

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        await _close_async_resource(self._stream)


class OpenAICompatibleChatProvider:
    """Normal production adapter for OpenAI-compatible chat endpoints."""

    def __init__(
        self,
        settings: ChatProviderSettings,
        *,
        client_type: Any | None = None,
        http_client_type: Any | None = None,
    ) -> None:
        validate_chat_provider_settings(settings)

        if client_type is None or http_client_type is None:
            try:
                from openai import AsyncOpenAI, DefaultAsyncHttpxClient
            except ImportError as exc:  # pragma: no cover - dependency gate
                raise PKVRuntimeError(
                    ErrorCode.PROVIDER_UNAVAILABLE,
                    "未安装 OpenAI-compatible Provider 依赖",
                    stage="provider_construction",
                    recoverable=True,
                ) from exc
            client_type = client_type or AsyncOpenAI
            http_client_type = http_client_type or DefaultAsyncHttpxClient

        suppress_unsafe_http_transport_logs()
        transport_base_url, endpoint_query = split_openai_transport_url(
            settings.base_url
        )
        client_kwargs: dict[str, Any] = {
            "api_key": settings.api_key,
            "base_url": transport_base_url,
            "timeout": settings.timeout_seconds,
            "max_retries": settings.max_retries,
        }
        if endpoint_query:
            client_kwargs["http_client"] = http_client_type(params=endpoint_query)

        self._settings = settings
        self._client = client_type(**client_kwargs)
        self._closed = False

    async def open_stream(
        self,
        messages: Sequence[Mapping[str, Any]],
    ) -> ChatStream:
        if self._closed:
            raise PKVRuntimeError(
                ErrorCode.PROVIDER_UNAVAILABLE,
                "Chat Provider 已关闭",
                stage="provider_request",
                recoverable=True,
            )

        stream = await self._client.chat.completions.create(
            model=self._settings.model,
            messages=[dict(message) for message in messages],
            stream=True,
            stream_options={"include_usage": True},
            max_tokens=self._settings.max_tokens,
            temperature=self._settings.temperature,
        )
        return _OpenAIChatStream(stream)

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        await _close_async_resource(self._client)


def create_chat_provider(settings: ChatProviderSettings) -> ChatProvider:
    """Build the configured production chat provider."""

    return OpenAICompatibleChatProvider(settings)


async def _close_async_resource(resource: Any) -> None:
    """Close SDK/test resources that expose ``aclose`` or ``close``."""

    close = getattr(resource, "aclose", None)
    if close is None:
        close = getattr(resource, "close", None)
    if close is None:
        return
    result = close()
    if inspect.isawaitable(result):
        await result
