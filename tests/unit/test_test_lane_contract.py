"""Static contract for W3-T0 source/packaging/Artifact lane governance."""

from configparser import ConfigParser
from pathlib import Path

import yaml


PROJECT_ROOT = Path(__file__).parents[2]
REGISTRY = PROJECT_ROOT / "tests" / "contracts" / "m13_test_lanes.v1.yaml"
PYTEST_INI = PROJECT_ROOT / "pytest.ini"
SOURCE_SELECTOR = "not manual and not network and not artifact"
ARTIFACT_SELECTOR = "artifact and not manual and not network"
PACKAGING_SELECTOR = (
    "packaging_contract and not manual and not network and not artifact"
)
ARTIFACT_TEST_ROOT = PROJECT_ROOT / "tests" / "artifact"


def _load_registry() -> dict:
    payload = yaml.safe_load(REGISTRY.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def _load_pytest_config() -> ConfigParser:
    config = ConfigParser()
    loaded = config.read(PYTEST_INI, encoding="utf-8")
    assert loaded == [str(PYTEST_INI)]
    return config


def test_default_pytest_lane_excludes_all_opt_in_test_classes() -> None:
    config = _load_pytest_config()["pytest"]

    assert f'-m "{SOURCE_SELECTOR}"' in config["addopts"]
    marker_lines = {
        line.strip().split(":", 1)[0] for line in config["markers"].splitlines()
    }
    assert {"manual", "network", "artifact"} <= marker_lines


def test_lane_registry_freezes_default_source_selection() -> None:
    payload = _load_registry()

    assert payload["schema_version"] == "pkv.m13.test-lanes.v1"
    assert payload["truth_source"].startswith(
        "docs/overview/阶段开发路线与依赖-2026-03.md"
    )
    assert payload["default_lane"] == "source"
    assert set(payload["lanes"]) == {
        "source",
        "packaging-contract",
        "artifact-only",
    }

    source = payload["lanes"]["source"]
    assert source == {
        "owner": "w1_w2_source_contracts",
        "selection": "default",
        "pytest_selector": SOURCE_SELECTOR,
        "includes_markers": ["packaging_contract"],
        "artifact_required": False,
        "missing_artifact": "not_applicable",
        "source_tree_import": "allowed",
        "allowed_inputs": [
            "versioned_source",
            "synthetic_fixtures",
            "offline_fault_injection",
        ],
        "allowed_outputs": ["source_verified", "w4_handoff_declaration"],
        "forbidden_substitutes": [
            "packaging_contract_proof",
            "artifact_evidence",
            "artifact_verified",
        ],
    }


def test_packaging_contract_lane_is_explicitly_owned_and_cannot_write_evidence() -> None:
    packaging = _load_registry()["lanes"]["packaging-contract"]

    assert packaging == {
        "owner": "w3_packaging_contract",
        "selection": "explicit_or_source_default",
        "pytest_selector": PACKAGING_SELECTOR,
        "pytest_marker": "packaging_contract",
        "included_in_source_default": True,
        "artifact_required": False,
        "missing_artifact": "not_applicable",
        "source_tree_import": "allowed",
        "artifact_state_output": "forbidden",
        "allowed_inputs": [
            "clean_checkout",
            "locked_toolchain",
            "versioned_packaging_config",
            "synthetic_harness_contract",
        ],
        "allowed_outputs": [
            "build_contract_result",
            "payload_manifest",
            "provenance_manifest",
            "harness_contract_result",
        ],
        "forbidden_outputs": ["source_verified", "artifact_verified"],
        "forbidden_substitutes": [
            "w2_domain_semantics",
            "installed_artifact_evidence",
        ],
    }


def test_artifact_lane_is_explicit_fail_closed_and_source_independent() -> None:
    artifact = _load_registry()["lanes"]["artifact-only"]

    assert artifact == {
        "owner": "w4_artifact_e2e",
        "selection": "explicit_only",
        "pytest_selector": ARTIFACT_SELECTOR,
        "pytest_marker": "artifact",
        "artifact_required": True,
        "missing_artifact": "fail",
        "source_tree_import": "forbidden",
        "repository_cwd": "forbidden",
        "runner": "scripts/run-artifact-e2e.ps1",
        "required_inputs": [
            "installed_artifact_root",
            "installed_entrypoint",
            "artifact_manifest",
            "synthetic_fixture",
            "evidence_root",
        ],
        "conditional_inputs": ["external_harness_when_scenario_requires"],
        "allowed_inputs": [
            "installed_artifact",
            "artifact_manifest",
            "synthetic_fixture",
            "external_harness_when_required",
        ],
        "allowed_outputs": [
            "artifact_evidence",
            "artifact_verified",
            "artifact_failed",
        ],
        "forbidden_outputs": ["source_verified", "packaging_contract_proof"],
        "forbidden_substitutes": [
            "source_tree_smoke",
            "direct_module_call",
            "handoff_declaration",
            "packaging_contract_result",
        ],
        "forbidden_outcomes": ["skip", "xfail", "xpass", "source_fallback"],
    }
    assert (PROJECT_ROOT / artifact["runner"]).is_file()


def test_artifact_tests_are_present_explicitly_marked_and_fail_closed() -> None:
    test_files = sorted(ARTIFACT_TEST_ROOT.glob("test_*.py"))
    assert test_files

    for test_file in test_files:
        source = test_file.read_text(encoding="utf-8")
        assert "pytestmark = pytest.mark.artifact" in source, test_file.name
        assert "pytest.skip(" not in source, test_file.name
        assert "pytest.mark.skip" not in source, test_file.name
        assert "pytest.xfail(" not in source, test_file.name
        assert "pytest.mark.xfail" not in source, test_file.name
