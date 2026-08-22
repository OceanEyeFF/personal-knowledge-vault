"""Static contract for the non-release Windows layered test topology."""

from __future__ import annotations

from pathlib import Path

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[2]
TOPOLOGY = PROJECT_ROOT / "tests" / "contracts" / "test_topology.v1.yaml"
HISTORICAL_M13 = PROJECT_ROOT / "tests" / "contracts" / "m13_test_lanes.v1.yaml"
SPECIFICATION = PROJECT_ROOT / "docs" / "specs" / "testing" / "分层测试体系合同-2026-08.md"


def _load_topology() -> dict:
    payload = yaml.safe_load(TOPOLOGY.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def test_topology_declares_an_internal_non_release_scope() -> None:
    payload = _load_topology()

    assert payload["schema_version"] == "pkv.test-topology.v1"
    assert payload["contract_status"] == "target_architecture"
    assert payload["classification"] == "INTERNAL TEST ONLY"
    assert {
        "pypi_publication",
        "release_eligibility",
        "release_candidate_promotion",
        "real_user_data_validation",
        "real_provider_or_network_validation",
        "gui_packaging_or_gui_tests",
    } <= set(payload["non_goals"])


def test_topology_classifies_current_and_historical_evidence_without_promoting_it() -> None:
    payload = _load_topology()
    categories = payload["evidence_categories"]

    assert set(categories) == {
        "CURRENT_ARCHITECTURE",
        "CURRENT_RELEASE_DECISION",
        "HISTORICAL_PRE_SPLIT_W4_EVIDENCE",
        "INTERNAL_SELF_TEST_ONLY",
        "INTERNAL_COMPATIBILITY_PROOF",
        "PLANNED_GATED",
    }
    assert categories["CURRENT_ARCHITECTURE"]["release_evidence"] == "forbidden"
    assert categories["CURRENT_RELEASE_DECISION"]["release_eligible"] is False
    assert categories["HISTORICAL_PRE_SPLIT_W4_EVIDENCE"][
        "proves_current_headless_revision"
    ] is False
    assert categories["INTERNAL_SELF_TEST_ONLY"]["proves_release_or_installer"] is False
    assert categories["INTERNAL_COMPATIBILITY_PROOF"]["proves_executable_or_release"] is False
    assert categories["PLANNED_GATED"]["quality_gate"] is False
    assert payload["evidence_rules"]["historical_held_candidate_does_not_prove"] == [
        "current_headless_revision",
        "internal_artifact",
    ]


def test_topology_preserves_the_historical_m13_contract_as_a_separate_authority() -> None:
    payload = _load_topology()

    authority = payload["authority"]
    assert authority["historical_m13_lane_contract"] == "tests/contracts/m13_test_lanes.v1.yaml"
    assert authority["historical_m13_lane_contract_treatment"] == "preserve_unchanged"
    assert HISTORICAL_M13.is_file()
    historical = yaml.safe_load(HISTORICAL_M13.read_text(encoding="utf-8"))
    assert historical["schema_version"] == "pkv.m13.test-lanes.v1"


def test_topology_has_the_required_layers_and_isolation_invariants() -> None:
    payload = _load_topology()

    assert set(payload["layers"]) == {
        "L0-harness-isolation",
        "L1-source-whitebox",
        "L2-build-contract",
        "L3-installed-wheel-blackbox",
        "L4-windows-internal-artifact-blackbox",
    }

    invariants = payload["global_invariants"]
    assert invariants["data_classification"] == "synthetic_only"
    assert invariants["provider_credentials"] == "forbidden"
    assert invariants["real_migration"] == "forbidden"
    assert invariants["default_network"] == "denied"
    assert invariants["historical_release_artifact_as_evidence"] == "forbidden"
    assert invariants["shared_product_data_root"] == "forbidden"
    assert invariants["required_test_data_root"] == ".data-test/<isolated-run-root>/<lane>"
    assert {
        "DATA_DIR",
        "DB_PATH",
        "VAULT_DIR",
        "VECTOR_DIR",
        "LOG_DIR",
        "TMP_DIR",
    } == set(invariants["test_runtime_paths"])
    assert invariants["formal_product_environment_overrides"] == [
        "PKV_DATA_ROOT",
        "PKV_LOG_LEVEL",
    ]


def test_windows_source_and_mcp_lanes_are_explicit_and_separate() -> None:
    lanes = _load_topology()["lanes"]

    source = lanes["windows-source-whitebox"]
    assert source["layer"] == "L1-source-whitebox"
    assert source["phase"] == "pre_artifact"
    assert source["platforms"] == ["windows-x86_64"]
    assert source["launcher"] == "scripts/test-conda.ps1"
    assert source["suite"] == "P0"
    assert source["source_tree_import"] == "allowed"
    assert (PROJECT_ROOT / source["launcher"]).is_file()

    mcp = lanes["windows-mcp-coverage"]
    assert mcp["layer"] == "L1-source-whitebox"
    assert mcp["platforms"] == ["windows-x86_64"]
    assert mcp["launcher"] == "scripts/test-conda.ps1"
    assert mcp["suite"] == "MCP"
    assert mcp["coverage_target"] == "src.mcp"
    assert mcp["coverage_fail_under"] == 95
    assert "p0_result" in mcp["forbidden_substitutes"]


def test_local_build_contract_distinguishes_builder_from_offline_smoke() -> None:
    build = _load_topology()["lanes"]["local-package-build-contract"]

    assert build["layer"] == "L2-build-contract"
    assert build["build_isolation"] == "trusted_local_toolchain_not_l0_offline_launcher"
    assert (PROJECT_ROOT / build["build_launcher"]).is_file()
    assert (PROJECT_ROOT / build["build_execution_wrapper"]).is_file()
    assert (PROJECT_ROOT / build["smoke_launcher"]).is_file()
    assert "launcher" not in build


def test_post_artifact_lanes_are_source_free_and_not_release_evidence() -> None:
    lanes = _load_topology()["lanes"]
    wheel = lanes["local-wheel-clean-install-blackbox"]
    artifact = lanes["windows-internal-artifact-blackbox"]

    assert wheel["layer"] == "L3-installed-wheel-blackbox"
    assert wheel["artifact_workspace"] == "outside_repository_checkout"
    assert wheel["source_tree_import"] == "forbidden"
    assert wheel["repository_cwd"] == "forbidden"
    assert wheel["public_interface"] == "pkv_kernel"
    assert {"editable_install", "source_tree_import", "pypi_download", "release_evidence"} <= set(
        wheel["forbidden_substitutes"]
    )

    assert artifact["layer"] == "L4-windows-internal-artifact-blackbox"
    assert artifact["classification"] == "INTERNAL TEST ONLY"
    assert artifact["source_tree_import"] == "forbidden_in_child_process"
    assert artifact["repository_cwd"] == "forbidden_in_child_process"
    assert (PROJECT_ROOT / artifact["build_launcher"]).is_file()
    assert (PROJECT_ROOT / artifact["smoke_runner"]).is_file()
    assert {
        "cli_help_result",
        "bm25_search_result",
        "mcp_stdio_initialize_result",
    } == set(artifact["required_outputs"])


def test_human_readable_specification_exists_and_names_the_machine_contract() -> None:
    specification = SPECIFICATION.read_text(encoding="utf-8")

    assert "test_topology.v1.yaml" in specification
    assert "L0" in specification
    assert "L4" in specification
    assert "INTERNAL TEST ONLY" in specification
    assert "PyPI" in specification
