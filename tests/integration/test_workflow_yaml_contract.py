"""W2 integration tests that execute the published YAML through fake steps."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

import src.workflow.engine as engine_module
import src.workflow.steps as steps_module
from src.storage.markdown_store import Entry
from src.workflow.engine import WorkflowEngine
from src.workflow.models import WorkflowContext
from src.workflow.steps import BaseStep, FetchStep, IdeaSharpenStep


PROJECT_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_PATH = PROJECT_ROOT / "tests" / "fixtures" / "w2" / "workflow" / "v1" / "states.v1.yaml"


def load_fixture() -> dict:
    return yaml.safe_load(FIXTURE_PATH.read_text(encoding="utf-8"))


def load_workflow(relative_path: str) -> dict:
    return yaml.safe_load((PROJECT_ROOT / relative_path).read_text(encoding="utf-8"))


class RecordingStep(BaseStep):
    async def execute(self, context: WorkflowContext) -> dict:
        trace = list(context.state.get("trace", []))
        trace.append(self.step_id)
        result = {"trace": trace}
        if context.state.get("fault_step") == self.step_id:
            if context.state.get("fault_kind") == "exception":
                context.state.set("trace", trace)
                raise RuntimeError("private fixture exception detail")
            result["errors"] = [f"fixture failure at {self.step_id}"]
        return result


STEP_REGISTRY = {
    "fetch_content": RecordingStep,
    "ai_analyze": RecordingStep,
    "idea_sharpen": RecordingStep,
    "review_entry": RecordingStep,
    "store_entry": RecordingStep,
}


@pytest.mark.asyncio
@pytest.mark.parametrize("workflow_name", ["archive-url", "archive-text"])
async def test_real_yaml_executes_declared_order(
    monkeypatch: pytest.MonkeyPatch,
    workflow_name: str,
) -> None:
    fixture = load_fixture()
    case = fixture["workflows"][workflow_name]
    config = load_workflow(case["config_path"])
    monkeypatch.setattr(engine_module, "get_workflow_config", lambda _name: config)

    result = await WorkflowEngine(
        reload_config=True,
        step_registry=STEP_REGISTRY,
    ).execute_async(workflow_name, case["input"])

    assert result.terminal == "success"
    assert result.data["trace"] == case["expected_steps"]


@pytest.mark.asyncio
@pytest.mark.parametrize("workflow_name", ["archive-url", "archive-text"])
async def test_every_real_yaml_on_error_branch_has_a_fault_oracle(
    monkeypatch: pytest.MonkeyPatch,
    workflow_name: str,
) -> None:
    fixture = load_fixture()
    case = fixture["workflows"][workflow_name]
    config = load_workflow(case["config_path"])
    monkeypatch.setattr(engine_module, "get_workflow_config", lambda _name: config)
    engine = WorkflowEngine(reload_config=True, step_registry=STEP_REGISTRY)

    assert {fault["step_id"] for fault in case["fault_cases"]} == {
        step["id"] for step in config["steps"]
    }
    for fault in case["fault_cases"]:
        result = await engine.execute_async(
            workflow_name,
            {
                **case["input"],
                "fault_step": fault["step_id"],
                "fault_kind": fault["kind"],
            },
        )

        assert result.terminal == fault["expected_terminal"], fault
        assert result.data["trace"] == fault["expected_trace"], fault
        assert result.issues[-1]["step_id"] == fault["step_id"]
        assert {"code", "message", "severity", "recoverable"}.issubset(
            result.issues[-1]
        )
        if fault["expected_terminal"] == "error":
            assert result.errors and not result.warnings
            assert result.issues[-1]["severity"] == "error"
        else:
            assert result.warnings and not result.errors
            assert result.issues[-1]["severity"] == "warning"
        assert "private fixture exception detail" not in str(result.to_dict())


def test_fixture_loads_every_live_trigger_branch_from_real_yaml() -> None:
    fixture = load_fixture()
    config = load_workflow(fixture["workflows"]["archive-url"]["config_path"])
    sharpen_config = next(
        step["config"] for step in config["steps"] if step["type"] == "idea_sharpen"
    )
    sharpen = IdeaSharpenStep("idea_sharpen", sharpen_config)

    observed = [
        sharpen._should_run(WorkflowContext(case["state"]))
        for case in fixture["trigger_cases"]
    ]

    assert observed == [case["expected"] for case in fixture["trigger_cases"]]


@pytest.mark.asyncio
async def test_fixture_drives_explicit_processor_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = load_fixture()
    route = fixture["processor_route"]
    config = load_workflow(fixture["workflows"]["archive-url"]["config_path"])
    fetch_config = dict(next(step["config"] for step in config["steps"] if step["type"] == "fetch_content"))
    fetch_config["processor"] = route["configured"]
    selected: list[str] = []

    class DeterministicProcessor:
        async def process(self, url: str) -> Entry:
            return Entry(
                title=route["expected_title"],
                source_type="generic",
                source_url=url,
                content="offline fixture content",
            )

    def select_exact(name: str) -> DeterministicProcessor:
        selected.append(name)
        return DeterministicProcessor()

    monkeypatch.setattr(steps_module, "get_processor_by_name", select_exact)
    monkeypatch.setattr(
        steps_module,
        "get_processor",
        lambda _url: (_ for _ in ()).throw(AssertionError("auto route used")),
    )

    result = await FetchStep("fetch_content", fetch_config).execute(
        WorkflowContext({"url": route["input_url"]})
    )

    assert selected == [route["configured"]]
    assert result["title"] == route["expected_title"]


def test_search_yaml_is_not_a_published_runtime_resource() -> None:
    search_path = PROJECT_ROOT / "config" / "workflows" / "search.yaml"
    manifest = json.loads(
        (PROJECT_ROOT / "packaging" / "runtime-resources.json").read_text(encoding="utf-8")
    )
    workflow_resources = [
        item for item in manifest["include_globs"] if item.startswith("config/workflows/")
    ]

    assert not search_path.exists()
    assert workflow_resources == [
        "config/workflows/archive-url.yaml",
        "config/workflows/archive-text.yaml",
    ]
    assert all("*" not in item for item in workflow_resources)


@pytest.mark.asyncio
async def test_search_workflow_never_returns_empty_success() -> None:
    result = await WorkflowEngine(reload_config=True).execute_async(
        "search",
        {"query": "deterministic"},
    )

    assert result.success is False
    assert result.terminal == "error"
    assert result.errors == ["工作流配置加载或校验失败"]


def test_fixture_schema_and_references_are_versioned_and_real() -> None:
    fixture = load_fixture()
    assert fixture["schema_version"] == "pkv.workflow.fixture.v1"
    for case in fixture["workflows"].values():
        config = load_workflow(case["config_path"])
        assert config["schema_version"] == 1
        assert config["steps"]
        for step in config["steps"]:
            if step["type"] in {"idea_sharpen", "review_entry"}:
                assert "timeout" not in step["config"]
                assert "skip_on_timeout" not in step["config"]
