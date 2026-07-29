"""Deterministic scorer for the offline MCP quality task set."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


MISSING = object()


@dataclass(frozen=True)
class CheckResult:
    """One scored contract check."""

    check_id: str
    dimension: str
    passed: bool
    weight: float
    expected: Any
    actual: Any
    message: str = ""
    priority: str = "P2"
    impact: str = "medium"
    phase_b_hint: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "check_id": self.check_id,
            "dimension": self.dimension,
            "passed": self.passed,
            "weight": self.weight,
            "expected": self.expected,
            "actual": _replace_missing(self.actual),
            "message": self.message,
            "priority": self.priority,
            "impact": self.impact,
            "phase_b_hint": self.phase_b_hint,
        }


@dataclass
class TaskScore:
    """Scored result for one fixed task."""

    task_id: str
    category: str
    prompt: str
    tool: str
    checks: list[CheckResult] = field(default_factory=list)
    output: dict[str, Any] = field(default_factory=dict)
    execution_error: str = ""

    @property
    def earned(self) -> float:
        return sum(check.weight for check in self.checks if check.passed)

    @property
    def possible(self) -> float:
        return sum(check.weight for check in self.checks)

    @property
    def score(self) -> float:
        return self.earned / self.possible if self.possible else 0.0

    @property
    def passed(self) -> bool:
        return not self.execution_error and all(check.passed for check in self.checks)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "category": self.category,
            "prompt": self.prompt,
            "tool": self.tool,
            "passed": self.passed,
            "score": round(self.score, 4),
            "earned": self.earned,
            "possible": self.possible,
            "execution_error": self.execution_error,
            "checks": [check.to_dict() for check in self.checks],
            "output": self.output,
        }


def select_path(payload: Any, path: str) -> Any:
    """Select a value with a small deterministic dotted-path syntax.

    Supported examples:
    - ``found``
    - ``items[0].knowledge_id``
    - ``items[*].knowledge_id``
    """

    if path in ("", "$"):
        return payload

    current = [payload]
    wildcard_used = False
    for raw_segment in path.split("."):
        next_values: list[Any] = []
        if raw_segment.endswith("[*]"):
            key = raw_segment[:-3]
            wildcard_used = True
            for value in current:
                target = _get_key(value, key)
                if not isinstance(target, list):
                    continue
                next_values.extend(target)
        elif "[" in raw_segment and raw_segment.endswith("]"):
            key, raw_index = raw_segment[:-1].split("[", 1)
            try:
                index = int(raw_index)
            except ValueError:
                return MISSING
            for value in current:
                target = _get_key(value, key)
                if isinstance(target, list) and -len(target) <= index < len(target):
                    next_values.append(target[index])
        else:
            for value in current:
                target = _get_key(value, raw_segment)
                if target is not MISSING or wildcard_used:
                    next_values.append(target)
        current = next_values
        if not current:
            return MISSING

    if wildcard_used or len(current) != 1:
        return current
    return current[0]


def score_assertion(assertion: dict[str, Any], output: dict[str, Any]) -> CheckResult:
    """Score one declarative assertion."""

    actual = select_path(output, str(assertion.get("path", "$")))
    operator = str(assertion["op"])
    expected = assertion.get("expected")
    passed = _apply_operator(operator, actual, expected)
    message = str(assertion.get("message", ""))
    if not passed and not message:
        message = f"{assertion.get('path', '$')} failed {operator}"
    return CheckResult(
        check_id=str(assertion["id"]),
        dimension=str(assertion["dimension"]),
        passed=passed,
        weight=float(assertion.get("weight", 1.0)),
        expected=expected,
        actual=actual,
        message=message,
        priority=str(assertion.get("priority", "P2")),
        impact=str(assertion.get("impact", "medium")),
        phase_b_hint=str(assertion.get("phase_b_hint", "")),
    )


def _get_key(value: Any, key: str) -> Any:
    if key == "":
        return value
    if isinstance(value, dict):
        return value.get(key, MISSING)
    return MISSING


def _apply_operator(operator: str, actual: Any, expected: Any) -> bool:
    if operator == "equals":
        return actual is not MISSING and actual == expected
    if operator == "not_empty":
        return actual is not MISSING and actual not in (None, "", [], {})
    if operator == "all_not_empty":
        return (
            isinstance(actual, list)
            and bool(actual)
            and all(
                item is not MISSING and item not in (None, "", [], {})
                for item in actual
            )
        )
    if operator == "truthy":
        return actual is not MISSING and bool(actual)
    if operator == "contains":
        if actual is MISSING:
            return False
        try:
            return expected in actual
        except TypeError:
            return False
    if operator == "contains_all":
        if actual is MISSING:
            return False
        try:
            return all(item in actual for item in expected)
        except TypeError:
            return False
    if operator == "set_equals":
        if actual is MISSING:
            return False
        try:
            return set(actual) == set(expected)
        except TypeError:
            return False
    if operator == "length_equals":
        if actual is MISSING:
            return False
        try:
            return len(actual) == int(expected)
        except (TypeError, ValueError):
            return False
    if operator == "gte":
        if actual is MISSING:
            return False
        try:
            return float(actual) >= float(expected)
        except (TypeError, ValueError):
            return False
    if operator == "lte":
        if actual is MISSING:
            return False
        try:
            return float(actual) <= float(expected)
        except (TypeError, ValueError):
            return False
    raise ValueError(f"unsupported assertion operator: {operator}")


def _replace_missing(value: Any) -> Any:
    if value is MISSING:
        return None
    if isinstance(value, list):
        return [_replace_missing(item) for item in value]
    if isinstance(value, dict):
        return {key: _replace_missing(item) for key, item in value.items()}
    return value
