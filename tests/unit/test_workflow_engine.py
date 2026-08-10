"""Workflow engine v1 contract tests."""

from __future__ import annotations

import logging
from typing import Callable

import pytest

import src.workflow.engine as engine_module
from src.runtime.errors import ErrorCode, PKVRuntimeError
from src.workflow.engine import WorkflowEngine
from src.workflow.models import WorkflowContext
from src.workflow.steps import BaseStep


class IncrementStep(BaseStep):
    async def execute(self, context: WorkflowContext) -> dict:
        value = context.state.get("count", 0)
        trace = list(context.state.get("trace", []))
        trace.append(self.step_id)
        return {"count": value + 1, "trace": trace}


class ErrorStep(BaseStep):
    async def execute(self, context: WorkflowContext) -> dict:
        trace = list(context.state.get("trace", []))
        trace.append(self.step_id)
        context.state.set("trace", trace)
        raise RuntimeError("step error")


class ErrorReturnStep(BaseStep):
    async def execute(self, context: WorkflowContext) -> dict:
        trace = list(context.state.get("trace", []))
        trace.append(self.step_id)
        return {"errors": ["soft error"], "count": 99, "trace": trace}


class IssueOnlyStep(BaseStep):
    async def execute(self, context: WorkflowContext) -> dict:
        return {
            "issues": [
                {
                    "code": "workflow_step_failed",
                    "message": "issue-only failure",
                    "severity": "error",
                    "recoverable": True,
                }
            ]
        }


class MalformedIssueStep(BaseStep):
    async def execute(self, context: WorkflowContext) -> dict:
        return {
            "issues": [
                {
                    "code": "workflow_step_failed",
                    "message": "invalid severity type",
                    "severity": [],
                    "recoverable": False,
                }
            ]
        }


class RecoverableRuntimeErrorStep(BaseStep):
    async def execute(self, context: WorkflowContext) -> dict:
        raise PKVRuntimeError(
            ErrorCode.SSRF_RESOLUTION_FAILED,
            "private resolver detail",
            stage="url_resolution",
            recoverable=True,
        )


class ConfigCaptureStep(BaseStep):
    async def execute(self, context: WorkflowContext) -> dict:
        return {"configured_processor": self.config.get("processor")}


def make_config_loader(config: dict) -> Callable[[str], dict]:
    def _loader(_name: str) -> dict:
        return config

    return _loader


def step(step_id: str, step_type: str, *, on_error: str = "fail") -> dict:
    return {
        "id": step_id,
        "type": step_type,
        "config": {},
        "on_error": on_error,
    }


def workflow(*steps: dict, name: str = "demo") -> dict:
    return {"schema_version": 1, "name": name, "steps": list(steps)}


def assert_issue_shape(issue: dict) -> None:
    assert {"code", "message", "severity", "recoverable"}.issubset(issue)


@pytest.mark.asyncio
async def test_engine_execute_async_success(monkeypatch: pytest.MonkeyPatch) -> None:
    config = workflow(step("first", "increment"), step("second", "increment"))
    monkeypatch.setattr(engine_module, "get_workflow_config", make_config_loader(config))
    monkeypatch.setitem(engine_module._STEP_REGISTRY, "increment", IncrementStep)

    result = await WorkflowEngine(reload_config=True).execute_async("demo", {"count": 1})

    assert result.success is True
    assert result.terminal == "success"
    assert result.data["count"] == 3
    assert result.data["trace"] == ["first", "second"]
    assert result.errors == []
    assert result.warnings == []
    assert result.issues == []


@pytest.mark.asyncio
async def test_config_is_validated_before_any_step_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    config = workflow(step("would-run", "increment"), step("bad", "unknown"))
    monkeypatch.setattr(engine_module, "get_workflow_config", make_config_loader(config))
    monkeypatch.setitem(engine_module._STEP_REGISTRY, "increment", IncrementStep)

    result = await WorkflowEngine(reload_config=True).execute_async("demo", {"count": 0})

    assert result.success is False
    assert result.terminal == "error"
    assert result.data["count"] == 0
    assert "trace" not in result.data
    assert result.issues[0]["code"] == ErrorCode.WORKFLOW_CONFIG_INVALID.value
    assert result.issues[0]["step_id"] == "bad"


@pytest.mark.asyncio
async def test_on_error_fail_stops_after_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    config = workflow(
        step("before", "increment"),
        step("boom", "boom", on_error="fail"),
        step("after", "increment"),
    )
    monkeypatch.setattr(engine_module, "get_workflow_config", make_config_loader(config))
    monkeypatch.setitem(engine_module._STEP_REGISTRY, "increment", IncrementStep)
    monkeypatch.setitem(engine_module._STEP_REGISTRY, "boom", ErrorStep)

    result = await WorkflowEngine(reload_config=True).execute_async("demo", {})

    assert result.success is False
    assert result.terminal == "error"
    assert result.data["trace"] == ["before", "boom"]
    assert result.errors and not result.warnings
    assert result.issues[-1]["code"] == ErrorCode.WORKFLOW_STEP_FAILED.value
    assert result.issues[-1]["severity"] == "error"


@pytest.mark.asyncio
async def test_on_error_continue_degrades_after_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    config = workflow(
        step("before", "increment"),
        step("boom", "boom", on_error="continue"),
        step("after", "increment"),
    )
    monkeypatch.setattr(engine_module, "get_workflow_config", make_config_loader(config))
    monkeypatch.setitem(engine_module._STEP_REGISTRY, "increment", IncrementStep)
    monkeypatch.setitem(engine_module._STEP_REGISTRY, "boom", ErrorStep)

    result = await WorkflowEngine(reload_config=True).execute_async("demo", {})

    assert result.success is True
    assert result.terminal == "degraded"
    assert result.data["trace"] == ["before", "boom", "after"]
    assert result.errors == []
    assert result.warnings == ["步骤 boom 执行失败"]
    assert result.issues[-1]["severity"] == "warning"


@pytest.mark.asyncio
async def test_exception_detail_is_not_exposed_in_result(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    canary = "TOP-SECRET-CANARY C:/private/vault.db"
    caplog.set_level(logging.ERROR)

    class SecretErrorStep(BaseStep):
        async def execute(self, context: WorkflowContext) -> dict:
            raise RuntimeError(canary)

    config = workflow(step("secret", "secret"))
    monkeypatch.setattr(engine_module, "get_workflow_config", make_config_loader(config))
    monkeypatch.setitem(engine_module._STEP_REGISTRY, "secret", SecretErrorStep)

    result = await WorkflowEngine(reload_config=True).execute_async("demo", {})

    assert canary not in str(result.to_dict())
    assert canary not in caplog.text
    assert result.issues[0]["cause_type"] == "RuntimeError"


@pytest.mark.asyncio
async def test_issue_only_error_controls_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    config = workflow(step("issue", "issue", on_error="continue"))
    monkeypatch.setattr(engine_module, "get_workflow_config", make_config_loader(config))
    monkeypatch.setitem(engine_module._STEP_REGISTRY, "issue", IssueOnlyStep)

    result = await WorkflowEngine(reload_config=True).execute_async("demo", {})

    assert result.success is True
    assert result.terminal == "degraded"
    assert result.warnings == ["issue-only failure"]
    assert result.issues[0]["severity"] == "warning"


@pytest.mark.asyncio
async def test_malformed_issues_obey_fail_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    config = workflow(step("issue", "malformed", on_error="fail"))
    monkeypatch.setattr(engine_module, "get_workflow_config", make_config_loader(config))
    monkeypatch.setitem(engine_module._STEP_REGISTRY, "malformed", MalformedIssueStep)

    result = await WorkflowEngine(reload_config=True).execute_async("demo", {})

    assert result.success is False
    assert result.terminal == "error"
    assert result.issues[0]["code"] == ErrorCode.WORKFLOW_STEP_FAILED.value


@pytest.mark.asyncio
async def test_fatal_policy_preserves_underlying_recoverability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = workflow(step("resolver", "resolver", on_error="fail"))
    monkeypatch.setattr(engine_module, "get_workflow_config", make_config_loader(config))
    monkeypatch.setitem(
        engine_module._STEP_REGISTRY,
        "resolver",
        RecoverableRuntimeErrorStep,
    )

    result = await WorkflowEngine(reload_config=True).execute_async("demo", {})

    assert result.terminal == "error"
    assert result.issues[0]["code"] == ErrorCode.SSRF_RESOLUTION_FAILED.value
    assert result.issues[0]["recoverable"] is True
    assert "private resolver detail" not in str(result.to_dict())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("on_error", "terminal", "success", "expected_trace"),
    [
        ("fail", "error", False, ["soft"]),
        ("continue", "degraded", True, ["soft", "after"]),
    ],
)
async def test_returned_errors_obey_on_error(
    monkeypatch: pytest.MonkeyPatch,
    on_error: str,
    terminal: str,
    success: bool,
    expected_trace: list[str],
) -> None:
    config = workflow(
        step("soft", "soft", on_error=on_error),
        step("after", "increment"),
    )
    monkeypatch.setattr(engine_module, "get_workflow_config", make_config_loader(config))
    monkeypatch.setitem(engine_module._STEP_REGISTRY, "soft", ErrorReturnStep)
    monkeypatch.setitem(engine_module._STEP_REGISTRY, "increment", IncrementStep)

    result = await WorkflowEngine(reload_config=True).execute_async("demo", {})

    assert result.success is success
    assert result.terminal == terminal
    assert result.data["trace"] == expected_trace
    if on_error == "fail":
        assert result.errors == ["soft error"]
        assert result.warnings == []
    else:
        assert result.errors == []
        assert result.warnings == ["soft error"]
    assert_issue_shape(result.issues[-1])


@pytest.mark.parametrize(
    "config",
    [
        {"name": "demo", "steps": []},
        {"schema_version": 2, "name": "demo", "steps": []},
        {"schema_version": 1, "name": "wrong", "steps": [step("one", "increment")]},
        workflow(step("one", "increment"), step("one", "increment")),
        workflow({"id": "one", "type": "increment", "config": {}}),
        workflow({"id": "one", "type": "increment", "config": {}, "on_error": "ignore"}),
        {"schema_version": 1, "name": "demo", "steps": ["increment"]},
    ],
)
@pytest.mark.asyncio
async def test_invalid_schema_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    config: dict,
) -> None:
    monkeypatch.setattr(engine_module, "get_workflow_config", make_config_loader(config))
    monkeypatch.setitem(engine_module._STEP_REGISTRY, "increment", IncrementStep)

    result = await WorkflowEngine(reload_config=True).execute_async("demo", {"count": 7})

    assert result.success is False
    assert result.terminal == "error"
    assert result.data == {"count": 7}
    assert result.issues[0]["code"] == ErrorCode.WORKFLOW_CONFIG_INVALID.value
    assert_issue_shape(result.issues[0])


def test_engine_execute_sync(monkeypatch: pytest.MonkeyPatch) -> None:
    config = workflow(step("increment", "increment"))
    monkeypatch.setattr(engine_module, "get_workflow_config", make_config_loader(config))
    monkeypatch.setitem(engine_module._STEP_REGISTRY, "increment", IncrementStep)

    result = WorkflowEngine(reload_config=True).execute("demo", {"count": 0})

    assert result.success is True
    assert result.data["count"] == 1


@pytest.mark.asyncio
async def test_engine_config_load_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_error(_name: str) -> dict:
        raise FileNotFoundError("missing config")

    monkeypatch.setattr(engine_module, "get_workflow_config", raise_error)
    result = await WorkflowEngine(reload_config=True).execute_async("missing", {})

    assert result.success is False
    assert result.terminal == "error"
    assert result.errors == ["工作流配置加载或校验失败"]
    assert result.issues[0]["code"] == ErrorCode.WORKFLOW_CONFIG_INVALID.value


@pytest.mark.asyncio
async def test_config_exception_detail_is_not_exposed(monkeypatch: pytest.MonkeyPatch) -> None:
    canary = "API_KEY=super-secret /private/config.yaml"

    def raise_secret(_name: str) -> dict:
        raise RuntimeError(canary)

    monkeypatch.setattr(engine_module, "get_workflow_config", raise_secret)
    result = await WorkflowEngine(reload_config=True).execute_async("demo", {})

    assert canary not in str(result.to_dict())
    assert result.issues[0]["cause_type"] == "RuntimeError"


def test_register_step_is_instance_scoped() -> None:
    engine = WorkflowEngine(step_registry={})
    engine.register_step("local-only", IncrementStep)

    assert engine._step_registry["local-only"] is IncrementStep
    assert "local-only" not in engine_module._STEP_REGISTRY


@pytest.mark.asyncio
async def test_processor_alias_is_canonical_from_schema_through_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = workflow(
        {
            "id": "fetch",
            "type": "fetch_content",
            "config": {
                "processor": "auto-processor",
                "url_key": "url",
                "timeout": 1,
                "retry": 0,
            },
            "on_error": "fail",
        }
    )
    monkeypatch.setattr(engine_module, "get_workflow_config", make_config_loader(config))

    result = await WorkflowEngine(
        reload_config=True,
        step_registry={"fetch_content": ConfigCaptureStep},
    ).execute_async("demo", {"url": "https://example.com"})

    assert result.terminal == "success"
    assert result.data["configured_processor"] == "auto"
