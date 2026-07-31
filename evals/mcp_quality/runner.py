"""Runner for the fixed, offline MCP quality evaluation suite."""

from __future__ import annotations

import argparse
import asyncio
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import tempfile
from typing import Any

from tests.offline_runtime import require_offline_runtime_ready

require_offline_runtime_ready()

import yaml  # noqa: E402

from .safety import reject_production_path, require_path_within  # noqa: E402
from .scenario import OfflineMcpScenario  # noqa: E402
from .scorer import (  # noqa: E402
    SUPPORTED_ASSERTION_OPERATORS,
    CheckResult,
    TaskScore,
    _strict_equal,
    score_assertion,
)


DEFAULT_TASKSET = Path(__file__).with_name("tasks.v1.yaml")
DEFAULT_PROPOSALS = Path(__file__).with_name("proposals.baseline.v1.yaml")
REQUIRED_TARGET_DIMENSIONS = frozenset(
    {
        "overall",
        "tool_selection",
        "parameters",
        "result",
        "evidence_relevance",
        "citability",
        "degradation",
    }
)


def _runtime_isolated_roots() -> list[Path]:
    roots = [
        Path(raw)
        for raw in (os.environ.get("DATA_DIR", ""), os.environ.get("TMP_DIR", ""))
        if raw
    ]
    if not roots:
        raise RuntimeError(
            "DATA_DIR/TMP_DIR 未设置；请通过 scripts/run-test.ps1 运行离线评测"
        )
    return roots


def _same_lexical_path(left: Path, right: Path) -> bool:
    left_key = os.path.normcase(os.path.abspath(os.path.normpath(left)))
    right_key = os.path.normcase(os.path.abspath(os.path.normpath(right)))
    return left_key == right_key


def _require_isolated_input_path(
    path: Path,
    *,
    default_path: Path,
    purpose: str,
) -> Path:
    """Allow the fixed tracked asset or a custom fixture under runtime roots."""

    if _same_lexical_path(path, default_path):
        return require_path_within(
            default_path,
            allowed_roots=[default_path.parent],
            purpose=purpose,
        )
    return require_path_within(
        path,
        allowed_roots=_runtime_isolated_roots(),
        purpose=purpose,
    )


ASSERTIONS_WITH_EXPECTED_VALUE = frozenset(
    {
        "contains",
        "contains_all",
        "equals",
        "gte",
        "length_equals",
        "lte",
        "set_equals",
    }
)
ALLOWED_PRIORITIES = frozenset({"P0", "P1", "P2"})
ALLOWED_IMPACTS = frozenset({"low", "medium", "high"})
OFFLINE_READ_ONLY_TOOL_ALLOWLIST = frozenset(
    {
        "collect_evidence",
        "contrast",
        "explain_relation",
        "find_bridges",
        "query_subgraph",
        "timeline_of",
    }
)


@dataclass
class EvaluationReport:
    """Complete evaluation result."""

    schema_version: str
    taskset_version: str
    proposals_version: str
    generated_at: str
    policy_mode: str
    ci_contract: str
    target_thresholds: dict[str, float]
    tasks: list[TaskScore]

    @property
    def dimension_scores(self) -> dict[str, float]:
        earned: dict[str, float] = defaultdict(float)
        possible: dict[str, float] = defaultdict(float)
        for task in self.tasks:
            for check in task.checks:
                possible[check.dimension] += check.weight
                if check.passed:
                    earned[check.dimension] += check.weight
        return {
            dimension: earned[dimension] / total if total else 0.0
            for dimension, total in sorted(possible.items())
        }

    @property
    def overall_score(self) -> float:
        possible = sum(task.possible for task in self.tasks)
        earned = sum(task.earned for task in self.tasks)
        return earned / possible if possible else 0.0

    @property
    def failed_checks(self) -> list[tuple[TaskScore, CheckResult]]:
        failures = []
        for task in self.tasks:
            failures.extend((task, check) for check in task.checks if not check.passed)
        return failures

    @property
    def targets_met(self) -> bool:
        if self.overall_score < float(self.target_thresholds.get("overall", 0.0)):
            return False
        scores = self.dimension_scores
        return all(
            scores.get(dimension, 0.0) >= threshold
            for dimension, threshold in self.target_thresholds.items()
            if dimension != "overall"
        )

    def to_dict(self, include_outputs: bool = False) -> dict[str, Any]:
        tasks = [task.to_dict() for task in self.tasks]
        if not include_outputs:
            for task in tasks:
                task.pop("output", None)
        return {
            "schema_version": self.schema_version,
            "taskset_version": self.taskset_version,
            "proposals_version": self.proposals_version,
            "generated_at": self.generated_at,
            "offline": True,
            "policy_mode": self.policy_mode,
            "ci_contract": self.ci_contract,
            "task_count": len(self.tasks),
            "overall_score": round(self.overall_score, 4),
            "dimension_scores": {
                key: round(value, 4) for key, value in self.dimension_scores.items()
            },
            "target_thresholds": self.target_thresholds,
            "targets_met": self.targets_met,
            "thresholds_met": self.targets_met,
            "passed_task_count": sum(task.passed for task in self.tasks),
            "failed_task_count": sum(not task.passed for task in self.tasks),
            "failed_check_count": len(self.failed_checks),
            "tasks": tasks,
        }


async def run_evaluation(
    taskset_path: Path = DEFAULT_TASKSET,
    proposals_path: Path = DEFAULT_PROPOSALS,
    *,
    work_dir: Path,
) -> EvaluationReport:
    """Execute the fixed task set against an isolated MCP scenario."""

    safe_work_dir = require_path_within(
        work_dir,
        allowed_roots=_runtime_isolated_roots(),
        purpose="离线 MCP 评测工作目录",
    )
    safe_taskset_path = _require_isolated_input_path(
        taskset_path,
        default_path=DEFAULT_TASKSET,
        purpose="MCP 评测任务集",
    )
    safe_proposals_path = _require_isolated_input_path(
        proposals_path,
        default_path=DEFAULT_PROPOSALS,
        purpose="MCP 评测 proposals",
    )

    payload = yaml.safe_load(safe_taskset_path.read_text(encoding="utf-8"))
    proposals_payload = yaml.safe_load(
        safe_proposals_path.read_text(encoding="utf-8")
    )
    _validate_taskset(payload)
    _validate_proposals(proposals_payload, payload)
    proposals_by_task = {
        str(item["task_id"]): item["proposed_call"]
        for item in proposals_payload["proposals"]
    }
    target_thresholds = {
        key: float(value) for key, value in payload["target_thresholds"].items()
    }
    task_scores: list[TaskScore] = []

    with OfflineMcpScenario(safe_work_dir) as scenario:
        registered_tools = await scenario.registered_tools()
        for task in payload["tasks"]:
            task_scores.append(
                await _run_task(
                    task,
                    proposals_by_task[str(task["id"])],
                    scenario,
                    registered_tools,
                )
            )

    policy = payload["policy"]
    return EvaluationReport(
        schema_version="pkv.mcp_quality_report.v1",
        taskset_version=str(payload["schema_version"]),
        proposals_version=str(proposals_payload["schema_version"]),
        generated_at=datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        policy_mode=str(policy["mode"]),
        ci_contract=str(policy["ci_contract"]),
        target_thresholds=target_thresholds,
        tasks=task_scores,
    )


async def _run_task(
    task: dict[str, Any],
    proposed_call: dict[str, Any],
    scenario: OfflineMcpScenario,
    registered_tools: dict[str, Any],
) -> TaskScore:
    proposed = scenario.resolve_aliases(proposed_call)
    expected = scenario.resolve_aliases(task["expected_call"])
    proposed["arguments"] = _normalize_mcp_arguments(
        dict(proposed.get("arguments", {}))
    )
    expected["arguments"] = _normalize_mcp_arguments(
        dict(expected.get("arguments", {}))
    )
    tool_name = str(proposed["tool"])
    score = TaskScore(
        task_id=str(task["id"]),
        category=str(task["category"]),
        prompt=str(task["prompt"]),
        tool=tool_name,
    )
    score.checks.extend(
        [
            CheckResult(
                check_id="tool_selection",
                dimension="tool_selection",
                passed=tool_name == expected["tool"],
                weight=1.0,
                expected=expected["tool"],
                actual=tool_name,
                message="proposed Tool must match the fixed task's gold Tool",
                priority="P0",
                impact="high",
            ),
            CheckResult(
                check_id="tool_registered",
                dimension="tool_selection",
                passed=tool_name in registered_tools,
                weight=1.0,
                expected="registered MCP Tool",
                actual=tool_name in registered_tools,
                message="selected Tool must be discoverable through MCP list_tools",
                priority="P0",
                impact="high",
            ),
            CheckResult(
                check_id="arguments_match",
                dimension="parameters",
                passed=_strict_equal(
                    proposed.get("arguments", {}),
                    expected.get("arguments", {}),
                ),
                weight=1.0,
                expected=expected.get("arguments", {}),
                actual=proposed.get("arguments", {}),
                message="proposed arguments must match the fixed gold call",
                priority="P0",
                impact="high",
            ),
        ]
    )

    if tool_name not in registered_tools:
        score.execution_error = f"Tool not registered: {tool_name}"
        score.checks.append(
            CheckResult(
                check_id="mcp_schema_accepts_arguments",
                dimension="parameters",
                passed=False,
                weight=1.0,
                expected="FastMCP accepts arguments",
                actual=score.execution_error,
                message="selected Tool is not registered",
                priority="P0",
                impact="high",
            )
        )
        _append_result_assertions(score, task, scenario)
        return score

    try:
        score.output = await scenario.call_tool(
            tool_name,
            dict(proposed.get("arguments", {})),
        )
    except Exception as exc:
        score.execution_error = f"{type(exc).__name__}: {exc}"
        score.checks.append(
            CheckResult(
                check_id="mcp_schema_accepts_arguments",
                dimension="parameters",
                passed=False,
                weight=1.0,
                expected="FastMCP accepts arguments",
                actual=score.execution_error,
                message="MCP schema validation or handler execution failed",
                priority="P0",
                impact="high",
            )
        )
        _append_result_assertions(score, task, scenario)
        return score

    score.checks.append(
        CheckResult(
            check_id="mcp_schema_accepts_arguments",
            dimension="parameters",
            passed=True,
            weight=1.0,
            expected="FastMCP accepts arguments",
            actual=True,
            message="",
            priority="P0",
            impact="high",
        )
    )
    _append_result_assertions(score, task, scenario)
    return score


def _append_result_assertions(
    score: TaskScore,
    task: dict[str, Any],
    scenario: OfflineMcpScenario,
) -> None:
    for assertion in task.get("assertions", []):
        resolved = scenario.resolve_aliases(assertion)
        score.checks.append(score_assertion(resolved, score.output))


def _normalize_mcp_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    """Keep numeric fixture aliases compatible with MCP's string ID schema."""

    string_id_fields = {
        "knowledge_id",
        "seed_knowledge_id",
        "source_knowledge_id",
        "target_knowledge_id",
    }
    return {
        key: (
            str(value)
            if (
                key in string_id_fields
                and isinstance(value, int)
                and not isinstance(value, bool)
            )
            else value
        )
        for key, value in arguments.items()
    }


def _validate_taskset(payload: dict[str, Any]) -> None:
    if not isinstance(payload, dict):
        raise ValueError("MCP quality taskset must be a mapping")
    if payload.get("schema_version") != "pkv.mcp_quality_tasks.v1":
        raise ValueError("unsupported MCP quality taskset schema")
    policy = payload.get("policy")
    if not isinstance(policy, dict):
        raise ValueError("taskset policy must be a mapping")
    if policy.get("mode") != "threshold_enforced":
        raise ValueError("MCP quality policy must enforce thresholds")
    if policy.get("ci_contract") != "schema_all_checks_and_thresholds":
        raise ValueError("unsupported MCP quality CI contract")
    if policy.get("target_gate_activation") != "active":
        raise ValueError("MCP quality target gate must be active")
    thresholds = payload.get("target_thresholds")
    if not isinstance(thresholds, dict):
        raise ValueError("taskset must declare target_thresholds as a mapping")
    if set(thresholds) != REQUIRED_TARGET_DIMENSIONS:
        raise ValueError(
            "target_thresholds must declare the complete v1 dimension set"
        )
    for dimension, threshold in thresholds.items():
        if (
            not isinstance(threshold, (int, float))
            or isinstance(threshold, bool)
            or not math.isfinite(float(threshold))
            or not 0.0 <= float(threshold) <= 1.0
        ):
            raise ValueError(
                f"target threshold must be a finite number in [0, 1]: {dimension}"
            )

    tasks = payload.get("tasks")
    if not isinstance(tasks, list) or not 10 <= len(tasks) <= 20:
        raise ValueError("MCP quality taskset must contain 10-20 tasks")

    if any(not isinstance(task, dict) for task in tasks):
        raise ValueError("each MCP quality task must be a mapping")
    task_ids = [task.get("id") for task in tasks]
    if (
        any(not isinstance(task_id, str) or not task_id.strip() for task_id in task_ids)
        or len(task_ids) != len(set(task_ids))
    ):
        raise ValueError("task ids must be non-empty and unique")
    if any("proposed_call" in task for task in tasks):
        raise ValueError("gold taskset must not embed proposed_call")

    assertion_ids: set[str] = set()
    for task in tasks:
        for field_name in ("category", "prompt"):
            value = task.get(field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(
                    f"task {task['id']} must contain non-empty {field_name}"
                )
        _validate_call(
            task.get("expected_call"),
            label=f"task {task['id']} expected_call",
        )
        assertions = task.get("assertions")
        if not isinstance(assertions, list) or not assertions:
            raise ValueError(f"task {task['id']} assertions must be a non-empty list")
        for assertion in assertions:
            _validate_assertion(assertion, assertion_ids, thresholds)


def _validate_proposals(
    payload: dict[str, Any],
    taskset_payload: dict[str, Any],
) -> None:
    if not isinstance(payload, dict):
        raise ValueError("MCP quality proposals must be a mapping")
    if payload.get("schema_version") != "pkv.mcp_quality_proposals.v1":
        raise ValueError("unsupported MCP quality proposals schema")
    if payload.get("taskset_version") != taskset_payload.get("schema_version"):
        raise ValueError("proposals taskset_version does not match taskset")
    proposals = payload.get("proposals")
    if not isinstance(proposals, list):
        raise ValueError("proposals must be a list")
    if any(not isinstance(item, dict) for item in proposals):
        raise ValueError("each proposal must be a mapping")
    proposal_ids = [item.get("task_id") for item in proposals]
    if any(
        not isinstance(task_id, str) or not task_id.strip()
        for task_id in proposal_ids
    ):
        raise ValueError("proposal task ids must be non-empty")
    if len(proposal_ids) != len(set(proposal_ids)):
        raise ValueError("proposal task ids must be unique")
    expected_ids = {str(task["id"]) for task in taskset_payload["tasks"]}
    if set(proposal_ids) != expected_ids:
        raise ValueError("proposals must cover exactly the taskset ids")
    for item in proposals:
        _validate_call(
            item.get("proposed_call"),
            label=f"proposal {item['task_id']} proposed_call",
        )


def _validate_call(value: Any, *, label: str) -> None:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a mapping")
    if set(value) != {"tool", "arguments"}:
        raise ValueError(f"{label} must contain exactly tool and arguments")
    tool = value.get("tool")
    if not isinstance(tool, str) or not tool.strip():
        raise ValueError(f"{label} must contain a non-empty string tool")
    if tool not in OFFLINE_READ_ONLY_TOOL_ALLOWLIST:
        raise ValueError(
            f"{label} selects Tool outside fixed offline read-only allowlist: {tool}"
        )
    if "arguments" not in value or not isinstance(value["arguments"], dict):
        raise ValueError(f"{label} must contain an arguments mapping")


def _validate_assertion(
    assertion: Any,
    assertion_ids: set[str],
    thresholds: dict[str, Any],
) -> None:
    if not isinstance(assertion, dict):
        raise ValueError("each task assertion must be a mapping")

    assertion_id = assertion.get("id")
    if not isinstance(assertion_id, str) or not assertion_id.strip():
        raise ValueError("assertion ids must be non-empty strings")
    if assertion_id in assertion_ids:
        raise ValueError(f"assertion ids must be globally unique: {assertion_id}")
    assertion_ids.add(assertion_id)

    dimension = assertion.get("dimension")
    if (
        not isinstance(dimension, str)
        or dimension == "overall"
        or dimension not in thresholds
    ):
        raise ValueError(f"assertion has unknown target dimension: {assertion_id}")

    operator = assertion.get("op")
    if (
        not isinstance(operator, str)
        or operator not in SUPPORTED_ASSERTION_OPERATORS
    ):
        raise ValueError(f"unsupported assertion operator: {operator}")

    path = assertion.get("path", "$")
    if not isinstance(path, str) or not path:
        raise ValueError(f"assertion path must be a non-empty string: {assertion_id}")

    weight = assertion.get("weight", 1.0)
    if (
        not isinstance(weight, (int, float))
        or isinstance(weight, bool)
        or not math.isfinite(float(weight))
        or float(weight) <= 0.0
    ):
        raise ValueError(f"assertion weight must be finite and positive: {assertion_id}")

    if operator in ASSERTIONS_WITH_EXPECTED_VALUE and "expected" not in assertion:
        raise ValueError(
            f"assertion operator requires expected value: {assertion_id}"
        )
    if operator == "contains_all" and (
        not isinstance(assertion.get("expected"), list)
        or not assertion["expected"]
    ):
        raise ValueError(
            f"contains_all expected must be a non-empty list: {assertion_id}"
        )

    if (
        "priority" in assertion
        and (
            not isinstance(assertion["priority"], str)
            or assertion["priority"] not in ALLOWED_PRIORITIES
        )
    ):
        raise ValueError(f"assertion priority is invalid: {assertion_id}")
    if (
        "impact" in assertion
        and (
            not isinstance(assertion["impact"], str)
            or assertion["impact"] not in ALLOWED_IMPACTS
        )
    ):
        raise ValueError(f"assertion impact is invalid: {assertion_id}")


def _default_work_parent() -> Path:
    raw = os.environ.get("TMP_DIR", "")
    if not raw:
        raise RuntimeError(
            "TMP_DIR 未设置；请通过 scripts/run-test.ps1 运行离线评测"
        )
    parent = reject_production_path(
        Path(raw),
        purpose="MCP 评测临时目录",
    )
    parent.mkdir(parents=True, exist_ok=True)
    return parent


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="运行固定、离线、可重复的 MCP 最小质量评测"
    )
    parser.add_argument(
        "--taskset",
        type=Path,
        default=DEFAULT_TASKSET,
        help="任务集 YAML（默认 evals/mcp_quality/tasks.v1.yaml）",
    )
    parser.add_argument(
        "--proposals",
        type=Path,
        default=DEFAULT_PROPOSALS,
        help="独立 proposed calls YAML（默认 proposals.baseline.v1.yaml）",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="可选 JSON 结果路径；临时结果建议写入 .data-test 场景目录",
    )
    parser.add_argument(
        "--include-outputs",
        action="store_true",
        help="在 JSON 中包含每个 Tool 的完整输出",
    )
    parser.add_argument(
        "--enforce-thresholds",
        action="store_true",
        help="当任一固定质量阈值未达到时返回非零退出码",
    )
    parser.add_argument(
        "--check-targets",
        action="store_true",
        help="--enforce-thresholds 的兼容别名",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    taskset_path = _require_isolated_input_path(
        args.taskset,
        default_path=DEFAULT_TASKSET,
        purpose="MCP 评测任务集",
    )
    proposals_path = _require_isolated_input_path(
        args.proposals,
        default_path=DEFAULT_PROPOSALS,
        purpose="MCP 评测 proposals",
    )
    output = (
        _require_isolated_output_path(args.output)
        if args.output is not None
        else None
    )
    work_parent = _default_work_parent()
    with tempfile.TemporaryDirectory(
        prefix="pkv-mcp-quality-",
        dir=work_parent,
    ) as temp_dir:
        report = asyncio.run(
            run_evaluation(
                taskset_path,
                proposals_path,
                work_dir=Path(temp_dir),
            )
        )

    result = report.to_dict(include_outputs=args.include_outputs)
    rendered = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    enforce_thresholds = args.enforce_thresholds or args.check_targets
    return 1 if enforce_thresholds and not report.targets_met else 0


def _require_isolated_output_path(output: Path) -> Path:
    return require_path_within(
        output,
        allowed_roots=_runtime_isolated_roots(),
        purpose="MCP 评测 JSON 输出",
    )


if __name__ == "__main__":
    raise SystemExit(main())
