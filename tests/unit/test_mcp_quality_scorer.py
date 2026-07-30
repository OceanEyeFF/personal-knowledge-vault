"""Unit tests for the Phase C offline MCP scorer and task contract."""

import copy
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
    build_parser,
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
    assert sum(len(task["assertions"]) + 4 for task in tasks) == 119
    assert payload["policy"] == {
        "mode": "threshold_enforced",
        "ci_contract": "schema_all_checks_and_thresholds",
        "target_gate_activation": "active",
    }
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


def test_cli_exposes_threshold_enforcement_and_compatibility_alias() -> None:
    parser = build_parser()

    assert parser.parse_args(["--enforce-thresholds"]).enforce_thresholds is True
    assert parser.parse_args(["--check-targets"]).check_targets is True


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
        ("set_equals", [1, 1, 2], [2, 1], True),
        ("set_equals", [True], [1], False),
        ("equals", True, 1, False),
        ("equals", [True], [1], False),
        ("length_equals", [1, 2], 2, True),
        ("length_equals", [1, 2], "2", False),
        ("gte", 0.8, 0.7, True),
        ("gte", "0.8", 0.7, False),
        ("gte", float("inf"), 0.7, False),
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
    payload = yaml.safe_load(DEFAULT_TASKSET.read_text(encoding="utf-8"))
    payload["tasks"] = payload["tasks"][:9]

    with pytest.raises(ValueError, match="10-20"):
        _validate_taskset(payload)


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("threshold_bool", r"finite number in \[0, 1\]"),
        ("threshold_missing", "complete v1 dimension set"),
        ("task_category", "non-empty category"),
        ("expected_tool_type", "non-empty string tool"),
        ("assertions_type", "non-empty list"),
        ("duplicate_assertion", "globally unique"),
        ("unknown_dimension", "unknown target dimension"),
        ("unsupported_operator", "unsupported assertion operator"),
        ("empty_path", "path must be a non-empty string"),
        ("zero_weight", "weight must be finite and positive"),
        ("missing_expected", "requires expected value"),
        ("invalid_priority", "priority is invalid"),
        ("invalid_impact", "impact is invalid"),
    ],
)
def test_validate_taskset_rejects_malformed_contracts(
    case: str,
    message: str,
) -> None:
    payload = yaml.safe_load(DEFAULT_TASKSET.read_text(encoding="utf-8"))
    first_task = payload["tasks"][0]
    first_assertion = first_task["assertions"][0]

    if case == "threshold_bool":
        payload["target_thresholds"]["overall"] = True
    elif case == "threshold_missing":
        payload["target_thresholds"].pop("citability")
    elif case == "task_category":
        first_task["category"] = ""
    elif case == "expected_tool_type":
        first_task["expected_call"]["tool"] = 7
    elif case == "assertions_type":
        first_task["assertions"] = {}
    elif case == "duplicate_assertion":
        duplicate = copy.deepcopy(payload["tasks"][1]["assertions"][0])
        duplicate["id"] = first_assertion["id"]
        payload["tasks"][1]["assertions"].append(duplicate)
    elif case == "unknown_dimension":
        first_assertion["dimension"] = "accuracy"
    elif case == "unsupported_operator":
        first_assertion["op"] = "roughly_equals"
    elif case == "empty_path":
        first_assertion["path"] = ""
    elif case == "zero_weight":
        first_assertion["weight"] = 0
    elif case == "missing_expected":
        first_assertion["op"] = "equals"
        first_assertion.pop("expected", None)
    elif case == "invalid_priority":
        first_assertion["priority"] = "urgent"
    elif case == "invalid_impact":
        first_assertion["impact"] = "catastrophic"
    else:  # pragma: no cover - parameter table is exhaustive
        raise AssertionError(case)

    with pytest.raises(ValueError, match=message):
        _validate_taskset(payload)


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("payload_type", "must be a mapping"),
        ("proposal_item_type", "each proposal must be a mapping"),
        ("task_id_type", "task ids must be non-empty"),
        ("tool_type", "non-empty string tool"),
        ("missing_arguments", "arguments mapping"),
    ],
)
def test_validate_proposals_rejects_malformed_calls(
    case: str,
    message: str,
) -> None:
    taskset = yaml.safe_load(DEFAULT_TASKSET.read_text(encoding="utf-8"))
    proposals = yaml.safe_load(DEFAULT_PROPOSALS.read_text(encoding="utf-8"))

    if case == "payload_type":
        proposals = []  # type: ignore[assignment]
    elif case == "proposal_item_type":
        proposals["proposals"][0] = "not-a-mapping"
    elif case == "task_id_type":
        proposals["proposals"][0]["task_id"] = 1
    elif case == "tool_type":
        proposals["proposals"][0]["proposed_call"]["tool"] = 7
    elif case == "missing_arguments":
        proposals["proposals"][0]["proposed_call"].pop("arguments")
    else:  # pragma: no cover - parameter table is exhaustive
        raise AssertionError(case)

    with pytest.raises(ValueError, match=message):
        _validate_proposals(proposals, taskset)


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
