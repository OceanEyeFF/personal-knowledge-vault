"""Unit tests for the Phase C offline MCP scorer and task contract."""

from pathlib import Path

import pytest
import yaml

from evals.mcp_quality.runner import (
    DEFAULT_TASKSET,
    _require_isolated_output_path,
    _validate_taskset,
)
from evals.mcp_quality.scorer import MISSING, score_assertion, select_path


def test_fixed_taskset_has_required_size_and_coverage() -> None:
    payload = yaml.safe_load(DEFAULT_TASKSET.read_text(encoding="utf-8"))

    _validate_taskset(payload)

    tasks = payload["tasks"]
    assert len(tasks) == 16
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
    raw = DEFAULT_TASKSET.read_text(encoding="utf-8").lower()

    assert "api_key" not in raw
    assert "config/local.yaml" not in raw
    assert ".data/" not in raw
    assert "http://example.test" not in raw
    assert "https://example.test" not in raw


def test_select_path_supports_indexes_and_wildcards() -> None:
    payload = {"items": [{"id": 1}, {"id": 2}]}

    assert select_path(payload, "items[0].id") == 1
    assert select_path(payload, "items[*].id") == [1, 2]
    assert select_path(payload, "items[3].id") is MISSING


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
        "thresholds": {},
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


def test_json_output_is_limited_to_isolated_runtime_roots(
    tmp_path: Path,
    monkeypatch,
) -> None:
    data_dir = tmp_path / "data-test"
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    monkeypatch.setenv("TMP_DIR", str(data_dir / "tmp"))

    _require_isolated_output_path(data_dir / "result.json")

    with pytest.raises(RuntimeError, match="DATA_DIR/TMP_DIR"):
        _require_isolated_output_path(tmp_path / "tracked-result.json")
