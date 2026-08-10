"""Strict schema-v1 and safe expression tests."""

from __future__ import annotations

import math

import pytest

from src.runtime.errors import ErrorCode, PKVRuntimeError
from src.workflow.config_schema import (
    evaluate_condition,
    validate_workflow_config,
)


REGISTRY = {
    "fetch_content": object(),
    "ai_analyze": object(),
    "idea_sharpen": object(),
    "review_entry": object(),
    "store_entry": object(),
}


def workflow_step(step_type: str, config: dict) -> dict:
    return {
        "schema_version": 1,
        "name": "demo",
        "steps": [
            {
                "id": "one",
                "type": step_type,
                "config": config,
                "on_error": "fail",
            }
        ],
    }


@pytest.mark.parametrize(
    "config",
    [
        {
            "schema_version": 1,
            "name": "demo",
            "steps": [],
            "unexpected": True,
        },
        {
            "schema_version": 1,
            "name": "demo",
            "steps": [
                {
                    "id": "one",
                    "type": "ai_analyze",
                    "config": {"tasks": ["summarize"]},
                    "on_error": "fail",
                    "retry": 1,
                }
            ],
        },
        workflow_step(
            "ai_analyze",
            {"tasks": ["summarize"], "max_word": 20},
        ),
    ],
)
def test_unknown_fields_fail_closed(config: dict) -> None:
    with pytest.raises(PKVRuntimeError) as exc_info:
        validate_workflow_config("demo", config, REGISTRY)
    assert exc_info.value.code is ErrorCode.WORKFLOW_CONFIG_INVALID


@pytest.mark.parametrize("value", [True, 0, -1, 1.5, math.nan, math.inf])
def test_analyze_integer_limits_are_strict(value: object) -> None:
    config = workflow_step(
        "ai_analyze",
        {"tasks": ["summarize"], "max_words": value, "num_tags": 5},
    )
    with pytest.raises(PKVRuntimeError) as exc_info:
        validate_workflow_config("demo", config, REGISTRY)
    assert exc_info.value.code is ErrorCode.WORKFLOW_CONFIG_INVALID


@pytest.mark.parametrize("value", [1, 2, 6])
def test_analyze_num_tags_outside_provider_range_fails_closed(value: int) -> None:
    config = workflow_step(
        "ai_analyze",
        {"tasks": ["extract_tags"], "max_words": 300, "num_tags": value},
    )

    with pytest.raises(PKVRuntimeError) as exc_info:
        validate_workflow_config("demo", config, REGISTRY)

    assert exc_info.value.code is ErrorCode.WORKFLOW_CONFIG_INVALID


@pytest.mark.parametrize("value", [3, 5])
def test_analyze_num_tags_provider_boundaries_are_valid(value: int) -> None:
    config = workflow_step(
        "ai_analyze",
        {"tasks": ["extract_tags"], "max_words": 300, "num_tags": value},
    )

    validated = validate_workflow_config("demo", config, REGISTRY)

    assert validated[0]["config"]["num_tags"] == value


def test_fetch_timeout_rejects_unrepresentable_number_with_stable_error() -> None:
    config = workflow_step(
        "fetch_content",
        {
            "processor": "auto",
            "url_key": "url",
            "timeout": 10**10_000,
            "retry": 0,
        },
    )

    with pytest.raises(PKVRuntimeError) as exc_info:
        validate_workflow_config("demo", config, REGISTRY)

    assert exc_info.value.code is ErrorCode.WORKFLOW_CONFIG_INVALID


@pytest.mark.parametrize("value", [True, 0, -1, math.nan, math.inf])
def test_review_timeout_is_not_a_published_schema_field(value: object) -> None:
    config = workflow_step(
        "review_entry",
        {
            "required": True,
            "skip_on_timeout": True,
            "timeout": value,
            "max_regenerations": 3,
            "preview_chars": 100,
        },
    )
    with pytest.raises(PKVRuntimeError) as exc_info:
        validate_workflow_config("demo", config, REGISTRY)
    assert exc_info.value.code is ErrorCode.WORKFLOW_CONFIG_INVALID


@pytest.mark.parametrize(
    ("step_type", "base_config", "forbidden_field", "forbidden_value"),
    [
        (
            "idea_sharpen",
            {"questions": ["Q1"], "condition": "content_length > 0"},
            "timeout",
            10,
        ),
        (
            "idea_sharpen",
            {"questions": ["Q1"], "condition": "content_length > 0"},
            "skip_on_timeout",
            True,
        ),
        (
            "review_entry",
            {"required": True, "max_regenerations": 3, "preview_chars": 100},
            "timeout",
            10,
        ),
        (
            "review_entry",
            {"required": True, "max_regenerations": 3, "preview_chars": 100},
            "skip_on_timeout",
            True,
        ),
    ],
)
def test_interactive_fake_timeout_fields_are_fail_closed(
    step_type: str,
    base_config: dict,
    forbidden_field: str,
    forbidden_value: object,
) -> None:
    config = workflow_step(
        step_type,
        {**base_config, forbidden_field: forbidden_value},
    )

    with pytest.raises(PKVRuntimeError) as exc_info:
        validate_workflow_config("demo", config, REGISTRY)

    assert exc_info.value.code is ErrorCode.WORKFLOW_CONFIG_INVALID


def test_safe_condition_supports_boolean_comparisons_and_membership() -> None:
    assert evaluate_condition(
        "content_length > 10 and content_type in ['wechat', 'zhihu']",
        {
            "content_length": 11,
            "content_type": "wechat",
            "tag_count": 0,
            "concept_count": 0,
            "source": "",
        },
    ) is True


@pytest.mark.parametrize(
    "expression",
    [
        "unknown_name == 1",
        "content_length + 1 > 10",
        "source.startswith('https')",
        "__import__('os')",
        "content_length >",
    ],
)
def test_safe_condition_rejects_names_calls_and_operators(expression: str) -> None:
    with pytest.raises(PKVRuntimeError) as exc_info:
        evaluate_condition(expression, {"content_length": 10})
    assert exc_info.value.code is ErrorCode.WORKFLOW_CONDITION_INVALID


def test_unknown_explicit_processor_has_stable_error() -> None:
    config = workflow_step(
        "fetch_content",
        {"processor": "does-not-exist", "url_key": "url", "timeout": 1, "retry": 0},
    )
    with pytest.raises(PKVRuntimeError) as exc_info:
        validate_workflow_config("demo", config, REGISTRY)
    assert exc_info.value.code is ErrorCode.WORKFLOW_PROCESSOR_UNKNOWN
