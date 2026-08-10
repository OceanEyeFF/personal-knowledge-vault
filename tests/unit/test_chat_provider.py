"""Unit contract for the production OpenAI-compatible Chat adapter."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.ai.chat_provider import (
    ChatStreamEvent,
    OpenAICompatibleChatProvider,
    _OpenAIChatStream,
)
from src.ai.provider_factory import ChatProviderSettings
from src.runtime.errors import ErrorCode, PKVRuntimeError


def _settings(**overrides) -> ChatProviderSettings:
    values = {
        "provider": "openai_compatible",
        "api_key": "fixture-key",
        "base_url": (
            "https://chat.example/v1?region=north&region=south&flag=#fragment"
        ),
        "model": "fixture-chat",
        "max_tokens": 321,
        "temperature": 0.25,
        "timeout_seconds": 7.0,
        "max_retries": 1,
    }
    values.update(overrides)
    return ChatProviderSettings(**values)


class RawStream:
    def __init__(self) -> None:
        self.index = 0
        self.close_calls = 0
        self.chunks = [
            SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(content="hello"),
                        finish_reason=None,
                    )
                ],
                usage=None,
            ),
            SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(content=None),
                        finish_reason="stop",
                    )
                ],
                usage=None,
            ),
            SimpleNamespace(
                choices=[],
                usage=SimpleNamespace(
                    prompt_tokens=5,
                    completion_tokens=2,
                ),
            ),
        ]

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.index >= len(self.chunks):
            raise StopAsyncIteration
        chunk = self.chunks[self.index]
        self.index += 1
        return chunk

    async def aclose(self) -> None:
        self.close_calls += 1


@pytest.mark.parametrize(
    "kwargs",
    [
        {"content": None},
        {"content": 7},
        {"prompt_tokens": True},
        {"prompt_tokens": -1},
        {"prompt_tokens": "7"},
        {"completion_tokens": 1_000_000_001},
        {"finish_reason": ""},
        {"finish_reason": "tool_calls"},
        {"finish_reason": True},
    ],
)
def test_stream_event_constructor_enforces_runtime_contract(kwargs) -> None:
    with pytest.raises(ValueError):
        ChatStreamEvent(**kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "chunk",
    [
        object(),
        SimpleNamespace(choices=(SimpleNamespace(),), usage=None),
        SimpleNamespace(choices=[], usage=None),
        SimpleNamespace(choices=[SimpleNamespace()], usage=None),
        SimpleNamespace(
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(),
                    finish_reason=None,
                )
            ],
            usage=None,
        ),
        SimpleNamespace(
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(content=7),
                    finish_reason=None,
                )
            ],
            usage=None,
        ),
        SimpleNamespace(
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(content="ignored"),
                    finish_reason="tool_calls",
                )
            ],
            usage=None,
        ),
        SimpleNamespace(
            choices=[],
            usage=SimpleNamespace(prompt_tokens=True, completion_tokens=0),
        ),
        SimpleNamespace(
            choices=[],
            usage=SimpleNamespace(prompt_tokens=1),
        ),
    ],
)
async def test_adapter_projects_malformed_sdk_chunk_to_protocol_failure(
    chunk,
) -> None:
    raw_stream = RawStream()
    raw_stream.chunks = [chunk]
    stream = _OpenAIChatStream(raw_stream)

    with pytest.raises(PKVRuntimeError) as captured:
        await stream.__anext__()

    assert captured.value.code is ErrorCode.PROVIDER_PROTOCOL_FAILED


@pytest.mark.asyncio
async def test_adapter_preserves_query_and_normalizes_stream() -> None:
    raw_stream = RawStream()
    captured: dict[str, object] = {}

    class HttpClient:
        def __init__(self, **kwargs) -> None:
            captured["http_kwargs"] = kwargs

    class Client:
        def __init__(self, **kwargs) -> None:
            captured["client_kwargs"] = kwargs
            self.close_calls = 0

            async def create(**request):
                captured["request"] = request
                return raw_stream

            self.chat = SimpleNamespace(
                completions=SimpleNamespace(create=create)
            )

        async def aclose(self) -> None:
            self.close_calls += 1

    provider = OpenAICompatibleChatProvider(
        _settings(),
        client_type=Client,
        http_client_type=HttpClient,
    )
    stream = await provider.open_stream(
        ({"role": "user", "content": "question"},)
    )
    events = [event async for event in stream]

    client_kwargs = captured["client_kwargs"]
    assert client_kwargs["base_url"] == "https://chat.example/v1"
    assert client_kwargs["timeout"] == 7.0
    assert client_kwargs["max_retries"] == 1
    assert str(captured["http_kwargs"]["params"]) == (
        "region=north&region=south&flag="
    )
    assert captured["request"] == {
        "model": "fixture-chat",
        "messages": [{"role": "user", "content": "question"}],
        "stream": True,
        "stream_options": {"include_usage": True},
        "max_tokens": 321,
        "temperature": 0.25,
    }
    assert events[0].content == "hello"
    assert events[1].content == ""
    assert events[1].finish_reason == "stop"
    assert events[2].prompt_tokens == 5
    assert events[2].completion_tokens == 2

    await stream.aclose()
    await stream.aclose()
    await provider.aclose()
    await provider.aclose()
    assert raw_stream.close_calls == 1
    assert provider._client.close_calls == 1


@pytest.mark.asyncio
async def test_adapter_rejects_eof_before_finish_marker() -> None:
    raw_stream = RawStream()
    raw_stream.chunks = [
        SimpleNamespace(
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(content="provisional"),
                    finish_reason=None,
                )
            ],
            usage=None,
        )
    ]
    stream = _OpenAIChatStream(raw_stream)

    assert (await stream.__anext__()).content == "provisional"
    with pytest.raises(PKVRuntimeError) as captured:
        await stream.__anext__()

    assert captured.value.code is ErrorCode.PROVIDER_PROTOCOL_FAILED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "late_chunk",
    [
        SimpleNamespace(
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(content="late"),
                    finish_reason=None,
                )
            ],
            usage=None,
        ),
        SimpleNamespace(
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(content=None),
                    finish_reason="stop",
                )
            ],
            usage=None,
        ),
    ],
)
async def test_adapter_rejects_choice_after_finish_marker(late_chunk) -> None:
    raw_stream = RawStream()
    raw_stream.chunks = [raw_stream.chunks[1], late_chunk]
    stream = _OpenAIChatStream(raw_stream)

    assert (await stream.__anext__()).finish_reason == "stop"
    with pytest.raises(PKVRuntimeError) as captured:
        await stream.__anext__()

    assert captured.value.code is ErrorCode.PROVIDER_PROTOCOL_FAILED


def test_adapter_rejects_unknown_provider() -> None:
    with pytest.raises(PKVRuntimeError) as exc_info:
        OpenAICompatibleChatProvider(
            _settings(provider="hidden-test-provider"),
            client_type=object,
            http_client_type=object,
        )
    assert exc_info.value.code is ErrorCode.PROVIDER_CONFIG_INVALID


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_tokens", True),
        ("max_tokens", 1.5),
        ("max_retries", True),
        ("max_retries", 1.9),
        ("max_retries", 11),
        ("timeout_seconds", float("nan")),
        ("timeout_seconds", 601),
        pytest.param("timeout_seconds", 10**10000, id="huge-timeout-int"),
        pytest.param("temperature", 10**10000, id="huge-temperature-int"),
        ("base_url", "https://chat.example:bad/v1"),
        ("base_url", "https://chat.example:0/v1"),
        ("base_url", "https://chat.example:65536/v1"),
    ],
)
def test_direct_settings_are_validated_before_client_construction(
    field: str,
    value,
) -> None:
    constructed = 0

    class Client:
        def __init__(self, **kwargs) -> None:
            nonlocal constructed
            constructed += 1

    with pytest.raises(PKVRuntimeError) as exc_info:
        OpenAICompatibleChatProvider(
            _settings(**{field: value}),
            client_type=Client,
            http_client_type=object,
        )

    assert exc_info.value.code is ErrorCode.PROVIDER_CONFIG_INVALID
    assert constructed == 0
