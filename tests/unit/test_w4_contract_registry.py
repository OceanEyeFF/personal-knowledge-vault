"""Static declaration/evidence separation checks for M13/W4 Artifact E2E."""

from copy import deepcopy
from pathlib import Path
import re

import pytest
import yaml


CONTRACTS = Path(__file__).parents[1] / "contracts"
HANDOFFS = CONTRACTS / "m13_w4_handoffs.v1.yaml"
EVIDENCE = CONTRACTS / "m13_w4_evidence.v1.yaml"
W2_REGISTRY = CONTRACTS / "m13_w2.v1.yaml"
FEATURE_SCENARIOS = {
    "w4.url_archive_ssrf_rejection.v1",
    "w4.offline_text_archive.v1",
    "w4.bm25_search.v1",
    "w4.semantic_provider_unavailable.v1",
    "w4.mcp_stdio_call.v1",
    "w4.chat_loopback.v1",
}
RELEASE_SCENARIOS = {
    "w4.application_lifecycle.v1",
    "w4.upgrade_rejection.v1",
    "w4.uninstall_data_retention.v1",
    "w4.release_audit.v1",
}
FEATURE_FIELDS = {
    "scenario_id",
    "matrix_rows",
    "capability_ids",
    "entrypoint",
    "fixture",
    "external_harness",
    "actions",
    "public_oracle",
    "state",
}
RELEASE_FIELDS = FEATURE_FIELDS - {"capability_ids"}
MATRIX_ROWS = {
    "payload_and_provenance",
    "installation_and_first_run",
    "gui_read_and_bm25",
    "offline_text_archive",
    "url_security_rejection",
    "semantic_provider_unavailable",
    "gui_chat_loopback",
    "mcp_stdio_lifecycle",
    "upgrade_rejection",
    "uninstall_and_data_boundary",
    "documentation_version_and_decision",
}
SHA256_FIELDS = {
    "artifact_sha256",
    "normalized_manifest_sha256",
    "build_fingerprint",
    "environment_fingerprint",
    "fixture_sha256",
    "evidence_manifest_sha256",
    "source_isolation_proof_sha256",
}
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
SOURCE_REVISION_PATTERN = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
EVIDENCE_FIELDS = {
    "scenario_id",
    "state",
    "producer_lane",
    "artifact_id",
    "artifact_sha256",
    "normalized_manifest_sha256",
    "build_fingerprint",
    "source_revision",
    "runner_version",
    "execution_id",
    "executed_at",
    "environment_fingerprint",
    "fixture_sha256",
    "harness_sha256",
    "evidence_manifest_sha256",
    "source_isolation_proof_sha256",
    "oracle_result",
    "evidence_paths",
}


def _load(path: Path) -> dict:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def _validate_evidence_record(
    record: dict,
    *,
    state_contract: dict,
    harness_required: bool,
) -> None:
    """Validate a W4 state transition without trusting the producer lane."""

    assert set(record) == EVIDENCE_FIELDS
    state = record["state"]
    assert state in state_contract
    contract = state_contract[state]

    if state == "artifact_pending":
        assert record["producer_lane"] is None
        assert record["oracle_result"] is None
        assert record["evidence_paths"] == []
        nullable_fields = EVIDENCE_FIELDS - {
            "scenario_id",
            "state",
            "producer_lane",
            "oracle_result",
            "evidence_paths",
        }
        assert all(record[field] is None for field in nullable_fields)
        return

    assert state in {"artifact_verified", "artifact_failed"}
    assert record["producer_lane"] == "artifact-only"
    assert record["oracle_result"] == contract["oracle_result"]
    for field in contract["required_identity_fields"]:
        assert isinstance(record[field], str) and record[field].strip()
    assert isinstance(record["source_revision"], str)
    assert SOURCE_REVISION_PATTERN.fullmatch(record["source_revision"])
    for field in SHA256_FIELDS:
        assert isinstance(record[field], str)
        assert SHA256_PATTERN.fullmatch(record[field])
    assert isinstance(record["evidence_paths"], list) and record["evidence_paths"]
    assert all(
        isinstance(path, str) and path.strip() for path in record["evidence_paths"]
    )

    if harness_required:
        assert isinstance(record["harness_sha256"], str)
        assert SHA256_PATTERN.fullmatch(record["harness_sha256"])
    else:
        assert record["harness_sha256"] is None


def _synthetic_completed_record(
    template: dict,
    *,
    state: str,
    harness_required: bool,
) -> dict:
    record = deepcopy(template)
    record.update(
        {
            "state": state,
            "producer_lane": "artifact-only",
            "artifact_id": "PersonalKnowledgeVault-0.8.1",
            "source_revision": "b" * 40,
            "runner_version": "pkv.m13.artifact-runner.v1",
            "execution_id": "w4-synthetic-validation",
            "executed_at": "2026-08-10T00:00:00Z",
            "oracle_result": (
                "passed" if state == "artifact_verified" else "failed"
            ),
            "evidence_paths": ["evidence/w4-synthetic-validation.json"],
        }
    )
    for field in SHA256_FIELDS:
        record[field] = "a" * 64
    record["harness_sha256"] = "c" * 64 if harness_required else None
    return record


def test_w4_handoffs_keep_six_feature_scenarios_and_four_release_scenarios() -> None:
    payload = _load(HANDOFFS)

    assert payload["schema_version"] == "pkv.m13.w4.handoffs.v1"
    assert payload["artifact_only"] is True
    assert payload["declaration_role"] == "artifact_scenario_handoff"
    assert payload["evidence_registry"] == "tests/contracts/m13_w4_evidence.v1.yaml"

    feature_scenarios = payload["scenarios"]
    release_scenarios = payload["release_scenarios"]
    assert len(feature_scenarios) == 6
    assert len(release_scenarios) == 4
    assert all(set(scenario) == FEATURE_FIELDS for scenario in feature_scenarios)
    assert all(set(scenario) == RELEASE_FIELDS for scenario in release_scenarios)
    ids = [
        scenario["scenario_id"]
        for scenario in feature_scenarios + release_scenarios
    ]
    assert len(ids) == len(set(ids))
    assert {scenario["scenario_id"] for scenario in feature_scenarios} == FEATURE_SCENARIOS
    assert {scenario["scenario_id"] for scenario in release_scenarios} == RELEASE_SCENARIOS
    claimed_rows = [
        row
        for scenario in feature_scenarios + release_scenarios
        for row in scenario["matrix_rows"]
    ]
    assert set(claimed_rows) == MATRIX_ROWS
    assert len(claimed_rows) == len(set(claimed_rows))
    release_audit = next(
        scenario
        for scenario in release_scenarios
        if scenario["scenario_id"] == "w4.release_audit.v1"
    )
    assert release_audit["matrix_rows"] == [
        "payload_and_provenance",
        "documentation_version_and_decision",
    ]


def test_w4_handoffs_are_installed_artifact_declarations_not_evidence() -> None:
    w2_contracts = _load(W2_REGISTRY)["capabilities"]
    w2_ids = {item["contract_id"] for item in w2_contracts}

    payload = _load(HANDOFFS)
    for scenario in payload["scenarios"]:
        assert scenario["state"] == "artifact_pending"
        assert scenario["entrypoint"].startswith("installed_")
        assert isinstance(scenario["fixture"], str) and scenario["fixture"]
        assert isinstance(scenario["actions"], str) and scenario["actions"]
        assert isinstance(scenario["public_oracle"], str) and scenario["public_oracle"]
        assert scenario["capability_ids"]
        assert set(scenario["capability_ids"]) <= w2_ids

    feature_by_id = {
        scenario["scenario_id"]: scenario for scenario in payload["scenarios"]
    }
    for contract in w2_contracts:
        if contract["support_level"] in {"supported", "partial-v1"}:
            scenario_id = contract["w4_scenario"]
            assert scenario_id in feature_by_id, contract["contract_id"]
            assert contract["contract_id"] in feature_by_id[scenario_id][
                "capability_ids"
            ]

    release_by_id = {
        scenario["scenario_id"]: scenario
        for scenario in payload["release_scenarios"]
    }
    for scenario in release_by_id.values():
        assert scenario["state"] == "artifact_pending"
        assert scenario["entrypoint"].startswith("installed_")
        assert isinstance(scenario["fixture"], str) and scenario["fixture"]
        assert isinstance(scenario["actions"], str) and scenario["actions"]
        assert isinstance(scenario["public_oracle"], str) and scenario["public_oracle"]

    lifecycle = release_by_id["w4.application_lifecycle.v1"]["actions"]
    assert all(
        token in lifecycle
        for token in (
            "install",
            "without_python_or_conda",
            "launch",
            "initialize",
            "exit",
            "restart",
            "resource_user_root_separation",
        )
    )
    assert "upgrade" in release_by_id["w4.upgrade_rejection.v1"]["actions"]
    uninstall = release_by_id["w4.uninstall_data_retention.v1"]
    assert "uninstall" in uninstall["actions"]
    assert "explicit_opt_in" in uninstall["actions"]
    assert "preserves_user_data" in uninstall["public_oracle"]
    audit = release_by_id["w4.release_audit.v1"]["actions"]
    assert all(
        token in audit
        for token in (
            "version",
            "license",
            "notices",
            "sbom",
            "build_fingerprint",
            "payload_manifest",
            "forbidden_payload",
            "hash",
        )
    )

    mcp = next(
        scenario
        for scenario in payload["scenarios"]
        if scenario["scenario_id"] == "w4.mcp_stdio_call.v1"
    )
    assert all(token in mcp["actions"] for token in ("14_tools", "9_resources", "3_prompts"))


def test_w4_evidence_registry_starts_pending_without_fabricated_proof() -> None:
    evidence = _load(EVIDENCE)
    handoffs = _load(HANDOFFS)

    assert evidence["schema_version"] == "pkv.m13.w4.evidence.v1"
    assert evidence["handoff_registry"] == "tests/contracts/m13_w4_handoffs.v1.yaml"
    assert evidence["artifact_only"] is True
    assert evidence["allowed_states"] == [
        "artifact_pending",
        "artifact_verified",
        "artifact_failed",
    ]
    assert evidence["state_contract"]["artifact_pending"] == {
        "producer_lane": None,
        "oracle_result": None,
        "evidence_paths": "empty",
    }
    required_identity_fields = {
        "artifact_id",
        "artifact_sha256",
        "normalized_manifest_sha256",
        "build_fingerprint",
        "source_revision",
        "runner_version",
        "execution_id",
        "executed_at",
        "environment_fingerprint",
        "fixture_sha256",
        "evidence_manifest_sha256",
        "source_isolation_proof_sha256",
    }
    for state, result in (
        ("artifact_verified", "passed"),
        ("artifact_failed", "failed"),
    ):
        contract = evidence["state_contract"][state]
        assert contract["producer_lane"] == "artifact-only"
        assert contract["oracle_result"] == result
        assert set(contract["required_identity_fields"]) == required_identity_fields
        assert contract["conditional_identity_fields"] == [
            "harness_sha256_when_external_harness_is_declared"
        ]
        assert contract["evidence_paths"] == "non_empty"

    records = evidence["records"]
    assert all(set(record) == EVIDENCE_FIELDS for record in records)
    declared_ids = {
        scenario["scenario_id"]
        for scenario in handoffs["scenarios"] + handoffs["release_scenarios"]
    }
    assert {record["scenario_id"] for record in records} == declared_ids
    assert len(records) == len({record["scenario_id"] for record in records})

    harness_by_id = {
        scenario["scenario_id"]: scenario["external_harness"] is not None
        for scenario in handoffs["scenarios"] + handoffs["release_scenarios"]
    }
    for record in records:
        _validate_evidence_record(
            record,
            state_contract=evidence["state_contract"],
            harness_required=harness_by_id[record["scenario_id"]],
        )
        assert record["state"] == "artifact_pending"
        assert record["producer_lane"] is None
        assert record["artifact_id"] is None
        assert record["artifact_sha256"] is None
        assert record["normalized_manifest_sha256"] is None
        assert record["build_fingerprint"] is None
        assert record["source_revision"] is None
        assert record["runner_version"] is None
        assert record["execution_id"] is None
        assert record["executed_at"] is None
        assert record["environment_fingerprint"] is None
        assert record["fixture_sha256"] is None
        assert record["harness_sha256"] is None
        assert record["evidence_manifest_sha256"] is None
        assert record["source_isolation_proof_sha256"] is None
        assert record["oracle_result"] is None
        assert record["evidence_paths"] == []


@pytest.mark.parametrize("state", ["artifact_verified", "artifact_failed"])
@pytest.mark.parametrize("scenario_id", ["w4.chat_loopback.v1", "w4.release_audit.v1"])
def test_w4_evidence_validator_accepts_only_complete_artifact_lane_records(
    state: str, scenario_id: str
) -> None:
    evidence = _load(EVIDENCE)
    template = next(
        record for record in evidence["records"] if record["scenario_id"] == scenario_id
    )
    harness_required = scenario_id == "w4.chat_loopback.v1"
    record = _synthetic_completed_record(
        template,
        state=state,
        harness_required=harness_required,
    )

    _validate_evidence_record(
        record,
        state_contract=evidence["state_contract"],
        harness_required=harness_required,
    )


def test_w4_evidence_validator_rejects_cross_lane_and_incomplete_proof() -> None:
    evidence = _load(EVIDENCE)
    template = next(
        record
        for record in evidence["records"]
        if record["scenario_id"] == "w4.chat_loopback.v1"
    )
    valid = _synthetic_completed_record(
        template,
        state="artifact_verified",
        harness_required=True,
    )

    invalid_records = []
    for producer in ("source", "packaging-contract"):
        record = deepcopy(valid)
        record["producer_lane"] = producer
        invalid_records.append(record)
    for field, value in (
        ("artifact_id", None),
        ("artifact_sha256", "not-a-sha256"),
        ("source_revision", "dirty-working-tree"),
        ("harness_sha256", None),
        ("evidence_paths", []),
    ):
        record = deepcopy(valid)
        record[field] = value
        invalid_records.append(record)

    for record in invalid_records:
        with pytest.raises(AssertionError):
            _validate_evidence_record(
                record,
                state_contract=evidence["state_contract"],
                harness_required=True,
            )
