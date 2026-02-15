"""
Workflow engine integration tests.
"""

import sys
from pathlib import Path

# Add project root to Python path
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

import pytest
from typing import Callable

import src.workflow.engine as engine_module
from src.workflow.engine import WorkflowEngine
from src.workflow.steps import BaseStep
from src.workflow.models import WorkflowContext


class AddValueStep(BaseStep):
    """Step that increments a value in state."""

    async def execute(self, context: WorkflowContext) -> dict:
        """Increment value in state."""
        value = context.state.get("value", 0)
        return {"value": value + 1}


class ErrorStep(BaseStep):
    """Step that raises an error."""

    async def execute(self, context: WorkflowContext) -> dict:
        """Always raise a runtime error."""
        raise RuntimeError("boom")


class WarningStep(BaseStep):
    """Step that returns an error list without raising."""

    async def execute(self, context: WorkflowContext) -> dict:
        """Return a soft error result."""
        return {"value": 42, "errors": ["soft failure"]}


def make_config_loader(config: dict) -> Callable[[str], dict]:
    """Create a config loader stub."""
    def _loader(_name: str) -> dict:
        """Return the provided config."""
        return config

    return _loader


@pytest.mark.asyncio
async def test_workflow_engine_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """WorkflowEngine should execute steps and merge state."""
    workflow_config = {
        "name": "demo",
        "steps": ["add", "add"],
    }

    monkeypatch.setattr(engine_module, "get_workflow_config", make_config_loader(workflow_config))
    monkeypatch.setitem(engine_module._STEP_REGISTRY, "add", AddValueStep)

    engine = WorkflowEngine(reload_config=True)
    result = await engine.execute_async("demo", {"value": 1})

    assert result.success is True
    assert result.data["value"] == 3
    assert result.errors == []


@pytest.mark.asyncio
async def test_workflow_engine_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """WorkflowEngine should collect step errors."""
    workflow_config = {
        "name": "demo",
        "steps": [{"id": "fail", "type": "fail"}],
    }

    monkeypatch.setattr(engine_module, "get_workflow_config", make_config_loader(workflow_config))
    monkeypatch.setitem(engine_module._STEP_REGISTRY, "fail", ErrorStep)

    engine = WorkflowEngine(reload_config=True)
    result = await engine.execute_async("demo", {"value": 1})

    assert result.success is False
    assert result.errors


@pytest.mark.asyncio
async def test_workflow_engine_collects_step_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """WorkflowEngine should append errors returned by steps."""
    workflow_config = {
        "name": "demo",
        "steps": [{"id": "warn", "type": "warn"}],
    }

    monkeypatch.setattr(engine_module, "get_workflow_config", make_config_loader(workflow_config))
    monkeypatch.setitem(engine_module._STEP_REGISTRY, "warn", WarningStep)

    engine = WorkflowEngine(reload_config=True)
    result = await engine.execute_async("demo", {})

    assert result.success is False
    assert "soft failure" in result.errors
    assert result.data["value"] == 42
