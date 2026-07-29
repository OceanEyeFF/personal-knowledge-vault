"""Regression contract for the Phase C fixed offline MCP baseline."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import yaml

from evals.mcp_quality import safety
from evals.mcp_quality.runner import DEFAULT_PROPOSALS, run_evaluation
from evals.mcp_quality.scenario import OfflineMcpScenario


def test_mcp_quality_baseline_is_deterministic_and_offline(
    tmp_path: Path,
    monkeypatch,
) -> None:
    def reject_network(*args, **kwargs):
        raise AssertionError("Phase C MCP evaluation attempted outbound network access")

    monkeypatch.setattr("socket.create_connection", reject_network)

    report = asyncio.run(run_evaluation(work_dir=tmp_path / "quality-eval"))

    assert report.schema_version == "pkv.mcp_quality_report.v1"
    assert report.taskset_version == "pkv.mcp_quality_tasks.v1"
    assert report.proposals_version == "pkv.mcp_quality_proposals.v1"
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
    assert report.policy_mode == "baseline_only"
    assert report.ci_contract == "schema_and_failure_matrix"
    assert report.targets_met is False

    failures = {
        (task.task_id, check.check_id)
        for task, check in report.failed_checks
    }
    assert failures == {
        ("collect_chunk_evidence_relevance", "all_chunks_stable_locator"),
        ("find_bridges_partial_contract", "all_bridge_evidence_traces"),
        ("timeline_partial_contract", "all_timeline_item_locators"),
        ("contrast_partial_contract", "contrast_dimension_provenance"),
    }


def test_mcp_quality_report_can_hide_tool_outputs(tmp_path: Path) -> None:
    report = asyncio.run(run_evaluation(work_dir=tmp_path / "quality-eval"))

    compact = report.to_dict()
    verbose = report.to_dict(include_outputs=True)

    assert "output" not in compact["tasks"][0]
    assert "output" in verbose["tasks"][0]
    assert compact["policy_mode"] == "baseline_only"
    assert compact["targets_met"] is False


def test_independent_proposals_detect_wrong_tool_arguments_and_chunk_query(
    tmp_path: Path,
) -> None:
    proposals = yaml.safe_load(DEFAULT_PROPOSALS.read_text(encoding="utf-8"))
    by_task = {item["task_id"]: item for item in proposals["proposals"]}
    by_task["subgraph_depth_two"]["proposed_call"]["arguments"]["depth"] = 3
    by_task["subgraph_relation_filter"]["proposed_call"] = {
        "tool": "timeline_of",
        "arguments": {"topic": "Alpha 时间线", "sort_order": "asc"},
    }
    by_task["collect_chunk_evidence_relevance"]["proposed_call"]["arguments"][
        "question"
    ] = "chunk-beta-only Beta 证据"
    proposals_path = tmp_path / "counterexample-proposals.yaml"
    proposals_path.write_text(
        yaml.safe_dump(proposals, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )

    report = asyncio.run(
        run_evaluation(
            proposals_path=proposals_path,
            work_dir=tmp_path / "counterexample-eval",
        )
    )
    tasks = {task.task_id: task for task in report.tasks}
    depth_checks = {
        check.check_id: check for check in tasks["subgraph_depth_two"].checks
    }
    tool_checks = {
        check.check_id: check for check in tasks["subgraph_relation_filter"].checks
    }
    chunk_checks = {
        check.check_id: check
        for check in tasks["collect_chunk_evidence_relevance"].checks
    }

    assert depth_checks["arguments_match"].passed is False
    assert tool_checks["tool_selection"].passed is False
    assert tool_checks["arguments_match"].passed is False
    assert chunk_checks["arguments_match"].passed is False
    assert chunk_checks["top_chunk_relevant_alpha"].passed is False
    assert chunk_checks["top_chunk_relevant_delta"].passed is False
    assert chunk_checks["chunk_ids_present"].passed is False


def test_chunk_fixture_distinguishes_queries(tmp_path: Path) -> None:
    with OfflineMcpScenario(tmp_path / "query-specific-chunks") as scenario:
        alpha_delta = scenario.chunk_searcher.search_chunks(
            "chunk-alpha-delta Alpha 到 Delta"
        )
        beta_only = scenario.chunk_searcher.search_chunks("chunk-beta-only Beta")
        unknown = scenario.chunk_searcher.search_chunks("ordinary unmatched query")

    assert [item.metadata["chunk_id"] for item in alpha_delta] == [101, 401, 301]
    assert [item.metadata["chunk_id"] for item in beta_only] == [201]
    assert alpha_delta[0].metadata["chunk_text"] != beta_only[0].metadata["chunk_text"]
    assert unknown == []


def test_public_paths_reject_production_before_read_or_create(
    tmp_path: Path,
    monkeypatch,
) -> None:
    fake_production = tmp_path / "repo" / ".data"
    monkeypatch.setattr(safety, "PRODUCTION_ROOT", fake_production)

    with pytest.raises(RuntimeError, match="生产 .data"):
        OfflineMcpScenario(fake_production / "direct-scenario")
    assert not fake_production.exists()

    with pytest.raises(RuntimeError, match="生产 .data"):
        asyncio.run(
            run_evaluation(work_dir=fake_production / "public-runner-scenario")
        )
    assert not fake_production.exists()

    safe_work = tmp_path / "safe-work"
    with pytest.raises(RuntimeError, match="生产 .data"):
        asyncio.run(
            run_evaluation(
                taskset_path=fake_production / "tasks.yaml",
                work_dir=safe_work,
            )
        )
    assert not fake_production.exists()
    assert not safe_work.exists()

    with pytest.raises(RuntimeError, match="生产 .data"):
        asyncio.run(
            run_evaluation(
                proposals_path=fake_production / "proposals.yaml",
                work_dir=safe_work,
            )
        )
    assert not fake_production.exists()
    assert not safe_work.exists()
