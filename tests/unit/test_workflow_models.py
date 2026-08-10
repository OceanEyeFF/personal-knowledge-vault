"""
Workflow models unit tests.
"""

import sys
from pathlib import Path

# Add project root to Python path
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from src.workflow.models import State, WorkflowContext, WorkflowResult


def test_state_get_set_has_to_dict() -> None:
    """State should support basic get/set/has/to_dict behavior."""
    state = State({"a": 1})

    assert state.get("a") == 1
    assert state.get("missing", 10) == 10
    assert not state.has("b")

    state.set("b", 2)
    assert state.has("b")
    assert state.get("b") == 2

    data = state.to_dict()
    assert data == {"a": 1, "b": 2}

    # Ensure to_dict returns a copy
    data["b"] = 3
    assert state.get("b") == 2


def test_workflow_context_log() -> None:
    """WorkflowContext should record logs."""
    context = WorkflowContext({"input": "test"})
    context.log("hello")

    assert context.logs == ["hello"]
    assert context.state.get("input") == "test"


def test_workflow_result_defaults() -> None:
    """WorkflowResult should hold defaults correctly."""
    result = WorkflowResult(success=True)

    assert result.success is True
    assert result.data == {}
    assert result.errors == []
    assert result.logs == []
    assert result.warnings == []
    assert result.issues == []
    assert result.terminal == "success"


def test_workflow_result_derives_degraded_and_serializes_copies() -> None:
    issue = {
        "code": "workflow_step_failed",
        "message": "optional step failed",
        "severity": "warning",
        "recoverable": True,
    }
    result = WorkflowResult(
        success=True,
        data={"answer": 42},
        warnings=["optional step failed"],
        issues=[issue],
    )

    payload = result.to_dict()

    assert result.terminal == "degraded"
    assert payload["terminal"] == "degraded"
    payload["data"]["answer"] = 0
    payload["issues"][0]["code"] = "changed"
    assert result.data["answer"] == 42
    assert result.issues[0]["code"] == "workflow_step_failed"


def test_workflow_result_rejects_inconsistent_terminal() -> None:
    try:
        WorkflowResult(success=False, terminal="success")
    except ValueError as exc:
        assert "不一致" in str(exc)
    else:  # pragma: no cover - explicit contract guard
        raise AssertionError("inconsistent result must fail")
