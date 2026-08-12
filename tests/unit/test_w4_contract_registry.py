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
FINAL_RUN_FIELDS = {
    "evidence_root",
    "execution_id",
    "runner_version",
    "source_revision",
    "artifact_id",
    "artifact_sha256",
    "normalized_manifest_sha256",
    "build_fingerprint",
    "artifact_kind",
    "artifact_status",
    "compliance_manifest_sha256",
    "release_inventory_artifact_closure_sha256",
    "release_inventory_closure_sha256",
    "release_inventory_sha256",
    "controller_sha256",
    "fixture_sha256",
    "release_eligible",
    "release_blockers",
    "scenarios_total",
    "matrix_rows_total",
    "artifact_verified",
    "artifact_failed",
    "artifact_pending",
    "functional_verified",
    "decision",
    "summary",
    "registry",
    "run_evidence_manifest",
    "launcher",
}
FINAL_RUN_ROOT = "E:/pkv-w4-evidence-53a45ed"
FINAL_RUN_SHA256 = {
    "artifact_sha256": "5de0ad975892ba0686c7116b17639ab97a785a098402faa0994b7eeb2e050b4c",
    "normalized_manifest_sha256": (
        "b82ee1eb113d456212d21dbb064e9281bacbce9f167793770b7b1b576132cf5f"
    ),
    "build_fingerprint": (
        "f1b666375a973aaef207075f1434301b55da84683ae70da17c2d67afe135d64c"
    ),
    "compliance_manifest_sha256": (
        "d1d9d8e0417360e35d11af15f5a088cf32ab4f5bb43a20d96c3a45d87d5e433f"
    ),
    "release_inventory_artifact_closure_sha256": (
        "b5d177c55b2b68ef6e582e23e938a0b319fac1d681ad765a27415dfc92eda9a8"
    ),
    "release_inventory_closure_sha256": (
        "80164d1fe474225e402c9bc4b1dcaa5f94605e5d8d7d287bea1a7bedc29377ce"
    ),
    "release_inventory_sha256": (
        "8ed02ad3c0bf4e55949675ba77a46727018a38ad3888fa02221cd55b8244a4a5"
    ),
    "controller_sha256": (
        "36f340e225acc87137db5d0a6a6f10ed6c45645f2e8e25ef33740054b59bd24f"
    ),
    "fixture_sha256": (
        "8502bf6654b1b2b66fc90965b32b04f35a090dfb02dca8630b3aadd74e7e8835"
    ),
}
FINAL_RUN_BLOCKERS = [
    "conda-native-license-materials-and-spdx",
    "html2text-gpl-compliance",
    "native-msvc-license-and-provenance",
    "qt-corresponding-source-location",
    "qt-linkage-and-replacement-not-proven",
    "qt-module-license-audit",
    "qt-notice-placeholders",
]
FINAL_RECORD_DETAILS = {
    "w4.release_audit.v1": (
        "2026-08-11T23:23:12.7019228Z",
        "4aa52f6ffd04552afe3d009d9e0c71ce82369e56546e58b77025d1a0580487f1",
        8,
        None,
    ),
    "w4.application_lifecycle.v1": (
        "2026-08-11T23:23:36.2779041Z",
        "b1c4fcb4c2885539f6c36d5fea277e5d12e75239ec3478c0feb0a31f156542a1",
        24,
        None,
    ),
    "w4.url_archive_ssrf_rejection.v1": (
        "2026-08-11T23:23:56.8144733Z",
        "55e982869786f8584e6c04fd5d58f6fa6c27685f69778aa7b8d600b7d3b46f04",
        16,
        None,
    ),
    "w4.semantic_provider_unavailable.v1": (
        "2026-08-11T23:24:19.9046563Z",
        "2fae196d244fd31ba7fefb8b7a2b637c35a5194d57719beed388197846da83a1",
        20,
        None,
    ),
    "w4.mcp_stdio_call.v1": (
        "2026-08-11T23:24:38.6677293Z",
        "92738b5f1d30bc064ed8279200bc5a09f316d30de99d65244e5017c797324132",
        12,
        None,
    ),
    "w4.offline_text_archive.v1": (
        "2026-08-11T23:25:07.7450903Z",
        "48e35cb0ad0225d1142c16dbee7ab9143212301dac0be09668c6aca507bd486e",
        34,
        None,
    ),
    "w4.bm25_search.v1": (
        "2026-08-11T23:25:33.7616534Z",
        "e6635157de25f4649974eb77727ae31986c5f4efe595748a59c03f7067f1cbfc",
        29,
        None,
    ),
    "w4.chat_loopback.v1": (
        "2026-08-11T23:26:03.1043579Z",
        "1d07ef53bce98aeced44e3d9fc4efff1508b43ad2fe44ec0054dc59dd6835714",
        36,
        "98221fbfec3f680147b6744675672f6c6e61a7b24ece3e846e9fbdf7be7d2843",
    ),
    "w4.upgrade_rejection.v1": (
        "2026-08-11T23:26:26.9599296Z",
        "96887477181532f45418deb1722dddf7bc29316de0fdf0c6a6e9937f1e1b7b1e",
        24,
        None,
    ),
    "w4.uninstall_data_retention.v1": (
        "2026-08-11T23:27:17.5702274Z",
        "f800b9c3259f54cb95f0f119d1fef1786eac81668eadf8a8450ee39de6f3daa3",
        24,
        None,
    ),
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


def test_w4_evidence_registry_records_the_completed_held_candidate() -> None:
    evidence = _load(EVIDENCE)
    handoffs = _load(HANDOFFS)

    assert set(evidence) == {
        "schema_version",
        "truth_source",
        "handoff_registry",
        "artifact_only",
        "allowed_states",
        "state_contract",
        "final_run",
        "records",
    }
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

    final_run = evidence["final_run"]
    assert set(final_run) == FINAL_RUN_FIELDS
    assert final_run["evidence_root"] == FINAL_RUN_ROOT
    assert final_run["execution_id"] == "w4-53a45ed"
    assert final_run["runner_version"] == "pkv.m13.artifact-runner.v2"
    assert final_run["source_revision"] == "53a45ed7535db8ead94370eb15b2f7874f5f330a"
    assert final_run["artifact_id"] == "PersonalKnowledgeVault-0.8.1-windows-x86_64"
    assert final_run["artifact_kind"] == "test_candidate"
    assert final_run["artifact_status"] == "test-candidate-on-compliance-hold"
    assert final_run["release_eligible"] is False
    assert final_run["release_blockers"] == FINAL_RUN_BLOCKERS
    assert (
        final_run["scenarios_total"],
        final_run["matrix_rows_total"],
        final_run["artifact_verified"],
        final_run["artifact_failed"],
        final_run["artifact_pending"],
        final_run["functional_verified"],
        final_run["decision"],
    ) == (10, 11, 10, 0, 0, True, "hold")
    for field, expected in FINAL_RUN_SHA256.items():
        assert final_run[field] == expected
        assert SHA256_PATTERN.fullmatch(final_run[field])

    assert final_run["summary"] == {
        "locator": f"{FINAL_RUN_ROOT}/runs/w4-53a45ed/w4-run-summary.json",
        "sha256": "fb34b212fca2397bf801870048db7bf401b6ab15df0d3dd14fbc2aaecd6d0b05",
    }
    assert final_run["registry"] == {
        "locator": f"{FINAL_RUN_ROOT}/runs/w4-53a45ed/w4-evidence-registry.json",
        "sha256": "1403ccde90b522cee6904a7f4edd059c92678cc85f12ddf95ea9c885999ec34e",
    }
    assert final_run["run_evidence_manifest"] == {
        "locator": f"{FINAL_RUN_ROOT}/runs/w4-53a45ed/run-evidence-manifest.json",
        "sha256": "e1057d787eb098070c5dbdc8004eabc200395eb1a27cbe5935fe765c9288cf41",
        "tree_sha256": "97c3162a8bbb5eed03d0efa2ba9c22abf0ab0f9125669061cbda807ac7329079",
        "entries_total": 243,
    }
    assert final_run["launcher"] == {
        "locator": f"{FINAL_RUN_ROOT}/launcher/w4-53a45ed/launcher-result.json",
        "sha256": "a6804df0a0fab1b11276df61acf65144d3f39249d7511de00a968ac13b1201b1",
        "controller_exit_code": 0,
        "timed_out": False,
        "forced_termination": False,
        "postcondition_verified": True,
    }
    for field in ("sha256", "tree_sha256"):
        for locator in (
            final_run["summary"],
            final_run["registry"],
            final_run["run_evidence_manifest"],
            final_run["launcher"],
        ):
            if field in locator:
                assert SHA256_PATTERN.fullmatch(locator[field])

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
        executed_at, evidence_manifest, path_count, harness_sha256 = FINAL_RECORD_DETAILS[
            record["scenario_id"]
        ]
        assert record["state"] == "artifact_verified"
        assert record["producer_lane"] == "artifact-only"
        assert record["artifact_id"] == final_run["artifact_id"]
        assert record["artifact_sha256"] == final_run["artifact_sha256"]
        assert (
            record["normalized_manifest_sha256"]
            == final_run["normalized_manifest_sha256"]
        )
        assert record["build_fingerprint"] == final_run["build_fingerprint"]
        assert record["source_revision"] == final_run["source_revision"]
        assert record["runner_version"] == final_run["runner_version"]
        assert record["execution_id"] == final_run["execution_id"]
        assert record["executed_at"] == executed_at
        assert record["environment_fingerprint"] == (
            "ca759a0aaa2a8d31f21b009094ecf859a765495c320ac13f7323e30caae8b8af"
        )
        assert record["fixture_sha256"] == final_run["fixture_sha256"]
        assert record["harness_sha256"] == harness_sha256
        assert record["evidence_manifest_sha256"] == evidence_manifest
        assert record["source_isolation_proof_sha256"] == (
            "7f09e35e4e9e576584d61e4359cd3f2e7daa26e3f2d40c799b29f90af00f44ad"
        )
        assert record["oracle_result"] == "passed"
        assert len(record["evidence_paths"]) == path_count
        assert all(
            path.startswith(f"scenarios/{record['scenario_id']}/")
            for path in record["evidence_paths"]
        )
        assert (
            f"scenarios/{record['scenario_id']}/evidence-manifest.json"
            in record["evidence_paths"]
        )
        assert f"scenarios/{record['scenario_id']}/oracle.json" in record["evidence_paths"]


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
