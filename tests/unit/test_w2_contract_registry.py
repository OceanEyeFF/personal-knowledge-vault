"""Static completeness checks for the M13/W2 capability registry."""

from pathlib import Path

import yaml


REGISTRY = Path(__file__).parents[1] / "contracts" / "m13_w2.v1.yaml"
HANDOFFS = Path(__file__).parents[1] / "contracts" / "m13_w4_handoffs.v1.yaml"
PROJECT_ROOT = Path(__file__).parents[2]
ALLOWED_SUPPORT_LEVELS = {"supported", "partial-v1", "unsupported", "deferred"}
ALLOWED_STATES = {
    "defined",
    "semantic_green",
    "adapter_green",
    "source_verified",
    "artifact_pending",
    "artifact_verified",
}
REQUIRED_FIELDS = {
    "contract_id",
    "support_level",
    "surfaces",
    "semantic_owner",
    "fixture",
    "fault_injections",
    "oracle",
    "w4_scenario",
    "state",
}


def _load_registry() -> dict:
    payload = yaml.safe_load(REGISTRY.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def _load_handoffs() -> dict:
    payload = yaml.safe_load(HANDOFFS.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def test_w2_registry_has_unique_complete_contracts() -> None:
    payload = _load_registry()

    assert payload["schema_version"] == "pkv.m13.w2.contracts.v1"
    assert payload["truth_source"].startswith(
        "docs/overview/阶段开发路线与依赖-2026-03.md"
    )
    contracts = payload["capabilities"]
    assert isinstance(contracts, list) and contracts
    ids = [item["contract_id"] for item in contracts]
    assert len(ids) == len(set(ids))

    for contract in contracts:
        assert set(contract) == REQUIRED_FIELDS
        assert contract["support_level"] in ALLOWED_SUPPORT_LEVELS
        assert contract["state"] in ALLOWED_STATES
        assert isinstance(contract["surfaces"], list)
        assert isinstance(contract["fault_injections"], list)
        assert contract["fault_injections"]
        assert isinstance(contract["oracle"], str) and contract["oracle"]


def test_w2_registry_fixtures_exist_and_are_nonempty() -> None:
    for contract in _load_registry()["capabilities"]:
        fixture_path = PROJECT_ROOT / contract["fixture"]
        assert fixture_path.exists(), contract["contract_id"]
        if fixture_path.is_dir():
            assert any(path.is_file() for path in fixture_path.rglob("*")), (
                contract["contract_id"]
            )
        else:
            assert fixture_path.stat().st_size > 0, contract["contract_id"]


def test_supported_w2_contracts_define_w4_handoff_without_claiming_artifact_proof() -> None:
    payload = _load_registry()
    handoffs = _load_handoffs()
    assert handoffs["schema_version"] == "pkv.m13.w4.handoffs.v1"
    assert handoffs["artifact_only"] is True
    scenarios = handoffs["scenarios"]
    scenario_ids = {item["scenario_id"] for item in scenarios}
    assert len(scenario_ids) == len(scenarios)

    for contract in payload["capabilities"]:
        if contract["support_level"] in {"supported", "partial-v1"}:
            assert contract["surfaces"]
            assert isinstance(contract["w4_scenario"], str)
            assert contract["w4_scenario"].startswith("w4.")
            assert contract["w4_scenario"] in scenario_ids
        else:
            assert contract["surfaces"] == []
        assert contract["state"] != "artifact_verified"

    contract_ids = {item["contract_id"] for item in payload["capabilities"]}
    covered_ids = set()
    for scenario in scenarios:
        assert scenario["state"] == "artifact_pending"
        assert scenario["entrypoint"].startswith("installed_")
        assert scenario["capability_ids"]
        assert set(scenario["capability_ids"]) <= contract_ids
        covered_ids.update(scenario["capability_ids"])

    assert {
        "workflow.archive_url.v1",
        "workflow.archive_text.v1",
        "retrieval.bm25.v1",
        "retrieval.semantic.v1",
        "mcp.stdio.v1",
        "gui.chat.v1",
    } <= covered_ids


def test_w2_status_vocabulary_matches_frozen_route() -> None:
    vocabulary = _load_registry()["status_vocabulary"]

    assert vocabulary["supported"] == [
        "success",
        "no_hits",
        "invalid",
        "error",
        "degraded",
    ]
    assert vocabulary["workflow_terminal"] == ["success", "degraded", "error"]
    assert vocabulary["chat_terminal"] == ["completed", "stopped", "error"]
