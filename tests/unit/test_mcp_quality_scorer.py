"""Unit tests for the Phase C offline MCP scorer and task contract."""

from pathlib import Path
import re

import pytest
import yaml

from evals.mcp_quality.runner import (
    DEFAULT_PROPOSALS,
    DEFAULT_TASKSET,
    _default_work_parent,
    _require_isolated_output_path,
    _validate_proposals,
    _validate_taskset,
    main,
)
from evals.mcp_quality.scorer import MISSING, score_assertion, select_path
from evals.mcp_quality import safety


def test_fixed_taskset_has_required_size_and_coverage() -> None:
    payload = yaml.safe_load(DEFAULT_TASKSET.read_text(encoding="utf-8"))

    _validate_taskset(payload)
    proposals = yaml.safe_load(DEFAULT_PROPOSALS.read_text(encoding="utf-8"))
    _validate_proposals(proposals, payload)

    tasks = payload["tasks"]
    assert len(tasks) == 16
    assert all("proposed_call" not in task for task in tasks)
    assert {item["task_id"] for item in proposals["proposals"]} == {
        task["id"] for task in tasks
    }
    tools = {task["expected_call"]["tool"] for task in tasks}
    assert {
        "query_subgraph",
        "explain_relation",
        "collect_evidence",
        "find_bridges",
        "timeline_of",
        "contrast",
    } <= tools
    categories = {task["category"] for task in tasks}
    assert {
        "relation_reasoning",
        "chunk_evidence",
        "degraded_contract",
        "partial_tool",
        "parameter_contract",
    } <= categories


def test_taskset_contains_no_external_or_production_inputs() -> None:
    taskset_raw = DEFAULT_TASKSET.read_text(encoding="utf-8")
    raw = (
        taskset_raw + DEFAULT_PROPOSALS.read_text(encoding="utf-8")
    ).lower()

    assert "api_key" not in raw
    assert "config/local.yaml" not in raw
    assert ".data/" not in raw
    assert "http://example.test" not in raw
    assert "https://example.test" not in raw
    assert "proposed_call:" not in taskset_raw
    assert re.search(r":\s+[&*][A-Za-z_]", taskset_raw) is None


def test_select_path_supports_indexes_and_wildcards() -> None:
    payload = {"items": [{"id": 1}, {"id": 2}]}

    assert select_path(payload, "items[0].id") == 1
    assert select_path(payload, "items[*].id") == [1, 2]
    assert select_path(payload, "items[3].id") is MISSING


def test_wildcard_keeps_missing_values_for_all_item_contracts() -> None:
    payload = {"items": [{"locator": "a"}, {}]}
    selected = select_path(payload, "items[*].locator")

    assert selected[0] == "a"
    assert selected[1] is MISSING
    result = score_assertion(
        {
            "id": "all-locators",
            "dimension": "citability",
            "path": "items[*].locator",
            "op": "all_not_empty",
        },
        payload,
    )
    assert result.passed is False
    assert result.to_dict()["actual"] == ["a", None]


@pytest.mark.parametrize(
    ("operator", "actual", "expected", "passed"),
    [
        ("equals", "partial", "partial", True),
        ("contains", ["a", "b"], "b", True),
        ("contains_all", ["a", "b"], ["a", "b"], True),
        ("set_equals", [2, 1], [1, 2], True),
        ("length_equals", [1, 2], 2, True),
        ("gte", 0.8, 0.7, True),
        ("lte", 4, 4, True),
        ("not_empty", "", None, False),
        ("all_not_empty", ["a", "b"], None, True),
        ("all_not_empty", ["a", ""], None, False),
    ],
)
def test_score_assertion_operators(
    operator: str,
    actual: object,
    expected: object,
    passed: bool,
) -> None:
    assertion = {
        "id": "check",
        "dimension": "result",
        "path": "value",
        "op": operator,
        "expected": expected,
    }

    result = score_assertion(assertion, {"value": actual})

    assert result.passed is passed


def test_validate_taskset_rejects_out_of_range_task_count() -> None:
    payload = {
        "schema_version": "pkv.mcp_quality_tasks.v1",
        "policy": {
            "mode": "baseline_only",
            "ci_contract": "schema_and_failure_matrix",
        },
        "target_thresholds": {},
        "tasks": [{"id": f"task-{index}"} for index in range(9)],
    }

    with pytest.raises(ValueError, match="10-20"):
        _validate_taskset(payload)


def test_default_taskset_is_version_controlled_beside_runner() -> None:
    assert DEFAULT_TASKSET == (
        Path(__file__).resolve().parents[2]
        / "evals"
        / "mcp_quality"
        / "tasks.v1.yaml"
    )
    assert DEFAULT_PROPOSALS == (
        Path(__file__).resolve().parents[2]
        / "evals"
        / "mcp_quality"
        / "proposals.baseline.v1.yaml"
    )


def test_json_output_is_limited_to_isolated_runtime_roots(
    tmp_path: Path,
    monkeypatch,
) -> None:
    data_dir = tmp_path / "data-test"
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    monkeypatch.setenv("TMP_DIR", str(data_dir / "tmp"))

    assert _require_isolated_output_path(data_dir / "result.json") == (
        data_dir / "result.json"
    ).resolve()

    with pytest.raises(RuntimeError, match="显式隔离目录"):
        _require_isolated_output_path(tmp_path / "tracked-result.json")


def test_default_work_parent_rejects_production_before_mkdir(
    tmp_path: Path,
    monkeypatch,
) -> None:
    fake_production = tmp_path / "repo" / ".data"
    requested = fake_production / "new-eval-tmp"
    monkeypatch.setattr(safety, "PRODUCTION_ROOT", fake_production)
    monkeypatch.setenv("TMP_DIR", str(requested))

    with pytest.raises(RuntimeError, match="生产 .data"):
        _default_work_parent()

    assert not fake_production.exists()


def test_output_guard_rejects_production_before_create(
    tmp_path: Path,
    monkeypatch,
) -> None:
    fake_production = tmp_path / "repo" / ".data"
    output = fake_production / "new-output" / "result.json"
    monkeypatch.setattr(safety, "PRODUCTION_ROOT", fake_production)
    monkeypatch.setenv("DATA_DIR", str(fake_production / "new-output"))
    monkeypatch.delenv("TMP_DIR", raising=False)

    with pytest.raises(RuntimeError, match="生产 .data"):
        _require_isolated_output_path(output)

    assert not fake_production.exists()


@pytest.mark.parametrize("path_flag", ["--taskset", "--proposals", "--output"])
def test_cli_preflights_paths_before_creating_temp_directory(
    path_flag: str,
    tmp_path: Path,
    monkeypatch,
) -> None:
    fake_production = tmp_path / "repo" / ".data"
    requested_path = fake_production / "new-path" / "payload.yaml"
    safe_tmp = tmp_path / "isolated" / "tmp"
    safe_data = tmp_path / "isolated"
    monkeypatch.setattr(safety, "PRODUCTION_ROOT", fake_production)
    monkeypatch.setenv("TMP_DIR", str(safe_tmp))
    monkeypatch.setenv("DATA_DIR", str(safe_data))

    with pytest.raises(RuntimeError, match="生产 .data"):
        main([path_flag, str(requested_path)])

    assert not fake_production.exists()
    assert not safe_tmp.exists()
