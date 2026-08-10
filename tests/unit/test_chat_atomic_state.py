"""W2 deterministic tests for the atomic GUI Chat request contract."""

from __future__ import annotations

import asyncio
import copy
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import yaml

from src.ai.chat_provider import ChatStreamEvent, _OpenAIChatStream
from src.gui.utils.preview_loader import PreviewIssue, PreviewOutcome
from src.gui.viewmodels.chat_viewmodel import ChatViewModel
from src.gui.views.chat_view import ChatView
from src.retrieval.result import RetrievalIssue, SearchResponse, SearchResult
from src.runtime.errors import ErrorCode
from src.utils.config import Config


FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "w2"
    / "chat"
    / "v1"
    / "scenarios.yaml"
)


@dataclass
class FakeConfig:
    db_path: str = ":memory:"
    llm_provider: str = "openai_compatible"
    llm_api_key: str | None = "fixture-key"
    llm_base_url: str = "https://provider.example/v1"
    llm_model: str = "fixture-model-v1"
    llm_max_tokens: int = 128
    llm_temperature: float = 0.2
    llm_timeout_seconds: float = 5.0
    llm_max_retries: int = 0


class FakeStore:
    def __init__(self, states: dict[str, dict[str, Any]]) -> None:
        self.states = copy.deepcopy(states)
        for session_id, state in self.states.items():
            state.setdefault("session_id", session_id)
        self.entries_by_url: dict[str, dict[str, Any]] = {}
        self.entries_by_id: dict[int, dict[str, Any]] = {}
        self.update_attempts: list[dict[str, Any]] = []
        self.fail_update = False
        self.commit_then_raise = False
        self.delete_then_raise = False
        self.zero_row_update = False

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        value = self.states.get(session_id)
        return copy.deepcopy(value) if value is not None else None

    def update_session(self, **payload: Any) -> None:
        self.update_attempts.append(copy.deepcopy(payload))
        if self.delete_then_raise:
            self.states.pop(payload["session_id"], None)
            raise RuntimeError("fixture-missing-after-failure-secret")
        if self.fail_update:
            raise RuntimeError("fixture-save-secret")
        if self.zero_row_update:
            return
        self.states[payload["session_id"]] = {
            "session_id": payload["session_id"],
            "messages": copy.deepcopy(payload["messages"]),
            "total_tokens": payload["total_tokens"],
            "round_count": payload["round_count"],
        }
        if self.commit_then_raise:
            self.commit_then_raise = False
            raise RuntimeError("fixture-after-commit-secret")

    def list_sessions(self, *, is_archived: bool = False) -> list[dict[str, Any]]:
        return []

    def query_by_url(self, url: str) -> dict[str, Any] | None:
        entry = self.entries_by_url.get(url)
        return copy.deepcopy(entry) if entry is not None else None

    def query_by_id(self, knowledge_id: int) -> dict[str, Any] | None:
        entry = self.entries_by_id.get(knowledge_id)
        return copy.deepcopy(entry) if entry is not None else None


class ControlledStream:
    def __init__(
        self,
        events: list[ChatStreamEvent | BaseException],
        *,
        gate_at: int | None = None,
        block_close: bool = False,
        ignore_close_cancellation: bool = False,
        auto_finish: bool = True,
    ) -> None:
        self.events = list(events)
        if auto_finish and not any(
            type(event) is ChatStreamEvent and event.finish_reason is not None
            for event in self.events
        ):
            insert_at = len(self.events)
            while insert_at > 0:
                candidate = self.events[insert_at - 1]
                if (
                    type(candidate) is ChatStreamEvent
                    and not candidate.content
                    and (
                        candidate.prompt_tokens is not None
                        or candidate.completion_tokens is not None
                    )
                ):
                    insert_at -= 1
                    continue
                break
            self.events.insert(
                insert_at,
                ChatStreamEvent(finish_reason="stop"),
            )
        self.gate_at = gate_at
        self.index = 0
        self.waiting = asyncio.Event()
        self.release = asyncio.Event()
        self.closed = False
        self.close_calls = 0
        self.block_close = block_close
        self.ignore_close_cancellation = ignore_close_cancellation
        self.close_started = asyncio.Event()
        self.close_release = asyncio.Event()
        if not block_close:
            self.close_release.set()

    def __aiter__(self) -> "ControlledStream":
        return self

    async def __anext__(self) -> ChatStreamEvent:
        if self.closed or self.index >= len(self.events):
            raise StopAsyncIteration
        if self.gate_at == self.index and not self.release.is_set():
            self.waiting.set()
            await self.release.wait()
            if self.closed:
                raise StopAsyncIteration
        event = self.events[self.index]
        self.index += 1
        if isinstance(event, BaseException):
            raise event
        return event

    async def aclose(self) -> None:
        self.close_calls += 1
        self.close_started.set()
        while not self.close_release.is_set():
            try:
                await self.close_release.wait()
            except asyncio.CancelledError:
                if not self.ignore_close_cancellation:
                    raise
        self.closed = True
        self.release.set()


class FakeProvider:
    def __init__(
        self,
        stream: ControlledStream,
        *,
        open_error: Exception | None = None,
    ) -> None:
        self.stream = stream
        self.open_error = open_error
        self.open_calls = 0
        self.close_calls = 0
        self.messages: tuple[Any, ...] = ()

    async def open_stream(self, messages):
        self.open_calls += 1
        self.messages = tuple(messages)
        if self.open_error is not None:
            raise self.open_error
        return self.stream

    async def aclose(self) -> None:
        self.close_calls += 1


class ProviderFactory:
    def __init__(self, *providers: FakeProvider) -> None:
        self.providers = list(providers)
        self.settings = []
        self.calls = 0

    def __call__(self, settings):
        self.settings.append(settings)
        provider = self.providers[self.calls]
        self.calls += 1
        return provider


def _corrupt_stream_event(field: str, value: Any) -> ChatStreamEvent:
    event = ChatStreamEvent(content="must-not-save")
    object.__setattr__(event, field, value)
    return event


def _state(message: str, *, tokens: int, rounds: int) -> dict[str, Any]:
    return {
        "messages": [{"role": "assistant", "content": message}],
        "total_tokens": tokens,
        "round_count": rounds,
    }


def _url_completion_data(
    knowledge_id: int,
    *,
    status: str = "ready",
    **overrides: Any,
) -> dict[str, Any]:
    data = {
        "knowledge_id": knowledge_id,
        "status": status,
        "operation_id": f"{knowledge_id:032x}",
        "core_committed": True,
        "do_not_retry": True,
        "repair_actions": (
            ["rebuild_vectors_for_entry"] if status == "degraded" else []
        ),
    }
    data.update(overrides)
    return data


def _vm(
    qtbot,
    *,
    stream: ControlledStream,
    states: dict[str, dict[str, Any]] | None = None,
):
    states = states or {"session-a": _state("before", tokens=10, rounds=1)}
    store = FakeStore(states)
    config = FakeConfig()
    provider = FakeProvider(stream)
    factory = ProviderFactory(provider)
    vm = ChatViewModel(
        config=config,
        store=store,
        provider_factory=factory,
    )
    assert vm.load_session("session-a")
    return vm, config, store, provider, factory


def _collect(vm: ChatViewModel) -> dict[str, list[tuple[Any, ...]]]:
    events: dict[str, list[tuple[Any, ...]]] = {
        "started": [],
        "tokens": [],
        "terminal": [],
        "rejected": [],
    }
    vm.chat_request_started.connect(
        lambda *args: events["started"].append(tuple(args))
    )
    vm.chat_token_received.connect(
        lambda *args: events["tokens"].append(tuple(args))
    )
    vm.chat_request_terminal.connect(
        lambda *args: events["terminal"].append(tuple(args))
    )
    vm.chat_request_rejected.connect(
        lambda *args: events["rejected"].append(tuple(args))
    )
    return events


@pytest.mark.asyncio
async def test_success_fixture_commits_once_and_closes_once(qtbot) -> None:
    fixture = yaml.safe_load(FIXTURE.read_text(encoding="utf-8"))
    scenario = fixture["scenarios"]["success"]
    stream = ControlledStream(
        [ChatStreamEvent(**event) for event in scenario["events"]]
    )
    vm, _, store, provider, factory = _vm(qtbot, stream=stream)
    events = _collect(vm)

    result = await vm.send_message.__wrapped__(vm, scenario["user_message"])

    assert result is True
    assert events["terminal"] == [
        (
            "session-a",
            events["started"][0][1],
            scenario["expected_terminal"],
            "",
            "",
        )
    ]
    assert store.states["session-a"]["messages"][-2:] == [
        {"role": "user", "content": "fixture-success"},
        {"role": "assistant", "content": scenario["expected_assistant"]},
    ]
    assert store.states["session-a"]["total_tokens"] == 16
    assert store.states["session-a"]["round_count"] == 2
    assert len(store.update_attempts) == 1
    assert factory.calls == 1
    assert stream.close_calls == 1
    assert provider.close_calls == 1
    assert vm.stop_stream() is False
    assert len(events["terminal"]) == 1


@pytest.mark.asyncio
async def test_session_switch_keeps_origin_and_frozen_config(qtbot) -> None:
    stream = ControlledStream(
        [
            ChatStreamEvent(content="part-1"),
            ChatStreamEvent(content="part-2"),
            ChatStreamEvent(prompt_tokens=3, completion_tokens=2),
        ],
        gate_at=1,
    )
    states = {
        "session-a": _state("a-before", tokens=8, rounds=1),
        "session-b": _state("b-before", tokens=40, rounds=4),
    }
    vm, config, store, provider, factory = _vm(
        qtbot,
        stream=stream,
        states=states,
    )
    events = _collect(vm)
    task = asyncio.create_task(
        vm.send_message.__wrapped__(vm, "origin-message")
    )
    await stream.waiting.wait()

    config.llm_model = "fixture-model-v2"
    config.llm_api_key = "rotated-key"
    assert vm.load_session("session-b")
    visible_before = copy.deepcopy(vm.current_messages)
    stream.release.set()
    assert await task is True

    assert factory.settings[0].model == "fixture-model-v1"
    assert factory.settings[0].api_key == "fixture-key"
    assert [dict(message) for message in provider.messages][-1] == {
        "role": "user",
        "content": "origin-message",
    }
    with pytest.raises(TypeError):
        provider.messages[0]["content"] = "mutation"
    assert store.update_attempts[0]["session_id"] == "session-a"
    assert vm.current_session_id == "session-b"
    assert vm.current_messages == visible_before
    assert vm.current_total_tokens == 40
    assert vm.current_round_count == 4
    assert {token[0] for token in events["tokens"]} == {"session-a"}
    assert events["terminal"][0][0] == "session-a"


@pytest.mark.asyncio
async def test_double_send_is_rejected_without_disturbing_active_request(qtbot) -> None:
    stream = ControlledStream(
        [ChatStreamEvent(content="ok")],
        gate_at=0,
    )
    vm, _, store, _, factory = _vm(qtbot, stream=stream)
    events = _collect(vm)
    first = asyncio.create_task(vm.send_message.__wrapped__(vm, "first"))
    await stream.waiting.wait()

    assert await vm.send_message.__wrapped__(vm, "second") is False
    assert vm.is_busy is True
    assert len(events["rejected"]) == 1
    assert events["rejected"][0][2] == "chat_busy"
    assert factory.calls == 1
    assert store.update_attempts == []

    stream.release.set()
    assert await first is True
    assert [event[2] for event in events["terminal"]] == ["completed"]
    assert store.states["session-a"]["messages"][-2]["content"] == "first"


@pytest.mark.asyncio
async def test_invalid_provider_config_is_rejection_without_provisional_turn(
    qtbot,
) -> None:
    stream = ControlledStream([ChatStreamEvent(content="unused")])
    vm, config, store, _, factory = _vm(qtbot, stream=stream)
    before = copy.deepcopy(vm.current_messages)
    config.llm_api_key = None
    events = _collect(vm)

    assert await vm.send_message.__wrapped__(vm, "must-not-start") is False

    assert vm.current_messages == before
    assert vm.current_total_tokens == 10
    assert vm.current_round_count == 1
    assert vm.is_busy is False
    assert factory.calls == 0
    assert store.update_attempts == []
    assert events["started"] == []
    assert events["terminal"] == []
    assert len(events["rejected"]) == 1
    assert events["rejected"][0][2] == "provider_config_invalid"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("llm_max_tokens", True),
        ("llm_max_tokens", 1.5),
        ("llm_max_tokens", "admission-secret-number"),
        ("llm_max_retries", 1.9),
        ("llm_timeout_seconds", float("inf")),
        ("llm_base_url", "https://chat.example:bad/v1"),
        ("llm_base_url", "https://chat.example:0/v1"),
        ("llm_base_url", "https://chat.example:65536/v1"),
    ],
)
def test_strict_provider_config_rejects_before_started_or_factory(
    qtbot,
    field: str,
    value,
) -> None:
    stream = ControlledStream([ChatStreamEvent(content="unused")])
    vm, config, store, _, factory = _vm(qtbot, stream=stream)
    setattr(config, field, value)
    before = copy.deepcopy(vm.current_messages)
    events = _collect(vm)

    assert vm.can_dispatch_message("must-not-start") is False

    assert vm.current_messages == before
    assert store.update_attempts == []
    assert factory.calls == 0
    assert events["started"] == []
    assert events["terminal"] == []
    assert len(events["rejected"]) == 1
    assert events["rejected"][0][2] == "provider_config_invalid"
    assert "admission-secret-number" not in str(events)


def test_real_invalid_yaml_allows_vm_start_and_reload_then_rejects_safely(
    qtbot,
    tmp_path: Path,
    caplog,
) -> None:
    sentinel = "STARTUP_RELOAD_SECRET_INVALID_NUMBER"
    payload = {
        "storage": {"data_dir": str(tmp_path / "data")},
        "ai": {
            "llm": {
                "provider": "openai_compatible",
                "api_key": "fixture-key",
                "base_url": "https://chat.example/v1",
                "model": "fixture-model",
                "max_tokens": sentinel,
                "temperature": 0.2,
                "timeout_seconds": 5.0,
                "max_retries": 0,
            },
            "embedding": {
                "provider": "openai_compatible",
                "api_key": "fixture-embedding-key",
                "base_url": "https://embedding.example/v1",
                "model": "fixture-embedding",
                "dim": 64,
                "timeout_seconds": 5.0,
                "max_retries": 0,
            },
        },
    }
    config_path = tmp_path / "invalid-config.yaml"
    config_path.write_text(
        yaml.safe_dump(payload, allow_unicode=True),
        encoding="utf-8",
    )
    config = Config(config_path=str(config_path))
    stream = ControlledStream([ChatStreamEvent(content="unused")])
    store = FakeStore({"session-a": _state("before", tokens=10, rounds=1)})
    provider = FakeProvider(stream)
    factory = ProviderFactory(provider)

    # Construction and reload must not touch the legacy coercing properties.
    vm = ChatViewModel(config=config, store=store, provider_factory=factory)
    assert vm.load_session("session-a")
    with patch("src.gui.viewmodels.chat_viewmodel.Config", return_value=config):
        vm.reload_provider_config()
    events = _collect(vm)

    with caplog.at_level("ERROR", logger="pkv.gui.viewmodels.chat"):
        assert vm.can_dispatch_message("must-not-start") is False

    assert events["started"] == []
    assert events["terminal"] == []
    assert events["rejected"][0][2] == "provider_config_invalid"
    assert factory.calls == 0
    assert sentinel not in caplog.text
    assert sentinel not in str(events)


@pytest.mark.asyncio
async def test_stop_is_prompt_idempotent_and_rolls_back_entire_turn(qtbot) -> None:
    stream = ControlledStream(
        [
            ChatStreamEvent(content="partial"),
            ChatStreamEvent(content="must-not-arrive"),
        ],
        gate_at=1,
    )
    vm, _, store, provider, _ = _vm(qtbot, stream=stream)
    before = copy.deepcopy(vm.current_messages)
    events = _collect(vm)
    task = asyncio.create_task(vm.send_message.__wrapped__(vm, "discard-me"))
    await stream.waiting.wait()

    assert vm.stop_stream() is True
    assert vm.stop_stream() is True
    assert await asyncio.wait_for(task, timeout=1.0) is False

    assert vm.current_messages == before
    assert vm.current_total_tokens == 10
    assert vm.current_round_count == 1
    assert store.update_attempts == []
    assert [event[2] for event in events["terminal"]] == ["stopped"]
    assert stream.close_calls == 1
    assert provider.close_calls == 1
    assert vm.is_busy is False
    assert vm.stop_stream() is False


@pytest.mark.asyncio
async def test_stop_after_durable_commit_cannot_rewrite_terminal_or_cancel_cleanup(
    qtbot,
) -> None:
    """A blocked aclose exposes the exact durable-commit/cleanup race window."""

    stream = ControlledStream(
        [
            ChatStreamEvent(content="durably-kept"),
            ChatStreamEvent(prompt_tokens=2, completion_tokens=3),
        ],
        block_close=True,
    )
    vm, _, store, provider, _ = _vm(qtbot, stream=stream)
    events = _collect(vm)
    task = asyncio.create_task(
        vm.send_message.__wrapped__(vm, "commit-before-close")
    )

    await asyncio.wait_for(stream.close_started.wait(), timeout=1.0)
    request = vm._active_request
    assert request is not None
    assert request.committed is True
    assert task.done() is False
    assert events["terminal"] == []
    assert store.states["session-a"]["messages"][-2:] == [
        {"role": "user", "content": "commit-before-close"},
        {"role": "assistant", "content": "durably-kept"},
    ]
    assert vm.current_messages == store.states["session-a"]["messages"]

    # False is the public button contract: Stop is too late and was not
    # accepted, so cleanup remains live and completion remains authoritative.
    assert vm.stop_stream() is False
    assert task.done() is False
    assert stream.close_calls == 1

    # Even an external task cancellation cannot strand the close coroutine or
    # contradict a commit that is already durable.
    task.cancel()
    await asyncio.sleep(0)
    assert task.done() is False
    assert stream.close_calls == 1

    stream.close_release.set()
    assert await asyncio.wait_for(task, timeout=1.0) is True
    assert [event[2] for event in events["terminal"]] == ["completed"]
    assert store.states["session-a"]["messages"] == vm.current_messages
    assert stream.close_calls == 1
    assert provider.close_calls == 1
    assert vm.is_busy is False


@pytest.mark.asyncio
async def test_never_returning_stream_close_has_bounded_terminal_and_no_busy_loop(
    qtbot,
) -> None:
    stream = ControlledStream(
        [ChatStreamEvent(content="partial"), ChatStreamEvent(content="late")],
        gate_at=1,
        block_close=True,
        ignore_close_cancellation=True,
    )
    vm, _, store, provider, _ = _vm(qtbot, stream=stream)
    events = _collect(vm)
    task = asyncio.create_task(vm.send_message.__wrapped__(vm, "stop-bounded"))
    await stream.waiting.wait()

    for _ in range(20):
        assert vm.stop_stream() is True

    heartbeat = 0

    async def count_heartbeats() -> None:
        nonlocal heartbeat
        for _ in range(200):
            heartbeat += 1
            await asyncio.sleep(0)

    heartbeat_task = asyncio.create_task(count_heartbeats())
    assert await asyncio.wait_for(task, timeout=1.0) is False
    await heartbeat_task

    assert heartbeat == 200
    assert [event[2] for event in events["terminal"]] == ["stopped"]
    assert store.update_attempts == []
    assert stream.close_calls == 1
    assert provider.close_calls == 1
    assert vm.is_busy is False

    # Let the deliberately uncooperative close settle so the retained task's
    # done callback can drain it without leaking into the test loop.
    stream.close_release.set()
    for _ in range(20):
        if not vm._detached_cleanup_tasks:
            break
        await asyncio.sleep(0)
    assert vm._detached_cleanup_tasks == set()


@pytest.mark.asyncio
async def test_stop_restores_state_before_reference_context_injection(qtbot) -> None:
    stream = ControlledStream(
        [ChatStreamEvent(content="partial"), ChatStreamEvent(content="late")],
        gate_at=1,
    )
    vm, _, store, _, _ = _vm(qtbot, stream=stream)
    checkpoint = vm.capture_turn_checkpoint()
    before = copy.deepcopy(vm.current_messages)
    vm.set_knowledge_context("provisional-reference-context")

    assert vm.dispatch_message("question", checkpoint=checkpoint) is True
    task = vm._active_request.task
    assert task is not None
    await stream.waiting.wait()
    assert vm.stop_stream() is True
    assert await asyncio.wait_for(task, timeout=1.0) is False

    assert vm.current_messages == before
    assert all(
        message.get("content") != "provisional-reference-context"
        for message in vm.current_messages
    )
    assert store.update_attempts == []


@pytest.mark.asyncio
async def test_same_session_stop_then_resend_uses_new_request_identity(qtbot) -> None:
    old_stream = ControlledStream(
        [ChatStreamEvent(content="old-partial"), ChatStreamEvent(content="late")],
        gate_at=1,
    )
    new_stream = ControlledStream(
        [
            ChatStreamEvent(content="new-complete"),
            ChatStreamEvent(prompt_tokens=1, completion_tokens=1),
        ]
    )
    store = FakeStore({"session-a": _state("before", tokens=10, rounds=1)})
    old_provider = FakeProvider(old_stream)
    new_provider = FakeProvider(new_stream)
    factory = ProviderFactory(old_provider, new_provider)
    vm = ChatViewModel(
        config=FakeConfig(),
        store=store,
        provider_factory=factory,
    )
    assert vm.load_session("session-a")
    events = _collect(vm)

    old_task = asyncio.create_task(
        vm.send_message.__wrapped__(vm, "old-discarded")
    )
    await old_stream.waiting.wait()
    assert vm.stop_stream()
    assert await asyncio.wait_for(old_task, timeout=1.0) is False
    assert await vm.send_message.__wrapped__(vm, "new-kept") is True

    old_request_id = events["started"][0][1]
    new_request_id = events["started"][1][1]
    assert old_request_id != new_request_id
    assert [(item[1], item[2]) for item in events["terminal"]] == [
        (old_request_id, "stopped"),
        (new_request_id, "completed"),
    ]
    contents = [message["content"] for message in store.states["session-a"]["messages"]]
    assert "old-discarded" not in contents
    assert "old-partial" not in contents
    assert contents[-2:] == ["new-kept", "new-complete"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_stage", ["factory", "open", "iterate"])
async def test_provider_failures_rollback_and_expose_no_raw_error(
    qtbot,
    caplog,
    failure_stage: str,
) -> None:
    sentinel = "provider-response-secret"
    stream_events: list[ChatStreamEvent | BaseException] = []
    if failure_stage == "iterate":
        stream_events = [
            ChatStreamEvent(content="partial"),
            RuntimeError(sentinel),
        ]
    stream = ControlledStream(stream_events)
    store = FakeStore({"session-a": _state("before", tokens=10, rounds=1)})
    config = FakeConfig()
    provider = FakeProvider(
        stream,
        open_error=RuntimeError(sentinel) if failure_stage == "open" else None,
    )

    def factory(settings):
        if failure_stage == "factory":
            raise RuntimeError(sentinel)
        return provider

    vm = ChatViewModel(config=config, store=store, provider_factory=factory)
    assert vm.load_session("session-a")
    before = copy.deepcopy(vm.current_messages)
    events = _collect(vm)

    with caplog.at_level("ERROR", logger="pkv.gui.viewmodels.chat"):
        assert await vm.send_message.__wrapped__(vm, "fail") is False

    assert vm.current_messages == before
    assert store.update_attempts == []
    assert len(events["terminal"]) == 1
    assert events["terminal"][0][2:4] == (
        "error",
        "chat_provider_failed",
    )
    assert sentinel not in events["terminal"][0][4]
    assert sentinel not in caplog.text
    assert provider.close_calls == (0 if failure_stage == "factory" else 1)
    assert stream.close_calls == (1 if failure_stage == "iterate" else 0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "malformed_event",
    [
        pytest.param(
            _corrupt_stream_event("content", None),
            id="content-none",
        ),
        pytest.param(
            _corrupt_stream_event(
                "content",
                {"secret": "event-content-secret"},
            ),
            id="content-not-string",
        ),
        pytest.param(
            _corrupt_stream_event("prompt_tokens", True),
            id="prompt-bool",
        ),
        pytest.param(
            _corrupt_stream_event("prompt_tokens", -1),
            id="prompt-negative",
        ),
        pytest.param(
            _corrupt_stream_event(
                "prompt_tokens",
                "event-usage-secret\r\nInjected-Header",
            ),
            id="prompt-numeric-string",
        ),
        pytest.param(
            _corrupt_stream_event("completion_tokens", 1_000_000_001),
            id="completion-oversized",
        ),
    ],
)
async def test_malformed_stream_event_is_protocol_failure_without_save(
    qtbot,
    caplog,
    malformed_event,
) -> None:
    stream = ControlledStream([malformed_event])
    vm, _, store, provider, _ = _vm(qtbot, stream=stream)
    before = copy.deepcopy(vm.current_messages)
    events = _collect(vm)

    with caplog.at_level("INFO"):
        assert await vm.send_message.__wrapped__(vm, "must-not-dispatch") is False

    assert vm.current_messages == before
    assert store.update_attempts == []
    assert [event[2:4] for event in events["terminal"]] == [
        ("error", "provider_protocol_failed")
    ]
    assert events["terminal"][0][4] == (
        "LLM Provider 返回格式无效，本轮内容未保存"
    )
    assert stream.close_calls == 1
    assert provider.close_calls == 1
    assert "event-content-secret" not in caplog.text
    assert "event-usage-secret" not in caplog.text
    assert "Injected-Header" not in caplog.text


@pytest.mark.asyncio
async def test_malformed_sdk_chunk_after_valid_token_never_commits(qtbot) -> None:
    """An ignored-looking SDK chunk is still a fatal provider protocol error."""

    class RawSDKStream:
        def __init__(self) -> None:
            self.index = 0
            self.close_calls = 0
            self.chunks = [
                SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            delta=SimpleNamespace(content="provisional"),
                            finish_reason=None,
                        )
                    ],
                    usage=None,
                ),
                object(),
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

    raw_stream = RawSDKStream()
    stream = _OpenAIChatStream(raw_stream)
    vm, _, store, provider, _ = _vm(qtbot, stream=stream)
    before = copy.deepcopy(vm.current_messages)
    events = _collect(vm)

    assert await vm.send_message.__wrapped__(vm, "must-not-save") is False

    assert vm.current_messages == before
    assert store.update_attempts == []
    assert [event[2:4] for event in events["terminal"]] == [
        ("error", "provider_protocol_failed")
    ]
    assert all(event[2] != "completed" for event in events["terminal"])
    assert raw_stream.close_calls == 1
    assert provider.close_calls == 1


@pytest.mark.asyncio
async def test_normalized_stream_eof_before_finish_never_commits(qtbot) -> None:
    stream = ControlledStream(
        [ChatStreamEvent(content="provisional")],
        auto_finish=False,
    )
    vm, _, store, provider, _ = _vm(qtbot, stream=stream)
    before = copy.deepcopy(vm.current_messages)
    events = _collect(vm)

    assert await vm.send_message.__wrapped__(vm, "must-not-save") is False

    assert vm.current_messages == before
    assert store.update_attempts == []
    assert [event[2:4] for event in events["terminal"]] == [
        ("error", "provider_protocol_failed")
    ]
    assert stream.close_calls == 1
    assert provider.close_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "late_event",
    [
        ChatStreamEvent(content="late"),
        ChatStreamEvent(finish_reason="stop"),
        ChatStreamEvent(),
    ],
)
async def test_normalized_stream_rejects_event_after_finish(
    qtbot,
    late_event,
) -> None:
    stream = ControlledStream(
        [
            ChatStreamEvent(content="provisional"),
            ChatStreamEvent(finish_reason="stop"),
            late_event,
        ],
        auto_finish=False,
    )
    vm, _, store, _, _ = _vm(qtbot, stream=stream)
    before = copy.deepcopy(vm.current_messages)
    events = _collect(vm)

    assert await vm.send_message.__wrapped__(vm, "must-not-save") is False

    assert vm.current_messages == before
    assert store.update_attempts == []
    assert [event[2:4] for event in events["terminal"]] == [
        ("error", "provider_protocol_failed")
    ]


@pytest.mark.asyncio
async def test_save_failure_rolls_back_and_has_single_error_terminal(qtbot) -> None:
    stream = ControlledStream(
        [
            ChatStreamEvent(content="complete-but-unsaved"),
            ChatStreamEvent(prompt_tokens=2, completion_tokens=3),
        ]
    )
    vm, _, store, provider, _ = _vm(qtbot, stream=stream)
    before = copy.deepcopy(vm.current_messages)
    store.fail_update = True
    events = _collect(vm)

    assert await vm.send_message.__wrapped__(vm, "save-fails") is False

    assert vm.current_messages == before
    assert vm.current_total_tokens == 10
    assert vm.current_round_count == 1
    assert len(store.update_attempts) == 1
    assert [event[2:4] for event in events["terminal"]] == [
        ("error", "chat_save_failed")
    ]
    assert stream.close_calls == 1
    assert provider.close_calls == 1


@pytest.mark.asyncio
async def test_save_commit_after_raise_is_compensated_to_pre_turn(qtbot) -> None:
    stream = ControlledStream([ChatStreamEvent(content="must-rollback")])
    vm, _, store, _, _ = _vm(qtbot, stream=stream)
    before = copy.deepcopy(store.states["session-a"])
    store.commit_then_raise = True
    events = _collect(vm)

    assert await vm.send_message.__wrapped__(vm, "commit-then-raise") is False

    assert store.states["session-a"] == before
    assert len(store.update_attempts) == 2
    assert events["terminal"][0][2:4] == (
        "error",
        "chat_save_failed",
    )
    assert events["terminal"][0][4] == "保存对话失败，本轮内容已回滚"


@pytest.mark.asyncio
async def test_missing_session_after_save_failure_is_unknown_not_rollback(
    qtbot,
) -> None:
    stream = ControlledStream([ChatStreamEvent(content="not-durable")])
    vm, _, store, _, _ = _vm(qtbot, stream=stream)
    store.delete_then_raise = True
    before = copy.deepcopy(vm.current_messages)
    events = _collect(vm)

    assert await vm.send_message.__wrapped__(vm, "row-disappears") is False

    assert store.states.get("session-a") is None
    assert vm.current_messages == before
    assert [event[2:4] for event in events["terminal"]] == [
        ("error", "chat_save_failed")
    ]
    assert events["terminal"][0][4] == (
        "保存对话失败，持久化状态需检查，请勿直接重试"
    )


@pytest.mark.asyncio
async def test_zero_row_update_fails_readback_and_never_emits_completed(qtbot) -> None:
    stream = ControlledStream([ChatStreamEvent(content="phantom-success")])
    vm, _, store, _, _ = _vm(qtbot, stream=stream)
    before = copy.deepcopy(vm.current_messages)
    store.states.pop("session-a")
    store.zero_row_update = True
    events = _collect(vm)

    assert await vm.send_message.__wrapped__(vm, "missing-before-update") is False

    assert store.states.get("session-a") is None
    assert vm.current_messages == before
    assert len(store.update_attempts) == 1
    assert [event[2:4] for event in events["terminal"]] == [
        ("error", "chat_save_failed")
    ]
    assert events["terminal"][0][4] == (
        "保存对话失败，持久化状态需检查，请勿直接重试"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "corrupt_ack",
    [
        pytest.param(
            {
                "session_id": "session-a",
                "messages": [
                    {"role": "assistant", "content": "before"},
                    {"role": "user", "content": "save-with-bool-ack"},
                    {"role": "assistant", "content": "answer"},
                ],
                "total_tokens": True,
                "round_count": 1,
            },
            id="bool-total-tokens",
        ),
        pytest.param(
            {
                "session_id": "session-a",
                "messages": [
                    {"role": "assistant", "content": "before"},
                    {"role": "user", "content": "save-with-bool-ack"},
                    {"role": "assistant", "content": "answer"},
                ],
                "total_tokens": 1,
                "round_count": True,
            },
            id="bool-round-count",
        ),
        pytest.param(
            MappingProxyType({
                "session_id": "session-a",
                "messages": [],
                "total_tokens": 10,
                "round_count": 1,
            }),
            id="frozen-session-mapping",
        ),
        pytest.param(
            {
                "session_id": "session-a",
                "messages": (
                    {"role": "assistant", "content": "before"},
                ),
                "total_tokens": 10,
                "round_count": 1,
            },
            id="frozen-message-sequence",
        ),
        pytest.param(
            {
                "session_id": "session-a",
                "messages": [
                    MappingProxyType({
                        "role": "assistant",
                        "content": "before",
                    })
                ],
                "total_tokens": 10,
                "round_count": 1,
            },
            id="frozen-message-mapping",
        ),
    ],
)
async def test_malformed_save_ack_never_crosses_commit_boundary(
    qtbot,
    corrupt_ack,
) -> None:
    stream = ControlledStream([ChatStreamEvent(content="answer")])
    vm, _, store, _, _ = _vm(
        qtbot,
        stream=stream,
        states={"session-a": _state("before", tokens=1, rounds=0)},
    )
    before = copy.deepcopy(vm.current_messages)
    store.get_session = MagicMock(return_value=corrupt_ack)
    events = _collect(vm)

    assert await vm.send_message.__wrapped__(
        vm,
        "save-with-bool-ack",
    ) is False

    assert vm.current_messages == before
    assert [event[2:4] for event in events["terminal"]] == [
        ("error", "chat_save_failed")
    ]
    assert all(event[2] != "completed" for event in events["terminal"])
    assert len(store.update_attempts) == 1


@pytest.mark.parametrize(
    "corrupt_session",
    [
        pytest.param({}, id="missing-fields"),
        pytest.param(
            MappingProxyType({
                "session_id": "session-b",
                "messages": [],
                "total_tokens": 0,
                "round_count": 0,
            }),
            id="frozen-session-mapping",
        ),
        pytest.param(
            {
                "session_id": True,
                "messages": [],
                "total_tokens": 0,
                "round_count": 0,
            },
            id="bool-session-id",
        ),
        pytest.param(
            {
                "session_id": "session-b",
                "messages": (),
                "total_tokens": 0,
                "round_count": 0,
            },
            id="frozen-message-sequence",
        ),
        pytest.param(
            {
                "session_id": "session-b",
                "messages": [
                    MappingProxyType({"role": "user", "content": "secret"})
                ],
                "total_tokens": 0,
                "round_count": 0,
            },
            id="frozen-message-mapping",
        ),
        pytest.param(
            {
                "session_id": "session-b",
                "messages": [{"role": "tool", "content": "secret"}],
                "total_tokens": 0,
                "round_count": 0,
            },
            id="unknown-role",
        ),
        pytest.param(
            {
                "session_id": "session-b",
                "messages": [{"role": "user", "content": True}],
                "total_tokens": 0,
                "round_count": 0,
            },
            id="bool-content",
        ),
        pytest.param(
            {
                "session_id": "session-b",
                "messages": [],
                "total_tokens": True,
                "round_count": 0,
            },
            id="bool-total-tokens",
        ),
        pytest.param(
            {
                "session_id": "session-b",
                "messages": [],
                "total_tokens": 0,
                "round_count": -1,
            },
            id="negative-round-count",
        ),
    ],
)
def test_malformed_session_load_preserves_previous_state(
    qtbot,
    corrupt_session,
) -> None:
    stream = ControlledStream([ChatStreamEvent(content="unused")])
    vm, _, store, _, _ = _vm(qtbot, stream=stream)
    before = (
        vm.current_session_id,
        copy.deepcopy(vm.current_messages),
        vm.current_total_tokens,
        vm.current_round_count,
    )
    store.get_session = MagicMock(return_value=corrupt_session)
    errors: list[str] = []
    loaded: list[str] = []
    vm.error_occurred.connect(errors.append)
    vm.session_loaded.connect(loaded.append)

    assert vm.load_session("session-b") is False

    assert (
        vm.current_session_id,
        vm.current_messages,
        vm.current_total_tokens,
        vm.current_round_count,
    ) == before
    assert errors == ["加载会话失败，请检查本地数据库状态"]
    assert loaded == []


@pytest.mark.asyncio
async def test_url_archive_malformed_existing_row_fails_before_workflow_construction(
    qtbot,
) -> None:
    """Only ``None`` is a cache miss; a malformed row is a terminal read error."""

    stream = ControlledStream([ChatStreamEvent(content="unused")])
    vm, _, store, _, _ = _vm(qtbot, stream=stream)
    store.query_by_url = MagicMock(return_value={})
    failures: list[tuple[Any, ...]] = []
    completions: list[tuple[Any, ...]] = []
    vm.url_archive_operation_failed.connect(
        lambda *args: failures.append(tuple(args))
    )
    vm.url_archive_operation_completed.connect(
        lambda *args: completions.append(tuple(args))
    )
    inject_context = MagicMock(wraps=vm.set_knowledge_context)
    before = copy.deepcopy(vm.current_messages)
    url = "https://a.example/malformed-existing"
    operation_id = "archive-op-malformed-existing"

    with (
        patch("src.workflow.engine.WorkflowEngine") as engine_constructor,
        patch.object(vm, "set_knowledge_context", inject_context),
    ):
        await vm.archive_url_and_inject.__wrapped__(
            vm,
            url,
            "session-a",
            operation_id,
        )

    assert failures == [
        (
            "session-a",
            operation_id,
            url,
            "归档失败（错误代码：workflow_step_failed，阶段：workflow_result）",
        )
    ]
    assert completions == []
    engine_constructor.assert_not_called()
    inject_context.assert_not_called()
    assert vm.current_messages == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("url", "expected_code"),
    [
        ("http://127.0.0.1/private", "ssrf_target_forbidden"),
        ("not-a-url", "url_invalid"),
    ],
)
async def test_url_archive_preflight_precedes_cache_lookup(
    qtbot,
    url,
    expected_code,
) -> None:
    stream = ControlledStream([ChatStreamEvent(content="unused")])
    vm, _, store, _, _ = _vm(qtbot, stream=stream)
    store.query_by_url = MagicMock(
        return_value={"knowledge_id": 99, "title": "must-not-complete"}
    )
    failures: list[tuple[Any, ...]] = []
    completions: list[tuple[Any, ...]] = []
    vm.url_archive_operation_failed.connect(
        lambda *args: failures.append(tuple(args))
    )
    vm.url_archive_operation_completed.connect(
        lambda *args: completions.append(tuple(args))
    )

    with patch("src.workflow.engine.WorkflowEngine") as engine_constructor:
        await vm.archive_url_and_inject.__wrapped__(
            vm,
            url,
            "session-a",
            "archive-op-preflight",
        )

    assert len(failures) == 1
    assert f"错误代码：{expected_code}" in failures[0][3]
    assert "阶段：url_preflight" in failures[0][3]
    assert completions == []
    store.query_by_url.assert_not_called()
    engine_constructor.assert_not_called()
    assert "session-a" not in vm._pending_knowledge_contexts


@pytest.mark.asyncio
async def test_url_archive_durable_title_never_uses_duck_string_coercion(
    qtbot,
    caplog,
) -> None:
    """A malformed durable field fails without invoking attacker methods."""

    sentinel = "archive-title-duck-secret"

    class DuckTitle:
        calls = 0

        def __str__(self) -> str:
            type(self).calls += 1
            return sentinel

    class SuccessfulWorkflowEngine:
        async def execute_async(self, workflow: str, payload: dict[str, Any]):
            return SimpleNamespace(
                terminal="success",
                success=True,
                data=_url_completion_data(99),
                errors=[],
                warnings=[],
                issues=[],
            )

    stream = ControlledStream([ChatStreamEvent(content="unused")])
    vm, _, store, _, _ = _vm(qtbot, stream=stream)
    store.query_by_id = MagicMock(
        return_value={"knowledge_id": 99, "title": DuckTitle()}
    )
    failures: list[tuple[Any, ...]] = []
    completions: list[tuple[Any, ...]] = []
    vm.url_archive_operation_failed.connect(
        lambda *args: failures.append(tuple(args))
    )
    vm.url_archive_operation_completed.connect(
        lambda *args: completions.append(tuple(args))
    )
    inject_context = MagicMock(wraps=vm.set_knowledge_context)
    before = copy.deepcopy(vm.current_messages)
    url = "https://a.example/duck-title"
    operation_id = "archive-op-duck-title"

    with (
        patch(
            "src.workflow.engine.WorkflowEngine",
            return_value=SuccessfulWorkflowEngine(),
        ) as engine_constructor,
        patch.object(vm, "set_knowledge_context", inject_context),
        caplog.at_level("INFO", logger="pkv.gui.viewmodels.chat"),
    ):
        await vm.archive_url_and_inject.__wrapped__(
            vm,
            url,
            "session-a",
            operation_id,
        )

    assert failures == [
        (
            "session-a",
            operation_id,
            url,
            "归档失败（错误代码：workflow_step_failed，阶段：workflow_result）",
        )
    ]
    assert completions == []
    engine_constructor.assert_called_once_with()
    store.query_by_id.assert_called_once_with(99)
    inject_context.assert_not_called()
    assert DuckTitle.calls == 0
    assert sentinel not in caplog.text
    assert vm.current_messages == before


@pytest.mark.asyncio
async def test_url_archive_completion_cannot_inject_origin_a_context_into_b(
    qtbot,
) -> None:
    """Archive ownership is frozen before the A -> B barrier is released."""

    class BarrierWorkflowEngine:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def execute_async(self, workflow: str, payload: dict[str, Any]):
            self.started.set()
            await self.release.wait()
            return SimpleNamespace(
                terminal="success",
                success=True,
                data=_url_completion_data(99),
                errors=[],
                warnings=[],
                issues=[],
            )

    stream = ControlledStream([ChatStreamEvent(content="answer-for-b")])
    states = {
        "session-a": _state("a-before", tokens=8, rounds=1),
        "session-b": _state("b-before", tokens=40, rounds=4),
    }
    vm, _, store, _, _ = _vm(qtbot, stream=stream, states=states)
    store.entries_by_id[99] = {
        "knowledge_id": 99,
        "title": "Archived A",
        "source_type": "web",
        "source_url": "https://a.example/article",
        "summary_one_sentence": "origin-a-only-context",
    }
    engine = BarrierWorkflowEngine()

    with patch("src.gui.views.chat_view.ChatViewModel", return_value=vm):
        view = ChatView()
    qtbot.addWidget(view)
    inject_context = MagicMock(wraps=vm.set_knowledge_context)

    with (
        patch("src.workflow.engine.WorkflowEngine", return_value=engine),
        patch.object(vm, "set_knowledge_context", inject_context),
    ):
        operation_id = "archive-op-session-a"
        archive_task = asyncio.create_task(
            vm.archive_url_and_inject.__wrapped__(
                vm,
                "https://a.example/article",
                "session-a",
                operation_id,
            )
        )
        await asyncio.wait_for(engine.started.wait(), timeout=1.0)
        assert ("session-a", operation_id) in view._pending_url_archives

        view._on_session_selected("session-b")
        visible_b = copy.deepcopy(vm.current_messages)
        engine.release.set()
        await asyncio.wait_for(archive_task, timeout=1.0)

        inject_context.assert_not_called()
        assert ("session-a", operation_id) not in view._pending_url_archives
        assert vm.current_session_id == "session-b"
        assert vm.current_messages == visible_b
        assert "Archived A" not in view.chat_area.message_display.toPlainText()

    next_request = vm._reserve_request("question-for-b")
    assert next_request is not None
    provider_payload = [dict(message) for message in next_request.request_messages]
    assert all(
        "origin-a-only-context" not in str(message.get("content", ""))
        for message in provider_payload
    )
    vm._release_request(next_request)


@pytest.mark.asyncio
async def test_same_turn_url_completions_merge_in_detection_order_and_persist(
    qtbot,
) -> None:
    first_stream = ControlledStream(
        [ChatStreamEvent(content="first answer")],
        gate_at=0,
    )
    vm, _, store, _, factory = _vm(qtbot, stream=first_stream)
    request_task = asyncio.create_task(
        vm.send_message.__wrapped__(vm, "two archive URLs")
    )
    await asyncio.wait_for(first_stream.waiting.wait(), timeout=1.0)

    first_operation = "archive-op-first"
    second_operation = "archive-op-second"
    vm._register_url_archive_operation("session-a", first_operation)
    vm._register_url_archive_operation("session-a", second_operation)
    first_entry = {
        "knowledge_id": 101,
        "title": "First URL",
        "source_type": "web",
        "source_url": "https://first.example/article",
        "summary_one_sentence": "first-url-context",
    }
    second_entry = {
        "knowledge_id": 102,
        "title": "Second URL",
        "source_type": "web",
        "source_url": "https://second.example/article",
        "summary_one_sentence": "second-url-context",
    }

    # Completion order is intentionally the reverse of synchronous detection.
    assert vm._publish_url_archive_completion(
        "session-a",
        second_operation,
        second_entry["source_url"],
        second_entry,
        "second.example",
    )
    assert vm._publish_url_archive_completion(
        "session-a",
        first_operation,
        first_entry["source_url"],
        first_entry,
        "first.example",
    )

    first_stream.release.set()
    assert await asyncio.wait_for(request_task, timeout=1.0) is True

    stored_context = store.states["session-a"]["messages"][0]["content"]
    assert stored_context.count("first-url-context") == 1
    assert stored_context.count("second-url-context") == 1
    assert stored_context.index("first-url-context") < stored_context.index(
        "second-url-context"
    )
    assert "session-a" not in vm._pending_knowledge_contexts

    second_stream = ControlledStream([ChatStreamEvent(content="second answer")])
    second_provider = FakeProvider(second_stream)
    factory.providers.append(second_provider)
    assert await vm.send_message.__wrapped__(vm, "next question") is True

    next_context = next(
        message["content"]
        for message in second_provider.messages
        if message["role"] == "system"
    )
    assert next_context.count("first-url-context") == 1
    assert next_context.count("second-url-context") == 1


@pytest.mark.asyncio
async def test_ready_url_context_queue_is_not_duplicated_at_commit(qtbot) -> None:
    stream = ControlledStream([ChatStreamEvent(content="answer")])
    states = {
        "session-a": _state("a-before", tokens=8, rounds=1),
        "session-b": _state("b-before", tokens=40, rounds=4),
    }
    vm, _, store, _, _ = _vm(qtbot, stream=stream, states=states)
    assert vm.load_session("session-b")

    entries = (
        {
            "knowledge_id": 201,
            "title": "Queued First",
            "source_type": "web",
            "source_url": "https://queued-first.example/article",
            "summary_one_sentence": "queued-first-context",
        },
        {
            "knowledge_id": 202,
            "title": "Queued Second",
            "source_type": "web",
            "source_url": "https://queued-second.example/article",
            "summary_one_sentence": "queued-second-context",
        },
    )
    operations = ("queued-op-first", "queued-op-second")
    for operation_id in operations:
        vm._register_url_archive_operation("session-a", operation_id)
    for operation_id, entry in zip(reversed(operations), reversed(entries)):
        assert vm._publish_url_archive_completion(
            "session-a",
            operation_id,
            entry["source_url"],
            entry,
            "queued.example",
        )

    assert vm.load_session("session-a")
    assert await vm.send_message.__wrapped__(vm, "consume queued refs") is True

    stored_context = store.states["session-a"]["messages"][0]["content"]
    assert stored_context.count("queued-first-context") == 1
    assert stored_context.count("queued-second-context") == 1
    assert stored_context.index("queued-first-context") < stored_context.index(
        "queued-second-context"
    )
    assert "session-a" not in vm._pending_knowledge_contexts


@pytest.mark.asyncio
async def test_idle_url_context_survives_session_reload_until_durable_commit(
    qtbot,
) -> None:
    stream = ControlledStream([ChatStreamEvent(content="answer")])
    states = {
        "session-a": _state("a-before", tokens=8, rounds=1),
        "session-b": _state("b-before", tokens=40, rounds=4),
    }
    vm, _, store, provider, _ = _vm(qtbot, stream=stream, states=states)
    operation_id = "idle-url-operation"
    entry = {
        "knowledge_id": 301,
        "title": "Idle URL",
        "source_type": "web",
        "source_url": "https://idle.example/article",
        "summary_one_sentence": "idle-url-durable-context",
    }
    vm._register_url_archive_operation("session-a", operation_id)

    assert vm._publish_url_archive_completion(
        "session-a",
        operation_id,
        entry["source_url"],
        entry,
        "idle.example",
    )
    assert "idle-url-durable-context" in vm.current_messages[0]["content"]
    assert operation_id in vm._pending_knowledge_contexts["session-a"]

    assert vm.load_session("session-b")
    assert vm.load_session("session-a")
    assert all(
        "idle-url-durable-context" not in message["content"]
        for message in vm.current_messages
    )
    assert operation_id in vm._pending_knowledge_contexts["session-a"]

    assert await vm.send_message.__wrapped__(vm, "persist idle reference") is True

    provider_context = next(
        message["content"]
        for message in provider.messages
        if message["role"] == "system"
    )
    stored_context = store.states["session-a"]["messages"][0]["content"]
    assert provider_context.count("idle-url-durable-context") == 1
    assert stored_context.count("idle-url-durable-context") == 1
    assert "session-a" not in vm._pending_knowledge_contexts


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "workflow_result",
    [
        pytest.param(
            SimpleNamespace(
                success=True,
                data={"knowledge_id": 99},
                errors=[],
                warnings=[],
                issues=[],
            ),
            id="missing-terminal-with-true-success",
        ),
        pytest.param(
            SimpleNamespace(
                terminal="committed",
                success=True,
                data={"knowledge_id": 99},
                errors=[],
                warnings=[],
                issues=[],
            ),
            id="unknown-terminal-with-true-success",
        ),
        pytest.param(
            SimpleNamespace(
                terminal=True,
                success=True,
                data={"knowledge_id": 99},
                errors=[],
                warnings=[],
                issues=[],
            ),
            id="non-string-terminal-with-true-success",
        ),
        pytest.param(
            SimpleNamespace(
                terminal="success",
                success=False,
                data={"knowledge_id": 99},
                errors=[],
                warnings=[],
                issues=[],
            ),
            id="success-terminal-with-false-success",
        ),
        pytest.param(
            SimpleNamespace(
                terminal="error",
                success=True,
                data={},
                errors=[],
                warnings=[],
                issues=[],
            ),
            id="error-terminal-with-true-success",
        ),
        pytest.param(
            SimpleNamespace(
                terminal="success",
                success=1,
                data={"knowledge_id": 99},
                errors=[],
                warnings=[],
                issues=[],
            ),
            id="non-bool-success",
        ),
        pytest.param(
            SimpleNamespace(
                terminal="success",
                success=True,
                data=[{"knowledge_id": 99}],
                errors=[],
                warnings=[],
                issues=[],
            ),
            id="non-dict-data",
        ),
        pytest.param(
            SimpleNamespace(
                terminal="success",
                success=True,
                data={},
                errors=[],
                warnings=[],
                issues=[],
            ),
            id="missing-knowledge-id",
        ),
        pytest.param(
            SimpleNamespace(
                terminal="degraded",
                success=True,
                data={"knowledge_id": 0},
                errors=[],
                warnings=["repair required"],
                issues=[],
            ),
            id="zero-knowledge-id",
        ),
        pytest.param(
            SimpleNamespace(
                terminal="success",
                success=True,
                data={"knowledge_id": True},
                errors=[],
                warnings=[],
                issues=[],
            ),
            id="bool-knowledge-id",
        ),
        pytest.param(
            SimpleNamespace(
                terminal="success",
                success=True,
                data={"knowledge_id": 99},
                errors=[],
                warnings=[],
                issues=[],
            ),
            id="missing-storage-status",
        ),
        pytest.param(
            SimpleNamespace(
                terminal="success",
                success=True,
                data={
                    "knowledge_id": 99,
                    "status": "unknown-storage-status-secret",
                },
                errors=[],
                warnings=[],
                issues=[],
            ),
            id="unknown-storage-status",
        ),
        pytest.param(
            SimpleNamespace(
                terminal="success",
                success=True,
                data=_url_completion_data(99, status="degraded"),
                errors=[],
                warnings=[],
                issues=[],
            ),
            id="success-terminal-with-degraded-status",
        ),
        pytest.param(
            SimpleNamespace(
                terminal="success",
                success=True,
                data={
                    key: value
                    for key, value in _url_completion_data(99).items()
                    if key != "core_committed"
                },
                errors=[],
                warnings=[],
                issues=[],
            ),
            id="missing-core-committed-proof",
        ),
        pytest.param(
            SimpleNamespace(
                terminal="success",
                success=True,
                data=_url_completion_data(99, core_committed=False),
                errors=[],
                warnings=[],
                issues=[],
            ),
            id="false-core-committed-proof",
        ),
        pytest.param(
            SimpleNamespace(
                terminal="success",
                success=True,
                data=MappingProxyType(_url_completion_data(99)),
                errors=[],
                warnings=[],
                issues=[],
            ),
            id="mapping-data",
        ),
    ],
)
async def test_url_archive_malformed_result_fails_scoped_without_context_injection(
    qtbot,
    caplog,
    workflow_result,
) -> None:
    """Only a self-consistent, typed WorkflowResult may complete A's archive."""

    class MalformedWorkflowEngine:
        async def execute_async(self, workflow: str, payload: dict[str, Any]):
            return workflow_result

    stream = ControlledStream([ChatStreamEvent(content="unused")])
    vm, _, store, _, _ = _vm(qtbot, stream=stream)
    store.entries_by_id[99] = {
        "knowledge_id": 99,
        "title": "must-not-be-injected",
        "source_type": "web",
        "source_url": "https://a.example/malformed",
        "summary_one_sentence": "must-not-enter-context",
    }

    with patch("src.gui.views.chat_view.ChatViewModel", return_value=vm):
        view = ChatView()
    qtbot.addWidget(view)

    scoped_completed: list[tuple[Any, ...]] = []
    scoped_failed: list[tuple[Any, ...]] = []
    legacy_completed: list[tuple[Any, ...]] = []
    vm.url_archive_operation_completed.connect(
        lambda *args: scoped_completed.append(tuple(args))
    )
    vm.url_archive_operation_failed.connect(
        lambda *args: scoped_failed.append(tuple(args))
    )
    vm.url_archive_completed.connect(
        lambda *args: legacy_completed.append(tuple(args))
    )
    inject_context = MagicMock(wraps=vm.set_knowledge_context)
    before = copy.deepcopy(vm.current_messages)
    url = "https://a.example/malformed"
    operation_id = "archive-op-session-a-malformed"
    expected_error = (
        "归档失败（错误代码：workflow_step_failed，阶段：workflow_result）"
    )

    with (
        patch(
            "src.workflow.engine.WorkflowEngine",
            return_value=MalformedWorkflowEngine(),
        ),
        patch.object(vm, "set_knowledge_context", inject_context),
        caplog.at_level("WARNING", logger="pkv.gui.viewmodels.chat"),
    ):
        await vm.archive_url_and_inject.__wrapped__(
            vm,
            url,
            "session-a",
            operation_id,
        )

    assert scoped_failed == [
        ("session-a", operation_id, url, expected_error)
    ]
    assert scoped_completed == []
    assert legacy_completed == []
    inject_context.assert_not_called()
    assert vm.current_messages == before
    assert ("session-a", operation_id) not in view._pending_url_archives
    rendered = view.chat_area.message_display.toPlainText()
    assert "must-not-be-injected" not in rendered
    assert "unknown-storage-status-secret" not in rendered
    assert "unknown-storage-status-secret" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("terminal", "errors", "warnings", "issues"),
    [
        pytest.param("success", (), [], [], id="errors-not-exact-list"),
        pytest.param("success", [], (), [], id="warnings-not-exact-list"),
        pytest.param("success", [], [], (), id="issues-not-exact-list"),
        pytest.param(
            "success",
            ["diagnostic-canary-secret"],
            [],
            [],
            id="success-conceals-error",
        ),
        pytest.param(
            "success",
            [],
            ["diagnostic-canary-secret"],
            [],
            id="success-conceals-warning",
        ),
        pytest.param(
            "success",
            [],
            [],
            [{"severity": "warning", "message": "diagnostic-canary-secret"}],
            id="success-conceals-warning-issue",
        ),
        pytest.param("degraded", [], [], [], id="degraded-without-diagnostic"),
        pytest.param("degraded", [], [7], [], id="warning-not-string"),
        pytest.param(
            "degraded",
            [],
            [],
            ["diagnostic-canary-secret"],
            id="issue-not-dict",
        ),
        pytest.param(
            "degraded",
            [],
            [],
            [{"severity": "error", "message": "diagnostic-canary-secret"}],
            id="degraded-error-issue",
        ),
    ],
)
async def test_url_archive_invalid_completion_diagnostics_fail_before_data_access(
    qtbot,
    caplog,
    terminal,
    errors,
    warnings,
    issues,
) -> None:
    class InvalidDiagnosticsResult:
        success = True

        def __init__(self) -> None:
            self.terminal = terminal
            self.errors = errors
            self.warnings = warnings
            self.issues = issues
            self.data_reads = 0

        @property
        def data(self) -> dict[str, Any]:
            self.data_reads += 1
            return {"knowledge_id": 99, "status": "ready"}

    result = InvalidDiagnosticsResult()

    class InvalidDiagnosticsWorkflowEngine:
        async def execute_async(self, workflow: str, payload: dict[str, Any]):
            return result

    stream = ControlledStream([ChatStreamEvent(content="unused")])
    vm, _, store, _, _ = _vm(qtbot, stream=stream)
    query_by_id = MagicMock(wraps=store.query_by_id)
    failures: list[tuple[Any, ...]] = []
    completions: list[tuple[Any, ...]] = []
    vm.url_archive_operation_failed.connect(
        lambda *args: failures.append(tuple(args))
    )
    vm.url_archive_operation_completed.connect(
        lambda *args: completions.append(tuple(args))
    )
    url = "https://a.example/invalid-diagnostics"
    operation_id = "archive-op-invalid-diagnostics"

    with (
        patch(
            "src.workflow.engine.WorkflowEngine",
            return_value=InvalidDiagnosticsWorkflowEngine(),
        ),
        patch.object(store, "query_by_id", query_by_id),
        caplog.at_level("INFO"),
    ):
        await vm.archive_url_and_inject.__wrapped__(
            vm,
            url,
            "session-a",
            operation_id,
        )

    assert failures == [
        (
            "session-a",
            operation_id,
            url,
            "归档失败（错误代码：workflow_step_failed，阶段：workflow_result）",
        )
    ]
    assert completions == []
    assert result.data_reads == 0
    query_by_id.assert_not_called()
    assert "diagnostic-canary-secret" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("terminal", "durable_entry"),
    [
        pytest.param("success", None, id="success-missing-row"),
        pytest.param("degraded", None, id="degraded-missing-row"),
        pytest.param("success", {}, id="entry-missing-id"),
        pytest.param(
            "success",
            {"knowledge_id": 100, "title": "wrong-row"},
            id="entry-id-mismatch",
        ),
        pytest.param(
            "success",
            {"knowledge_id": True, "title": "bool-row"},
            id="entry-bool-id",
        ),
    ],
)
async def test_url_archive_invalid_durable_entry_never_completes_or_injects_context(
    qtbot,
    terminal,
    durable_entry,
) -> None:
    """A valid workflow payload cannot replace the durable entry readback."""

    class SuccessfulWorkflowEngine:
        async def execute_async(self, workflow: str, payload: dict[str, Any]):
            return SimpleNamespace(
                terminal=terminal,
                success=True,
                data=_url_completion_data(
                    99,
                    status=(
                        "degraded" if terminal == "degraded" else "ready"
                    ),
                    title="unverified-missing-row-title",
                    summary_one_sentence="unverified-missing-row-context",
                ),
                errors=[],
                warnings=(
                    ["repair required"] if terminal == "degraded" else []
                ),
                issues=[],
            )

    stream = ControlledStream([ChatStreamEvent(content="unused")])
    vm, _, store, _, _ = _vm(qtbot, stream=stream)
    if durable_entry is not None:
        store.entries_by_id[99] = durable_entry

    with patch("src.gui.views.chat_view.ChatViewModel", return_value=vm):
        view = ChatView()
    qtbot.addWidget(view)

    scoped_completed: list[tuple[Any, ...]] = []
    scoped_failed: list[tuple[Any, ...]] = []
    scoped_warning: list[tuple[Any, ...]] = []
    legacy_completed: list[tuple[Any, ...]] = []
    legacy_warning: list[tuple[Any, ...]] = []
    vm.url_archive_operation_completed.connect(
        lambda *args: scoped_completed.append(tuple(args))
    )
    vm.url_archive_operation_failed.connect(
        lambda *args: scoped_failed.append(tuple(args))
    )
    vm.url_archive_operation_warning.connect(
        lambda *args: scoped_warning.append(tuple(args))
    )
    vm.url_archive_completed.connect(
        lambda *args: legacy_completed.append(tuple(args))
    )
    vm.url_archive_warning.connect(
        lambda *args: legacy_warning.append(tuple(args))
    )
    inject_context = MagicMock(wraps=vm.set_knowledge_context)
    before = copy.deepcopy(vm.current_messages)
    url = "https://a.example/missing-row"
    operation_id = "archive-op-session-a-missing-row"
    expected_error = (
        "归档失败（错误代码：workflow_step_failed，阶段：workflow_result）"
    )

    with (
        patch(
            "src.workflow.engine.WorkflowEngine",
            return_value=SuccessfulWorkflowEngine(),
        ),
        patch.object(vm, "set_knowledge_context", inject_context),
    ):
        await vm.archive_url_and_inject.__wrapped__(
            vm,
            url,
            "session-a",
            operation_id,
        )

    assert scoped_failed == [
        ("session-a", operation_id, url, expected_error)
    ]
    assert scoped_completed == []
    assert scoped_warning == []
    assert legacy_completed == []
    assert legacy_warning == []
    inject_context.assert_not_called()
    assert vm.current_messages == before
    assert ("session-a", operation_id) not in view._pending_url_archives
    rendered = view.chat_area.message_display.toPlainText()
    assert "unverified-missing-row-title" not in rendered
    assert "unverified-missing-row-context" not in rendered


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("storage_status", "expected_code", "expect_do_not_retry"),
    [
        ("repair_required", "workflow_step_failed", False),
        ("rejected", "workflow_step_failed", False),
    ],
)
async def test_url_archive_fatal_storage_status_never_completes_or_injects(
    qtbot,
    storage_status,
    expected_code,
    expect_do_not_retry,
) -> None:
    class FatalStorageWorkflowEngine:
        async def execute_async(self, workflow: str, payload: dict[str, Any]):
            return SimpleNamespace(
                terminal="success",
                success=True,
                data={
                    "knowledge_id": 99,
                    "status": storage_status,
                    "operation_id": "a" * 32,
                    "repair_actions": ["repair_operation_journal"],
                },
                errors=[],
                warnings=[],
                issues=[],
            )

    stream = ControlledStream([ChatStreamEvent(content="unused")])
    vm, _, store, _, _ = _vm(qtbot, stream=stream)
    store.entries_by_id[99] = {
        "knowledge_id": 99,
        "title": "must-not-inject-fatal-storage-entry",
    }
    with patch("src.gui.views.chat_view.ChatViewModel", return_value=vm):
        view = ChatView()
    qtbot.addWidget(view)

    failed: list[tuple[Any, ...]] = []
    completed: list[tuple[Any, ...]] = []
    warnings: list[tuple[Any, ...]] = []
    vm.url_archive_operation_failed.connect(
        lambda *args: failed.append(tuple(args))
    )
    vm.url_archive_operation_completed.connect(
        lambda *args: completed.append(tuple(args))
    )
    vm.url_archive_operation_warning.connect(
        lambda *args: warnings.append(tuple(args))
    )
    inject_context = MagicMock(wraps=vm.set_knowledge_context)

    with (
        patch(
            "src.workflow.engine.WorkflowEngine",
            return_value=FatalStorageWorkflowEngine(),
        ),
        patch.object(vm, "set_knowledge_context", inject_context),
    ):
        await vm.archive_url_and_inject.__wrapped__(
            vm,
            "https://a.example/fatal-storage",
            "session-a",
            "archive-op-fatal-storage",
        )

    assert len(failed) == 1
    message = failed[0][3]
    assert f"错误代码：{expected_code}" in message
    assert completed == []
    assert warnings == []
    inject_context.assert_not_called()
    assert "must-not-inject-fatal-storage-entry" not in (
        view.chat_area.message_display.toPlainText()
    )
    assert ("请勿盲目重试" in message) is expect_do_not_retry


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_stage", ["reference-build", "context-injection"])
async def test_url_archive_reference_preflight_failure_is_single_failed_terminal(
    qtbot,
    caplog,
    failure_stage,
) -> None:
    """Reference construction/injection must finish before completed is visible."""

    class DegradedWorkflowEngine:
        async def execute_async(self, workflow: str, payload: dict[str, Any]):
            return SimpleNamespace(
                terminal="degraded",
                success=True,
                data=_url_completion_data(
                    99,
                    status="degraded",
                    repair_actions=["rebuild_index"],
                ),
                errors=[],
                warnings=["repair required"],
                issues=[],
            )

    sentinel = f"url-reference-{failure_stage}-secret"
    stream = ControlledStream([ChatStreamEvent(content="unused")])
    vm, _, store, _, _ = _vm(qtbot, stream=stream)
    store.entries_by_id[99] = {
        "knowledge_id": 99,
        "title": "must-not-render-after-preflight-failure",
        "source_type": "web",
        "source_url": "https://a.example/preflight",
        "summary_one_sentence": "must-not-enter-context-after-failure",
    }

    with patch("src.gui.views.chat_view.ChatViewModel", return_value=vm):
        view = ChatView()
    qtbot.addWidget(view)

    scoped_completed: list[tuple[Any, ...]] = []
    scoped_failed: list[tuple[Any, ...]] = []
    scoped_warning: list[tuple[Any, ...]] = []
    legacy_completed: list[tuple[Any, ...]] = []
    vm.url_archive_operation_completed.connect(
        lambda *args: scoped_completed.append(tuple(args))
    )
    vm.url_archive_operation_failed.connect(
        lambda *args: scoped_failed.append(tuple(args))
    )
    vm.url_archive_operation_warning.connect(
        lambda *args: scoped_warning.append(tuple(args))
    )
    vm.url_archive_completed.connect(
        lambda *args: legacy_completed.append(tuple(args))
    )

    if failure_stage == "reference-build":
        failure = patch(
            "src.gui.utils.knowledge_ref.build_knowledge_reference",
            side_effect=RuntimeError(sentinel),
        )
    else:
        failure = patch.object(
            vm,
            "set_knowledge_context",
            side_effect=RuntimeError(sentinel),
        )

    before = copy.deepcopy(vm.current_messages)
    url = "https://a.example/preflight"
    operation_id = f"archive-op-session-a-{failure_stage}"
    expected_error = (
        "归档失败（错误代码：workflow_step_failed，阶段：workflow_result）"
    )
    with (
        patch(
            "src.workflow.engine.WorkflowEngine",
            return_value=DegradedWorkflowEngine(),
        ),
        failure,
        caplog.at_level("ERROR", logger="pkv.gui.viewmodels.chat"),
    ):
        await vm.archive_url_and_inject.__wrapped__(
            vm,
            url,
            "session-a",
            operation_id,
        )

    assert scoped_failed == [
        ("session-a", operation_id, url, expected_error)
    ]
    assert scoped_completed == []
    assert scoped_warning == []
    assert legacy_completed == []
    assert vm.current_messages == before
    assert ("session-a", operation_id) not in view._pending_url_archives
    rendered = view.chat_area.message_display.toPlainText()
    assert "must-not-render-after-preflight-failure" not in rendered
    assert "must-not-enter-context-after-failure" not in rendered
    assert sentinel not in caplog.text


def test_begin_url_archive_freezes_current_session_and_operation_id(qtbot) -> None:
    stream = ControlledStream([ChatStreamEvent(content="unused")])
    states = {
        "session-a": _state("a-before", tokens=8, rounds=1),
        "session-b": _state("b-before", tokens=40, rounds=4),
    }
    vm, _, _, _, _ = _vm(qtbot, stream=stream, states=states)

    with patch.object(vm, "archive_url_and_inject") as schedule:
        operation_id = vm.begin_url_archive("https://a.example/article")
        assert vm.load_session("session-b")

    assert operation_id
    schedule.assert_called_once_with(
        "https://a.example/article",
        "session-a",
        operation_id,
    )


def test_same_session_stale_events_cannot_pollute_new_request() -> None:
    """Old queued Qt events are ignored after stop -> immediate resend."""

    renderer = SimpleNamespace(add_token=MagicMock())
    chat_area = SimpleNamespace(
        stream_renderer=renderer,
        finish_assistant_message=MagicMock(),
    )
    view = SimpleNamespace(
        _active_ui_request=("session-a", "request-new"),
        _pending_user_messages={
            ("session-a", "request-old"): "old",
            ("session-a", "request-new"): "new",
        },
        viewmodel=SimpleNamespace(current_session_id="session-a"),
        chat_area=chat_area,
        _restore_send_controls=MagicMock(),
    )

    ChatView._on_chat_token_received(
        view,
        "session-a",
        "request-old",
        "late-old-token",
    )
    ChatView._on_chat_request_completed(
        view,
        "session-a",
        "request-old",
    )
    ChatView._on_chat_request_stopped(
        view,
        "session-a",
        "request-old",
    )
    ChatView._on_chat_request_failed(
        view,
        "session-a",
        "request-old",
        "chat_provider_failed",
        "late-old-error",
    )

    renderer.add_token.assert_not_called()
    chat_area.finish_assistant_message.assert_not_called()
    view._restore_send_controls.assert_not_called()
    assert view._active_ui_request == ("session-a", "request-new")
    assert view._pending_user_messages[("session-a", "request-new")] == "new"

    ChatView._on_chat_token_received(
        view,
        "session-a",
        "request-new",
        "new-token",
    )
    renderer.add_token.assert_called_once_with("new-token")


def test_busy_view_rejection_creates_no_ghost_turn() -> None:
    input_box = SimpleNamespace(
        toPlainText=MagicMock(return_value="second message"),
        clear=MagicMock(),
    )
    chat_area = SimpleNamespace(
        input_area=SimpleNamespace(input_box=input_box),
        add_user_message=MagicMock(),
        start_assistant_message=MagicMock(),
    )
    viewmodel = SimpleNamespace(
        can_dispatch_message=MagicMock(return_value=False),
    )
    view = SimpleNamespace(chat_area=chat_area, viewmodel=viewmodel)

    ChatView._on_send_clicked(view)

    viewmodel.can_dispatch_message.assert_called_once_with("second message")
    chat_area.add_user_message.assert_not_called()
    chat_area.start_assistant_message.assert_not_called()
    input_box.clear.assert_not_called()


def test_reference_search_consumes_degraded_response_explicitly() -> None:
    result = SearchResult(
        knowledge_id=7,
        title="Fixture",
        score=0.8,
        highlight="hit",
        metadata={},
    )
    response = SearchResponse.degraded_response(
        [result],
        [
            RetrievalIssue(
                code=ErrorCode.RETRIEVAL_INDEX_UNAVAILABLE,
                message="stable public message",
                stage="bm25",
                recoverable=True,
            )
        ],
        strategy="bm25",
    )
    retriever = SimpleNamespace(search=MagicMock(return_value=response))
    store = SimpleNamespace(
        query_by_id=MagicMock(
            return_value={"knowledge_id": 7, "title": "Fixture"}
        )
    )
    view = SimpleNamespace(
        _show_reference_status=MagicMock(),
    )

    with (
        patch("src.gui.stores.get_bm25_retriever", return_value=retriever),
        patch("src.gui.stores.get_sqlite_store", return_value=store),
        patch("src.gui.stores.get_markdown_store", return_value=MagicMock()),
        patch(
            "src.gui.views.chat_view.load_entry_preview_outcome",
            return_value=PreviewOutcome(status="success", content="preview"),
        ),
    ):
        resolution = ChatView._search_entry(view, "fixture")

    assert resolution.status == "degraded"
    assert resolution.entry["knowledge_id"] == 7
    assert resolution.content == "preview"
    assert [issue.code for issue in resolution.issues] == [
        ErrorCode.RETRIEVAL_INDEX_UNAVAILABLE
    ]


def test_reference_search_rejects_non_bm25_strategy_identity() -> None:
    result = SearchResult(
        knowledge_id=7,
        title="Fixture",
        score=0.8,
        highlight="hit",
        metadata={},
    )
    response = SearchResponse.completed([result], strategy="vector")
    retriever = SimpleNamespace(search=MagicMock(return_value=response))
    store = SimpleNamespace(query_by_id=MagicMock())

    with (
        patch("src.gui.stores.get_bm25_retriever", return_value=retriever),
        patch("src.gui.stores.get_sqlite_store", return_value=store),
    ):
        resolution = ChatView._search_entry(SimpleNamespace(), "fixture")

    assert resolution.status == "error"
    assert [issue.code for issue in resolution.issues] == [
        ErrorCode.RETRIEVAL_BACKEND_FAILED
    ]
    assert resolution.issues[0].stage == "reference_search"
    assert resolution.issues[0].cause_type == "InvalidSearchResponse"
    store.query_by_id.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("lookup_mode", "expected_code"),
    [
        ("missing", "resource_missing"),
        ("error", "storage_primary_failed"),
    ],
)
async def test_explicit_knowledge_reference_failure_never_dispatches_provider(
    qtbot,
    caplog,
    lookup_mode,
    expected_code,
) -> None:
    sentinel = "knowledge-db-failure-secret"
    stream = ControlledStream([ChatStreamEvent(content="must-not-run")])
    vm, _, _, provider, factory = _vm(qtbot, stream=stream)
    external_store = SimpleNamespace(query_by_id=MagicMock(return_value=None))
    if lookup_mode == "error":
        external_store.query_by_id.side_effect = RuntimeError(sentinel)

    with patch("src.gui.views.chat_view.ChatViewModel", return_value=vm):
        view = ChatView()
    qtbot.addWidget(view)
    view.chat_area.input_area.input_box.setPlainText(
        "请基于 @知识库/404 回答"
    )
    before = copy.deepcopy(vm.current_messages)

    with (
        patch("src.gui.stores.get_sqlite_store", return_value=external_store),
        caplog.at_level("ERROR", logger="pkv.gui.views.chat"),
    ):
        view._on_send_clicked()
        await asyncio.sleep(0)

    assert factory.calls == 0
    assert provider.open_calls == 0
    assert vm.active_request_id is None
    assert vm.current_messages == before
    assert view.chat_area.input_area.input_box.toPlainText()
    rendered = view.chat_area.message_display.toPlainText()
    assert "status=error" in rendered
    assert f"code={expected_code}" in rendered
    assert "本轮未发送" in rendered
    assert sentinel not in rendered
    assert sentinel not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("search_mode", "expected_code"),
    [
        ("no_hits", "resource_missing"),
        ("missing_entry", "retrieval_metadata_inconsistent"),
        ("backend_error", "retrieval_backend_failed"),
    ],
)
async def test_explicit_search_reference_failure_never_dispatches_provider(
    qtbot,
    caplog,
    search_mode,
    expected_code,
) -> None:
    sentinel = "search-reference-failure-secret"
    stream = ControlledStream([ChatStreamEvent(content="must-not-run")])
    vm, _, _, provider, factory = _vm(qtbot, stream=stream)
    result = SearchResult(
        knowledge_id=7,
        title="Fixture",
        score=0.8,
        highlight="hit",
        metadata={},
    )
    response = (
        SearchResponse.completed([], strategy="bm25")
        if search_mode == "no_hits"
        else SearchResponse.completed([result], strategy="bm25")
    )
    retriever = SimpleNamespace(search=MagicMock(return_value=response))
    if search_mode == "backend_error":
        retriever.search.side_effect = RuntimeError(sentinel)
    external_store = SimpleNamespace(query_by_id=MagicMock(return_value=None))

    with patch("src.gui.views.chat_view.ChatViewModel", return_value=vm):
        view = ChatView()
    qtbot.addWidget(view)
    view.chat_area.input_area.input_box.setPlainText(
        "请基于 @搜索/fixture 回答"
    )
    before = copy.deepcopy(vm.current_messages)

    with (
        patch("src.gui.stores.get_bm25_retriever", return_value=retriever),
        patch("src.gui.stores.get_sqlite_store", return_value=external_store),
        caplog.at_level("ERROR", logger="pkv.gui.views.chat"),
    ):
        view._on_send_clicked()
        await asyncio.sleep(0)

    assert factory.calls == 0
    assert provider.open_calls == 0
    assert vm.active_request_id is None
    assert vm.current_messages == before
    rendered = view.chat_area.message_display.toPlainText()
    assert "status=error" in rendered
    assert f"code={expected_code}" in rendered
    assert "本轮未发送" in rendered
    assert sentinel not in rendered
    assert sentinel not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("reference_kind", ["knowledge", "search"])
@pytest.mark.parametrize(
    "malformed_field",
    ["title", "source_url", "summary_one_sentence", "file_path", "tags"],
)
async def test_malformed_reference_row_never_reaches_preview_or_provider(
    qtbot,
    caplog,
    reference_kind,
    malformed_field,
) -> None:
    sentinel = "REFERENCE-ROW-DUCK-CANARY"

    class DuckValue:
        calls = 0

        def __str__(self) -> str:
            type(self).calls += 1
            return sentinel

    stream = ControlledStream([ChatStreamEvent(content="must-not-run")])
    vm, _, _, provider, factory = _vm(qtbot, stream=stream)
    row = {
        "knowledge_id": 7,
        "title": "Fixture",
        "source_type": "web",
        "source_url": "https://example.test/article",
        "summary_one_sentence": "safe summary",
        "file_path": "vault/fixture.md",
        "tags": ["safe"],
    }
    row[malformed_field] = (
        [DuckValue()] if malformed_field == "tags" else DuckValue()
    )
    external_store = SimpleNamespace(query_by_id=MagicMock(return_value=row))
    result = SearchResult(
        knowledge_id=7,
        title="Fixture",
        score=0.8,
        highlight="hit",
        metadata={},
    )
    retriever = SimpleNamespace(
        search=MagicMock(
            return_value=SearchResponse.completed([result], strategy="bm25")
        )
    )
    message = (
        "请基于 @知识库/7 回答"
        if reference_kind == "knowledge"
        else "请基于 @搜索/fixture 回答"
    )
    expected_code = (
        "resource_not_readable"
        if reference_kind == "knowledge"
        else "retrieval_metadata_inconsistent"
    )
    preview = MagicMock(
        return_value=PreviewOutcome(status="success", content="must-not-preview")
    )

    with patch("src.gui.views.chat_view.ChatViewModel", return_value=vm):
        view = ChatView()
    qtbot.addWidget(view)
    view.chat_area.input_area.input_box.setPlainText(message)
    before = copy.deepcopy(vm.current_messages)

    with (
        patch("src.gui.stores.get_sqlite_store", return_value=external_store),
        patch("src.gui.stores.get_bm25_retriever", return_value=retriever),
        patch("src.gui.stores.get_markdown_store", return_value=MagicMock()),
        patch(
            "src.gui.views.chat_view.load_entry_preview_outcome",
            preview,
        ),
        caplog.at_level("INFO"),
    ):
        view._on_send_clicked()
        await asyncio.sleep(0)

    assert factory.calls == 0
    assert provider.open_calls == 0
    assert preview.call_count == 0
    assert vm.active_request_id is None
    assert vm.current_messages == before
    rendered = view.chat_area.message_display.toPlainText()
    assert "status=error" in rendered
    assert f"code={expected_code}" in rendered
    assert DuckValue.calls == 0
    assert sentinel not in rendered
    assert sentinel not in caplog.text


@pytest.mark.asyncio
async def test_preview_error_rejects_explicit_reference_before_provider(
    qtbot,
    caplog,
) -> None:
    sentinel = "preview-error-secret"
    stream = ControlledStream([ChatStreamEvent(content="must-not-run")])
    vm, _, _, provider, factory = _vm(qtbot, stream=stream)
    entry = {
        "knowledge_id": 7,
        "title": "Fixture",
        "file_path": f"C:/private/{sentinel}.md",
    }
    external_store = SimpleNamespace(query_by_id=MagicMock(return_value=entry))
    outcome = PreviewOutcome(
        status="error",
        content="",
        issue=PreviewIssue(
            code=ErrorCode.RESOURCE_NOT_READABLE,
            stage="preview_markdown",
            cause_type="PreviewErrorSecret",
        ),
    )

    with patch("src.gui.views.chat_view.ChatViewModel", return_value=vm):
        view = ChatView()
    qtbot.addWidget(view)
    view.chat_area.input_area.input_box.setPlainText(
        "请基于 @知识库/7 回答"
    )
    before = copy.deepcopy(vm.current_messages)

    with (
        patch("src.gui.stores.get_sqlite_store", return_value=external_store),
        patch("src.gui.stores.get_markdown_store", return_value=MagicMock()),
        patch(
            "src.gui.views.chat_view.load_entry_preview_outcome",
            return_value=outcome,
        ),
        caplog.at_level("ERROR", logger="pkv.gui.views.chat"),
    ):
        view._on_send_clicked()
        await asyncio.sleep(0)

    assert factory.calls == 0
    assert provider.open_calls == 0
    assert vm.active_request_id is None
    assert vm.current_messages == before
    rendered = view.chat_area.message_display.toPlainText()
    assert "status=error" in rendered
    assert "code=resource_not_readable" in rendered
    assert "stage=preview_markdown" in rendered
    assert sentinel not in rendered
    assert sentinel not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("reference_kind", ["knowledge", "search"])
async def test_corrupt_preview_outcome_rejects_without_dispatch_or_canary(
    qtbot,
    caplog,
    reference_kind,
) -> None:
    sentinel = "CORRUPT-PREVIEW-REFERENCE-CANARY"
    stream = ControlledStream([ChatStreamEvent(content="must-not-run")])
    vm, _, _, provider, factory = _vm(qtbot, stream=stream)
    entry = {
        "knowledge_id": 7,
        "title": "Fixture",
        "file_path": "vault/fixture.md",
    }
    external_store = SimpleNamespace(query_by_id=MagicMock(return_value=entry))
    corrupt = PreviewOutcome(
        status="degraded",
        content=sentinel,
        issue=PreviewIssue(
            code=ErrorCode.RESOURCE_NOT_READABLE,
            stage="preview_markdown",
            cause_type="PreviewUnavailable",
        ),
    )
    object.__setattr__(corrupt, "issue", None)

    result = SearchResult(
        knowledge_id=7,
        title="Fixture",
        score=0.8,
        highlight="hit",
        metadata={},
    )
    retriever = SimpleNamespace(
        search=MagicMock(
            return_value=SearchResponse.completed([result], strategy="bm25")
        )
    )
    message = (
        "请基于 @知识库/7 回答"
        if reference_kind == "knowledge"
        else "请基于 @搜索/fixture 回答"
    )

    with patch("src.gui.views.chat_view.ChatViewModel", return_value=vm):
        view = ChatView()
    qtbot.addWidget(view)
    view.chat_area.input_area.input_box.setPlainText(message)
    before = copy.deepcopy(vm.current_messages)

    with (
        patch("src.gui.stores.get_sqlite_store", return_value=external_store),
        patch("src.gui.stores.get_markdown_store", return_value=MagicMock()),
        patch("src.gui.stores.get_bm25_retriever", return_value=retriever),
        patch(
            "src.gui.views.chat_view.load_entry_preview_outcome",
            return_value=corrupt,
        ),
        caplog.at_level("INFO"),
    ):
        view._on_send_clicked()
        await asyncio.sleep(0)

    assert factory.calls == 0
    assert provider.open_calls == 0
    assert vm.active_request_id is None
    assert vm.current_messages == before
    rendered = view.chat_area.message_display.toPlainText()
    assert "status=error" in rendered
    assert "code=resource_not_readable" in rendered
    assert sentinel not in rendered
    assert sentinel not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "corruption",
    ["degraded-without-issue", "success-with-issue"],
)
async def test_corrupt_search_response_rejects_without_dispatch_or_canary(
    qtbot,
    caplog,
    corruption,
) -> None:
    sentinel = "CORRUPT-SEARCH-RESPONSE-CANARY"
    stream = ControlledStream([ChatStreamEvent(content="must-not-run")])
    vm, _, _, provider, factory = _vm(qtbot, stream=stream)
    result = SearchResult(
        knowledge_id=7,
        title=sentinel,
        score=0.8,
        highlight=sentinel,
        metadata={},
    )
    issue = RetrievalIssue(
        code=ErrorCode.RETRIEVAL_INDEX_UNAVAILABLE,
        message="stable public message",
        stage="bm25",
        recoverable=True,
    )
    if corruption == "degraded-without-issue":
        response = SearchResponse.degraded_response(
            [result],
            [issue],
            strategy="bm25",
        )
        object.__setattr__(response, "issues", ())
    else:
        response = SearchResponse.completed([result], strategy="bm25")
        object.__setattr__(response, "issues", (issue,))

    retriever = SimpleNamespace(search=MagicMock(return_value=response))
    external_store = SimpleNamespace(query_by_id=MagicMock())
    with patch("src.gui.views.chat_view.ChatViewModel", return_value=vm):
        view = ChatView()
    qtbot.addWidget(view)
    view.chat_area.input_area.input_box.setPlainText(
        "请基于 @搜索/fixture 回答"
    )
    before = copy.deepcopy(vm.current_messages)

    with (
        patch("src.gui.stores.get_bm25_retriever", return_value=retriever),
        patch("src.gui.stores.get_sqlite_store", return_value=external_store),
        caplog.at_level("INFO"),
    ):
        view._on_send_clicked()
        await asyncio.sleep(0)

    assert factory.calls == 0
    assert provider.open_calls == 0
    assert vm.active_request_id is None
    assert vm.current_messages == before
    external_store.query_by_id.assert_not_called()
    rendered = view.chat_area.message_display.toPlainText()
    assert "status=error" in rendered
    assert "code=retrieval_backend_failed" in rendered
    assert sentinel not in rendered
    assert sentinel not in caplog.text


@pytest.mark.asyncio
async def test_preview_degraded_warns_and_sends_only_safe_summary_context(
    qtbot,
    caplog,
) -> None:
    sentinels = (
        "url-user-secret",
        "url-pass-secret",
        "matrix-secret",
        "query-secret",
        "fragment-secret",
    )
    source_url = (
        "https://url-user-secret:url-pass-secret@example.com/"
        "article;token=matrix-secret?api_key=query-secret&safe=1"
        "#fragment-secret"
    )
    stream = ControlledStream(
        [
            ChatStreamEvent(content="safe answer"),
            ChatStreamEvent(prompt_tokens=2, completion_tokens=2),
        ]
    )
    vm, _, _, provider, factory = _vm(qtbot, stream=stream)
    entry = {
        "knowledge_id": 7,
        "title": "Fixture",
        "source_type": "web",
        "source_url": source_url,
        "file_path": "vault/fixture.md",
        "summary_one_sentence": "safe database summary",
    }
    external_store = SimpleNamespace(query_by_id=MagicMock(return_value=entry))
    outcome = PreviewOutcome(
        status="degraded",
        content="safe-preview-summary",
        issue=PreviewIssue(
            code=ErrorCode.RESOURCE_NOT_READABLE,
            stage="preview_markdown",
            recoverable=True,
            cause_type="PreviewUnavailable",
        ),
    )

    with patch("src.gui.views.chat_view.ChatViewModel", return_value=vm):
        view = ChatView()
    qtbot.addWidget(view)
    view.chat_area.input_area.input_box.setPlainText(
        "请基于 @知识库/7 回答"
    )

    with (
        patch("src.gui.stores.get_sqlite_store", return_value=external_store),
        patch("src.gui.stores.get_markdown_store", return_value=MagicMock()),
        patch(
            "src.gui.views.chat_view.load_entry_preview_outcome",
            return_value=outcome,
        ),
        caplog.at_level("INFO"),
    ):
        view._on_send_clicked()
        request = vm._active_request
        assert request is not None and request.task is not None
        await asyncio.wait_for(asyncio.shield(request.task), timeout=1.0)

    assert factory.calls == 1
    assert provider.open_calls == 1
    provider_payload = "\n".join(
        str(message.get("content", "")) for message in provider.messages
    )
    assert "safe-preview-summary" in provider_payload
    rendered = view.chat_area.message_display.toPlainText()
    assert "status=degraded" in rendered
    assert "code=resource_not_readable" in rendered
    assert "正在使用安全摘要" in rendered
    for sentinel in sentinels:
        assert sentinel not in provider_payload
        assert sentinel not in rendered
        assert sentinel not in caplog.text


@pytest.mark.asyncio
async def test_search_reference_source_url_is_sanitized_before_provider(
    qtbot,
    caplog,
) -> None:
    sentinels = (
        "search-user-secret",
        "search-pass-secret",
        "search-matrix-secret",
        "search-query-secret",
        "search-fragment-secret",
    )
    source_url = (
        "https://search-user-secret:search-pass-secret@example.com/"
        "article;token=search-matrix-secret?api_key=search-query-secret"
        "#search-fragment-secret"
    )
    stream = ControlledStream([ChatStreamEvent(content="safe answer")])
    vm, _, _, provider, factory = _vm(qtbot, stream=stream)
    result = SearchResult(
        knowledge_id=7,
        title="Fixture",
        score=0.8,
        highlight="hit",
        metadata={},
    )
    response = SearchResponse.completed([result], strategy="bm25")
    retriever = SimpleNamespace(search=MagicMock(return_value=response))
    entry = {
        "knowledge_id": 7,
        "title": "Fixture",
        "source_type": "web",
        "source_url": source_url,
        "file_path": "vault/fixture.md",
    }
    external_store = SimpleNamespace(query_by_id=MagicMock(return_value=entry))

    with patch("src.gui.views.chat_view.ChatViewModel", return_value=vm):
        view = ChatView()
    qtbot.addWidget(view)
    view.chat_area.input_area.input_box.setPlainText(
        "请基于 @搜索/fixture 回答"
    )

    with (
        patch("src.gui.stores.get_bm25_retriever", return_value=retriever),
        patch("src.gui.stores.get_sqlite_store", return_value=external_store),
        patch("src.gui.stores.get_markdown_store", return_value=MagicMock()),
        patch(
            "src.gui.views.chat_view.load_entry_preview_outcome",
            return_value=PreviewOutcome(
                status="success",
                content="safe-search-preview",
            ),
        ),
        caplog.at_level("INFO"),
    ):
        view._on_send_clicked()
        request = vm._active_request
        assert request is not None and request.task is not None
        await asyncio.wait_for(asyncio.shield(request.task), timeout=1.0)

    assert factory.calls == 1
    assert provider.open_calls == 1
    provider_payload = "\n".join(
        str(message.get("content", "")) for message in provider.messages
    )
    assert "safe-search-preview" in provider_payload
    rendered = view.chat_area.message_display.toPlainText()
    for sentinel in sentinels:
        assert sentinel not in provider_payload
        assert sentinel not in rendered
        assert sentinel not in caplog.text


@pytest.mark.asyncio
async def test_url_archive_degraded_diagnostics_and_source_url_are_sanitized(
    qtbot,
    caplog,
) -> None:
    sentinels = (
        "archive-user-secret",
        "archive-pass-secret",
        "archive-matrix-secret",
        "archive-query-secret",
        "archive-fragment-secret",
        "diagnostic-status-secret",
        "diagnostic-operation-secret",
        "diagnostic-repair-secret",
    )
    source_url = (
        "https://archive-user-secret:archive-pass-secret@example.com/"
        "article;token=archive-matrix-secret?api_key=archive-query-secret"
        "#archive-fragment-secret"
    )

    class DegradedWorkflowEngine:
        async def execute_async(self, workflow: str, payload: dict[str, Any]):
            return SimpleNamespace(
                terminal="degraded",
                success=True,
                data=_url_completion_data(99, status="degraded"),
                errors=[],
                warnings=["safe degraded warning"],
                issues=[],
            )

    stream = ControlledStream([ChatStreamEvent(content="unused")])
    vm, _, store, _, _ = _vm(qtbot, stream=stream)
    store.entries_by_id[99] = {
        "knowledge_id": 99,
        "title": "Safe archive title",
        "source_type": "web",
        "source_url": source_url,
        "summary_one_sentence": "safe archive summary",
    }
    engine = DegradedWorkflowEngine()

    with patch("src.gui.views.chat_view.ChatViewModel", return_value=vm):
        view = ChatView()
    qtbot.addWidget(view)
    warnings: list[tuple[Any, ...]] = []
    completions: list[tuple[Any, ...]] = []
    vm.url_archive_operation_warning.connect(
        lambda *args: warnings.append(tuple(args))
    )
    vm.url_archive_operation_completed.connect(
        lambda *args: completions.append(tuple(args))
    )

    with (
        patch("src.workflow.engine.WorkflowEngine", return_value=engine),
        caplog.at_level("INFO"),
    ):
        await vm.archive_url_and_inject.__wrapped__(
            vm,
            "https://archive.example/article",
            "session-a",
            "archive-op-safe",
        )

    assert len(warnings) == 1
    warning = warnings[0][3]
    assert "status=degraded" in warning
    assert "repair=rebuild_vectors_for_entry" in warning
    assert f"operation_id={99:032x}" in warning
    assert len(completions) == 1
    completed_entry = completions[0][3]
    context = "\n".join(
        message.get("content", "")
        for message in vm.current_messages
        if message.get("role") == "system"
    )
    rendered = view.chat_area.message_display.toPlainText()
    for sentinel in sentinels:
        assert sentinel not in warning
        assert sentinel not in completed_entry.get("source_url", "")
        assert sentinel not in context
        assert sentinel not in rendered
        assert sentinel not in caplog.text


@pytest.mark.asyncio
async def test_url_archive_error_diagnostics_are_allowlisted_and_redacted(
    qtbot,
    caplog,
) -> None:
    sentinels = (
        "issue-code-api-secret",
        "issue-stage-path-secret",
        "error-status-secret",
        "error-operation-secret",
        "error-repair-secret",
    )

    class FailedWorkflowEngine:
        async def execute_async(self, workflow: str, payload: dict[str, Any]):
            return SimpleNamespace(
                terminal="error",
                success=False,
                data={
                    "core_committed": True,
                    "do_not_retry": True,
                    "status": "error-status-secret\r\nheader",
                    "operation_id": "error-operation-secret",
                    "repair_actions": ["C:/private/error-repair-secret"],
                },
                warnings=[],
                issues=[
                    {
                        "code": "issue-code-api-secret\r\nheader",
                        "stage": "C:/private/issue-stage-path-secret",
                    }
                ],
            )

    stream = ControlledStream([ChatStreamEvent(content="unused")])
    vm, _, _, _, _ = _vm(qtbot, stream=stream)
    engine = FailedWorkflowEngine()

    with patch("src.gui.views.chat_view.ChatViewModel", return_value=vm):
        view = ChatView()
    qtbot.addWidget(view)
    failures: list[tuple[Any, ...]] = []
    completions: list[tuple[Any, ...]] = []
    vm.url_archive_operation_failed.connect(
        lambda *args: failures.append(tuple(args))
    )
    vm.url_archive_operation_completed.connect(
        lambda *args: completions.append(tuple(args))
    )

    with (
        patch("src.workflow.engine.WorkflowEngine", return_value=engine),
        caplog.at_level("INFO"),
    ):
        await vm.archive_url_and_inject.__wrapped__(
            vm,
            "https://archive.example/failure",
            "session-a",
            "archive-op-error",
        )

    assert len(failures) == 1
    message = failures[0][3]
    assert "错误代码：workflow_step_failed" in message
    assert "阶段：workflow" in message
    assert "status=unknown" in message
    assert "repair=repair_required" in message
    assert "operation_id=" not in message
    assert completions == []
    rendered = view.chat_area.message_display.toPlainText()
    for sentinel in sentinels:
        assert sentinel not in message
        assert sentinel not in rendered
        assert sentinel not in caplog.text
