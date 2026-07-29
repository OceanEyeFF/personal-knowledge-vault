"""Runner for the fixed, offline MCP quality evaluation suite."""

from __future__ import annotations

import argparse
import asyncio
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
from typing import Any

import yaml

from .safety import reject_production_path, require_path_within
from .scenario import OfflineMcpScenario
from .scorer import CheckResult, TaskScore, score_assertion


DEFAULT_TASKSET = Path(__file__).with_name("tasks.v1.yaml")
DEFAULT_PROPOSALS = Path(__file__).with_name("proposals.baseline.v1.yaml")


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

    safe_work_dir = reject_production_path(
        work_dir,
        purpose="离线 MCP 评测工作目录",
    )
    safe_taskset_path = reject_production_path(
        taskset_path,
        purpose="MCP 评测任务集",
    )
    safe_proposals_path = reject_production_path(
        proposals_path,
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
                passed=proposed.get("arguments", {}) == expected.get("arguments", {}),
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
        key: str(value) if key in string_id_fields and isinstance(value, int) else value
        for key, value in arguments.items()
    }


def _validate_taskset(payload: dict[str, Any]) -> None:
    if payload.get("schema_version") != "pkv.mcp_quality_tasks.v1":
        raise ValueError("unsupported MCP quality taskset schema")
    policy = payload.get("policy", {})
    if policy.get("mode") != "baseline_only":
        raise ValueError("Phase C MCP quality policy must be baseline_only")
    if policy.get("ci_contract") != "schema_and_failure_matrix":
        raise ValueError("unsupported Phase C CI contract")
    if "target_thresholds" not in payload:
        raise ValueError("taskset must declare target_thresholds")
    tasks = payload.get("tasks")
    if not isinstance(tasks, list) or not 10 <= len(tasks) <= 20:
        raise ValueError("MCP quality taskset must contain 10-20 tasks")
    task_ids = [str(task.get("id", "")) for task in tasks]
    if any(not task_id for task_id in task_ids) or len(task_ids) != len(set(task_ids)):
        raise ValueError("task ids must be non-empty and unique")
    if any("proposed_call" in task for task in tasks):
        raise ValueError("gold taskset must not embed proposed_call")


def _validate_proposals(
    payload: dict[str, Any],
    taskset_payload: dict[str, Any],
) -> None:
    if payload.get("schema_version") != "pkv.mcp_quality_proposals.v1":
        raise ValueError("unsupported MCP quality proposals schema")
    if payload.get("taskset_version") != taskset_payload.get("schema_version"):
        raise ValueError("proposals taskset_version does not match taskset")
    proposals = payload.get("proposals")
    if not isinstance(proposals, list):
        raise ValueError("proposals must be a list")
    proposal_ids = [str(item.get("task_id", "")) for item in proposals]
    if any(not task_id for task_id in proposal_ids):
        raise ValueError("proposal task ids must be non-empty")
    if len(proposal_ids) != len(set(proposal_ids)):
        raise ValueError("proposal task ids must be unique")
    expected_ids = {str(task["id"]) for task in taskset_payload["tasks"]}
    if set(proposal_ids) != expected_ids:
        raise ValueError("proposals must cover exactly the taskset ids")
    for item in proposals:
        proposed_call = item.get("proposed_call")
        if not isinstance(proposed_call, dict):
            raise ValueError("each proposal must contain proposed_call")
        if not proposed_call.get("tool") or not isinstance(
            proposed_call.get("arguments", {}),
            dict,
        ):
            raise ValueError("proposed_call must contain tool and arguments")


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
        "--check-targets",
        action="store_true",
        help="诊断未来 Phase B 目标；当前 baseline-only CI 不启用",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    taskset_path = reject_production_path(
        args.taskset,
        purpose="MCP 评测任务集",
    )
    proposals_path = reject_production_path(
        args.proposals,
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
    return 1 if args.check_targets and not report.targets_met else 0


def _require_isolated_output_path(output: Path) -> Path:
    allowed_roots = [
        Path(raw)
        for raw in (os.environ.get("DATA_DIR", ""), os.environ.get("TMP_DIR", ""))
        if raw
    ]
    return require_path_within(
        output,
        allowed_roots=allowed_roots,
        purpose="MCP 评测 JSON 输出",
    )


if __name__ == "__main__":
    raise SystemExit(main())
