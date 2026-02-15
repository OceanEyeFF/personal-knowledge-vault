"""
Workflow engine unit tests.
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


class IncrementStep(BaseStep):
    """Step that increments a counter."""

    async def execute(self, context: WorkflowContext) -> dict:
        """Increment the count in state."""
        value = context.state.get("count", 0)
        return {"count": value + 1}


class ErrorStep(BaseStep):
    """Step that raises an exception."""

    async def execute(self, context: WorkflowContext) -> dict:
        """Always raise a runtime error."""
        raise RuntimeError("step error")


class ErrorReturnStep(BaseStep):
    """Step that returns an error list."""

    async def execute(self, context: WorkflowContext) -> dict:
        """Return a soft error result."""
        return {"errors": ["soft error"], "count": 99}


def make_config_loader(config: dict) -> Callable[[str], dict]:
    """Create a config loader stub."""
    def _loader(_name: str) -> dict:
        """Return the provided config."""
        return config

    return _loader


@pytest.mark.asyncio
async def test_engine_execute_async_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """execute_async should run steps and merge state."""
    workflow_config = {"name": "demo", "steps": ["increment", "increment"]}

    monkeypatch.setattr(engine_module, "get_workflow_config", make_config_loader(workflow_config))
    monkeypatch.setitem(engine_module._STEP_REGISTRY, "increment", IncrementStep)

    engine = WorkflowEngine(reload_config=True)
    result = await engine.execute_async("demo", {"count": 1})

    assert result.success is True
    assert result.data["count"] == 3


@pytest.mark.asyncio
async def test_engine_unknown_step_type(monkeypatch: pytest.MonkeyPatch) -> None:
    """Engine should record unknown step type errors."""
    workflow_config = {"name": "demo", "steps": [{"id": "x", "type": "unknown"}]}
    monkeypatch.setattr(engine_module, "get_workflow_config", make_config_loader(workflow_config))

    engine = WorkflowEngine(reload_config=True)
    result = await engine.execute_async("demo", {})

    assert result.success is False
    assert any("未知步骤类型" in err for err in result.errors)


@pytest.mark.asyncio
async def test_engine_step_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    """Engine should catch step exceptions and continue."""
    workflow_config = {"name": "demo", "steps": [{"id": "boom", "type": "boom"}]}
    monkeypatch.setattr(engine_module, "get_workflow_config", make_config_loader(workflow_config))
    monkeypatch.setitem(engine_module._STEP_REGISTRY, "boom", ErrorStep)

    engine = WorkflowEngine(reload_config=True)
    result = await engine.execute_async("demo", {})

    assert result.success is False
    assert any("执行失败" in err for err in result.errors)


@pytest.mark.asyncio
async def test_engine_collects_step_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """Engine should collect errors returned by steps."""
    workflow_config = {"name": "demo", "steps": [{"id": "warn", "type": "warn"}]}
    monkeypatch.setattr(engine_module, "get_workflow_config", make_config_loader(workflow_config))
    monkeypatch.setitem(engine_module._STEP_REGISTRY, "warn", ErrorReturnStep)

    engine = WorkflowEngine(reload_config=True)
    result = await engine.execute_async("demo", {})

    assert result.success is False
    assert "soft error" in result.errors
    assert result.data["count"] == 99


def test_engine_execute_sync(monkeypatch: pytest.MonkeyPatch) -> None:
    """execute should wrap execute_async when no loop is running."""
    workflow_config = {"name": "demo", "steps": ["increment"]}
    monkeypatch.setattr(engine_module, "get_workflow_config", make_config_loader(workflow_config))
    monkeypatch.setitem(engine_module._STEP_REGISTRY, "increment", IncrementStep)

    engine = WorkflowEngine(reload_config=True)
    result = engine.execute("demo", {"count": 0})

    assert result.success is True
    assert result.data["count"] == 1


@pytest.mark.asyncio
async def test_engine_config_load_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Engine should return failure when config load fails."""
    def raise_error(_name: str) -> dict:
        """Raise a config error for testing."""
        raise FileNotFoundError("missing config")

    monkeypatch.setattr(engine_module, "get_workflow_config", raise_error)
    engine = WorkflowEngine(reload_config=True)

    result = await engine.execute_async("missing", {})

    assert result.success is False
    assert any("配置加载失败" in err for err in result.errors)
