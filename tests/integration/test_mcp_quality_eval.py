"""Regression contract for the Phase C fixed offline MCP baseline."""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path

import pytest
import yaml

from evals.mcp_quality import runner as quality_runner
from evals.mcp_quality import safety
from evals.mcp_quality.runner import (
    DEFAULT_PROPOSALS,
    DEFAULT_TASKSET,
    run_evaluation,
)
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
    assert report.overall_score == 1.0
    assert report.dimension_scores == {
        "citability": 1.0,
        "degradation": 1.0,
        "evidence_relevance": 1.0,
        "parameters": 1.0,
        "result": 1.0,
        "tool_selection": 1.0,
    }
    assert report.policy_mode == "threshold_enforced"
    assert report.ci_contract == "schema_all_checks_and_thresholds"
    assert report.targets_met is True

    failures = {
        (task.task_id, check.check_id)
        for task, check in report.failed_checks
    }
    assert failures == set()
    assert sum(len(task.checks) for task in report.tasks) == 151


def test_mcp_quality_report_can_hide_tool_outputs(tmp_path: Path) -> None:
    report = asyncio.run(run_evaluation(work_dir=tmp_path / "quality-eval"))

    compact = report.to_dict()
    verbose = report.to_dict(include_outputs=True)

    assert "output" not in compact["tasks"][0]
    assert "output" in verbose["tasks"][0]
    assert compact["policy_mode"] == "threshold_enforced"
    assert compact["targets_met"] is True
    assert compact["thresholds_met"] is True


@pytest.mark.parametrize(
    ("payload_kind", "tool_name"),
    [
        ("taskset", "archive_url"),
        ("taskset", "archive_text"),
        ("proposals", "archive_url"),
        ("proposals", "archive_text"),
    ],
)
def test_non_allowlisted_tools_fail_before_scenario_creation(
    payload_kind: str,
    tool_name: str,
    tmp_path: Path,
    monkeypatch,
) -> None:
    taskset = yaml.safe_load(DEFAULT_TASKSET.read_text(encoding="utf-8"))
    proposals = yaml.safe_load(DEFAULT_PROPOSALS.read_text(encoding="utf-8"))
    if payload_kind == "taskset":
        taskset["tasks"][0]["expected_call"]["tool"] = tool_name
    else:
        proposals["proposals"][0]["proposed_call"]["tool"] = tool_name

    taskset_path = tmp_path / "blocked-taskset.yaml"
    proposals_path = tmp_path / "blocked-proposals.yaml"
    taskset_path.write_text(
        yaml.safe_dump(taskset, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    proposals_path.write_text(
        yaml.safe_dump(proposals, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    scenario_attempted = False

    def fail_if_scenario_created(*args, **kwargs):
        nonlocal scenario_attempted
        scenario_attempted = True
        raise AssertionError("scenario must not be created for a blocked Tool")

    monkeypatch.setattr(
        quality_runner,
        "OfflineMcpScenario",
        fail_if_scenario_created,
    )
    work_dir = tmp_path / "blocked-work"

    with pytest.raises(ValueError, match="fixed offline read-only allowlist"):
        asyncio.run(
            run_evaluation(
                taskset_path=taskset_path,
                proposals_path=proposals_path,
                work_dir=work_dir,
            )
        )

    assert scenario_attempted is False
    assert not work_dir.exists()


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
    assert report.targets_met is False
    assert report.dimension_scores["tool_selection"] < 1.0
    assert report.dimension_scores["parameters"] < 1.0


def test_chunk_fixture_distinguishes_queries(tmp_path: Path) -> None:
    with OfflineMcpScenario(tmp_path / "query-specific-chunks") as scenario:
        alpha_delta = scenario.chunk_searcher.search_chunks(
            "chunk-alpha-delta Alpha 到 Delta"
        )
        beta_only = scenario.chunk_searcher.search_chunks("chunk-beta-only Beta")
        unknown = scenario.chunk_searcher.search_chunks("ordinary unmatched query")

    assert [item.metadata["chunk_id"] for item in alpha_delta.results] == [101, 401, 301]
    assert [item.metadata["chunk_id"] for item in beta_only.results] == [201]
    assert (
        alpha_delta.results[0].metadata["chunk_text"]
        != beta_only.results[0].metadata["chunk_text"]
    )
    assert unknown.status == "no_hits"
    assert unknown.results == ()


def test_phase_b_citations_resolve_and_runtime_contracts_execute(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        with OfflineMcpScenario(tmp_path / "resource-contract") as scenario:
            await scenario.registered_tools()
            outputs = [
                await scenario.call_tool(
                    "collect_evidence",
                    {
                        "question": "chunk-alpha-delta Alpha 到 Delta 的证据是什么？",
                        "top_k": 3,
                        "relation_max_depth": 2,
                        "include_chunks": True,
                    },
                ),
                await scenario.call_tool(
                    "find_bridges",
                    {
                        "seed_knowledge_id": str(scenario.aliases["alpha_id"]),
                        "top_k": 5,
                        "max_depth": 2,
                    },
                ),
                await scenario.call_tool(
                    "timeline_of",
                    {
                        "topic": "Alpha 时间线",
                        "top_k": 5,
                        "sort_order": "asc",
                    },
                ),
                await scenario.call_tool(
                    "contrast",
                    {
                        "topic_a": "Topic A",
                        "topic_b": "Topic B",
                        "top_k": 5,
                    },
                ),
            ]

            locators = [
                locator
                for output in outputs
                for locator in scenario._citation_locators(output)
            ]
            assert any("/chunks/" in locator for locator in locators)
            assert any("/metadata/" in locator for locator in locators)
            assert any(locator.startswith("pkv://relations/") for locator in locators)
            for locator in locators:
                assert await scenario.read_resource(locator)

    asyncio.run(exercise())


def test_runtime_contract_rejects_readable_but_mismatched_locators() -> None:
    wrong_chunk_locator = "pkv://entries/2/chunks/201"
    wrong_chunk_resource = {
        "knowledge_id": 2,
        "chunk_id": 201,
        "chunk_index": 0,
        "chunk_text": "来自另一条目的真实 chunk",
        "citation_locator": wrong_chunk_locator,
    }
    OfflineMcpScenario._validate_resource_success(
        wrong_chunk_locator,
        wrong_chunk_resource,
    )
    with pytest.raises(AssertionError, match="entry locator 未指向所在 Tool 条目"):
        OfflineMcpScenario._validate_collect_contract(
            {
                "evidence": [
                    {
                        "knowledge_id": 1,
                        "chunk_id": 101,
                        "chunk_index": 0,
                        "chunk_text": "应属于条目 1 的 chunk",
                        "citation_locator": wrong_chunk_locator,
                    }
                ]
            },
            {wrong_chunk_locator: wrong_chunk_resource},
        )

    wrong_relation_locator = "pkv://relations/8"
    wrong_relation_resource = {
        "relation_id": 8,
        "source_knowledge_id": 11,
        "target_knowledge_id": 12,
        "relation_type": "mentions",
        "relation_source_type": "backfill",
        "citation_locator": wrong_relation_locator,
    }
    OfflineMcpScenario._validate_resource_success(
        wrong_relation_locator,
        wrong_relation_resource,
    )
    with pytest.raises(AssertionError, match="relation locator 未指向所在 Tool 关系边"):
        OfflineMcpScenario._assert_relation_locator_matches(
            {
                "relation_id": 7,
                "source_knowledge_id": 11,
                "target_knowledge_id": 12,
                "relation_type": "mentions",
                "relation_source_type": "backfill",
                "citation_locator": wrong_relation_locator,
            },
            {wrong_relation_locator: wrong_relation_resource},
        )


def test_phase_b_oracles_reject_bridge_timeline_contrast_and_chunk_mutations(
    tmp_path: Path,
) -> None:
    async def resources_for(
        scenario: OfflineMcpScenario,
        payload: dict,
    ) -> dict[str, object]:
        locators = scenario._citation_locators(payload)
        return {
            locator: await scenario.read_resource(locator)
            for locator in locators
        }

    async def exercise() -> None:
        with OfflineMcpScenario(tmp_path / "oracle-mutations") as scenario:
            await scenario.registered_tools()

            bridge = await scenario.call_tool(
                "find_bridges",
                {
                    "seed_knowledge_id": str(scenario.aliases["alpha_id"]),
                    "top_k": 5,
                    "max_depth": 2,
                },
            )
            bridge_resources = await resources_for(scenario, bridge)
            bad_bridge = copy.deepcopy(bridge)
            bridge_edge = next(
                edge
                for item in bad_bridge["items"]
                for edge in item["evidence_path"]
                if "seed_path" in edge["evidence_roles"]
            )
            bridge_edge["hop_index"] = 99
            with pytest.raises(
                AssertionError,
                match="bridge seed path hop_index 不连续",
            ):
                scenario._validate_bridge_contract(
                    bad_bridge,
                    bridge_resources,
                )

            timeline = await scenario.call_tool(
                "timeline_of",
                {
                    "topic": "__contract_event_time__",
                    "top_k": 1,
                    "sort_order": "asc",
                },
            )
            timeline_resources = await resources_for(scenario, timeline)
            bad_timeline = copy.deepcopy(timeline)
            bad_timeline["items"][0]["time_value"] = "1900-01-01"
            with pytest.raises(
                AssertionError,
                match="timeline Resource 时间值与 Tool 输出不一致",
            ):
                scenario._validate_timeline_contract(
                    bad_timeline,
                    timeline_resources,
                )

            contrast = await scenario.call_tool(
                "contrast",
                {
                    "topic_a": "Topic A",
                    "topic_b": "Topic B",
                    "top_k": 5,
                },
            )
            contrast_resources = await resources_for(scenario, contrast)
            bad_contrast = copy.deepcopy(contrast)
            dimension_name = next(
                name
                for name in ("shared_tags", "only_a_tags", "only_b_tags")
                if bad_contrast[name]
            )
            visible_value = bad_contrast[dimension_name][0]
            bad_contrast["comparison_dimensions"]["provenance"][
                dimension_name
            ].pop(visible_value)
            with pytest.raises(
                AssertionError,
                match=f"contrast {dimension_name} provenance 覆盖不完整",
            ):
                scenario._validate_contrast_contract(
                    bad_contrast,
                    contrast_resources,
                )

            collected = await scenario.call_tool(
                "collect_evidence",
                {
                    "question": "chunk-alpha-delta Alpha 到 Delta 的证据是什么？",
                    "top_k": 3,
                    "relation_max_depth": 2,
                    "include_chunks": True,
                },
            )
            collect_resources = await resources_for(scenario, collected)
            wrong_locator = (
                f"pkv://entries/{scenario.aliases['beta_id']}/chunks/201"
            )
            collect_resources[wrong_locator] = await scenario.read_resource(
                wrong_locator
            )
            bad_collect = copy.deepcopy(collected)
            chunk_item = next(
                item
                for item in bad_collect["evidence"]
                if item.get("chunk_id") is not None
            )
            chunk_item["citation_locator"] = wrong_locator
            with pytest.raises(
                AssertionError,
                match="entry locator 未指向所在 Tool 条目",
            ):
                scenario._validate_collect_contract(
                    bad_collect,
                    collect_resources,
                )

    asyncio.run(exercise())


def test_timeline_unavailable_and_local_sources_are_safe_through_fastmcp(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        with OfflineMcpScenario(tmp_path / "unavailable-local-source") as scenario:
            await scenario.registered_tools()
            timeline = await scenario.call_tool(
                "timeline_of",
                {
                    "topic": "__contract_unavailable__",
                    "top_k": 1,
                    "sort_order": "asc",
                },
            )
            item = timeline["items"][0]
            assert item["time_source"] == "unavailable"
            assert item["time_precision"] == "unavailable"
            assert item["time_source_field"] == ""
            assert item["time_value"] == ""
            assert item["source_url"] == ""
            assert item["source"] == item["citation_locator"]
            assert item["citation_locator"] == (
                f"pkv://entries/{scenario.aliases['no_time_id']}"
            )
            assert await scenario.read_resource(item["citation_locator"])
            assert any(
                "不作为精确时间点" in note
                for note in timeline["limitation_notes"]
            )

            collect = await scenario.call_tool(
                "collect_evidence",
                {
                    "question": "chunk-alpha-delta 本地 chat 来源安全检查",
                    "top_k": 3,
                    "relation_max_depth": 2,
                    "include_chunks": True,
                },
            )
            contrast = await scenario.call_tool(
                "contrast",
                {"topic_a": "Topic A", "topic_b": "Topic B", "top_k": 5},
            )
            scenario._assert_no_absolute_paths(collect)
            scenario._assert_no_absolute_paths(contrast)
            assert "[redacted-local-reference]" in json.dumps(
                collect,
                ensure_ascii=False,
            )
            assert "[redacted-local-reference]" in json.dumps(
                contrast,
                ensure_ascii=False,
            )

            for output in (collect, contrast):
                for locator in scenario._citation_locators(output):
                    assert await scenario.read_resource(locator)

            for alias in (
                "gamma_id",
                "delta_id",
                "version_base_id",
                "no_time_id",
            ):
                metadata = await scenario.read_resource(
                    f"pkv://entries/{scenario.aliases[alias]}/metadata"
                )
                assert metadata["source_url"] == ""
                scenario._assert_no_absolute_paths(metadata)

            relation = next(
                edge
                for edge in scenario.query_service.query_subgraph(
                    scenario.aliases["alpha_id"],
                    depth=2,
                ).edges
                if edge.relation_id is not None
            )
            relation_resource = await scenario.read_resource(
                f"pkv://relations/{relation.relation_id}"
            )
            scenario._assert_no_absolute_paths(relation_resource)
            security_fixture = relation_resource["evidence_payload"].get(
                "security_fixture"
            )
            assert security_fixture
            assert security_fixture["source_url"] == ""
            assert security_fixture["raw_target"] == "[redacted-local-reference]"
            assert security_fixture["origin"] == "[redacted-local-reference]"
            assert security_fixture["nested"]["raw_target"] == (
                "[redacted-local-reference]"
            )

    asyncio.run(exercise())


def test_entry_resources_enforce_vault_boundary_and_real_errors(
    tmp_path: Path,
) -> None:
    async def assert_rejected_without_path(
        scenario: OfflineMcpScenario,
        locator: str,
        forbidden_paths: list[Path],
    ) -> None:
        with pytest.raises(Exception) as exc_info:
            await scenario.read_resource(locator)
        message = str(exc_info.value)
        for forbidden in forbidden_paths:
            assert str(forbidden) not in message

    async def exercise() -> None:
        with OfflineMcpScenario(tmp_path / "vault-boundary") as scenario:
            await scenario.registered_tools()

            outside_file = scenario.resource_boundary_fixtures["outside_path"]
            missing_file = scenario.resource_boundary_fixtures["missing_path"]
            for locator in scenario.resource_boundary_fixtures[
                "rejected_locators"
            ]:
                await assert_rejected_without_path(
                    scenario,
                    locator,
                    [outside_file, scenario.vault_dir, missing_file],
                )

            for knowledge_id in scenario.resource_boundary_fixtures[
                "unsafe_entry_ids"
            ]:
                detail = await scenario.call_tool(
                    "get_entry",
                    {"knowledge_id": str(knowledge_id)},
                )
                assert detail["content"] == "(内容不可用)"
                assert detail["status"] == "degraded"
                assert detail["issues"]
                scenario._assert_no_absolute_paths(detail)
                assert "secret" not in json.dumps(detail, ensure_ascii=False)

            invalid = await scenario.call_tool(
                "get_entry",
                {"knowledge_id": str(outside_file)},
            )
            assert invalid["status"] == "invalid"
            assert invalid["error"] == "检索参数无效"
            assert invalid["issues"][0]["code"] == "retrieval_invalid_query"
            assert invalid["issues"][0]["stage"] == "knowledge_id_validation"
            scenario._assert_no_absolute_paths(invalid)

            collected = await scenario.call_tool(
                "collect_evidence",
                {
                    "question": "__contract_outside_entry__",
                    "top_k": 1,
                    "relation_max_depth": 1,
                    "include_chunks": False,
                },
            )
            assert collected["found"] is False
            assert collected["evidence"] == []
            assert any(
                "vault 文件边界校验" in note
                for note in collected["limitation_notes"]
            )
            scenario._assert_no_absolute_paths(collected)

            chunk_collected = await scenario.call_tool(
                "collect_evidence",
                {
                    "question": "__contract_outside_chunk__",
                    "top_k": 1,
                    "relation_max_depth": 1,
                    "include_chunks": True,
                },
            )
            assert chunk_collected["found"] is False
            assert chunk_collected["evidence"] == []
            assert "CHUNK_SECRET" not in json.dumps(
                chunk_collected,
                ensure_ascii=False,
            )
            assert any(
                "chunk 检索候选未通过 vault 文件边界校验" in note
                for note in chunk_collected["limitation_notes"]
            )
            scenario._assert_no_absolute_paths(chunk_collected)

            alpha_id = scenario.aliases["alpha_id"]
            assert await scenario.read_resource(f"pkv://entries/{alpha_id}")
            detail = await scenario.call_tool(
                "get_entry",
                {"knowledge_id": str(alpha_id)},
            )
            assert "# Alpha" in detail["content"]
            chunk = await scenario.read_resource(
                f"pkv://entries/{alpha_id}/chunks/101"
            )
            assert chunk["knowledge_id"] == alpha_id
            metadata = await scenario.read_resource(
                f"pkv://entries/{alpha_id}/metadata/event_time"
            )
            assert metadata["field"] == "event_time"

    asyncio.run(exercise())


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
