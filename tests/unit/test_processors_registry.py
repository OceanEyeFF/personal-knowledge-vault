"""Processor registry and exact route tests."""

from __future__ import annotations

import pytest

import src.processors as processors
from src.processors.base import BaseProcessor
from src.processors.chat_processor import ChatProcessor as RealChatProcessor
from src.processors.generic_processor import GenericProcessor
from src.runtime.errors import ErrorCode, PKVRuntimeError
from src.storage.markdown_store import Entry


class ExactRouteProcessor(BaseProcessor):
    @classmethod
    def can_handle(cls, url: str) -> bool:
        return False

    async def process(self, url: str) -> Entry:
        return Entry(title="exact", source_type="test", content=url)


class AIChatProcessor(BaseProcessor):
    @classmethod
    def can_handle(cls, url: str) -> bool:
        return True

    async def process(self, url: str) -> Entry:
        return Entry(title="ai", source_type="test", content=url)


def test_explicit_route_constructs_exact_class_without_can_handle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(processors, "_PROCESSORS", [ExactRouteProcessor])

    selected = processors.get_processor_by_name("exact-route")

    assert isinstance(selected, ExactRouteProcessor)


def test_registry_uses_stable_snake_case_for_initialisms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(processors, "_PROCESSORS", [AIChatProcessor])

    assert processors.get_available_processor_names() == ("ai_chat",)
    assert isinstance(processors.get_processor_by_name("ai-chat"), AIChatProcessor)


def test_unknown_explicit_route_never_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(processors, "_PROCESSORS", [ExactRouteProcessor])

    with pytest.raises(PKVRuntimeError) as exc_info:
        processors.get_processor_by_name("generic")

    assert exc_info.value.code is ErrorCode.WORKFLOW_PROCESSOR_UNKNOWN
    assert exc_info.value.stage == "workflow_processor_selection"


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/export.json",
        "https://example.com/transcript.txt",
        "HTTPS://EXAMPLE.COM/EXPORT.JSON",
    ],
)
def test_remote_text_or_json_url_routes_to_safe_generic_processor(
    monkeypatch: pytest.MonkeyPatch,
    url: str,
) -> None:
    monkeypatch.setattr(processors, "_PROCESSORS", None)

    selected = processors.get_processor(url)

    assert isinstance(selected, GenericProcessor)


@pytest.mark.parametrize("suffix", [".json", ".txt"])
def test_local_text_or_json_path_still_routes_to_chat_processor(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    suffix: str,
) -> None:
    local_path = tmp_path / f"transcript{suffix}"
    local_path.write_text("[]" if suffix == ".json" else "hello", encoding="utf-8")
    monkeypatch.setattr(processors, "_PROCESSORS", None)

    selected = processors.get_processor(str(local_path))

    assert isinstance(selected, RealChatProcessor)
