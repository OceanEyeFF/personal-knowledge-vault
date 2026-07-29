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

from .scenario import OfflineMcpScenario, PROJECT_ROOT
from .scorer import CheckResult, TaskScore, score_assertion


DEFAULT_TASKSET = Path(__file__).with_name("tasks.v1.yaml")


@dataclass
class EvaluationReport:
    """Complete evaluation result."""

    schema_version: str
    taskset_version: str
    generated_at: str
    thresholds: dict[str, float]
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
    def thresholds_met(self) -> bool:
        if self.overall_score < float(self.thresholds.get("overall", 0.0)):
            return False
        scores = self.dimension_scores
        return all(
            scores.get(dimension, 0.0) >= threshold
            for dimension, threshold in self.thresholds.items()
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
            "generated_at": self.generated_at,
            "offline": True,
            "task_count": len(self.tasks),
            "overall_score": round(self.overall_score, 4),
            "dimension_scores": {
                key: round(value, 4) for key, value in self.dimension_scores.items()
            },
            "thresholds": self.thresholds,
            "thresholds_met": self.thresholds_met,
            "passed_task_count": sum(task.passed for task in self.tasks),
            "failed_task_count": sum(not task.passed for task in self.tasks),
            "failed_check_count": len(self.failed_checks),
            "tasks": tasks,
        }


async def run_evaluation(
    taskset_path: Path = DEFAULT_TASKSET,
    *,
    work_dir: Path,
) -> EvaluationReport:
    """Execute the fixed task set against an isolated MCP scenario."""

    payload = yaml.safe_load(taskset_path.read_text(encoding="utf-8"))
    _validate_taskset(payload)
    thresholds = {
        key: float(value) for key, value in payload["thresholds"].items()
    }
    task_scores: list[TaskScore] = []

    with OfflineMcpScenario(work_dir) as scenario:
        registered_tools = await scenario.registered_tools()
        for task in payload["tasks"]:
            task_scores.append(
                await _run_task(task, scenario, registered_tools)
            )

    return EvaluationReport(
        schema_version="pkv.mcp_quality_report.v1",
        taskset_version=str(payload["schema_version"]),
        generated_at=datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        thresholds=thresholds,
        tasks=task_scores,
    )


async def _run_task(
    task: dict[str, Any],
    scenario: OfflineMcpScenario,
    registered_tools: dict[str, Any],
) -> TaskScore:
    proposed = scenario.resolve_aliases(task["proposed_call"])
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
    tasks = payload.get("tasks")
    if not isinstance(tasks, list) or not 10 <= len(tasks) <= 20:
        raise ValueError("MCP quality taskset must contain 10-20 tasks")
    task_ids = [str(task.get("id", "")) for task in tasks]
    if any(not task_id for task_id in task_ids) or len(task_ids) != len(set(task_ids)):
        raise ValueError("task ids must be non-empty and unique")


def _default_work_parent() -> Path:
    raw = os.environ.get("TMP_DIR", "")
    if not raw:
        raise RuntimeError(
            "TMP_DIR 未设置；请通过 scripts/run-test.ps1 运行离线评测"
        )
    parent = Path(raw).resolve()
    parent.mkdir(parents=True, exist_ok=True)
    _reject_production_path(parent)
    return parent


def _reject_production_path(path: Path) -> None:
    production = (PROJECT_ROOT / ".data").resolve()
    try:
        path.relative_to(production)
    except ValueError:
        return
    raise RuntimeError(f"评测工作目录不得位于生产 .data: {path}")


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
        help="任一目标阈值未达到时返回退出码 1",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    work_parent = _default_work_parent()
    with tempfile.TemporaryDirectory(
        prefix="pkv-mcp-quality-",
        dir=work_parent,
    ) as temp_dir:
        report = asyncio.run(
            run_evaluation(
                args.taskset.resolve(),
                work_dir=Path(temp_dir),
            )
        )

    result = report.to_dict(include_outputs=args.include_outputs)
    rendered = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output:
        output = args.output.resolve()
        _require_isolated_output_path(output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 1 if args.enforce_thresholds and not report.thresholds_met else 0


def _require_isolated_output_path(output: Path) -> None:
    _reject_production_path(output)
    allowed_roots = [
        Path(raw).resolve()
        for raw in (os.environ.get("DATA_DIR", ""), os.environ.get("TMP_DIR", ""))
        if raw
    ]
    for root in allowed_roots:
        try:
            output.relative_to(root)
        except ValueError:
            continue
        return
    raise RuntimeError(
        "评测 JSON 只能写入 run-test.ps1 提供的 DATA_DIR/TMP_DIR 隔离目录"
    )


if __name__ == "__main__":
    raise SystemExit(main())
