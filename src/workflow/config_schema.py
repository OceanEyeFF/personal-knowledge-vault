"""Fail-closed schema and expression support for published workflows."""

from __future__ import annotations

import ast
import math
from collections.abc import Mapping, Sequence
from typing import Any

from src.runtime.errors import ErrorCode, PKVRuntimeError


WORKFLOW_SCHEMA_VERSION = 1
ON_ERROR_VALUES = frozenset({"fail", "continue"})
CONDITION_VARIABLES = frozenset(
    {
        "content_length",
        "tag_count",
        "concept_count",
        "content_type",
        "source",
    }
)
TRIGGER_RULES = frozenset(
    {
        "content_length_gt",
        "core_concepts_ge",
        "content_type_in",
        "contains_keywords",
    }
)


def _config_error(message: str, *, step_id: str | None = None) -> PKVRuntimeError:
    stage = "workflow_configuration"
    if step_id:
        stage = f"workflow_configuration:{step_id}"
    return PKVRuntimeError(
        ErrorCode.WORKFLOW_CONFIG_INVALID,
        message,
        stage=stage,
        recoverable=True,
    )


def _condition_error(message: str, *, step_id: str | None = None) -> PKVRuntimeError:
    stage = "workflow_condition"
    if step_id:
        stage = f"workflow_condition:{step_id}"
    return PKVRuntimeError(
        ErrorCode.WORKFLOW_CONDITION_INVALID,
        message,
        stage=stage,
        recoverable=True,
    )


def _is_number(value: object) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(float(value))
    except (OverflowError, TypeError, ValueError):
        return False


def _require_positive_int(value: object, label: str, *, step_id: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise _config_error(f"步骤 {step_id} 的 {label} 必须是正整数", step_id=step_id)


def _reject_unknown_keys(
    value: Mapping[str, Any],
    allowed: set[str],
    label: str,
    *,
    step_id: str | None = None,
) -> None:
    unknown = sorted(str(key) for key in value if key not in allowed)
    if unknown:
        raise _config_error(
            f"{label} 含未知字段: {', '.join(unknown)}",
            step_id=step_id,
        )


def _require_positive_number(
    value: object,
    label: str,
    *,
    step_id: str,
    allow_zero: bool = False,
) -> None:
    valid = _is_number(value) and (value >= 0 if allow_zero else value > 0)
    if not valid:
        comparator = "非负数" if allow_zero else "正数"
        raise _config_error(f"步骤 {step_id} 的 {label} 必须是{comparator}", step_id=step_id)


def parse_condition(expression: str, *, step_id: str | None = None) -> ast.Expression:
    """Parse a condition and reject every node outside the small safe grammar."""
    if not isinstance(expression, str) or not expression.strip():
        raise _condition_error("condition 必须是非空字符串", step_id=step_id)
    if len(expression) > 500:
        raise _condition_error("condition 过长", step_id=step_id)

    try:
        tree = ast.parse(expression, mode="eval")
    except (SyntaxError, ValueError) as exc:
        raise _condition_error(f"condition 语法无效: {exc}", step_id=step_id) from exc

    allowed_nodes = (
        ast.Expression,
        ast.BoolOp,
        ast.And,
        ast.Or,
        ast.UnaryOp,
        ast.Not,
        ast.Compare,
        ast.Eq,
        ast.NotEq,
        ast.Gt,
        ast.GtE,
        ast.Lt,
        ast.LtE,
        ast.In,
        ast.NotIn,
        ast.Name,
        ast.Load,
        ast.Constant,
        ast.List,
        ast.Tuple,
        ast.Set,
    )
    for node in ast.walk(tree):
        if not isinstance(node, allowed_nodes):
            raise _condition_error(
                f"condition 包含禁止语法: {type(node).__name__}",
                step_id=step_id,
            )
        if isinstance(node, ast.Name) and node.id not in CONDITION_VARIABLES:
            raise _condition_error(
                f"condition 引用了未知变量: {node.id}",
                step_id=step_id,
            )
        if isinstance(node, ast.Constant):
            value = node.value
            if not isinstance(value, (str, int, float, bool, type(None))):
                raise _condition_error(
                    f"condition 包含禁止常量类型: {type(value).__name__}",
                    step_id=step_id,
                )
            if _is_number(value) is False and isinstance(value, float):
                raise _condition_error(
                    "condition 数值必须是有限值",
                    step_id=step_id,
                )
    return tree


def evaluate_condition(expression: str, variables: Mapping[str, Any]) -> bool:
    """Evaluate the validated condition without ``eval`` or executable objects."""
    tree = parse_condition(expression)

    def evaluate(node: ast.AST) -> Any:
        if isinstance(node, ast.Expression):
            return evaluate(node.body)
        if isinstance(node, ast.Name):
            return variables.get(node.id)
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.List):
            return [evaluate(item) for item in node.elts]
        if isinstance(node, ast.Tuple):
            return tuple(evaluate(item) for item in node.elts)
        if isinstance(node, ast.Set):
            return {evaluate(item) for item in node.elts}
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            return not bool(evaluate(node.operand))
        if isinstance(node, ast.BoolOp):
            if isinstance(node.op, ast.And):
                for value in node.values:
                    if not bool(evaluate(value)):
                        return False
                return True
            for value in node.values:
                if bool(evaluate(value)):
                    return True
            return False
        if isinstance(node, ast.Compare):
            left = evaluate(node.left)
            for operator, comparator_node in zip(node.ops, node.comparators):
                right = evaluate(comparator_node)
                if isinstance(operator, ast.Eq):
                    passed = left == right
                elif isinstance(operator, ast.NotEq):
                    passed = left != right
                elif isinstance(operator, ast.Gt):
                    passed = left > right
                elif isinstance(operator, ast.GtE):
                    passed = left >= right
                elif isinstance(operator, ast.Lt):
                    passed = left < right
                elif isinstance(operator, ast.LtE):
                    passed = left <= right
                elif isinstance(operator, ast.In):
                    passed = left in right
                elif isinstance(operator, ast.NotIn):
                    passed = left not in right
                else:  # pragma: no cover - parse_condition excludes this branch
                    raise AssertionError(f"unsupported operator: {operator!r}")
                if not passed:
                    return False
                left = right
            return True
        raise AssertionError(f"unsafe condition node reached evaluator: {node!r}")

    try:
        return bool(evaluate(tree))
    except (TypeError, ValueError) as exc:
        raise _condition_error(f"condition 比较失败: {exc}") from exc


def validate_trigger_rules(rules: object, *, step_id: str) -> None:
    """Validate the YAML trigger DSL (OR across single-key rules)."""
    if not isinstance(rules, list) or not rules:
        raise _config_error(
            f"步骤 {step_id} 的 trigger_rules 必须是非空列表",
            step_id=step_id,
        )
    for index, rule in enumerate(rules):
        if not isinstance(rule, Mapping) or len(rule) != 1:
            raise _config_error(
                f"步骤 {step_id} 的 trigger_rules[{index}] 必须是单键映射",
                step_id=step_id,
            )
        key, value = next(iter(rule.items()))
        if key not in TRIGGER_RULES:
            raise _config_error(
                f"步骤 {step_id} 含未知 trigger rule: {key}",
                step_id=step_id,
            )
        if key in {"content_length_gt", "core_concepts_ge"}:
            _require_positive_number(value, key, step_id=step_id, allow_zero=True)
        elif not (
            isinstance(value, list)
            and value
            and all(isinstance(item, str) and item.strip() for item in value)
        ):
            raise _config_error(
                f"步骤 {step_id} 的 {key} 必须是非空字符串列表",
                step_id=step_id,
            )


def evaluate_trigger_rules(rules: Sequence[Mapping[str, Any]], variables: Mapping[str, Any]) -> bool:
    """Evaluate the validated trigger DSL using deterministic pure operations."""
    content = str(variables.get("content") or "")
    content_type = str(variables.get("content_type") or "")
    try:
        for rule in rules:
            key, expected = next(iter(rule.items()))
            if key == "content_length_gt" and variables.get("content_length", 0) > expected:
                return True
            if key == "core_concepts_ge" and variables.get("concept_count", 0) >= expected:
                return True
            if key == "content_type_in" and content_type in expected:
                return True
            if key == "contains_keywords" and any(
                keyword.casefold() in content.casefold() for keyword in expected
            ):
                return True
    except (TypeError, ValueError) as exc:
        raise _condition_error("trigger rule 状态类型无效") from exc
    return False


def validate_workflow_config(
    workflow_name: str,
    config: object,
    step_registry: Mapping[str, object],
) -> list[dict[str, Any]]:
    """Validate a complete workflow before any step object is constructed."""
    if not isinstance(config, Mapping):
        raise _config_error("工作流配置根节点必须是映射")
    _reject_unknown_keys(
        config,
        {"schema_version", "name", "description", "steps"},
        "工作流配置",
    )
    schema_version = config.get("schema_version")
    if (
        not isinstance(schema_version, int)
        or isinstance(schema_version, bool)
        or schema_version != WORKFLOW_SCHEMA_VERSION
    ):
        raise _config_error(
            f"工作流 schema_version 必须为 {WORKFLOW_SCHEMA_VERSION}"
        )
    configured_name = config.get("name")
    if not isinstance(configured_name, str) or configured_name != workflow_name:
        raise _config_error(
            f"工作流 name 必须与请求名称一致: expected={workflow_name!r}, actual={configured_name!r}"
        )
    if "description" in config and not isinstance(config["description"], str):
        raise _config_error("工作流 description 必须是字符串")

    raw_steps = config.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise _config_error("工作流 steps 必须是非空列表")

    normalized: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, raw_step in enumerate(raw_steps):
        if not isinstance(raw_step, Mapping):
            raise _config_error(f"steps[{index}] 必须是映射")
        _reject_unknown_keys(
            raw_step,
            {"id", "type", "config", "on_error"},
            f"steps[{index}]",
        )
        step_id = raw_step.get("id")
        step_type = raw_step.get("type")
        step_config = raw_step.get("config")
        on_error = raw_step.get("on_error")
        if not isinstance(step_id, str) or not step_id.strip():
            raise _config_error(f"steps[{index}] 缺少非空 id")
        if step_id in seen_ids:
            raise _config_error(f"步骤 id 重复: {step_id}", step_id=step_id)
        seen_ids.add(step_id)
        if not isinstance(step_type, str) or not step_type.strip():
            raise _config_error(f"步骤 {step_id} 缺少非空 type", step_id=step_id)
        if step_type not in step_registry:
            raise _config_error(f"未知步骤类型: {step_type}", step_id=step_id)
        if not isinstance(step_config, Mapping):
            raise _config_error(f"步骤 {step_id} 的 config 必须是映射", step_id=step_id)
        if on_error not in ON_ERROR_VALUES:
            raise _config_error(
                f"步骤 {step_id} 的 on_error 必须是 fail 或 continue",
                step_id=step_id,
            )

        copied_config = dict(step_config)
        allowed_config_keys = {
            "fetch_content": {"processor", "url_key", "timeout", "retry"},
            "ai_analyze": {"tasks", "max_words", "num_tags"},
            "idea_sharpen": {
                "questions",
                "condition",
                "trigger_rules",
            },
            "review_entry": {
                "required",
                "max_regenerations",
                "preview_chars",
            },
            "store_entry": {"targets"},
        }.get(step_type, set())
        _reject_unknown_keys(
            copied_config,
            allowed_config_keys,
            f"步骤 {step_id} 的 config",
            step_id=step_id,
        )
        if step_type == "fetch_content":
            _validate_fetch_config(step_id, copied_config)
        elif step_type == "ai_analyze":
            _validate_analyze_config(step_id, copied_config)
        elif step_type == "idea_sharpen":
            _validate_idea_sharpen_config(step_id, copied_config)
        elif step_type == "review_entry":
            _validate_review_config(step_id, copied_config)
        elif step_type == "store_entry":
            _validate_store_config(step_id, copied_config)

        normalized.append(
            {
                "id": step_id,
                "type": step_type,
                "config": copied_config,
                "on_error": on_error,
            }
        )
    return normalized


def _validate_fetch_config(step_id: str, config: dict[str, Any]) -> None:
    from src.processors import is_processor_available, normalize_processor_name

    processor = config.get("processor", "auto")
    if not isinstance(processor, str) or not processor.strip():
        raise _config_error(f"步骤 {step_id} 的 processor 必须是非空字符串", step_id=step_id)
    normalized = normalize_processor_name(processor)
    if normalized != "auto" and not is_processor_available(normalized):
        raise PKVRuntimeError(
            ErrorCode.WORKFLOW_PROCESSOR_UNKNOWN,
            f"步骤 {step_id} 指定了未知或不可用 processor: {processor}",
            stage=f"workflow_configuration:{step_id}",
            recoverable=True,
        )
    config["processor"] = normalized
    retry = config.get("retry", 0)
    if not isinstance(retry, int) or isinstance(retry, bool) or retry < 0:
        raise _config_error(f"步骤 {step_id} 的 retry 必须是非负整数", step_id=step_id)
    if "timeout" in config:
        _require_positive_number(config["timeout"], "timeout", step_id=step_id)
    if "url_key" in config and not (
        isinstance(config["url_key"], str) and config["url_key"].strip()
    ):
        raise _config_error(f"步骤 {step_id} 的 url_key 必须是非空字符串", step_id=step_id)


def _validate_analyze_config(step_id: str, config: Mapping[str, Any]) -> None:
    tasks = config.get("tasks", ["summarize", "extract_tags"])
    supported = {"summarize", "extract_tags"}
    if not (
        isinstance(tasks, list)
        and tasks
        and all(isinstance(task, str) and task in supported for task in tasks)
    ):
        raise _config_error(
            f"步骤 {step_id} 的 tasks 只能包含 summarize/extract_tags",
            step_id=step_id,
        )
    if len(set(tasks)) != len(tasks):
        raise _config_error(f"步骤 {step_id} 的 tasks 不得重复", step_id=step_id)
    if "max_words" in config:
        _require_positive_int(config["max_words"], "max_words", step_id=step_id)
    if "num_tags" in config:
        _require_positive_int(config["num_tags"], "num_tags", step_id=step_id)
        if not 3 <= config["num_tags"] <= 5:
            raise _config_error(
                f"步骤 {step_id} 的 num_tags 必须在 3 到 5 之间",
                step_id=step_id,
            )


def _validate_idea_sharpen_config(step_id: str, config: Mapping[str, Any]) -> None:
    questions = config.get("questions")
    if not (
        isinstance(questions, list)
        and questions
        and all(isinstance(question, str) and question.strip() for question in questions)
    ):
        raise _config_error(
            f"步骤 {step_id} 的 questions 必须是非空字符串列表",
            step_id=step_id,
        )
    if "condition" in config:
        parse_condition(config["condition"], step_id=step_id)
    if "trigger_rules" in config:
        validate_trigger_rules(config["trigger_rules"], step_id=step_id)
    if "condition" not in config and "trigger_rules" not in config:
        raise _config_error(
            f"步骤 {step_id} 必须配置 condition 或 trigger_rules",
            step_id=step_id,
        )


def _validate_review_config(step_id: str, config: Mapping[str, Any]) -> None:
    if "required" not in config or not isinstance(config["required"], bool):
        raise _config_error(f"步骤 {step_id} 的 required 必须是布尔值", step_id=step_id)
    for key in ("max_regenerations", "preview_chars"):
        if key in config:
            _require_positive_int(config[key], key, step_id=step_id)


def _validate_store_config(step_id: str, config: Mapping[str, Any]) -> None:
    targets = config.get("targets")
    supported = {"markdown", "sqlite", "vector_index"}
    if not (
        isinstance(targets, list)
        and targets
        and all(isinstance(target, str) and target in supported for target in targets)
    ):
        raise _config_error(
            f"步骤 {step_id} 的 targets 只能包含 markdown/sqlite/vector_index",
            step_id=step_id,
        )
    if len(set(targets)) != len(targets):
        raise _config_error(f"步骤 {step_id} 的 targets 不得重复", step_id=step_id)
    required = {"markdown", "sqlite"}
    if not required.issubset(targets):
        missing = ", ".join(sorted(required - set(targets)))
        raise _config_error(
            f"步骤 {step_id} 缺少 W1 核心存储 target: {missing}",
            step_id=step_id,
        )
