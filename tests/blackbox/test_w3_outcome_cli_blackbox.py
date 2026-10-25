"""W3/OC-10a: version-bound Outcome review through real CLI processes."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from src.storage.coordinator import StorageCoordinator
from src.storage.markdown_store import Entry, MarkdownStore
from src.storage.sqlite_store import SQLiteStore
from tests.offline_runtime import prepare_offline_child_env


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SOURCE_BODY = "w3-cli-private-source-body"


def _environment(data_root: Path, *, ready: bool) -> dict[str, str]:
    env = prepare_offline_child_env(
        project_root=PROJECT_ROOT,
        runtime_overrides={
            "DATA_DIR": data_root,
            "DB_PATH": data_root / "db" / "knowledge_vault.db",
            "VAULT_DIR": data_root / "vault",
            "VECTOR_DIR": data_root / "vectors",
            "LOG_DIR": data_root / "logs",
            "TMP_DIR": data_root / "tmp",
        },
    )
    env.pop("PKV_TEST_SYNTHETIC_RUNTIME_READY", None)
    env.pop("PKV_TEST_ABSENT_DATA_ROOT", None)
    env["PKV_TEST_SYNTHETIC_RUNTIME_READY" if ready else "PKV_TEST_ABSENT_DATA_ROOT"] = "1"
    return env


def _seed_source(data_root: Path) -> int:
    store = SQLiteStore(data_root / "db" / "knowledge_vault.db")
    store.initialize()
    archived = StorageCoordinator(
        MarkdownStore(data_root / "vault"), store, data_root / "runtime" / "operations"
    ).archive(
        Entry(title="W3 CLI source", source_type="text", content=SOURCE_BODY),
        chunks=[SOURCE_BODY],
    )
    assert archived.knowledge_id is not None
    return archived.knowledge_id


def _run_cli(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "tests" / "offline_entrypoint.py"), "cli", *args],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=45,
        check=False,
    )


def _v1_result(process: subprocess.CompletedProcess[str]) -> dict[str, object]:
    assert process.returncode == 0, process.stdout + process.stderr
    payload = json.loads(process.stdout)
    assert payload["status"] == "success", payload
    assert isinstance(payload["result"], dict)
    return payload["result"]


def _v2_payload(
    process: subprocess.CompletedProcess[str], operation: str, *, error: str | None = None
) -> dict[str, object]:
    assert process.returncode == (1 if error else 0), process.stdout + process.stderr
    payload = json.loads(process.stderr if error else process.stdout)
    assert set(payload) == {"schema", "status", "terminal", "operation", "result", "issues"}
    assert payload["schema"] == "pkv.lifecycle-response.v2"
    assert payload["operation"] == operation
    assert payload["status"] == payload["terminal"] == ("error" if error else "success")
    assert isinstance(payload["issues"], list)
    if error:
        assert process.stdout == ""
        assert payload["result"] is None
        assert payload["issues"][0]["code"] == error
    else:
        assert payload["issues"] == []
    assert SOURCE_BODY not in json.dumps(payload)
    return payload


def _review_arguments(confirmation: dict[str, object]) -> tuple[str, ...]:
    assert set(confirmation) == {
        "outcome_id", "expected_revision", "content_sha256", "expected_input_revision"
    }
    return (
        str(confirmation["outcome_id"]),
        "--expected-revision", str(confirmation["expected_revision"]),
        "--content-sha256", str(confirmation["content_sha256"]),
        "--expected-input-revision", str(confirmation["expected_input_revision"]),
    )


def test_outcome_cli_v2_exact_review_replay_and_v1_deprecation(tmp_path: Path) -> None:
    """OC-10a: old confirmation cannot approve a newer persisted revision."""

    data_root = tmp_path / "w3-outcome-cli"
    knowledge_id = _seed_source(data_root)
    env = _environment(data_root, ready=True)
    for command in ("approve", "return"):
        rejected_legacy = _run_cli(env, "outcome", command, "topic-missing")
        assert rejected_legacy.returncode == 1
        assert rejected_legacy.stdout == ""
        legacy_error = json.loads(rejected_legacy.stderr)
        assert legacy_error["status"] == "error"
        assert legacy_error["operation"] == f"{command}_research_brief"
        assert legacy_error["issues"][0]["code"] == "outcome_not_found"
        assert legacy_error["deprecation"] == {
            "replacement": f"outcome {command}-v2", "removal_api_major": 2,
        }
    topic = _v1_result(_run_cli(
        env, "outcome", "create-topic", "--title", "W3 CLI review",
        "--question", "Does the selected source support the exact reviewed revision?",
    ))
    topic_id = str(topic["topic_id"])
    evidence = _v1_result(_run_cli(
        env, "outcome", "attach-evidence", topic_id, f"pkv://entries/{knowledge_id}",
    ))
    claim = _v1_result(_run_cli(
        env, "outcome", "create-claim", topic_id, "The source supports the first statement.",
        "--state", "supported",
    ))
    _v1_result(_run_cli(
        env, "outcome", "link-evidence", str(claim["claim_id"]),
        str(evidence["evidence_id"]), "--polarity", "support",
    ))

    before = _v2_payload(_run_cli(env, "outcome", "get-v2", topic_id), "get_outcome_v2")["result"]
    assert before["schema"] == "pkv.outcome.bundle.v2"
    assert before["brief"] is None
    assert _v2_payload(
        _run_cli(env, "outcome", "brief-v2", "--topic-id", topic_id),
        "get_outcome_research_brief_v2",
    )["result"] is None

    first = _v2_payload(
        _run_cli(env, "outcome", "render-v2", topic_id), "render_outcome_research_brief_v2",
    )["result"]
    assert first["schema"] == "pkv.outcome.brief.v2"
    assert first["state"] == "awaiting_review"
    assert first["binding_state"] == "verified"
    old_confirmation = first["review_confirmation"]
    assert isinstance(old_confirmation, dict)

    _v1_result(_run_cli(
        env, "outcome", "revise-claim", str(claim["claim_id"]),
        "--statement", "The source supports the revised statement.",
    ))
    second = _v2_payload(
        _run_cli(env, "outcome", "render-v2", topic_id), "render_outcome_research_brief_v2",
    )["result"]
    assert second["revision"] > first["revision"]
    assert second["current_input_revision"] > first["current_input_revision"]
    assert second["outcome_id"] == first["outcome_id"]
    confirmation = second["review_confirmation"]
    assert isinstance(confirmation, dict)
    _v2_payload(
        _run_cli(env, "outcome", "approve-v2", *_review_arguments(old_confirmation)),
        "approve_outcome_research_brief_v2", error="outcome_state_conflict",
    )
    observed = _v2_payload(
        _run_cli(env, "outcome", "brief-v2", "--outcome-id", str(second["outcome_id"])),
        "get_outcome_research_brief_v2",
    )["result"]
    assert observed["revision"] == second["revision"]
    assert observed["state"] == "awaiting_review"

    approved = _v2_payload(
        _run_cli(env, "outcome", "approve-v2", *_review_arguments(confirmation)),
        "approve_outcome_research_brief_v2",
    )["result"]
    replayed = _v2_payload(
        _run_cli(env, "outcome", "approve-v2", *_review_arguments(confirmation)),
        "approve_outcome_research_brief_v2",
    )["result"]
    assert approved["schema"] == "pkv.outcome.review-result.v2"
    assert approved["idempotent"] is False
    assert replayed["idempotent"] is True
    assert approved["review_receipt_id"] == replayed["review_receipt_id"]
    assert replayed["brief"]["state"] == "approved"

    return_args = (*_review_arguments(confirmation), "--reason-code", "needs_revision")
    returned = _v2_payload(
        _run_cli(env, "outcome", "return-v2", *return_args), "return_outcome_research_brief_v2",
    )["result"]
    returned_replay = _v2_payload(
        _run_cli(env, "outcome", "return-v2", *return_args), "return_outcome_research_brief_v2",
    )["result"]
    assert returned["idempotent"] is False
    assert returned_replay["idempotent"] is True
    assert returned["review_receipt_id"] == returned_replay["review_receipt_id"]
    assert returned["review_receipt_id"] != approved["review_receipt_id"]
    _v2_payload(
        _run_cli(env, "outcome", "approve-v2", *_review_arguments(confirmation)),
        "approve_outcome_research_brief_v2", error="outcome_state_conflict",
    )
    _v2_payload(
        _run_cli(env, "outcome", "return-v2", *_review_arguments(confirmation), "--reason-code", "other"),
        "return_outcome_research_brief_v2", error="outcome_state_conflict",
    )
    final = _v2_payload(_run_cli(env, "outcome", "get-v2", topic_id), "get_outcome_v2")
    assert final["result"]["brief"]["state"] == "returned"
    assert final["result"]["brief"]["revision"] == second["revision"]
    assert str(data_root) not in json.dumps(final)

    # W3 is a compatibility stage: old names remain callable and observable.
    _v2_payload(_run_cli(env, "outcome", "render-v2", topic_id), "render_outcome_research_brief_v2")
    for command, replacement, expected_state in (
        ("approve", "approve-v2", "approved"), ("return", "return-v2", "returned")
    ):
        legacy = _run_cli(env, "outcome", command, topic_id)
        assert _v1_result(legacy)["state"] == expected_state
        assert f"outcome {replacement}" in legacy.stderr
        assert "API major 2" in legacy.stderr


@pytest.mark.parametrize(
    ("arguments", "exit_code", "fragment"),
    [
        pytest.param(("get-v2", "--help"), 0, "TOPIC_ID", id="get-help"),
        pytest.param(("brief-v2", "--help"), 0, "--outcome-id", id="brief-help"),
        pytest.param(("render-v2", "--help"), 0, "TOPIC_ID", id="render-help"),
        pytest.param(("approve-v2", "--help"), 0, "--expected-revision", id="approve-help"),
        pytest.param(("return-v2", "--help"), 0, "--reason-code", id="return-help"),
        pytest.param(("brief-v2",), 2, "exactly one", id="missing-selector"),
        pytest.param(
            ("brief-v2", "--topic-id", "topic-test", "--outcome-id", "outcome-test"),
            2, "exactly one", id="two-selectors",
        ),
        pytest.param(("approve-v2",), 2, "Missing", id="missing-confirmation"),
        pytest.param(("approve-v2", "outcome-test", "--expected-revision", "true"), 2, "Invalid value", id="invalid-integer"),
        pytest.param(
            ("approve-v2", "outcome-test", "--expected-revision", "0",
             "--content-sha256", "a" * 64, "--expected-input-revision", "1"),
            2, "Invalid value", id="zero-revision",
        ),
        pytest.param(
            ("approve-v2", "outcome-test", "--expected-revision", str(2**63),
             "--content-sha256", "a" * 64, "--expected-input-revision", "1"),
            2, "Invalid value", id="overflow-revision",
        ),
        pytest.param(
            ("approve-v2", "outcome-test", "--expected-revision", "1",
             "--content-sha256", "a" * 64, "--expected-input-revision", "-1"),
            2, "Invalid value", id="negative-input-revision",
        ),
        pytest.param(
            ("approve-v2", "outcome-test", "--expected-revision", "1",
             "--content-sha256", "A" * 64, "--expected-input-revision", "1"),
            2, "Invalid value", id="uppercase-sha256",
        ),
        pytest.param(("get-v2", "topic-test", "--extra-field", "secret"), 2, "No such option", id="unknown-option"),
    ],
)
def test_outcome_cli_v2_help_and_parse_precede_unready_runtime(
    tmp_path: Path, arguments: tuple[str, ...], exit_code: int, fragment: str
) -> None:
    """The new leaves inherit W2's parser-first contract without a data root."""

    data_root = tmp_path / "absent-runtime"
    env = _environment(data_root, ready=False)
    assert not os.path.lexists(data_root)
    process = _run_cli(env, "outcome", *arguments)
    assert process.returncode == exit_code, process.stdout + process.stderr
    assert fragment in (process.stdout if exit_code == 0 else process.stderr)
    assert "runtime_readiness" not in process.stdout + process.stderr
    assert not os.path.lexists(data_root)


def test_outcome_cli_v2_valid_unready_request_uses_v2_error_envelope(tmp_path: Path) -> None:
    """A parsed v2 request preserves the envelope even at the root readiness gate."""

    data_root = tmp_path / "absent-v2-runtime"
    env = _environment(data_root, ready=False)
    payload = _v2_payload(
        _run_cli(env, "outcome", "get-v2", "topic-test"),
        "get_outcome_v2", error="setup_required",
    )
    assert payload["issues"][0]["stage"] == "runtime_readiness"
    assert str(data_root) not in json.dumps(payload)
    assert not os.path.lexists(data_root)
