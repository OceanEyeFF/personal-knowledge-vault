"""
MVP blocker regression tests.
"""

from __future__ import annotations

from click.testing import CliRunner

from src.cli import commands
from src.processors.generic_processor import GenericProcessor
from src.storage.markdown_store import Entry
from src.workflow.models import WorkflowResult


def test_archive_quiet_skips_interactive_review(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeWorkflowEngine:
        async def execute_async(self, workflow_name: str, input_data: dict):
            captured["workflow_name"] = workflow_name
            captured["input_data"] = input_data
            entry = Entry(
                title="Archived article",
                source_type="generic",
                source_url="https://example.com/article",
            )
            return WorkflowResult(
                success=True,
                terminal="success",
                data={
                    "knowledge_id": 42,
                    "status": "ready",
                    "core_committed": True,
                    "entry": entry,
                },
            )

    monkeypatch.setattr(commands, "_load_config", lambda: object())
    monkeypatch.setattr(commands, "WorkflowEngine", FakeWorkflowEngine)

    result = CliRunner().invoke(
        commands.cli,
        ["archive", "https://example.com/article", "--quiet"],
    )

    assert result.exit_code == 0
    assert result.output.strip() == "42"
    assert captured["workflow_name"] == "archive-url"
    assert captured["input_data"] == {
        "url": "https://example.com/article",
        "skip_sharpen": True,
        "skip_review": True,
    }


def test_generic_processor_remains_available_when_optional_processors_are_missing(monkeypatch) -> None:
    import src.processors as processors

    monkeypatch.setattr(processors, "_PROCESSORS", None)

    processor = processors.get_processor("https://example.com/article")

    assert isinstance(processor, GenericProcessor)
