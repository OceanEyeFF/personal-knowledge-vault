"""Regression contract for the Phase C fixed offline MCP baseline."""

from __future__ import annotations

import asyncio
from pathlib import Path

from evals.mcp_quality.runner import run_evaluation


def test_mcp_quality_baseline_is_deterministic_and_offline(
    tmp_path: Path,
    monkeypatch,
) -> None:
    def reject_network(*args, **kwargs):
        raise AssertionError("Phase C MCP evaluation attempted outbound network access")

    monkeypatch.setattr("socket.create_connection", reject_network)

    report = asyncio.run(run_evaluation(work_dir=tmp_path / "quality-eval"))

    assert len(report.tasks) == 16
    assert report.overall_score == 115 / 119
    assert report.dimension_scores == {
        "citability": 1 / 3,
        "degradation": 1.0,
        "evidence_relevance": 1.0,
        "parameters": 1.0,
        "result": 1.0,
        "tool_selection": 1.0,
    }
    assert report.thresholds_met is False

    failures = {
        (task.task_id, check.check_id)
        for task, check in report.failed_checks
    }
    assert failures == {
        ("collect_chunk_evidence_relevance", "top_chunk_stable_locator"),
        ("find_bridges_partial_contract", "bridge_evidence_trace"),
        ("timeline_partial_contract", "timeline_item_locator"),
        ("contrast_partial_contract", "contrast_dimension_provenance"),
    }


def test_mcp_quality_report_can_hide_tool_outputs(tmp_path: Path) -> None:
    report = asyncio.run(run_evaluation(work_dir=tmp_path / "quality-eval"))

    compact = report.to_dict()
    verbose = report.to_dict(include_outputs=True)

    assert "output" not in compact["tasks"][0]
    assert "output" in verbose["tasks"][0]
